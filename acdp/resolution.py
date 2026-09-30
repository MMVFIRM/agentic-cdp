"""Identity resolution primitives — all pure and deterministic.

compare -> score -> band -> (agent for gray band, gated) -> constrained clustering.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable

from .normalize import jaro_winkler

RESOLVER_VERSION = "2026.09-r15"  # bump on any change to compare/score/band/ambiguity — invalidates cached decisions
PERSONAL_EMAIL_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "yahoo.com", "ymail.com", "outlook.com", "hotmail.com",
    "live.com", "icloud.com", "me.com", "proton.me", "protonmail.com", "aol.com",
})

# Log-likelihood weights (hand-set, documented; see GATES.md for calibration).
W = {
    "email": {"agree": 14.0, "shared": 1.0, "disagree": -2.0},
    "phone": {"agree": 9.0, "shared": 1.0, "disagree": -1.0},
    "first_name": {"agree": 4.0, "partial": 2.5, "disagree": -5.0},
    "last_name": {"agree": 4.0, "partial": 2.5, "disagree": -5.0},
    "birth_date": {"agree": 8.0},
    "address": {"agree": 4.0, "shared": 0.5, "partial": 2.0, "disagree": -0.5},
    "postal_code": {"agree": 2.0, "disagree": -1.0},
    "city": {"agree": 0.5},
    "name_frequency": {"rare": 1.5},
}


def _status_set(a: list[str], b: list[str]) -> str:
    if not a or not b:
        return "missing"
    return "agree" if set(a) & set(b) else "disagree"


def identifiers(k: dict[str, Any]) -> list[str]:
    """Identifiers and quasi-identifiers whose agreement is evidence of identity."""
    ids = [f"e:{e}" for e in k["email_keys"]] + [f"p:{p}" for p in k["phones"]]
    if k["fn"] and k["ln"] and k["addr"]:
        ids.append(f"na:{k['fn']}|{k['ln']}|{k['addr']}")
    if k["fn"] and k["ln"] and k["postal"]:
        ids.append(f"np:{k['fn']}|{k['ln']}|{k['postal']}")
    return ids


def ambiguous_identifiers(keys: dict[str, dict[str, Any]]) -> set[str]:
    """An identifier observed on provably distinct people is not individuating.

    Provably distinct = two different birth dates, two different generational
    suffixes, or (for email/phone) two clearly different given names.
    Catches household phones, family inboxes, placeholder emails
    ("noreply@..."), and father/son at one address.
    """
    seen: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "dob": set(), "sfx": set(), "fn": set(), "nosfx": False,
        "contact_variants": defaultdict(lambda: {"suffixed": set(), "unsuffixed": set()}),
    })
    contact_cohorts: dict[str, dict[tuple[str, str], dict[str, Any]]] = defaultdict(
        lambda: defaultdict(lambda: {"dob": set(), "other": set(), "records": 0}))
    for k in keys.values():
        contacts = {*(f"e:{e}" for e in k["email_keys"]), *(f"p:{p}" for p in k["phones"])}
        full_name = f"{k['fn']}|{k['ln']}" if k.get("fn") and k.get("ln") else None
        locality = f"{k.get('postal')}|{k.get('addr')}" if k.get("postal") and k.get("addr") else None
        for i in identifiers(k):
            d = seen[i]
            if k.get("dob"):
                d["dob"].add(k["dob"])
            if k.get("suffix"):
                d["sfx"].add(k["suffix"])
            else:
                d["nosfx"] = True
            if k.get("fn") and i[0] in "ep":
                d["fn"].add(k["fn"])
            if full_name and i[0] in "ep":
                bucket = "suffixed" if k.get("suffix") else "unsuffixed"
                d["contact_variants"][full_name][bucket].update(contacts - {i})
        if full_name and locality:
            for i in contacts:
                if not i.startswith("e:"):
                    continue
                cohort = contact_cohorts[i][(full_name, locality)]
                if k.get("dob"):
                    cohort["dob"].add(k["dob"])
                cohort["other"].update(contacts - {i})
                cohort["records"] += 1
    amb = set()
    for i, d in seen.items():
        if len(d["dob"]) > 1 or len(d["sfx"]) > 1:
            amb.add(i)
            continue
        # Generational ambiguity: "Robert Scott Jr" lives at this address, so an un-suffixed
        # "Robert Scott" there may be his father. Name-at-place stops being individuating.
        # (Tried and reverted: extending this to phones/emails cost 1.5-2.2pts recall and +60-90% review
        # load for +0.04..0.65pts precision across 3 seeds. See GATES.md, "Negative results".)
        if i.startswith("n") and d["sfx"] and d["nosfx"]:
            amb.add(i)
            continue
        fns = sorted(d["fn"])
        if any(jaro_winkler(x, y) < 0.8 for n, x in enumerate(fns) for y in fns[n + 1:]):
            amb.add(i)
            continue
        # If the same full name appears with and without a suffix, and each
        # cohort uses disjoint secondary contacts, the shared contact is not
        # enough to auto-link them. This narrows the household/Jr-Sr guard to
        # cases with additional evidence of separate contact footprints.
        for variants in d["contact_variants"].values():
            suffixed, unsuffixed = variants["suffixed"], variants["unsuffixed"]
            if suffixed and unsuffixed and suffixed.isdisjoint(unsuffixed):
                amb.add(i)
                break
    # A contact reused by the same full name at different addresses can be a
    # household identifier. If each address has a disjoint alternate-contact
    # footprint and no DOB bridges them, that contact is shared rather than
    # individuating.
    for i, cohorts in contact_cohorts.items():
        if i in amb:
            continue
        grouped: dict[str, list[tuple[str, dict[str, set[str]]]]] = defaultdict(list)
        for (full_name, locality), facts in cohorts.items():
            grouped[full_name].append((locality, facts))
        for observations in grouped.values():
            for n, (left_loc, left) in enumerate(observations):
                for right_loc, right in observations[n + 1:]:
                    if (left_loc != right_loc and (left["records"] > 1 or right["records"] > 1)
                            and not (left["dob"] & right["dob"])
                            and left["other"] and right["other"]
                            and left["other"].isdisjoint(right["other"])):
                        amb.add(i)
                        break
                if i in amb:
                    break
            if i in amb:
                break
    return amb


def rare_names(keys: dict[str, dict[str, Any]]) -> set[str]:
    """Term-frequency signal: a full name seen in only one locality across the whole dataset.

    Self-calibrating: in a small regional dataset most names are rare; in a national one,
    'John Smith' is observed in hundreds of postal codes and never qualifies.
    """
    places: dict[str, set[str]] = defaultdict(set)
    for k in keys.values():
        if k.get("fn") and k.get("ln"):
            loc = k.get("postal") or (f"c:{k['city']}" if k.get("city") else None)
            if loc:
                places[f"{k['fn']}|{k['ln']}"].add(loc)
    return {n for n, p in places.items() if len(p) == 1}


def _status_shared(a: list[str], b: list[str], prefix: str, amb: set[str]) -> str:
    if not a or not b:
        return "missing"
    common = set(a) & set(b)
    if not common:
        return "disagree"
    return "agree" if any(f"{prefix}:{x}" not in amb for x in common) else "shared"


def compare(ka: dict[str, Any], kb: dict[str, Any], amb: set[str] | frozenset = frozenset(),
            rare: set[str] | frozenset = frozenset()) -> dict[str, dict[str, Any]]:
    c: dict[str, dict[str, Any]] = {}
    c["email"] = {"status": _status_shared(ka["email_keys"], kb["email_keys"], "e", amb)}
    c["phone"] = {"status": _status_shared(ka["phones"], kb["phones"], "p", amb)}

    def name(field: str, a: str | None, b: str | None, thr: float, a_raw=None, b_raw=None):
        if not a or not b:
            c[field] = {"status": "missing"}
            return
        if a == b:
            c[field] = {"status": "agree", "similarity": 1.0}
            return
        sim = max(jaro_winkler(a, b), jaro_winkler(a_raw or a, b_raw or b))
        c[field] = {"status": "partial" if sim >= thr else "disagree", "similarity": round(sim, 3)}

    name("first_name", ka["fn"], kb["fn"], 0.90, ka.get("fn_raw"), kb.get("fn_raw"))
    name("last_name", ka["ln"], kb["ln"], 0.92)
    if ka["dob"] and kb["dob"]:
        c["birth_date"] = {"status": "agree" if ka["dob"] == kb["dob"] else "conflict"}
    else:
        c["birth_date"] = {"status": "missing"}
    if ka.get("suffix") and kb.get("suffix") and ka["suffix"] != kb["suffix"]:
        c["name_suffix"] = {"status": "conflict"}
    elif ka.get("suffix") and kb.get("suffix"):
        c["name_suffix"] = {"status": "agree"}
    elif ka.get("suffix") or kb.get("suffix"):
        c["name_suffix"] = {"status": "partial"}
    else:
        c["name_suffix"] = {"status": "missing"}
    if ka["addr"] and kb["addr"]:
        if ka["addr"] == kb["addr"]:
            na = f"na:{ka['fn']}|{ka['ln']}|{ka['addr']}"
            c["address"] = {"status": "shared" if na in amb else "agree", "similarity": 1.0}
        else:
            sim = jaro_winkler(ka["addr"], kb["addr"])
            c["address"] = {"status": "partial" if sim >= 0.93 else "disagree", "similarity": round(sim, 3)}
    else:
        c["address"] = {"status": "missing"}
    if not (ka["postal"] and kb["postal"]):
        c["postal_code"] = {"status": "missing"}
    elif ka["postal"] != kb["postal"]:
        c["postal_code"] = {"status": "disagree"}
    else:
        np_ = f"np:{ka['fn']}|{ka['ln']}|{ka['postal']}"
        c["postal_code"] = {"status": "shared" if np_ in amb else "agree"}
    c["city"] = {"status": "missing"} if not (ka["city"] and kb["city"]) else \
        {"status": "agree" if ka["city"] == kb["city"] else "disagree"}
    if ka["fn"] and ka["fn"] == kb["fn"] and ka["ln"] and ka["ln"] == kb["ln"]:
        c["name_frequency"] = {"status": "rare" if f"{ka['fn']}|{ka['ln']}" in rare else "common"}
    else:
        c["name_frequency"] = {"status": "missing"}
    return c


CONTACT_CONFLICT = -3.0  # both independent contact identifiers present and both disagree


def score(c: dict[str, dict[str, Any]]) -> float:
    s = sum(W.get(f, {}).get(v["status"], 0.0) for f, v in c.items())
    if c["email"]["status"] == "disagree" and c["phone"]["status"] == "disagree":
        s += CONTACT_CONFLICT
    return round(s, 3)


def band(c: dict[str, dict[str, Any]], s: float, auto_match: float, non_match: float) -> str:
    """Auto-match requires a strong identifier (email, phone or birth date) to agree: quasi-identifiers
    (name, street, postal) never merge on their own without adjudication."""
    if any(v["status"] == "conflict" for v in c.values()):
        return "conflict"
    strong = any(c[f]["status"] == "agree" for f in ("email", "phone", "birth_date"))
    dob_only = (c["birth_date"]["status"] == "agree"
                and c["email"]["status"] != "agree" and c["phone"]["status"] != "agree")
    partial_surname = c["last_name"]["status"] == "partial"
    contact_conflict = c["email"]["status"] == "disagree" and c["phone"]["status"] == "disagree"
    if (s >= auto_match and strong and c["first_name"]["status"] != "disagree" and not contact_conflict
            and not (dob_only and partial_surname)):
        return "match"
    if s <= non_match:
        return "non_match"
    return "gray"


def blocking_keys(k: dict[str, Any]) -> list[str]:
    keys = [f"e:{e}" for e in k["email_keys"]] + [f"p:{p}" for p in k["phones"]]
    if k["ln"] and k["postal"]:
        keys.append(f"lp:{k['ln']}|{k['postal']}")
    if k["ln"] and k["dob"]:
        keys.append(f"ld:{k['ln']}|{k['dob']}")
    if k["fn"] and k["dob"]:
        keys.append(f"fd:{k['fn']}|{k['dob']}")
    if k["fn"] and k["postal"]:
        keys.append(f"fp:{k['fn']}|{k['postal']}")
    if k["fn"] and k["addr"]:
        keys.append(f"fa:{k['fn']}|{k['addr']}")
    return keys


def candidate_pairs(keys: dict[str, dict[str, Any]], max_block: int) -> tuple[set[tuple[str, str]], list[str]]:
    blocks: dict[str, list[str]] = defaultdict(list)
    for rid, k in keys.items():
        for bk in blocking_keys(k):
            blocks[bk].append(rid)
    pairs: set[tuple[str, str]] = set()
    oversized = []
    for bk, ids in blocks.items():
        if len(ids) > max_block:
            oversized.append(f"{bk.split(':')[0]}:<{len(ids)} records>")
            continue
        ids = sorted(ids)
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                pairs.add((ids[i], ids[j]))
    return pairs, oversized


class ConstrainedClusters:
    """Union-find that refuses unions creating an impossible cluster.

    A cluster may hold at most one distinct birth date and one distinct
    generational suffix, and never both ends of a cannot-link. This blocks the
    classic transitive-closure over-merge (A~B, B~C, but A!=C).
    """

    def __init__(self, ids: Iterable[str], keys: dict[str, dict[str, Any]], cannot: set[tuple[str, str]],
                 sources: dict[str, str] | None = None, ambiguous: set[str] | frozenset[str] = frozenset(),
                 source_guard: bool = True, contact_guard: bool = True):
        self.keys = keys
        self.sources = sources or {}
        self.ambiguous = set(ambiguous)
        self.source_guard = source_guard
        self.contact_guard = contact_guard
        self.parent = {i: i for i in ids}
        self.members = {i: {i} for i in self.parent}
        self.dobs = {i: ({keys[i]["dob"]} if keys[i].get("dob") else set()) for i in self.parent}
        self.sfx = {i: ({keys[i]["suffix"]} if keys[i].get("suffix") else set()) for i in self.parent}
        self.source_sets = {i: ({self.sources[i]} if i in self.sources else set()) for i in self.parent}
        self.cannot: dict[str, set[str]] = defaultdict(set)
        for a, b in cannot:
            self.cannot[a].add(b)
            self.cannot[b].add(a)

    def find(self, x: str) -> str:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def conflict(self, a: str, b: str) -> str | None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return None
        if len(self.dobs[ra] | self.dobs[rb]) > 1:
            return "birth_date"
        if len(self.sfx[ra] | self.sfx[rb]) > 1:
            return "name_suffix"
        small, big = (ra, rb) if len(self.members[ra]) <= len(self.members[rb]) else (rb, ra)
        for m in self.members[small]:
            if self.cannot.get(m, set()) & self.members[big]:
                return "cannot_link"
        if self._ambiguous_locality_phone(a, b):
            return "ambiguous_locality_phone"
        left, right = self.members[ra], self.members[rb]
        if (self.source_guard and self.source_sets[ra] & self.source_sets[rb]
                and not self._components_share_strong_identifier(left, right)):
            return "source_overlap_identity"
        if self.contact_guard and self._conflicting_contact_footprints(left, right):
            return "contact_footprint_identity"
        return None

    def _ambiguous_locality_phone(self, a: str, b: str) -> bool:
        """Do not let phone alone bridge an ambiguous same-name postal cohort.

        Exact name + postal code was independently marked ambiguous (for
        example by mixed suffix or DOB observations), the records agree on
        city, and there is no unambiguous shared email or DOB. In this narrow
        case a matching phone can be a household line rather than a person ID.
        """
        ka, kb = self.keys[a], self.keys[b]
        fn, ln, postal = ka.get("fn"), ka.get("ln"), ka.get("postal")
        if not (fn and ln and postal and ka.get("city") and ka.get("city") == kb.get("city")
                and fn == kb.get("fn") and ln == kb.get("ln") and postal == kb.get("postal")):
            return False
        if f"np:{fn}|{ln}|{postal}" not in self.ambiguous:
            return False
        if ka.get("dob") and ka.get("dob") == kb.get("dob"):
            return False
        common_phones = set(ka.get("phones", ())) & set(kb.get("phones", ()))
        if not any(f"p:{phone}" not in self.ambiguous for phone in common_phones):
            return False
        common_emails = set(ka.get("email_keys", ())) & set(kb.get("email_keys", ()))
        if any(f"e:{email}" not in self.ambiguous for email in common_emails):
            return False
        return True

    def _components_share_strong_identifier(self, left: set[str], right: set[str]) -> bool:
        """Same-source rows need a shared unambiguous contact or DOB before joining."""
        for a in left:
            ka = self.keys[a]
            for b in right:
                kb = self.keys[b]
                if ka.get("dob") and ka.get("dob") == kb.get("dob"):
                    return True
                for field, prefix in (("email_keys", "e"), ("phones", "p")):
                    common = set(ka.get(field, ())) & set(kb.get(field, ()))
                    if any(f"{prefix}:{value}" not in self.ambiguous for value in common):
                        return True
        return False

    def _conflicting_contact_footprints(self, left: set[str], right: set[str]) -> bool:
        """Review same-name/address links with disjoint personal email footprints."""
        for a in left:
            ka = self.keys[a]
            if not (ka.get("fn") and ka.get("ln") and ka.get("postal") and ka.get("addr")
                    and ka.get("email_keys")):
                continue
            for b in right:
                kb = self.keys[b]
                if not (ka["fn"] == kb.get("fn") and ka["ln"] == kb.get("ln")
                        and ka["postal"] == kb.get("postal") and ka["addr"] == kb.get("addr")
                        and kb.get("email_keys")):
                    continue
                if ka.get("dob") and ka.get("dob") == kb.get("dob"):
                    continue
                left_emails = {e for e in ka["email_keys"] if f"e:{e}" not in self.ambiguous
                               and e.rpartition("@")[2] in PERSONAL_EMAIL_DOMAINS}
                right_emails = {e for e in kb["email_keys"] if f"e:{e}" not in self.ambiguous
                                and e.rpartition("@")[2] in PERSONAL_EMAIL_DOMAINS}
                if left_emails and right_emails and left_emails.isdisjoint(right_emails):
                    return True
        return False

    def union(self, a: str, b: str, force: bool = False) -> str | None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return None
        why = None if force else self.conflict(a, b)
        if why:
            return why
        if len(self.members[ra]) < len(self.members[rb]) or (len(self.members[ra]) == len(self.members[rb]) and rb < ra):
            ra, rb = rb, ra
        self.parent[rb] = ra
        self.members[ra] |= self.members.pop(rb)
        self.dobs[ra] |= self.dobs.pop(rb)
        self.sfx[ra] |= self.sfx.pop(rb)
        self.source_sets[ra] |= self.source_sets.pop(rb)
        return None

    def clusters(self) -> list[list[str]]:
        return sorted((sorted(m) for m in self.members.values()), key=lambda m: m[0])


# ---------------------------------------------------------------- survivorship

def survive(members: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    """members: [{id, source_id, trust, observed_at, data}] -> (golden, lineage).

    Rules (documented, deterministic, tie-broken by record id):
      names/suffix : highest source trust, then most recent
      birth_date   : most frequent value, then trust
      emails/phones: union; primary = most recent, then trust
      address      : whole block from the most recent record that has a street line (never mixes fields)
      company/title: most recent
      attributes   : per key, most recent wins
    """
    by_trust = sorted(members, key=lambda m: (-m["trust"], _neg_ts(m["observed_at"]), m["id"]))
    by_recent = sorted(members, key=lambda m: (_neg_ts(m["observed_at"]), -m["trust"], m["id"]))
    g: dict[str, Any] = {}
    lin: dict[str, Any] = {}

    def pick(field: str, order: list[dict[str, Any]], rule: str):
        for m in order:
            v = m["data"].get(field)
            if v:
                g[field] = v
                lin[field] = {"record": m["id"], "source": m["source_id"], "rule": rule}
                return

    for f in ("first_name", "last_name", "name_suffix"):
        pick(f, by_trust, "highest_trust_then_recent")
    counts: dict[str, list] = defaultdict(list)
    for m in by_trust:
        if m["data"].get("birth_date"):
            counts[m["data"]["birth_date"]].append(m)
    if counts:
        best = sorted(counts.items(), key=lambda kv: (-len(kv[1]), by_trust.index(kv[1][0])))[0]
        g["birth_date"] = best[0]
        lin["birth_date"] = {"record": best[1][0]["id"], "source": best[1][0]["source_id"], "rule": "most_frequent_then_trust"}
    for f, plural in (("email", "emails"), ("phone", "phones")):
        vals: list[str] = []
        for m in by_recent:
            for v in m["data"].get(plural, []):
                if v not in vals:
                    vals.append(v)
        if vals:
            g[plural] = vals
            g[f"primary_{f}"] = vals[0]
            src = next(m for m in by_recent if vals[0] in m["data"].get(plural, []))
            lin[f"primary_{f}"] = {"record": src["id"], "source": src["source_id"], "rule": "most_recent_then_trust"}
    for m in by_recent:
        if m["data"].get("address", {}).get("line1"):
            g["address"] = m["data"]["address"]
            lin["address"] = {"record": m["id"], "source": m["source_id"], "rule": "most_recent_complete_block"}
            break
    else:
        for m in by_recent:
            if m["data"].get("address"):
                g["address"] = m["data"]["address"]
                lin["address"] = {"record": m["id"], "source": m["source_id"], "rule": "most_recent_partial_block"}
                break
    for f in ("company", "job_title"):
        pick(f, by_recent, "most_recent")
    attrs: dict[str, Any] = {}
    for m in reversed(by_recent):
        attrs.update(m["data"].get("attributes", {}))
    if attrs:
        g["attributes"] = dict(sorted(attrs.items()))
    g["sources"] = sorted({m["source_id"] for m in members})
    return g, lin


def _neg_ts(ts: str | None) -> str:
    # Sort key: most recent first. Invert characters so lexical sort is descending.
    ts = ts or "0000"
    return "".join(chr(0x10FFFF - ord(ch)) for ch in ts)


def resolve_consent(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Per purpose: latest observation wins; ties resolve to *denied*. Absent -> not granted."""
    out: dict[str, Any] = {}
    for e in sorted(events, key=lambda e: (e["purpose"], e["observed_at"], e["granted"])):
        # ascending by time, and within a timestamp False(0) sorts before True(1) -> so we must
        # explicitly prefer False on ties:
        cur = out.get(e["purpose"])
        if cur is None or e["observed_at"] > cur["observed_at"]:
            out[e["purpose"]] = {"granted": e["granted"], "observed_at": e["observed_at"], "source": e["source_id"]}
        elif e["observed_at"] == cur["observed_at"] and not e["granted"]:
            out[e["purpose"]] = {"granted": False, "observed_at": e["observed_at"], "source": e["source_id"]}
    return out
