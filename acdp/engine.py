"""The engine: the only component that writes state.

Invariant: every state change commits in the same transaction as the ledger
event that describes it. Agents are called here, but their output reaches
state only through acdp.gates.
"""
from __future__ import annotations

import datetime as dt
import hashlib
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

import httpx
from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from . import canonical as C
from . import gates as G
from . import ledger
from . import normalize as N
from . import resolution as R
from .agents import Agent, AgentError, DeterministicAgent, build_agent
from .config import Settings, get_settings
from .connectors import REGISTRY, SourceRow
from .connectors.base import ConnectorError
from .models import (
    CanonicalRecord, ConsentEvent, Constraint, PairDecision, Profile, ProfileAlias, ProfileMember, Proposal,
    RawRecord, Run, SchemaMapping, Source, Suppression, utcnow,
)
from .util import canonical_json, digest, mask_value, pii_hash

MAX_BLOCK = 200
AGENT_BUDGET = 2000  # gray-zone agent calls per run; overflow goes to the deterministic adjudicator


class EngineError(RuntimeError):
    pass


@dataclass
class Computed:
    clusters: list[list[str]]
    golden: dict[str, tuple[dict, dict]]  # first-member -> (golden, lineage)
    consent: dict[str, dict]
    state_digest: str
    stats: dict[str, Any] = field(default_factory=dict)
    refused: list[tuple[str, str, str, float]] = field(default_factory=list)
    missing_decisions: int = 0
    uf: Any = None


class Engine:
    def __init__(self, session_factory, settings: Settings | None = None, agent: Agent | None = None,
                 http_client: httpx.Client | None = None, sleep: Callable[[float], None] | None = None):
        self.sf = session_factory
        self.settings = settings or get_settings()
        self.key = self.settings.require_hmac_key()
        self.agent: Agent = agent or build_agent(self.settings)
        self.fallback = DeterministicAgent()
        self.http_client = http_client
        self.sleep = sleep or (lambda _s: None)
        self.policy = self.settings.policy

    # ------------------------------------------------------------ helpers
    def _h(self, kind: str, v: str) -> str:
        return pii_hash(self.key, kind, v)

    def _policy_digest(self) -> str:
        p = self.policy
        return digest([R.RESOLVER_VERSION, p.auto_match, p.non_match, p.agent_min_confidence, R.W, R.CONTACT_CONFLICT])[:16]

    # ------------------------------------------------------------ sources
    def register_source(self, source_id: str, kind: str, config: dict[str, Any] | None = None,
                        trust_rank: int = 50, actor: str = "system") -> Source:
        if kind not in REGISTRY:
            raise EngineError(f"unknown connector kind {kind!r}; available: {sorted(REGISTRY)}")
        config = config or {}
        leaked = [k for k, v in config.items() if isinstance(v, str) and any(
            s in k.lower() for s in ("token", "secret", "password", "apikey", "api_key")) and not k.endswith("_env")]
        if leaked:
            raise EngineError(f"secrets must be passed by env-var name (*_env), not inline: {leaked}")
        with self.sf() as s, s.begin():
            src = s.get(Source, source_id)
            if src is None:
                src = Source(id=source_id, kind=kind, config=config, trust_rank=trust_rank)
                s.add(src)
            else:
                src.kind, src.config, src.trust_rank = kind, config, trust_rank
            ledger.append(s, "source_registered", actor, {"source": source_id, "kind": kind, "trust_rank": trust_rank,
                                                          "config_digest": digest(config)})
        return src

    def _connector(self, src: Source):
        return REGISTRY[src.kind](src.id, dict(src.config), client=self.http_client, sleep=self.sleep)

    # ------------------------------------------------------------ schema mapping
    def field_facts(self, rows: list[SourceRow]) -> dict[str, dict[str, Any]]:
        flats = [C.flatten(r.payload) for r in rows]
        names = sorted({k for f in flats for k in f})
        facts: dict[str, dict[str, Any]] = {}
        cc = self.policy.default_country_code
        for n in names:
            vals = [f.get(n) for f in flats]
            vals = [v for v in vals if v is not None and str(v).strip() != "" and not isinstance(v, (dict, list))]
            vr = {}
            for t in C.TARGETS:
                if t == "attribute":
                    continue
                fn = C.validator(t, cc)
                if t in {"updated_at", "created_at"}:
                    temporal_name = any(token in n.lower() for token in (
                        "timestamp", "created", "updated", "modified", "datetime", "time", "stamp"))
                    fn = lambda v, allow_unix=temporal_name: N.timestamp(v, allow_unix=allow_unix)
                ok = sum(1 for v in vals if fn(v) is not None)
                vr[t] = round(ok / len(vals), 3) if vals else 0.0
            facts[n] = {"non_null": len(vals), "non_null_rate": round(len(vals) / max(1, len(flats)), 3),
                        "valid_rate": vr, "samples": vals[:5]}
        return facts

    def _mapping_task(self, src: Source, facts: dict[str, dict[str, Any]]) -> dict[str, Any]:
        raw = self.settings.llm_pii_mode == "raw"
        return {
            "source_id": src.id, "source_kind": src.kind,
            "fields": [{"name": n, "samples": [str(v) if raw else mask_value(v) for v in f["samples"]],
                        "stats": {"non_null_rate": f["non_null_rate"],
                                  "valid_rate": {k: v for k, v in f["valid_rate"].items() if v > 0}}}
                       for n, f in facts.items()],
            "targets": C.TARGETS, "transforms": sorted(C.TRANSFORMS),
        }

    def _record_proposal(self, s: Session, kind: str, subject: str, agent: str, task: dict, out: dict,
                         gates: list[G.GateResult], outcome: str) -> Proposal:
        p = Proposal(kind=kind, subject=subject, agent=agent, input_digest=digest(task), input=task, output=out,
                     gates=[g.to_dict() for g in gates], outcome=outcome)
        s.add(p)
        s.flush()
        return p

    def _propose_mapping(self, s: Session, src: Source, rows: list[SourceRow], actor: str) -> SchemaMapping | None:
        facts = self.field_facts(rows)
        task = self._mapping_task(src, facts)
        baseline = self.fallback.propose_mapping(task)
        is_det = isinstance(self.agent, DeterministicAgent)
        try:
            out = baseline if is_det else self.agent.propose_mapping(task)
        except AgentError as e:
            out = {"error": str(e)}
        gates = G.mapping_gates(out, facts, None if is_det else baseline, self.policy.min_mapping_validity)
        v = G.verdict(gates)
        prop = self._record_proposal(s, "schema_mapping", src.id, self.agent.name, task, out, gates, v)
        mapping_out = out
        if v == "rejected" and not is_det:
            ledger.append(s, "proposal_rejected", self.agent.name, {"proposal": prop.id, "kind": "schema_mapping",
                          "source": src.id, "failed": [g.gate for g in gates if not g.passed]})
            gates = G.mapping_gates(baseline, facts, None, self.policy.min_mapping_validity)
            v = G.verdict(gates)
            prop = self._record_proposal(s, "schema_mapping", src.id, f"{self.fallback.name}(fallback)", task,
                                         baseline, gates, v)
            mapping_out = baseline
        if v == "rejected":
            ledger.append(s, "mapping_rejected", "gates", {"proposal": prop.id, "source": src.id,
                          "failed": [g.gate for g in gates if not g.passed]})
            return None
        version = (s.execute(select(func.max(SchemaMapping.version)).where(SchemaMapping.source_id == src.id))
                   .scalar_one() or 0) + 1
        clean = {"fields": {k: {"target": v2.get("target"), "transform": v2.get("transform", "default")}
                            for k, v2 in mapping_out["fields"].items() if v2.get("target")},
                 # columns that were empty when mapped: if they start carrying values, re-map
                 "deferred": sorted(n for n, f in facts.items() if f["non_null"] == 0)}
        sm = SchemaMapping(source_id=src.id, version=version, mapping=clean, digest=digest(clean),
                           status="approved" if v == "auto_applied" else "proposed", proposal_id=prop.id)
        s.add(sm)
        s.flush()
        if v == "auto_applied":
            self._activate_mapping(s, src, sm, actor=f"gates:{prop.agent}")
        else:
            ledger.append(s, "mapping_queued", "gates", {"proposal": prop.id, "source": src.id, "mapping": sm.id,
                          "soft_failures": [g.gate for g in gates if not g.passed]})
        return sm

    def _activate_mapping(self, s: Session, src: Source, sm: SchemaMapping, actor: str) -> None:
        if src.active_mapping_id and src.active_mapping_id != sm.id:
            old = s.get(SchemaMapping, src.active_mapping_id)
            if old:
                old.status = "superseded"
        sm.status = "approved"
        src.active_mapping_id = sm.id
        ledger.append(s, "mapping_applied", actor, {"source": src.id, "mapping": sm.id, "version": sm.version,
                                                    "digest": sm.digest})

    def set_mapping(self, source_id: str, mapping: dict[str, Any], actor: str, force: bool = False) -> dict[str, Any]:
        """Steward-authored mapping. Humans pass the same hard gates unless an admin forces it."""
        with self.sf() as s, s.begin():
            src = s.get(Source, source_id)
            if not src:
                raise EngineError("unknown source")
            sample = s.execute(select(RawRecord).where(RawRecord.source_id == source_id, RawRecord.erased.is_(False))
                               .limit(200)).scalars().all()
            facts = self.field_facts([SourceRow(r.source_key, r.payload) for r in sample])
            fm = {"fields": {k: {**v, "confidence": v.get("confidence", 1.0)} for k, v in mapping.get("fields", {}).items()}}
            # Fields not present in the sample cannot be validated; they are allowed only with force.
            gates = G.mapping_gates(fm, facts or {k: {} for k in fm["fields"]}, None, self.policy.min_mapping_validity)
            v = G.verdict(gates)
            if v == "rejected" and not force:
                return {"applied": False, "gates": [g.to_dict() for g in gates]}
            version = (s.execute(select(func.max(SchemaMapping.version)).where(SchemaMapping.source_id == source_id))
                       .scalar_one() or 0) + 1
            clean = {"fields": {k: {"target": x["target"], "transform": x.get("transform", "default")}
                                for k, x in mapping["fields"].items() if x.get("target")}}
            prop = self._record_proposal(s, "schema_mapping", source_id, f"steward:{actor}", {"mapping": mapping},
                                         clean, gates, "overridden" if v == "rejected" else "approved")
            sm = SchemaMapping(source_id=source_id, version=version, mapping=clean, digest=digest(clean),
                               status="approved", proposal_id=prop.id)
            s.add(sm)
            s.flush()
            self._activate_mapping(s, src, sm, actor=f"steward:{actor}" + (":FORCED" if v == "rejected" else ""))
            return {"applied": True, "mapping_id": sm.id, "forced": v == "rejected", "gates": [g.to_dict() for g in gates]}

    # ------------------------------------------------------------ ingest
    def ingest(self, source_id: str, rows: Iterable[SourceRow] | None = None, actor: str = "system") -> dict[str, Any]:
        with self.sf() as s:
            src = s.get(Source, source_id)
            if not src:
                raise EngineError(f"unknown source {source_id}")
            conn = self._connector(src)
            push = rows is not None
            if not push and conn.push_only:
                return {"source": source_id, "status": "push_only"}
            try:
                sample = list(rows) if push else conn.discover(200)
            except ConnectorError as e:
                ledger.append(s, "ingest_failed", actor, {"source": source_id, "error": str(e)[:300]})
                s.commit()
                return {"source": source_id, "status": "failed", "error": str(e)}
            if not sample:
                return {"source": source_id, "status": "empty"}
            fp = digest(sorted({k for r in sample for k in C.flatten(r.payload)}))
            drift = bool(src.schema_fingerprint and src.schema_fingerprint != fp)
            populated = []
            if src.active_mapping_id and not drift:
                deferred = set((s.get(SchemaMapping, src.active_mapping_id).mapping or {}).get("deferred", []))
                if deferred:
                    flats = [C.flatten(r.payload) for r in sample]
                    populated = sorted(f for f in deferred if any(x.get(f) not in (None, "") for x in flats))
            if src.active_mapping_id is None or drift or populated:
                if drift or populated:
                    ledger.append(s, "schema_drift", "system", {"source": source_id, "old": src.schema_fingerprint,
                                                                "new": fp, "newly_populated": populated})
                    src.active_mapping_id = None
                self._propose_mapping(s, src, sample, actor)
            src.schema_fingerprint = fp
            mapping_id = src.active_mapping_id
            s.commit()
            if mapping_id is None:
                return {"source": source_id, "status": "blocked_on_mapping"}
            sm = s.get(SchemaMapping, mapping_id)
            try:
                stream = sample if push else conn.read(src.cursor)
                stats = self._ingest_rows(s, src, sm, stream)
            except ConnectorError as e:
                s.rollback()
                ledger.append(s, "ingest_failed", actor, {"source": source_id, "error": str(e)[:300]})
                s.commit()
                return {"source": source_id, "status": "failed", "error": str(e)}
            if stats.get("quarantined"):
                s.rollback()
                src = s.get(Source, source_id)
                src.schema_fingerprint = None
                src.active_mapping_id = None
                p = self._record_proposal(s, "drift_alert", source_id, "gates", {"stats": stats}, {},
                                          [G.GateResult("I1.batch_validity", False, "soft", stats["quarantined"])],
                                          "queued")
                ledger.append(s, "ingest_quarantined", "gates", {"source": source_id, "proposal": p.id,
                                                                  "reason": stats["quarantined"]})
                s.commit()
                return {"source": source_id, "status": "quarantined", **stats}
            if not push and conn.next_cursor:
                src.cursor = conn.next_cursor
            ledger.append(s, "ingest_batch", actor, {"source": source_id, "mapping": sm.id,
                                                     **{k: v for k, v in stats.items() if isinstance(v, int)}})
            s.commit()
            return {"source": source_id, "status": "ok", **stats}

    def _ingest_rows(self, s: Session, src: Source, sm: SchemaMapping, rows: Iterable[SourceRow]) -> dict[str, Any]:
        stats = Counter()
        cc = self.policy.default_country_code
        fields = sm.mapping.get("fields", {})
        ident_fields = {k: v["target"] for k, v in fields.items() if v["target"] in C.IDENTITY_TARGETS}
        seen_valid: Counter = Counter()
        seen_non_null: Counter = Counter()
        suppressed = {h for (h,) in s.execute(select(Suppression.hash)).all()}
        existing = {r.source_key: r for r in s.execute(select(RawRecord).where(RawRecord.source_id == src.id)).scalars()}
        for row in rows:
            stats["seen"] += 1
            flat = C.flatten(row.payload)
            for f, t in ident_fields.items():
                v = flat.get(f)
                if v is not None and str(v).strip() != "":
                    seen_non_null[f] += 1
                    if C.validator(t, cc)(v) is not None:
                        seen_valid[f] += 1
            data = C.canonicalize(flat, sm.mapping, cc)
            keys = C.match_keys(data)
            hashes = {self._h("email", e) for e in keys["email_keys"]}
            if not keys["email_keys"]:
                hashes |= {self._h("phone", p) for p in keys["phones"]}
            if hashes & suppressed:
                stats["suppressed"] += 1
                continue
            d = digest(row.payload)
            raw = existing.get(row.key)
            rid = f"{src.id}:{row.key}"
            if raw is not None and raw.erased:
                stats["suppressed"] += 1
                continue
            if raw is not None and raw.digest == d:
                cr = s.get(CanonicalRecord, rid)
                if cr is not None and cr.mapping_id == sm.id:
                    stats["unchanged"] += 1
                    continue
            if raw is None:
                raw = RawRecord(source_id=src.id, source_key=row.key, payload=row.payload, digest=d,
                                source_updated_at=row.updated_at)
                s.add(raw)
                existing[row.key] = raw
                stats["new"] += 1
            else:
                raw.payload, raw.digest, raw.source_updated_at = row.payload, d, row.updated_at
                stats["changed"] += 1
            observed = data.get("updated_at") or (C.validator("updated_at")(row.updated_at) if row.updated_at else None) \
                or utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
            cd = digest(data)
            cr = s.get(CanonicalRecord, rid)
            if cr is None:
                s.add(CanonicalRecord(id=rid, source_id=src.id, mapping_id=sm.id, data=data, keys=keys, digest=cd,
                                      observed_at=observed))
            else:
                cr.mapping_id, cr.data, cr.keys, cr.digest, cr.observed_at = sm.id, data, keys, cd, observed
            for purpose, granted in data.get("consent", {}).items():
                # A consent event is a CHANGE of value. A record touched for other reasons (new job title)
                # does not re-assert its old consent at the new modification time.
                last = s.execute(select(ConsentEvent).where(
                    ConsentEvent.record_id == rid, ConsentEvent.purpose == purpose)
                    .order_by(ConsentEvent.observed_at.desc()).limit(1)).scalar_one_or_none()
                if last is None or (last.granted != bool(granted) and observed > last.observed_at):
                    s.add(ConsentEvent(record_id=rid, purpose=purpose, granted=bool(granted), observed_at=observed,
                                       source_id=src.id))
            if not C.has_identity(keys):
                stats["no_identity"] += 1
        for f, n in seen_non_null.items():
            if n >= 20 and seen_valid[f] / n < self.policy.min_mapping_validity:
                stats_out = dict(stats)
                stats_out["quarantined"] = f"field {f} validity {seen_valid[f] / n:.2f} below {self.policy.min_mapping_validity}"
                return stats_out
        s.flush()
        return dict(stats)

    # ------------------------------------------------------------ resolution
    def _adjudication_task(self, a: str, b: str, sc: float, comp: dict, sa: str, sb: str) -> dict[str, Any]:
        return {"pair": [self._h("rec", a)[:16], self._h("rec", b)[:16]], "score": sc,
                "sources": [sa, sb], "comparisons": comp}

    def _adjudicate(self, s: Session, a: str, b: str, sc: float, comp: dict, srcs: tuple[str, str],
                    cannot: bool, use_agent: bool) -> tuple[str, str, int]:
        task = self._adjudication_task(a, b, sc, comp, *srcs)
        agents = [self.agent] if use_agent else []
        if not use_agent or not isinstance(self.agent, DeterministicAgent):
            agents.append(self.fallback)
        prop = None
        for ag in agents:
            try:
                out = ag.adjudicate(task)
            except AgentError as e:
                out = {"error": str(e)}
            gates = G.adjudication_gates(out, comp, cannot, self.policy.agent_min_confidence)
            v = G.verdict(gates)
            name = ag.name if ag is self.agent and use_agent else f"{ag.name}(fallback)"
            prop = self._record_proposal(s, "match_adjudication", f"{a}|{b}", name, task, out, gates, v)
            if v == "auto_applied":
                return out["decision"], name, prop.id
            if v == "queued":
                return "review", name, prop.id
            # rejected -> try the next (fallback) agent
        return "review", "gates", prop.id if prop else None

    def _compute(self, s: Session, mode: str = "live") -> Computed:
        """mode 'live' may call agents and write decisions; 'replay' is read-only and hermetic."""
        recs = s.execute(select(CanonicalRecord).where(CanonicalRecord.erased.is_(False))).scalars().all()
        trust = {x.id: x.trust_rank for x in s.execute(select(Source)).scalars()}
        keys = {r.id: r.keys for r in recs}
        by_id = {r.id: r for r in recs}
        cons = s.execute(select(Constraint)).scalars().all()
        cannot = {(c.a, c.b) for c in cons if c.kind == "cannot_link"}
        cannot_any = cannot | {(b, a) for a, b in cannot}
        must = [(c.a, c.b) for c in cons if c.kind == "must_link" and c.a in keys and c.b in keys]
        cache: dict[tuple[str, str, str], PairDecision] = {}
        for d in s.execute(select(PairDecision)).scalars():
            cache[(d.a, d.b, d.evidence_digest)] = d
        pairs, oversized = R.candidate_pairs(keys, MAX_BLOCK)
        amb = R.ambiguous_identifiers(keys)
        rare = R.rare_names(keys)
        ids_of = {rid: set(R.identifiers(k)) for rid, k in keys.items()}
        pol = self._policy_digest()
        edges: list[tuple[float, str, str]] = []
        review_pairs: list[tuple[str, str]] = []  # current-evidence decisions only
        st: Counter = Counter()
        missing = 0
        agent_calls = 0
        for a, b in sorted(pairs):
            # ambiguity + name-rarity context is part of the evidence: if it changes, the pair is re-decided
            ctx = sorted((ids_of[a] & ids_of[b]) & amb) + [f"rare:{keys[a]['fn']}|{keys[a]['ln']}"
                                                            if f"{keys[a]['fn']}|{keys[a]['ln']}" in rare else ""]
            ev = digest([keys[a], keys[b], ctx, pol])
            d = cache.get((a, b, ev))
            if d is not None:
                decision, sc = d.decision, d.score
                st[f"cached_{d.band}"] += 1
            else:
                comp = R.compare(keys[a], keys[b], amb, rare)
                sc = R.score(comp)
                bd = R.band(comp, sc, self.policy.auto_match, self.policy.non_match)
                if mode == "replay":
                    if bd == "gray":
                        missing += 1
                        continue
                    decision = "match" if bd == "match" else "no_match"
                else:
                    proposal_id = None
                    if bd == "match":
                        decision, by = "match", "rule"
                    elif bd in {"non_match", "conflict"}:
                        decision, by = "no_match", "rule"
                    else:
                        use_agent = agent_calls < AGENT_BUDGET
                        if not isinstance(self.agent, DeterministicAgent) and use_agent:
                            agent_calls += 1
                        decision, by, proposal_id = self._adjudicate(
                            s, a, b, sc, comp, (by_id[a].source_id, by_id[b].source_id),
                            (a, b) in cannot_any, use_agent)
                    s.add(PairDecision(a=a, b=b, evidence_digest=ev, score=sc, band=bd, decision=decision,
                                       decided_by=by, proposal_id=proposal_id))
                    st[f"new_{bd}"] += 1
            if decision == "match":
                edges.append((sc, a, b))
            elif decision == "review":
                review_pairs.append((a, b))
        cannot_map: dict[str, set[str]] = defaultdict(set)
        for a, b in cannot:
            cannot_map[a].add(b)
            cannot_map[b].add(a)

        def cannot_partners(cl: list[str]) -> set[str]:
            return {x for i in cl for x in cannot_map.get(i, ())}
        uf = R.ConstrainedClusters(keys.keys(), keys, cannot,
                                   sources={rid: rec.source_id for rid, rec in by_id.items()}, ambiguous=amb,
                                   source_guard=True, contact_guard=True)
        for a, b in sorted(must):
            uf.union(a, b, force=True)
        refused = []
        for sc, a, b in sorted(edges, key=lambda e: (-e[0], e[1], e[2])):
            why = uf.union(a, b)
            if why:
                refused.append((a, b, why, sc))
                if why in {"source_overlap_identity", "contact_footprint_identity", "ambiguous_locality_phone"}:
                    review_pairs.append((a, b))
        clusters = uf.clusters()
        ev_by_rec: dict[str, list] = defaultdict(list)
        for e in s.execute(select(ConsentEvent)).scalars():
            ev_by_rec[e.record_id].append({"purpose": e.purpose, "granted": e.granted, "observed_at": e.observed_at,
                                           "source_id": e.source_id, "record_id": e.record_id})
        # Channel consent follows the channel identifier: an opt-out recorded against an email address
        # (or phone) applies to every profile holding that address, even across unmerged fragments.
        by_email: dict[str, set[str]] = defaultdict(set)
        by_phone: dict[str, set[str]] = defaultdict(set)
        for rid, k in keys.items():
            for e_ in k["email_keys"]:
                by_email[e_].add(rid)
            for p_ in k["phones"]:
                by_phone[p_].add(rid)
        # Records the engine could not decide about (queued for review) are an identity-uncertain
        # neighbourhood: their opt-outs are honoured until a human says they are someone else.
        review_nb: dict[str, set[str]] = defaultdict(set)
        for a, b in review_pairs:
            review_nb[a].add(b)
            review_nb[b].add(a)
        # Consent conflicts are tracked at source-record granularity. Old
        # denials superseded on the same record are no longer active.
        current_event_by_record: dict[tuple[str, str], dict] = {}
        for rid, events in ev_by_rec.items():
            for event in events:
                key = (rid, event["purpose"])
                previous = current_event_by_record.get(key)
                if previous is None or event["observed_at"] > previous["observed_at"] or (
                    event["observed_at"] == previous["observed_at"] and not event["granted"]
                ):
                    current_event_by_record[key] = event
        # When a denial cannot be linked through contact data, an exact
        # normalized full-name collision is still a consent risk. It does not
        # merge profiles; it only holds a grant when the denial is at least as
        # recent as that profile's grant.
        denied_by_name: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
        denied_by_name_postal: dict[tuple[str, str, str, str], list[dict]] = defaultdict(list)
        denied_by_last_city: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
        denied_by_first_address: dict[tuple[str, str, str, str], list[dict]] = defaultdict(list)
        for (rid, _), event in current_event_by_record.items():
            if event["granted"]:
                continue
            k = keys.get(rid, {})
            purpose = event["purpose"]
            if k.get("fn") and k.get("ln"):
                denied_by_name[(purpose, k["fn"], k["ln"])].append(event)
                if k.get("postal"):
                    denied_by_name_postal[(purpose, k["fn"], k["ln"], k["postal"])].append(event)
                if k.get("city"):
                    denied_by_last_city[(purpose, k["ln"], k["city"])].append(event)
                if k.get("addr") and k.get("postal"):
                    denied_by_first_address[(purpose, k["fn"], k["addr"], k["postal"])].append(event)
        golden, consent = {}, {}
        for cl in clusters:
            members = [{"id": i, "source_id": by_id[i].source_id, "trust": trust.get(by_id[i].source_id, 50),
                        "observed_at": by_id[i].observed_at, "data": by_id[i].data} for i in cl]
            golden[cl[0]] = R.survive(members)
            own = [e for i in cl for e in ev_by_rec.get(i, [])]
            email_recs = {r for i in cl for e_ in keys[i]["email_keys"] for r in by_email[e_]}
            phone_recs = {r for i in cl for p_ in keys[i]["phones"] for r in by_phone[p_]}
            # Only DENIALS propagate along a channel identifier; a grant must come from the person's own
            # records. (Shared inboxes and placeholder emails would otherwise leak one person's opt-in.)
            chan = [e for r in sorted(email_recs - set(cl)) for e in ev_by_rec.get(r, [])
                    if e["purpose"] == "email_marketing" and not e["granted"]]
            chan += [e for r in sorted(phone_recs - set(cl)) for e in ev_by_rec.get(r, [])
                     if e["purpose"] == "sms_marketing" and not e["granted"]]
            nb = {r for i in cl for r in review_nb.get(i, ())} - set(cl) - cannot_partners(cl)
            chan += [e for r in sorted(nb) for e in ev_by_rec.get(r, []) if not e["granted"]]
            base_consent = R.resolve_consent(own + chan)
            name_holds = []
            identity_holds = []
            # A newer grant on one record must not erase an active denial on a
            # different phone identity inside a profile. This can be a false
            # merge, or a person's records may disagree; either way, activation
            # stays blocked until the denying record itself is updated.
            current_by_record: dict[tuple[str, str], dict] = {}
            for event in own:
                key = (event["record_id"], event["purpose"])
                prev = current_by_record.get(key)
                if prev is None or event["observed_at"] > prev["observed_at"] or (
                    event["observed_at"] == prev["observed_at"] and not event["granted"]
                ):
                    current_by_record[key] = event
            contact_holds = []
            for purpose, current in base_consent.items():
                if current.get("granted") is not True:
                    continue
                active = [e for (rid, p), e in current_by_record.items() if p == purpose]
                active_denials = [e for e in active if not e["granted"]]
                active_grants = [e for e in active if e["granted"]]
                for denial in active_denials:
                    denial_emails = set(keys.get(denial["record_id"], {}).get("email_keys", []))
                    denial_phones = set(keys.get(denial["record_id"], {}).get("phones", []))
                    for grant in active_grants:
                        grant_emails = set(keys.get(grant["record_id"], {}).get("email_keys", []))
                        grant_phones = set(keys.get(grant["record_id"], {}).get("phones", []))
                        email_conflict = denial_emails and grant_emails and denial_emails.isdisjoint(grant_emails)
                        phone_conflict = denial_phones and grant_phones and denial_phones.isdisjoint(grant_phones)
                        if email_conflict or phone_conflict:
                            contact_holds.append({
                                **denial,
                                "observed_at": current["observed_at"],
                                "source_id": "conflicting_contact_denial_hold",
                            })
                            break
                for i in cl:
                    k = keys[i]
                    if not k.get("fn") or not k.get("ln"):
                        continue
                    candidates = [e for e in denied_by_name.get((purpose, k["fn"], k["ln"]), [])
                                  if e["record_id"] not in cl and e["observed_at"] >= current["observed_at"]]
                    if candidates:
                        latest = max(candidates, key=lambda e: (e["observed_at"], e["source_id"], e["record_id"]))
                        name_holds.append({**latest, "source_id": "same_name_denial_hold"})
                    # A weak identity neighborhood can still be enough to make
                    # an export unsafe: a name change at the same address, or a
                    # near-variant first name with the same surname and city.
                    # Keep profiles separate, but hold their grants for review.
                    nearby = list(denied_by_first_address.get((purpose, k["fn"], k.get("addr"),
                                                                k.get("postal")), ())) if (
                        k.get("addr") and k.get("postal")
                    ) else []
                    if k.get("city"):
                        nearby.extend(
                            e for e in denied_by_last_city.get((purpose, k["ln"], k["city"]), ())
                            if N.jaro_winkler(k["fn"], keys[e["record_id"]].get("fn", "")) >= 0.82
                        )
                    if k.get("postal"):
                        nearby.extend(denied_by_name_postal.get((purpose, k["fn"], k["ln"], k["postal"]), ()))
                    nearby = [e for e in nearby if e["record_id"] not in cl]
                    if nearby:
                        nearest = max(nearby, key=lambda e: (e["observed_at"], e["source_id"], e["record_id"]))
                        identity_holds.append({**nearest, "observed_at": current["observed_at"],
                                               "source_id": "identity_uncertainty_denial_hold"})
            consent[cl[0]] = R.resolve_consent(own + chan + name_holds + contact_holds + identity_holds)
        sd = digest([[cl, digest(golden[cl[0]][0]), digest(consent[cl[0]])] for cl in clusters])
        st.update({"records": len(recs), "candidate_pairs": len(pairs), "match_edges": len(edges),
                   "refused_unions": len(refused), "clusters": len(clusters), "agent_calls": agent_calls,
                   "max_cluster": max((len(c) for c in clusters), default=0)})
        if oversized:
            st["oversized_blocks"] = len(oversized)
        return Computed(clusters, golden, consent, sd, dict(st), refused, missing, uf)

    def _running(self, s: Session) -> Run | None:
        r = s.execute(select(Run).where(Run.status == "running").order_by(Run.id.desc())).scalars().first()
        if r and (utcnow() - r.started_at) > dt.timedelta(hours=2):
            r.status = "failed"
            r.stats = {**(r.stats or {}), "error": "stale lock reclaimed"}
            return None
        return r

    def resolve(self, actor: str = "system", force: bool = False) -> dict[str, Any]:
        with self.sf() as s:
            with s.begin():
                if self._running(s):
                    raise EngineError("a resolution run is already in progress")
                run = Run(status="running", actor=actor)
                s.add(run)
                s.flush()
                run_id = run.id
            try:
                with s.begin():
                    comp = self._compute(s, "live")
                    self._record_conflicts(s, comp.refused)
                    comp.stats.update(self._consolidate_queue(s, comp.uf))
                # decisions + proposals are committed even if publication halts (agent work is not lost)
                with s.begin():
                    run = s.get(Run, run_id)
                    breakers = self._breakers(s, comp)
                    if breakers and not force:
                        run.status, run.finished_at = "halted", utcnow()
                        run.stats = {**comp.stats, "breakers": breakers}
                        run.state_digest = comp.state_digest
                        ledger.append(s, "run_halted", actor, {"run": run_id, "breakers": breakers,
                                                              "state_digest": comp.state_digest})
                        return {"run": run_id, "status": "halted", "breakers": breakers, **comp.stats}
                    changes = self._publish(s, comp, actor)
                    run.status, run.finished_at = "published", utcnow()
                    run.stats = {**comp.stats, **changes, **({"breakers_overridden": breakers} if breakers else {})}
                    run.state_digest = comp.state_digest
                    ledger.append(s, "run_published", actor, {"run": run_id, "state_digest": comp.state_digest,
                                                              "forced": bool(breakers and force), **changes})
                    return {"run": run_id, "status": "published", "state_digest": comp.state_digest,
                            **comp.stats, **changes}
            except Exception as e:
                with self.sf() as s2, s2.begin():
                    r = s2.get(Run, run_id)
                    r.status, r.finished_at, r.stats = "failed", utcnow(), {"error": str(e)[:500]}
                    ledger.append(s2, "run_failed", actor, {"run": run_id, "error": type(e).__name__})
                raise

    def _record_conflicts(self, s: Session, refused) -> None:
        open_subjects = {p.subject for p in s.execute(select(Proposal).where(
            Proposal.kind == "cluster_conflict")).scalars()}
        for a, b, why, sc in refused:
            subj = f"{a}|{b}"
            if subj in open_subjects:
                continue
            self._record_proposal(s, "cluster_conflict", subj, "gates", {"reason": why, "score": sc}, {},
                                  [G.GateResult("C1.cluster_consistency", False, "soft",
                                                f"union refused: would create a cluster with conflicting {why}")],
                                  "queued")

    def _consolidate_queue(self, s: Session, uf: R.ConstrainedClusters) -> dict[str, int]:
        """Only surface questions a human's answer could change.

        A queued pair whose records ended up in the same cluster anyway is moot; several queued pairs
        spanning the same two clusters are one question, so only the highest-scoring one stays open.
        """
        out = Counter()
        best: dict[tuple[str, str], Proposal] = {}
        for p in s.execute(select(Proposal).where(Proposal.outcome == "queued", Proposal.kind.in_(
                ["match_adjudication", "cluster_conflict"])).order_by(Proposal.id)).scalars():
            a, _, b = p.subject.partition("|")
            if a not in uf.parent or b not in uf.parent:
                continue
            ra, rb = uf.find(a), uf.find(b)
            if ra == rb:
                p.outcome, p.resolved_by, p.resolved_at = "moot", "gates:transitive", utcnow()
                out["review_moot"] += 1
                continue
            k = (min(ra, rb), max(ra, rb))
            sc = float((p.input or {}).get("score", 0))
            cur = best.get(k)
            if cur is None:
                best[k] = p
            else:
                keep, drop = (p, cur) if sc > float((cur.input or {}).get("score", 0)) else (cur, p)
                drop.outcome, drop.resolved_by, drop.resolved_at = "superseded", f"gates:dup-of-{keep.id}", utcnow()
                best[k] = keep
                out["review_superseded"] += 1
        out["review_open"] = len(best)
        return dict(out)

    def _breakers(self, s: Session, comp: Computed) -> list[str]:
        out = []
        if comp.stats["max_cluster"] > self.policy.max_cluster_size:
            out.append(f"max_cluster_size: {comp.stats['max_cluster']} > {self.policy.max_cluster_size}")
        prev = s.execute(select(func.count()).select_from(Profile)).scalar_one()
        new = len(comp.clusters)
        prev_records = s.execute(select(func.count()).select_from(ProfileMember)).scalar_one()
        if prev >= 50 and new < prev * (1 - self.policy.max_merge_ratio_jump) and comp.stats["records"] >= prev_records:
            out.append(f"merge_ratio_jump: profiles {prev} -> {new} (> {self.policy.max_merge_ratio_jump:.0%} collapse)")
        return out

    def _publish(self, s: Session, comp: Computed, actor: str) -> dict[str, int]:
        prev_member = {m.record_id: m.profile_id for m in s.execute(select(ProfileMember)).scalars()}
        existing = {p.id: p for p in s.execute(select(Profile)).scalars()}
        taken = set(existing) | {a.old_id for a in s.execute(select(ProfileAlias)).scalars()}
        order = sorted(comp.clusters, key=lambda c: (-len(c), c[0]))
        claimed: dict[str, list[str]] = {}
        for cl in order:
            cnt = Counter(prev_member[i] for i in cl if i in prev_member)
            pid = next((p for p, _ in sorted(cnt.items(), key=lambda kv: (-kv[1], kv[0])) if p not in claimed), None)
            if pid is None:
                n = 0
                while True:
                    pid = "p_" + hashlib.sha256(f"{cl[0]}|{n}".encode()).hexdigest()[:20]
                    if pid not in taken and pid not in claimed:
                        break
                    n += 1
            claimed[pid] = cl
        ch = Counter()
        rec_to_new = {i: pid for pid, cl in claimed.items() for i in cl}
        s.execute(delete(ProfileMember))
        now = utcnow()
        for pid, cl in claimed.items():
            golden, lineage = comp.golden[cl[0]]
            consent = comp.consent[cl[0]]
            pd = digest([golden, consent, cl])
            p = existing.get(pid)
            if p is None:
                s.add(Profile(id=pid, data=golden, lineage=lineage, consent=consent, digest=pd, member_count=len(cl),
                              created_at=now, updated_at=now))
                ch["profiles_created"] += 1
            elif p.digest != pd:
                p.data, p.lineage, p.consent, p.digest, p.member_count, p.updated_at = golden, lineage, consent, pd, len(cl), now
                ch["profiles_updated"] += 1
            for i in cl:
                s.add(ProfileMember(record_id=i, profile_id=pid))
            olds = sorted({prev_member[i] for i in cl if i in prev_member} - {pid})
            if olds:
                ledger.append(s, "profiles_merged", actor, {"into": pid, "retired": olds,
                                                            "members": [self._h("rec", i)[:16] for i in cl]})
                ch["merges"] += len(olds)
            if pid in existing:
                prev_set = {r for r, p2 in prev_member.items() if p2 == pid}
                moved_out = sorted(prev_set - set(cl))
                if moved_out:
                    dests = sorted({rec_to_new[r] for r in moved_out if r in rec_to_new})
                    ledger.append(s, "profile_split", actor, {"profile": pid, "to": dests,
                                                              "moved": [self._h("rec", r)[:16] for r in moved_out]})
                    ch["splits"] += 1
        for pid, p in existing.items():
            if pid in claimed:
                continue
            members = [r for r, p2 in prev_member.items() if p2 == pid]
            dest = Counter(rec_to_new[r] for r in members if r in rec_to_new).most_common(1)
            s.delete(p)
            if not s.get(ProfileAlias, pid):
                s.add(ProfileAlias(old_id=pid, new_id=dest[0][0] if dest else "", reason="merged" if dest else "retired"))
            ch["profiles_retired"] += 1
        s.flush()
        return dict(ch)

    # ------------------------------------------------------------ replay & state
    def current_state_digest(self) -> str:
        with self.sf() as s:
            members: dict[str, list[str]] = defaultdict(list)
            for m in s.execute(select(ProfileMember)).scalars():
                members[m.profile_id].append(m.record_id)
            profs = {p.id: p for p in s.execute(select(Profile)).scalars()}
            rows = sorted(([sorted(ms), digest(profs[pid].data), digest(profs[pid].consent)]
                           for pid, ms in members.items() if pid in profs), key=lambda r: r[0][0])
            return digest(rows)

    def replay(self) -> dict[str, Any]:
        """Hermetic replay: recompute from stored records + recorded decisions, no agent calls, no writes."""
        with self.sf() as s:
            chain = ledger.verify(s)
            comp = self._compute(s, "replay")
            s.rollback()
            last = s.execute(select(Run).where(Run.status == "published").order_by(Run.id.desc())).scalars().first()
        cur = self.current_state_digest()
        ok = chain["ok"] and comp.missing_decisions == 0 and comp.state_digest == cur
        return {"ok": ok, "ledger": chain, "replayed_digest": comp.state_digest, "published_digest": cur,
                "last_run_digest": last.state_digest if last else None, "missing_decisions": comp.missing_decisions}

    # ------------------------------------------------------------ stewardship
    def add_constraint(self, kind: str, a: str, b: str, actor: str, reason: str = "") -> None:
        if kind not in {"must_link", "cannot_link"}:
            raise EngineError("kind must be must_link or cannot_link")
        a, b = sorted((a, b))
        with self.sf() as s, s.begin():
            for rid in (a, b):
                if not s.get(CanonicalRecord, rid):
                    raise EngineError(f"unknown record {rid}")
            opposite = "cannot_link" if kind == "must_link" else "must_link"
            s.execute(delete(Constraint).where(Constraint.kind == opposite, Constraint.a == a, Constraint.b == b))
            if not s.execute(select(Constraint).where(Constraint.kind == kind, Constraint.a == a, Constraint.b == b)).first():
                s.add(Constraint(kind=kind, a=a, b=b, actor=actor, reason=reason))
            ledger.append(s, f"constraint_{kind}", actor, {"a": self._h("rec", a)[:16], "b": self._h("rec", b)[:16],
                                                           "reason_digest": digest(reason)[:16]})

    def split_profile(self, profile_id: str, record_ids: list[str], actor: str, reason: str = "") -> int:
        """Detach record_ids from the rest of the profile (cannot_link to every remaining member)."""
        with self.sf() as s:
            members = [m.record_id for m in s.execute(select(ProfileMember).where(
                ProfileMember.profile_id == profile_id)).scalars()]
        rest = [m for m in members if m not in record_ids]
        n = 0
        for a in record_ids:
            for b in rest:
                self.add_constraint("cannot_link", a, b, actor, reason)
                n += 1
        return n

    def review(self, proposal_id: int, approve: bool, actor: str, note: str = "") -> dict[str, Any]:
        with self.sf() as s, s.begin():
            p = s.get(Proposal, proposal_id)
            if not p:
                raise EngineError("unknown proposal")
            if p.outcome != "queued":
                raise EngineError(f"proposal is {p.outcome}, not queued")
            p.outcome = "approved" if approve else "rejected"
            p.resolved_by, p.resolved_at, p.note = actor, utcnow(), note
            ledger.append(s, "review_" + p.outcome, actor, {"proposal": p.id, "kind": p.kind,
                                                            "note_digest": digest(note)[:16]})
            kind, subject = p.kind, p.subject
            if kind == "schema_mapping":
                sm = s.execute(select(SchemaMapping).where(SchemaMapping.proposal_id == p.id)).scalars().first()
                src = s.get(Source, subject)
                if sm and approve:
                    self._activate_mapping(s, src, sm, actor=f"steward:{actor}")
                elif sm:
                    sm.status = "rejected"
        if kind in {"match_adjudication", "cluster_conflict"}:
            a, b = subject.split("|", 1)
            self.add_constraint("must_link" if approve else "cannot_link", a, b, actor, note or f"review #{proposal_id}")
        return {"proposal": proposal_id, "outcome": "approved" if approve else "rejected"}

    # ------------------------------------------------------------ privacy
    def erase(self, actor: str, reason: str, email: str | None = None, phone: str | None = None,
              profile_id: str | None = None, allow_multiple: bool = False) -> dict[str, Any]:
        """Right-to-erasure. Deletes PII everywhere, keeps a hash-only ledger entry, and adds
        suppression hashes so the person is not re-ingested from a lagging source."""
        from . import normalize as N
        with self.sf() as s, s.begin():
            targets: set[str] = set()
            ident_hash = None
            if profile_id:
                pid = self._follow_alias(s, profile_id)
                targets |= {m.record_id for m in s.execute(select(ProfileMember).where(
                    ProfileMember.profile_id == pid)).scalars()}
                ident_hash = digest(["profile", profile_id])[:16]
            ek = N.email_key(N.email(email)) if email and N.email(email) else None
            ph = N.phone(phone, self.policy.default_country_code) if phone else None
            if email and not ek:
                raise EngineError("invalid email")
            if ek or ph:
                ident_hash = self._h("email", ek) if ek else self._h("phone", ph)
                for r in s.execute(select(CanonicalRecord).where(CanonicalRecord.erased.is_(False))).scalars():
                    if (ek and ek in r.keys.get("email_keys", [])) or (ph and ph in r.keys.get("phones", [])):
                        targets.add(r.id)
                # expand to whole profiles containing those records
                pids = {m.profile_id for m in s.execute(select(ProfileMember).where(
                    ProfileMember.record_id.in_(targets))).scalars()} if targets else set()
                if len(pids) > 1 and not allow_multiple:
                    raise EngineError(f"identifier is held by {len(pids)} profiles (shared inbox/phone?). "
                                      "Erase by profile_id after identity verification, or pass allow_multiple.")
                targets |= {m.record_id for m in s.execute(select(ProfileMember).where(
                    ProfileMember.profile_id.in_(pids))).scalars()} if pids else set()
            if not targets:
                ledger.append(s, "erasure_noop", actor, {"identifier": ident_hash, "reason_digest": digest(reason)[:16]})
                return {"erased_records": 0, "profiles": []}
            # Identifiers still held by someone else (family inbox, household landline) are NOT suppressed:
            # doing so would silently block other people's future records.
            held_elsewhere_e: set[str] = set()
            held_elsewhere_p: set[str] = set()
            for r in s.execute(select(CanonicalRecord).where(CanonicalRecord.erased.is_(False))).scalars():
                if r.id not in targets:
                    held_elsewhere_e.update(r.keys.get("email_keys", []))
                    held_elsewhere_p.update(r.keys.get("phones", []))
            sup = set()
            shared_skipped = 0
            for rid in targets:
                cr = s.get(CanonicalRecord, rid)
                if cr is None:
                    continue
                for e in cr.keys.get("email_keys", []):
                    if e in held_elsewhere_e:
                        shared_skipped += 1
                    else:
                        sup.add((self._h("email", e), "email"))
                if not cr.keys.get("email_keys"):
                    for p_ in cr.keys.get("phones", []):
                        if p_ in held_elsewhere_p:
                            shared_skipped += 1
                        else:
                            sup.add((self._h("phone", p_), "phone"))
                cr.data, cr.keys, cr.erased, cr.digest = {}, {"email_keys": [], "phones": []}, True, "erased"
                src_id, _, skey = rid.partition(":")
                raw = s.execute(select(RawRecord).where(RawRecord.source_id == src_id,
                                                        RawRecord.source_key == skey)).scalar_one_or_none()
                if raw:
                    raw.payload, raw.erased, raw.digest = {}, True, "erased"
            if ek and ek not in held_elsewhere_e:
                sup.add((self._h("email", ek), "email"))
            for h, kind in sup:
                if not s.get(Suppression, h):
                    s.add(Suppression(hash=h, kind=kind, reason="erasure"))
            tl = list(targets)
            s.execute(delete(ConsentEvent).where(ConsentEvent.record_id.in_(tl)))
            s.execute(delete(PairDecision).where(or_(PairDecision.a.in_(tl), PairDecision.b.in_(tl))))
            s.execute(delete(Constraint).where(or_(Constraint.a.in_(tl), Constraint.b.in_(tl))))
            for p in s.execute(select(Proposal).where(Proposal.kind.in_(
                    ["match_adjudication", "cluster_conflict"]))).scalars():
                a, _, b = p.subject.partition("|")
                if a in targets or b in targets:
                    s.delete(p)
            pids = sorted({m.profile_id for m in s.execute(select(ProfileMember).where(
                ProfileMember.record_id.in_(tl))).scalars()})
            s.execute(delete(ProfileMember).where(ProfileMember.record_id.in_(tl)))
            for pid in pids:
                if not s.execute(select(ProfileMember).where(ProfileMember.profile_id == pid)).first():
                    prof = s.get(Profile, pid)
                    if prof:
                        s.delete(prof)
                    s.merge(ProfileAlias(old_id=pid, new_id="", reason="erased"))
            ledger.append(s, "erasure", actor, {"identifier": ident_hash, "profiles": pids, "records": len(targets),
                                                "record_hashes": sorted(self._h("rec", t)[:16] for t in targets),
                                                "suppressions": len(sup), "shared_identifiers_not_suppressed": shared_skipped,
                                                "reason_digest": digest(reason)[:16]})
            return {"erased_records": len(targets), "profiles": pids, "suppressions": len(sup),
                    "shared_identifiers_not_suppressed": shared_skipped}

    def _follow_alias(self, s: Session, pid: str) -> str:
        seen = set()
        while pid not in seen:
            seen.add(pid)
            a = s.get(ProfileAlias, pid)
            if not a:
                return pid
            if not a.new_id:
                return ""
            pid = a.new_id
        return pid

    def get_profile(self, pid: str) -> dict[str, Any] | None:
        with self.sf() as s:
            real = self._follow_alias(s, pid)
            if real == "":
                return {"id": pid, "status": "erased_or_retired"}
            p = s.get(Profile, real)
            if not p:
                return None
            members = [m.record_id for m in s.execute(select(ProfileMember).where(
                ProfileMember.profile_id == real)).scalars()]
            return {"id": p.id, "requested_id": pid if pid != real else None, "data": p.data, "lineage": p.lineage,
                    "consent": p.consent, "members": sorted(members), "updated_at": p.updated_at.isoformat()}

    def export(self, purpose: str, limit: int = 10000) -> list[dict[str, Any]]:
        """Activation export. Fail-closed: only profiles with an explicit, current grant for `purpose`."""
        with self.sf() as s:
            out = []
            for p in s.execute(select(Profile).order_by(Profile.id)).scalars():
                c = (p.consent or {}).get(purpose)
                if c and c.get("granted") is True:
                    out.append({"id": p.id, **{k: p.data.get(k) for k in (
                        "first_name", "last_name", "primary_email", "primary_phone", "address")}})
                    if len(out) >= limit:
                        break
            ledger.append(s, "export", "system", {"purpose": purpose, "count": len(out)})
            s.commit()
            return out

    def run_all(self, actor: str = "system", force: bool = False) -> dict[str, Any]:
        """Autopilot: pull every pull-source, then resolve. The only human touchpoints are queued proposals."""
        with self.sf() as s:
            ids = [x.id for x in s.execute(select(Source).order_by(Source.id)).scalars()]
        ing = [self.ingest(i, actor=actor) for i in ids]
        return {"ingest": ing, "resolve": self.resolve(actor=actor, force=force)}
