from acdp import gates as G
from acdp import ledger
from acdp import normalize as N
from acdp.models import LedgerEvent, make_session_factory


def test_email_key_collapses_gmail_variants():
    assert N.email_key(N.email("J.Doe+promo@GoogleMail.com")) == "jdoe@gmail.com"
    assert N.email_key("j.doe@yahoo.com") == "j.doe@yahoo.com"  # dots matter outside gmail
    assert N.email("not an email") is None


def test_phone_e164():
    for v in ["(727) 555-1234", "727.555.1234", "+1 727 555 1234", "17275551234", "727-555-1234 ext 9"]:
        assert N.phone(v) == "+17275551234"
    assert N.phone("555-1234") is None
    assert N.phone("+44 20 7946 0958") == "+442079460958"


def test_names_and_dates():
    assert N.split_full_name("Smith, Robert Jr") == ("Robert", "Smith", "jr")
    assert N.split_full_name("Dr. Mary Ann Lee") == ("Mary", "Lee", None)
    assert N.first_name_key("Bob") == N.first_name_key("Robert") == "robert"
    assert N.date("03/02/1980") == "1980-03-02"
    assert N.timestamp("2025-03-01T12:00:00.000+0000") == "2025-03-01T12:00:00Z"
    assert N.timestamp("1700000000") == "2023-11-14T22:13:20Z"
    assert N.postal("33755-1234") == "33755" and N.postal("2134") == "02134"


def test_timestamp_rejects_numeric_ids_and_unsupported_ranges():
    assert N.timestamp("63745491631") is None
    assert N.timestamp("1700000000", allow_unix=False) is None
    assert N.timestamp("1700000000") == "2023-11-14T22:13:20Z"


FACTS = {"e": {"non_null": 10, "valid_rate": {"email": 1.0}}, "n": {"non_null": 10, "valid_rate": {"full_name": 1.0}},
         "d": {"non_null": 10, "valid_rate": {"birth_date": 0.2}}}


def _failed(results):
    return {r.gate for r in results if not r.passed}


def test_mapping_gates():
    ok = {"fields": {"e": {"target": "email", "transform": "default", "confidence": 0.9},
                     "n": {"target": "full_name", "transform": "split_full_name", "confidence": 0.9}}}
    assert G.verdict(G.mapping_gates(ok, FACTS, None, 0.9)) == "auto_applied"
    invented = {"fields": {"e": {"target": "ssn", "transform": "default", "confidence": 1}}}
    assert "M1.vocabulary" in _failed(G.mapping_gates(invented, FACTS, None, 0.9))
    dup = {"fields": {"e": {"target": "birth_date", "transform": "default", "confidence": 1},
                      "d": {"target": "birth_date", "transform": "default", "confidence": 1}}}
    f = _failed(G.mapping_gates(dup, FACTS, None, 0.9))
    assert {"M2.single_valued_targets", "M3.value_validation", "M4.identity_coverage"} <= f
    low = {"fields": {"e": {"target": "email", "transform": "default", "confidence": 0.5}}}
    assert G.verdict(G.mapping_gates(low, FACTS, None, 0.9)) == "queued"
    disagree = G.mapping_gates(ok, FACTS, {"fields": {"n": {"target": "company"}}}, 0.9)
    assert "M6.baseline_consensus" in _failed(disagree)


COMP = {"email": {"status": "disagree"}, "phone": {"status": "shared"}, "first_name": {"status": "agree"},
        "last_name": {"status": "agree"}, "birth_date": {"status": "missing"}, "address": {"status": "agree"},
        "postal_code": {"status": "agree"}, "name_frequency": {"status": "common"}}


def test_adjudication_gates_block_fabrication_and_insufficiency():
    fab = {"decision": "match", "confidence": 1.0, "evidence": [{"field": "email", "status": "agree"}]}
    assert "A2.evidence_truthful" in _failed(G.adjudication_gates(fab, COMP, False, 0.85))
    # Exact name + street + postal, without a double contact conflict, is
    # sufficient locality evidence.
    ok = {"decision": "match", "confidence": 0.9, "evidence": [
        {"field": "first_name", "status": "agree"}, {"field": "last_name", "status": "agree"},
        {"field": "address", "status": "agree"}, {"field": "postal_code", "status": "agree"}]}
    assert G.verdict(G.adjudication_gates(ok, COMP, False, 0.85)) == "auto_applied"
    conflict = dict(COMP, phone={"status": "disagree"})
    assert "A4.evidence_sufficiency" in _failed(G.adjudication_gates(ok, conflict, False, 0.85))
    names_only = {"decision": "match", "confidence": 0.99, "evidence": [
        {"field": "first_name", "status": "agree"}, {"field": "last_name", "status": "agree"}]}
    assert "A4.evidence_sufficiency" in _failed(G.adjudication_gates(names_only, COMP, False, 0.85))
    assert "A3.hard_constraints" in _failed(G.adjudication_gates(ok, COMP, True, 0.85))
    abstain = {"decision": "abstain", "confidence": 0.5, "evidence": []}
    assert G.verdict(G.adjudication_gates(abstain, COMP, False, 0.85)) == "queued"
    garbage = {"decision": "yes"}
    assert G.verdict(G.adjudication_gates(garbage, COMP, False, 0.85)) == "rejected"


def test_ledger_chain_and_tamper(tmp_path):
    sf = make_session_factory(f"sqlite:///{tmp_path / 'l.db'}")
    with sf() as s:
        for i in range(5):
            ledger.append(s, "t", "me", {"i": i})
        s.commit()
        assert ledger.verify(s)["ok"]
        ev = s.get(LedgerEvent, 3)
        ev.payload = '{"i":99}'
        s.commit()
        v = ledger.verify(s)
        assert not v["ok"] and "seq 3" in v["error"]
