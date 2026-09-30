"""Benchmark + falsifiable gates.

    python -m bench.run                    # deterministic agent, 1500 people
    python -m bench.run --people 5000
    ACDP_LLM_PROVIDER=anthropic ACDP_LLM_MODEL=... ACDP_LLM_API_KEY=... python -m bench.run --agent env

Writes bench/out/report.json and bench/out/REPORT.md. Exit code 1 if any gate fails.
"""
from __future__ import annotations

import argparse
import copy
import csv
import json
import os
import shutil
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import httpx
from sqlalchemy import select, update

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))

from acdp import normalize as N  # noqa: E402
from acdp.agents import DeterministicAgent, build_agent  # noqa: E402
from acdp.config import ResolutionPolicy, Settings  # noqa: E402
from acdp.connectors import SourceRow  # noqa: E402
from acdp.connectors.saas import SegmentConnector  # noqa: E402
from acdp.engine import Engine  # noqa: E402
from acdp.models import (  # noqa: E402
    CanonicalRecord, LedgerEvent, Profile, ProfileMember, Proposal, SchemaMapping, Source, make_session_factory,
)
from bench.agents import FabricatingAgent, NullAgent, OvereagerAgent  # noqa: E402
from bench.generate import MAPPING_TRUTH, build_sources, build_universe  # noqa: E402
from bench.mocks import VendorMock  # noqa: E402

TOKENS = {"salesforce": "sf-test-token", "hubspot": "hs-test-token", "shopify": "shp-test-token", "stripe": "sk_test_x"}
KEY_FIELD = {"salesforce": "Id", "hubspot": "id", "shopify": "id", "stripe": "id", "legacy_csv": "Customer #",
             "segment": "userId"}
TRUST = {"salesforce": 80, "stripe": 70, "hubspot": 60, "shopify": 50, "segment": 40, "legacy_csv": 30}


def truth_map(data) -> dict[str, str]:
    out = {}
    for s, rows in data.items():
        if s.startswith("_"):
            continue
        for r in rows:
            out[f"{s}:{r[KEY_FIELD[s]]}"] = r["_truth"]
    return out


def make_engine(db: Path, agent, policy: ResolutionPolicy | None = None, mock: VendorMock | None = None) -> Engine:
    st = Settings(database_url=f"sqlite:///{db}", pii_hmac_key="bench-hmac-key", allow_insecure_dev=False,
                  policy=policy or ResolutionPolicy())
    client = httpx.Client(transport=mock.transport()) if mock else None
    return Engine(make_session_factory(st.database_url), st, agent=agent, http_client=client)


def register(e: Engine, out: Path) -> None:
    for k, v in TOKENS.items():
        os.environ[f"BENCH_{k.upper()}_TOKEN"] = v
    e.register_source("salesforce", "salesforce", {"instance_url": "https://acme.my.salesforce.com",
                                                   "token_env": "BENCH_SALESFORCE_TOKEN"}, TRUST["salesforce"])
    e.register_source("hubspot", "hubspot", {"token_env": "BENCH_HUBSPOT_TOKEN"}, TRUST["hubspot"])
    e.register_source("shopify", "shopify", {"shop": "acme", "token_env": "BENCH_SHOPIFY_TOKEN"}, TRUST["shopify"])
    e.register_source("stripe", "stripe", {"token_env": "BENCH_STRIPE_TOKEN"}, TRUST["stripe"])
    e.register_source("legacy_csv", "csv", {"path": str(out / "legacy.csv"), "key_field": "Customer #"},
                      TRUST["legacy_csv"])
    e.register_source("segment", "segment", {}, TRUST["segment"])


def write_csv(rows, path: Path) -> None:
    cols = [k for k in rows[0] if k != "_truth"]
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({k: r[k] for k in cols})


def push_segment(e: Engine, rows) -> dict:
    clean = [{k: v for k, v in r.items() if k != "_truth"} for r in rows]
    return e.ingest("segment", rows=SegmentConnector.rows_from_events(clean), actor="segment-webhook")


def full_run(e: Engine, data, force=False) -> dict:
    r = e.run_all(actor="bench", force=force)
    seg = push_segment(e, data["segment"])
    res = e.resolve(actor="bench", force=force)
    return {"ingest": r["ingest"] + [seg], "resolve": res}


def clusters_of(e: Engine) -> dict[str, list[str]]:
    with e.sf() as s:
        cl = defaultdict(list)
        for m in s.execute(select(ProfileMember)).scalars():
            cl[m.profile_id].append(m.record_id)
        return dict(cl)


def pairwise(clusters: list[list[str]], truth: dict[str, str]) -> dict:
    recs = [r for c in clusters for r in c]
    by_truth = Counter(truth[r] for r in recs)
    true_pairs = sum(n * (n - 1) // 2 for n in by_truth.values())
    pred = tp = 0
    for c in clusters:
        n = len(c)
        pred += n * (n - 1) // 2
        for n2 in Counter(truth[r] for r in c).values():
            tp += n2 * (n2 - 1) // 2
    p = tp / pred if pred else 1.0
    r = tp / true_pairs if true_pairs else 1.0
    return {"precision": round(p, 5), "recall": round(r, 5), "f1": round(2 * p * r / (p + r), 5) if p + r else 0,
            "pred_pairs": pred, "true_pairs": true_pairs, "tp": tp, "records": len(recs),
            "clusters": len(clusters), "true_entities": len(by_truth)}


def hard_negative_report(e: Engine, clusters, truth, people) -> dict:
    tags = {p.pid: set(p.tags) for p in people}
    with e.sf() as s:
        keys = {r.id: r.keys for r in s.execute(select(CanonicalRecord)).scalars()}
    impure = Counter()
    distinguishable = 0
    examples = []
    for c in clusters:
        pids = {truth[r] for r in c}
        if len(pids) < 2:
            continue
        kinds = set.intersection(*(tags[p] for p in pids)) & {"jrsr", "twin", "household"}
        impure[next(iter(kinds)) if kinds else "stranger"] += 1
        dobs = {keys[r].get("dob") for r in c if keys[r].get("dob")}
        sfx = {keys[r].get("suffix") for r in c if keys[r].get("suffix")}
        if len(dobs) > 1 or len(sfx) > 1:
            distinguishable += 1
        if len(examples) < 5:
            examples.append({"records": c, "truth": sorted(pids), "kind": sorted(kinds) or ["stranger"]})
    return {"impure_clusters_by_kind": dict(impure), "distinguishable_merges": distinguishable, "examples": examples}


def baseline(e: Engine, truth, key_fn) -> dict:
    with e.sf() as s:
        recs = [(r.id, r.keys) for r in s.execute(select(CanonicalRecord).where(CanonicalRecord.erased.is_(False))).scalars()]
    parent = {r: r for r, _ in recs}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    idx = defaultdict(list)
    for rid, k in recs:
        for key in key_fn(k):
            idx[key].append(rid)
    for ids in idx.values():
        for other in ids[1:]:
            parent[find(other)] = find(ids[0])
    groups = defaultdict(list)
    for rid, _ in recs:
        groups[find(rid)].append(rid)
    return pairwise(list(groups.values()), truth)


def mapping_accuracy(e: Engine) -> dict:
    out = {}
    with e.sf() as s:
        for src in s.execute(select(Source)).scalars():
            sm = s.get(SchemaMapping, src.active_mapping_id) if src.active_mapping_id else None
            fields = sm.mapping["fields"] if sm else {}
            exp = MAPPING_TRUTH[src.id]
            wrong = []
            for f, t in exp.items():
                got = (fields.get(f) or {}).get("target")
                if t is None:
                    if got in {"birth_date", "email", "phone", "first_name", "last_name", "full_name"}:
                        wrong.append(f"{f}: mapped to {got}, should be non-identity")
                elif got != t:
                    wrong.append(f"{f}: got {got}, want {t}")
            out[src.id] = {"accuracy": round(1 - len(wrong) / len(exp), 4), "errors": wrong,
                           "agent": s.get(Proposal, sm.proposal_id).agent if sm else None}
    return out


def consent_check(e: Engine, clusters: dict, truth, consent_truth) -> dict:
    latest = {}
    for pid, evs in consent_truth.items():
        ts, g = sorted(evs, key=lambda x: (x[0], x[1]))[-1]
        same_ts = [x for x in evs if x[0] == ts]
        latest[pid] = g and all(x[1] for x in same_ts)
    exported = e.export("email_marketing", limit=10**7)
    viol = pure_viol = 0
    for row in exported:
        pids = {truth[r] for r in clusters.get(row["id"], [])}
        bad = [p for p in pids if latest.get(p) is not True]
        if bad:
            viol += 1
            if len(pids) == 1:
                pure_viol += 1
    return {"exported": len(exported), "violations": viol, "pure_profile_violations": pure_viol,
            "violation_rate": round(viol / max(1, len(exported)), 5)}


def mutate_for_increment(data, seed=3):
    import random
    r = random.Random(seed)
    d = copy.deepcopy(data)
    changed = 0
    for rec in d["salesforce"]:
        if r.random() < 0.05:
            rec["Phone"] = None if rec["Phone"] else rec["Phone"]
            rec["Title"] = "Updated"
            rec["SystemModstamp"] = "2026-09-20T10:00:00.000+0000"
            changed += 1
    for rec in d["hubspot"]:
        if r.random() < 0.05:
            rec["properties"]["jobtitle"] = "Updated"
            rec["properties"]["lastmodifieddate"] = "2026-09-20T10:00:00Z"
            changed += 1
    return d, changed


def majority_profile(clusters: dict, truth) -> dict[str, str]:
    votes = defaultdict(Counter)
    for pid, recs in clusters.items():
        for r in recs:
            votes[truth[r]][pid] += 1
    return {t: sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))[0][0] for t, c in votes.items()}


def pii_scan(db: Path, needles: list[str]) -> dict:
    con = sqlite3.connect(db)
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    dump = "\n".join(con.iterdump()).lower()
    con.close()
    raw = db.read_bytes().lower()
    hits_dump = [n for n in needles if n.lower() in dump]
    hits_raw = [n for n in needles if n.lower().encode() in raw]
    return {"logical_hits": hits_dump, "physical_hits": hits_raw}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--people", type=int, default=1500)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--agent", default="deterministic", help="deterministic | env (use ACDP_LLM_* settings)")
    ap.add_argument("--out", default=str(ROOT / "out"))
    a = ap.parse_args(argv)
    gates = json.loads((ROOT / "gates.json").read_text())
    out = Path(a.out)
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    t0 = time.time()

    people = build_universe(a.people, a.seed)
    data = build_sources(people, a.seed + 4)
    consent_truth = data.pop("_consent_truth")
    truth = truth_map(data)
    write_csv(data["legacy_csv"], out / "legacy.csv")
    report: dict = {"config": {"people": a.people, "seed": a.seed, "agent": a.agent,
                               "records_by_source": {k: len(v) for k, v in data.items()}}}
    agent = DeterministicAgent() if a.agent == "deterministic" else build_agent(Settings())

    # ---------------------------------------------------------------- main run
    mock = VendorMock(data, TOKENS)
    mock.fail_next = 2  # first two HTTP calls get 429 — exercises retry/backoff
    db = out / "main.db"
    e = make_engine(db, agent, mock=mock)
    register(e, out)
    run1 = full_run(e, data)
    report["run1"] = {"ingest": [{k: v for k, v in i.items()} for i in run1["ingest"]], "resolve": run1["resolve"]}
    cl = clusters_of(e)
    m = pairwise(list(cl.values()), truth)
    report["metrics"] = m
    report["hard_negatives"] = hard_negative_report(e, list(cl.values()), truth, people)
    report["mapping"] = mapping_accuracy(e)
    with e.sf() as s:
        q = s.execute(select(Proposal).where(Proposal.outcome == "queued")).scalars().all()
        report["review_queue"] = {"total": len(q), "by_kind": dict(Counter(p.kind for p in q)),
                                  "per_record": round(len(q) / m["records"], 5)}
        report["proposal_outcomes"] = dict(Counter(f"{p.agent} -> {p.outcome}" for p in s.execute(select(Proposal)).scalars()))
    report["replay"] = e.replay()
    report["consent"] = consent_check(e, cl, truth, consent_truth)

    # negative controls
    report["baseline_email_only"] = baseline(e, truth, lambda k: [f"e:{x}" for x in k["email_keys"]])
    report["baseline_name_only"] = baseline(e, truth, lambda k: [f"n:{k['fn']}|{k['ln']}"] if k["fn"] and k["ln"] else [])

    # ---------------------------------------------------------------- incremental + id stability
    before = majority_profile(cl, truth)
    data2, n_changed = mutate_for_increment(data)
    mock.data = {k: [{kk: vv for kk, vv in r.items() if kk != "_truth"} for r in v] for k, v in data2.items()}
    run2 = full_run(e, data2)
    cl2 = clusters_of(e)
    after = majority_profile(cl2, truth)
    same = sum(1 for t in before if after.get(t) == before[t])
    report["incremental"] = {"changed_source_records": n_changed,
                             "ingest": [{k: i.get(k) for k in ("source", "status", "seen", "new", "changed", "unchanged")} for i in run2["ingest"]],
                             "resolve_status": run2["resolve"]["status"],
                             "profile_id_stability": round(same / len(before), 5),
                             "metrics_after": pairwise(list(cl2.values()), truth)}

    # ---------------------------------------------------------------- tamper detection (on a copy)
    tdb = out / "tamper.db"
    con = sqlite3.connect(db)
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.close()
    shutil.copy(db, tdb)
    te = make_engine(tdb, DeterministicAgent())
    with te.sf() as s:
        s.execute(update(LedgerEvent).where(LedgerEvent.seq == 5).values(actor="someone-else"))
        s.commit()
        from acdp import ledger
        tamper = ledger.verify(s)
    with e.sf() as s:
        from acdp import ledger
        intact = ledger.verify(s)
    report["ledger"] = {"intact": intact, "after_tamper": tamper}

    # ---------------------------------------------------------------- circuit breaker (on copies)
    # (a) observation: all thresholds disabled -> do the structural constraints alone bound the damage?
    rdb = out / "reckless.db"
    shutil.copy(db, rdb)
    re_ = make_engine(rdb, DeterministicAgent(), policy=ResolutionPolicy(auto_match=-50.0, non_match=-100.0))
    rr = re_.resolve(actor="bench-reckless")
    reckless_obs = {"status": rr["status"], "max_cluster": rr.get("max_cluster"),
                    "metrics": pairwise(list(clusters_of(re_).values()), truth) if rr["status"] == "published" else None}
    # (b) the gate: a catastrophic merge that bypasses scoring — an operator's bad bulk must_link import
    bdb = out / "breaker.db"
    shutil.copy(db, bdb)
    be = make_engine(bdb, DeterministicAgent())
    with be.sf() as s:
        ids = sorted(r.id for r in s.execute(select(CanonicalRecord).where(CanonicalRecord.erased.is_(False))).scalars())[:120]
    for x, y in zip(ids, ids[1:]):
        be.add_constraint("must_link", x, y, actor="bad-bulk-import", reason="csv import bug")
    pre = be.current_state_digest()
    br = be.resolve(actor="bench-breaker")
    report["circuit_breaker"] = {"scenario": "bulk must_link chain over 120 records", "status": br["status"],
                                 "breakers": br.get("breakers"),
                                 "published_state_unchanged": be.current_state_digest() == pre,
                                 "max_cluster_attempted": br.get("max_cluster"), "reckless_policy_observation": reckless_obs}

    # ---------------------------------------------------------------- erasure
    victim = next(p for p in people if sum(1 for r, t in truth.items() if t == p.pid) >= 3)
    er = e.erase("bench-dpo", "DSR-0001", email=victim.emails[0])
    # Scan only for identifiers that belong to the victim alone (a family inbox legitimately
    # remains in the other household members' records).
    others = {x for p in people if p.pid != victim.pid for x in p.emails + [p.work_email, p.mobile, p.home_phone] if x}
    needles = [x for x in victim.emails + ([victim.work_email] if victim.work_email else []) + [victim.mobile]
               if x not in others]
    needles += [x.split("@")[0].replace(".", "") + "@gmail.com" for x in needles if x.endswith("@gmail.com")]
    scan = pii_scan(db, needles)
    rerun = full_run(e, data2)
    with e.sf() as s:
        resurrected = [r.id for r in s.execute(select(CanonicalRecord).where(CanonicalRecord.erased.is_(False))).scalars()
                       if truth.get(r.id) == victim.pid]
    report["replay_final"] = e.replay()  # after increment + erasure + re-ingest
    report["erasure"] = {"result": er, "needles_checked": len(needles), "residual_pii": scan, "resurrected_records": resurrected,
                         "victim_records_total": sum(1 for t in truth.values() if t == victim.pid),
                         "rerun_status": rerun["resolve"]["status"]}

    # ---------------------------------------------------------------- adversarial + ablation agents
    adv = {}
    for ag in (FabricatingAgent(), OvereagerAgent(), NullAgent()):
        adb = out / f"adv_{ag.name.replace('/', '_')}.db"
        amock = VendorMock(data, TOKENS)
        ae = make_engine(adb, ag, mock=amock)
        register(ae, out)
        ar = full_run(ae, data)
        acl = clusters_of(ae)
        with ae.sf() as s:
            props = s.execute(select(Proposal).where(Proposal.agent == ag.name)).scalars().all()
            applied = [p for p in props if p.outcome == "auto_applied"]
            fab_applied = sum(1 for p in applied if any(g["gate"] == "A2.evidence_truthful" and not g["passed"] for g in p.gates))
            failed_gates = Counter(g["gate"] for p in props for g in p.gates if not g["passed"])
            queued = s.execute(select(Proposal).where(Proposal.outcome == "queued")).scalars().all()
        adv[ag.name] = {"metrics": pairwise(list(acl.values()), truth), "proposals": len(props),
                        "auto_applied": len(applied), "fabrications_applied": fab_applied,
                        "gate_failures": dict(failed_gates), "review_queue": len(queued),
                        "mapping": {k: v["accuracy"] for k, v in mapping_accuracy(ae).items()},
                        "resolve_status": ar["resolve"]["status"]}
    report["adversarial"] = adv
    report["elapsed_s"] = round(time.time() - t0, 1)

    # ---------------------------------------------------------------- gates
    G = []

    def gate(name, ok, observed, threshold):
        G.append({"gate": name, "passed": bool(ok), "observed": observed, "threshold": threshold})

    gate("G1 pairwise precision", m["precision"] >= gates["G1_pairwise_precision_min"], m["precision"], f">= {gates['G1_pairwise_precision_min']}")
    gate("G2 pairwise recall", m["recall"] >= gates["G2_pairwise_recall_min"], m["recall"], f">= {gates['G2_pairwise_recall_min']}")
    hn = report["hard_negatives"]["distinguishable_merges"]
    gate("G3 distinguishable hard-negative merges", hn <= gates["G3_distinguishable_hard_negative_merges_max"], hn, "== 0")
    gate("G4 hermetic replay digest (after run 1 and after increment+erasure)",
         report["replay"]["ok"] and report["replay_final"]["ok"],
         {"run1": report["replay"]["ok"], "final": report["replay_final"]["ok"]}, "true/true")
    gate("G5 ledger intact + tamper detected", intact["ok"] and not tamper["ok"], {"intact": intact["ok"], "tamper_detected": not tamper["ok"]}, "true/true")
    macc = min(v["accuracy"] for v in report["mapping"].values())
    gate("G6 mapping identity accuracy (min over sources)", macc >= gates["G6_mapping_identity_accuracy_min"], macc, ">= 1.0")
    res = len(scan["logical_hits"]) + len(scan["physical_hits"])
    gate("G7a erasure residual PII (logical+physical)", res <= gates["G7_erasure_residual_pii_max"], scan, "0 hits")
    gate("G7b erasure re-ingest suppressed", not resurrected, len(resurrected), "0 records")
    cr = report["consent"]
    gate("G8 consent violation rate", cr["violation_rate"] <= gates["G8_consent_violation_rate_max"] and cr["pure_profile_violations"] == 0,
         {"rate": cr["violation_rate"], "pure": cr["pure_profile_violations"]}, "<= 0.005 and pure == 0")
    stab = report["incremental"]["profile_id_stability"]
    gate("G9 profile-id stability after increment", stab >= gates["G9_profile_id_stability_min"], stab, ">= 0.99")
    eo, no = report["baseline_email_only"], report["baseline_name_only"]
    teeth = eo["recall"] < gates["G2_pairwise_recall_min"] and no["precision"] < gates["G1_pairwise_precision_min"]
    gate("G10 negative controls fail (benchmark has teeth)", teeth,
         {"email_only_recall": eo["recall"], "name_only_precision": no["precision"]}, "email-only recall < G2, name-only precision < G1")
    for name in ("adversary/fabricator", "adversary/overeager"):
        am = adv[name]
        gate(f"G11 {name}: precision held", am["metrics"]["precision"] >= gates["G11_adversarial_agent_precision_min"], am["metrics"]["precision"], ">= 0.98")
        gate(f"G11 {name}: fabrications applied", am["fabrications_applied"] <= gates["G11_adversarial_fabrications_applied_max"], am["fabrications_applied"], "== 0")
    rq = report["review_queue"]["per_record"]
    gate("G12 human review load per record", rq <= gates["G12_review_queue_per_record_max"], rq, "<= 0.05")
    cb = report["circuit_breaker"]
    gate("G13 circuit breaker halts catastrophic merge", cb["status"] == "halted" and cb["published_state_unchanged"], cb["status"], "halted + state unchanged")
    report["gates"] = G
    report["all_passed"] = all(g["passed"] for g in G)

    (out / "report.json").write_text(json.dumps(report, indent=2, default=str))
    md = [f"# Agentic CDP benchmark — {a.people} people, seed {a.seed}, agent `{a.agent}`", "",
          f"Records: {m['records']} across 6 sources · true entities {m['true_entities']} · profiles {m['clusters']} · {report['elapsed_s']}s", "",
          "| Gate | Result | Observed | Threshold |", "|---|---|---|---|"]
    for g in G:
        md.append(f"| {g['gate']} | {'PASS' if g['passed'] else '**FAIL**'} | `{json.dumps(g['observed'], default=str)[:90]}` | {g['threshold']} |")
    md += ["", "## Ablations", "", "| Variant | Precision | Recall | F1 | Review queue |", "|---|---|---|---|---|",
           f"| Deterministic agent (main) | {m['precision']} | {m['recall']} | {m['f1']} | {report['review_queue']['total']} |"]
    for k, v in adv.items():
        md.append(f"| {k} | {v['metrics']['precision']} | {v['metrics']['recall']} | {v['metrics']['f1']} | {v['review_queue']} |")
    md += [f"| Baseline: exact email only | {eo['precision']} | {eo['recall']} | {eo['f1']} | – |",
           f"| Baseline: name only | {no['precision']} | {no['recall']} | {no['f1']} | – |", "",
           "## Impure clusters by kind", "", f"`{json.dumps(report['hard_negatives']['impure_clusters_by_kind'])}`"]
    (out / "REPORT.md").write_text("\n".join(md) + "\n")
    print("\n".join(md))
    return 0 if report["all_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
