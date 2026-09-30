"""Deterministic gates. Pure functions over (proposal, facts the engine computed).

Severity:
  hard -> proposal rejected (engine may fall back to the deterministic agent)
  soft -> proposal queued for a human steward
All pass -> auto-applied.

The same gates bind humans: a steward-edited mapping must pass the hard gates
unless an admin forces it, and the force is itself ledgered.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .canonical import IDENTITY_TARGETS, MULTI_TARGETS, TARGETS, TRANSFORMS

STRONG = {"email", "phone", "birth_date"}
STATUSES = {"agree", "shared", "partial", "disagree", "missing", "conflict", "rare", "common"}


@dataclass
class GateResult:
    gate: str
    passed: bool
    severity: str  # hard|soft
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def verdict(results: list[GateResult]) -> str:
    if any(not r.passed and r.severity == "hard" for r in results):
        return "rejected"
    if any(not r.passed for r in results):
        return "queued"
    return "auto_applied"


# ---------------------------------------------------------------- schema mapping

def mapping_gates(proposal: dict[str, Any], field_facts: dict[str, dict[str, Any]], baseline: dict[str, Any] | None,
                  min_validity: float, min_conf: float = 0.8) -> list[GateResult]:
    out: list[GateResult] = []
    fields = proposal.get("fields") if isinstance(proposal, dict) else None
    if not isinstance(fields, dict) or not fields:
        return [GateResult("M0.schema", False, "hard", "proposal has no 'fields' object")]

    bad = []
    for name, spec in fields.items():
        if name not in field_facts:
            bad.append(f"{name}: unknown source field")
            continue
        if not isinstance(spec, dict):
            bad.append(f"{name}: spec not an object")
            continue
        t, tr = spec.get("target"), spec.get("transform", "default")
        if t is not None and t not in TARGETS:
            bad.append(f"{name}: target {t!r} not in vocabulary")
        if tr not in TRANSFORMS:
            bad.append(f"{name}: transform {tr!r} not allowed")
        if tr == "split_full_name" and t != "full_name":
            bad.append(f"{name}: split_full_name only valid for full_name")
        if tr == "invert_bool" and not (t or "").startswith("consent."):
            bad.append(f"{name}: invert_bool only valid for consent targets")
        c = spec.get("confidence")
        if not isinstance(c, (int, float)) or not 0 <= c <= 1:
            bad.append(f"{name}: confidence missing or out of range")
    out.append(GateResult("M1.vocabulary", not bad, "hard", "; ".join(bad[:8])))

    seen: dict[str, str] = {}
    dup = []
    for name, spec in fields.items():
        t = spec.get("target") if isinstance(spec, dict) else None
        if t and t not in MULTI_TARGETS and t != "attribute":
            if t in seen:
                dup.append(f"{t}: {seen[t]} & {name}")
            seen[t] = name
    out.append(GateResult("M2.single_valued_targets", not dup, "hard", "; ".join(dup)))

    invalid = []
    for name, spec in fields.items():
        if not isinstance(spec, dict) or name not in field_facts:
            continue
        t = spec.get("target")
        if not t or t == "attribute":
            continue
        facts = field_facts[name]
        if facts.get("non_null", 0) == 0:
            continue
        vr = facts.get("valid_rate", {}).get(t, 0.0)
        if vr < min_validity:
            invalid.append(f"{name}->{t} validity {vr:.2f} < {min_validity}")
    out.append(GateResult("M3.value_validation", not invalid, "hard", "; ".join(invalid[:8])))

    targets = {s.get("target") for s in fields.values() if isinstance(s, dict)}
    has_strong = bool(targets & {"email", "phone"})
    has_name = "full_name" in targets or {"first_name", "last_name"} <= targets
    has_locality = bool(targets & {"postal_code", "birth_date", "address_line1"})
    ok = has_strong or (has_name and has_locality)
    out.append(GateResult("M4.identity_coverage", ok, "hard",
                          "" if ok else "mapping yields no resolvable identity key (need email/phone or name+locality)"))

    low = [f"{n} ({s.get('confidence')})" for n, s in fields.items()
           if isinstance(s, dict) and s.get("target") in IDENTITY_TARGETS
           and isinstance(s.get("confidence"), (int, float)) and s["confidence"] < min_conf]
    out.append(GateResult("M5.confidence", not low, "soft", "low-confidence identity fields: " + ", ".join(low) if low else ""))

    if baseline is not None:
        dis = []
        bf = baseline.get("fields", {})
        for name, spec in fields.items():
            if not isinstance(spec, dict):
                continue
            a, b = spec.get("target"), (bf.get(name) or {}).get("target")
            if (a in IDENTITY_TARGETS or b in IDENTITY_TARGETS) and a and b and a != b:
                dis.append(f"{name}: agent={a} baseline={b}")
        out.append(GateResult("M6.baseline_consensus", not dis, "soft", "; ".join(dis[:8])))
    return out


# ---------------------------------------------------------------- match adjudication

def adjudication_gates(proposal: dict[str, Any], comparisons: dict[str, dict[str, Any]], cannot_link: bool,
                       min_conf: float) -> list[GateResult]:
    out: list[GateResult] = []
    if not isinstance(proposal, dict) or proposal.get("decision") not in {"match", "no_match", "abstain"}:
        return [GateResult("A1.schema", False, "hard", "decision missing or invalid")]
    conf = proposal.get("confidence")
    ev = proposal.get("evidence", [])
    if not isinstance(conf, (int, float)) or not 0 <= conf <= 1 or not isinstance(ev, list):
        return [GateResult("A1.schema", False, "hard", "confidence/evidence malformed")]
    out.append(GateResult("A1.schema", True, "hard"))

    fabricated = []
    for e in ev:
        if not isinstance(e, dict):
            fabricated.append(repr(e)[:40])
            continue
        f, s = e.get("field"), e.get("status")
        if f not in comparisons or comparisons[f]["status"] != s:
            fabricated.append(f"{f}={s} (actual {comparisons.get(f, {}).get('status', 'absent')})")
    out.append(GateResult("A2.evidence_truthful", not fabricated, "hard", "; ".join(fabricated[:6])))

    decision = proposal["decision"]
    if decision == "match":
        conflicts = [f for f, c in comparisons.items() if c["status"] == "conflict"]
        hard = conflicts + (["steward cannot_link"] if cannot_link else [])
        out.append(GateResult("A3.hard_constraints", not hard, "hard", ", ".join(hard)))

        cited_agree = {e["field"] for e in ev if isinstance(e, dict) and e.get("status") == "agree"
                       and comparisons.get(e.get("field"), {}).get("status") == "agree"}
        cited_partial = {e["field"] for e in ev if isinstance(e, dict) and e.get("status") in {"agree", "partial"}
                         and comparisons.get(e.get("field"), {}).get("status") == e.get("status")}
        cited_shared_contact = {e["field"] for e in ev if isinstance(e, dict) and e.get("field") in {"email", "phone"}
                                and e.get("status") == "shared"
                                and comparisons.get(e.get("field"), {}).get("status") == "shared"}
        name_ok = {"first_name", "last_name"} <= cited_partial and "first_name" in cited_partial
        fn_disagree = comparisons.get("first_name", {}).get("status") == "disagree"
        strong = bool(cited_agree & STRONG)
        dob_only = (comparisons.get("birth_date", {}).get("status") == "agree"
                    and comparisons.get("email", {}).get("status") != "agree"
                    and comparisons.get("phone", {}).get("status") != "agree")
        exact_name = {"first_name", "last_name"} <= cited_agree
        exact_surname = "last_name" in cited_agree
        contact_conflict = (comparisons.get("email", {}).get("status") == "disagree"
                            and comparisons.get("phone", {}).get("status") == "disagree")
        corroborating_name = (name_ok and bool(cited_agree & {"first_name", "last_name"}))
        postal_city = ({"postal_code", "city"} <= cited_agree
                       and comparisons.get("address", {}).get("status") != "disagree")
        street_locality = ("address" in cited_agree
                           and bool(cited_agree & {"postal_code", "city"})
                           and comparisons.get("postal_code", {}).get("status") != "disagree"
                           and comparisons.get("city", {}).get("status") != "disagree")
        shared_contact_locality = (corroborating_name and comparisons.get("name_suffix", {}).get("status") != "partial"
                                   and (postal_city or street_locality) and bool(cited_shared_contact))
        cited_rare = any(isinstance(e, dict) and e.get("field") == "name_frequency" and e.get("status") == "rare"
                         and comparisons.get("name_frequency", {}).get("status") == "rare" for e in ev)
        locality = exact_name and not contact_conflict and (
            {"address", "postal_code"} <= cited_agree or ("postal_code" in cited_agree and cited_rare))
        sufficient = (not fn_disagree) and not contact_conflict and (
            (name_ok and strong and (not dob_only or exact_surname)) or locality or shared_contact_locality)
        out.append(GateResult("A4.evidence_sufficiency", sufficient, "hard",
                              "" if sufficient else
                              "match requires name + a strong identifier (email/phone/dob; DOB alone requires an exact surname), or exact name + postal + "
                              "(street or a locally-unique name), or near-exact name + corroborated locality + a cited shared contact; given-name disagreement "
                              "is never sufficient"))
    out.append(GateResult("A5.confidence", decision != "abstain" and conf >= min_conf, "soft",
                          f"confidence {conf} < {min_conf}" if conf < min_conf else ("abstained" if decision == "abstain" else "")))
    return out
