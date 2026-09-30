from acdp import gates as G
from acdp.agents.deterministic import DeterministicAgent
from acdp.resolution import ConstrainedClusters, ambiguous_identifiers, band, compare


def test_dob_only_with_partial_name_is_held_for_review():
    comp = {
        "email": {"status": "disagree"},
        "phone": {"status": "missing"},
        "first_name": {"status": "agree"},
        "last_name": {"status": "partial"},
        "birth_date": {"status": "agree"},
        "address": {"status": "disagree"},
        "postal_code": {"status": "disagree"},
        "name_frequency": {"status": "common"},
    }
    assert band(comp, 20, 12, 4) == "gray"
    proposal = DeterministicAgent().adjudicate({"comparisons": comp})
    assert proposal["decision"] == "abstain"
    gates = G.adjudication_gates(
        {"decision": "match", "confidence": 0.99, "evidence": [
            {"field": "first_name", "status": "agree"},
            {"field": "last_name", "status": "partial"},
            {"field": "birth_date", "status": "agree"},
        ]}, comp, False, 0.85)
    assert "A4.evidence_sufficiency" in {g.gate for g in gates if not g.passed}


def test_dob_only_with_partial_given_name_and_exact_surname_can_match():
    comp = {
        "email": {"status": "disagree"},
        "phone": {"status": "missing"},
        "first_name": {"status": "partial"},
        "last_name": {"status": "agree"},
        "birth_date": {"status": "agree"},
        "address": {"status": "disagree"},
        "postal_code": {"status": "disagree"},
        "name_frequency": {"status": "common"},
    }
    assert band(comp, 20, 12, 4) == "match"
    assert DeterministicAgent().adjudicate({"comparisons": comp})["decision"] == "match"


def test_exact_name_locality_can_corroborate_a_shared_contact():
    comp = {
        "email": {"status": "shared"},
        "phone": {"status": "missing"},
        "first_name": {"status": "agree"},
        "last_name": {"status": "agree"},
        "birth_date": {"status": "missing"},
        "name_suffix": {"status": "missing"},
        "address": {"status": "missing"},
        "postal_code": {"status": "agree"},
        "city": {"status": "agree"},
        "name_frequency": {"status": "common"},
    }
    proposal = DeterministicAgent().adjudicate({"comparisons": comp})
    assert proposal["decision"] == "match"
    assert {e["field"] for e in proposal["evidence"]} >= {
        "first_name", "last_name", "email", "postal_code", "city"
    }
    gates = G.adjudication_gates(proposal, comp, False, 0.85)
    assert all(g.passed for g in gates)


def test_shared_contact_without_exact_name_and_full_locality_is_insufficient():
    comp = {
        "email": {"status": "shared"},
        "phone": {"status": "missing"},
        "first_name": {"status": "partial"},
        "last_name": {"status": "agree"},
        "birth_date": {"status": "missing"},
        "name_suffix": {"status": "missing"},
        "address": {"status": "missing"},
        "postal_code": {"status": "agree"},
        "city": {"status": "agree"},
        "name_frequency": {"status": "common"},
    }
    proposal = {"decision": "match", "confidence": 0.99, "evidence": [
        {"field": f, "status": v["status"]} for f, v in comp.items()
        if v["status"] in {"agree", "shared"}
    ]}
    gates = G.adjudication_gates(proposal, comp, False, 0.85)
    assert any(g.gate == "A4.evidence_sufficiency" and not g.passed for g in gates)


def test_near_name_requires_shared_contact_and_exact_full_locality():
    comp = {
        "email": {"status": "shared"},
        "phone": {"status": "missing"},
        "first_name": {"status": "partial"},
        "last_name": {"status": "agree"},
        "birth_date": {"status": "missing"},
        "name_suffix": {"status": "missing"},
        "address": {"status": "agree"},
        "postal_code": {"status": "agree"},
        "city": {"status": "agree"},
        "name_frequency": {"status": "common"},
    }
    proposal = DeterministicAgent().adjudicate({"comparisons": comp})
    assert proposal["decision"] == "match"
    gates = G.adjudication_gates(proposal, comp, False, 0.85)
    assert all(g.passed for g in gates)


def test_one_sided_suffix_is_not_silently_treated_as_missing_for_new_rule():
    a = {"email_keys": ["family@example.test"], "phones": [], "fn": "alex", "ln": "smith",
         "dob": None, "suffix": "jr", "postal": "10001", "addr": None, "city": "newyork"}
    b = {"email_keys": ["family@example.test"], "phones": [], "fn": "alex", "ln": "smith",
         "dob": None, "suffix": None, "postal": "10001", "addr": None, "city": "newyork"}
    comp = compare(a, b, {"e:family@example.test"})
    assert comp["name_suffix"]["status"] == "partial"
    proposal = DeterministicAgent().adjudicate({"comparisons": comp})
    assert proposal["decision"] == "abstain"


def _keys(email, *, dob=None):
    return {"email_keys": [email] if email else [], "phones": [], "fn": "alex", "ln": "smith",
            "dob": dob, "suffix": None, "postal": "10001", "addr": "10mainst"}


def test_same_source_records_without_shared_strong_identifier_are_separated():
    keys = {"a": _keys("a@example.test"), "b": _keys("b@example.test")}
    uf = ConstrainedClusters(keys, keys, set(), sources={"a": "crm", "b": "crm"})
    assert uf.union("a", "b") == "source_overlap_identity"


def test_same_source_records_with_shared_dob_can_join():
    keys = {"a": _keys("a@example.test", dob="1980-01-01"),
            "b": _keys("b@example.test", dob="1980-01-01")}
    uf = ConstrainedClusters(keys, keys, set(), sources={"a": "crm", "b": "crm"})
    assert uf.union("a", "b") is None


def test_disjoint_contact_footprints_at_same_name_and_address_are_reviewed():
    keys = {"a": _keys("a@gmail.com"), "b": _keys("b@yahoo.com")}
    uf = ConstrainedClusters(keys, keys, set(), sources={"a": "crm-a", "b": "crm-b"})
    assert uf.union("a", "b") == "contact_footprint_identity"


def test_shared_contact_across_distinct_addresses_with_disjoint_footprints_is_ambiguous():
    common = {"fn": "charles", "ln": "rossi", "dob": None, "suffix": None, "postal": "80249"}
    keys = {
        "a": dict(common, addr="9283washingtonrd", email_keys=["family@example.test"], phones=["+13035550101"]),
        "a2": dict(common, addr="9283washingtonrd", email_keys=["family@example.test"], phones=["+13035550101"]),
        "b": dict(common, addr="4941hillrd", email_keys=["family@example.test"], phones=["+16175550101"]),
    }
    assert "e:family@example.test" in ambiguous_identifiers(keys)


def test_phone_alone_cannot_bridge_ambiguous_name_postal_cohort():
    keys = {
        "a": {"email_keys": ["alex.one@example.test"], "phones": ["+12065550100"],
              "fn": "alex", "ln": "smith", "dob": None, "suffix": None,
              "postal": "10001", "addr": None, "city": "newyork"},
        "b": {"email_keys": ["alex.two@example.test"], "phones": ["+12065550100"],
              "fn": "alex", "ln": "smith", "dob": None, "suffix": None,
              "postal": "10001", "addr": None, "city": "newyork"},
        "suffix_observation": {"email_keys": ["alex.jr@example.test"], "phones": ["+12065550101"],
                              "fn": "alex", "ln": "smith", "dob": None, "suffix": "jr",
                              "postal": "10001", "addr": None, "city": "newyork"},
    }
    amb = ambiguous_identifiers(keys)
    assert "np:alex|smith|10001" in amb
    uf = ConstrainedClusters(keys, keys, set(), sources={k: k for k in keys}, ambiguous=amb)
    assert uf.union("a", "b") == "ambiguous_locality_phone"


def test_ambiguous_locality_phone_guard_yields_to_shared_dob_or_email():
    keys = {
        "a": {"email_keys": ["alex.one@example.test"], "phones": ["+12065550100"],
              "fn": "alex", "ln": "smith", "dob": "1980-01-01", "suffix": None,
              "postal": "10001", "addr": None, "city": "newyork"},
        "b": {"email_keys": ["alex.two@example.test"], "phones": ["+12065550100"],
              "fn": "alex", "ln": "smith", "dob": "1980-01-01", "suffix": None,
              "postal": "10001", "addr": None, "city": "newyork"},
        "suffix_observation": {"email_keys": ["alex.jr@example.test"], "phones": ["+12065550101"],
                              "fn": "alex", "ln": "smith", "dob": None, "suffix": "jr",
                              "postal": "10001", "addr": None, "city": "newyork"},
    }
    amb = ambiguous_identifiers(keys)
    uf = ConstrainedClusters(keys, keys, set(), sources={k: k for k in keys}, ambiguous=amb)
    assert uf.union("a", "b") is None

