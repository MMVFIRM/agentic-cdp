import pytest
from sqlalchemy import select

from acdp.config import ResolutionPolicy, Settings
from acdp.engine import Engine, EngineError
from acdp.models import Profile, ProfileMember, Proposal, make_session_factory

from conftest import SourceRow, sf_row, shop_row


def profiles(engine):
    with engine.sf() as s:
        out = {}
        for m in s.execute(select(ProfileMember)).scalars():
            out.setdefault(m.profile_id, set()).add(m.record_id)
        return sorted(sorted(v) for v in out.values())


def setup(engine):
    engine.register_source("crm", "memory", trust_rank=80)
    engine.register_source("shop", "memory", trust_rank=50)


def test_nickname_gmail_variant_merge_and_survivorship(engine):
    setup(engine)
    engine.ingest("crm", rows=[sf_row("1", "Robert", "Smith", "bob.smith@gmail.com", "727-555-1234", "1980-02-03",
                                      "12 Main St", "33755", optout=False)])
    engine.ingest("shop", rows=[shop_row("a", "Bob", "Smith", "bobsmith+x@gmail.com", zip_="33755",
                                         street="12 Main Street", consent="subscribed")])
    r = engine.resolve()
    assert r["status"] == "published"
    assert profiles(engine) == [["crm:1", "shop:a"]]
    with engine.sf() as s:
        p = s.execute(select(Profile)).scalar_one()
        assert p.data["first_name"] == "Robert"  # crm is more trusted
        assert p.lineage["first_name"]["source"] == "crm"
        assert p.consent["email_marketing"]["granted"] is True  # shop's later grant wins


def test_household_and_jr_sr_not_merged(engine):
    setup(engine)
    engine.ingest("crm", rows=[
        sf_row("1", "Robert", "Smith", "rsmith@x.com", "727-555-1234", "1950-01-01", "12 Main St", "33755", suffix="Sr"),
        sf_row("2", "Robert", "Smith", "robjr@x.com", "727-555-1234", "1985-01-01", "12 Main St", "33755", suffix="Jr"),
        sf_row("3", "Alice", "Smith", "family@x.com", "727-555-1234", None, "12 Main St", "33755"),
        sf_row("4", "Carol", "Smith", "family@x.com", "727-555-1234", None, "12 Main St", "33755"),
    ])
    engine.ingest("shop", rows=[shop_row("a", "Robert", "Smith", phone="727-555-1234", zip_="33755", street="12 Main St")])
    engine.resolve()
    groups = profiles(engine)
    for g in groups:
        assert not ({"crm:1", "crm:2"} <= set(g))
        assert not ({"crm:3", "crm:4"} <= set(g))
    # the un-suffixed shop "Robert Smith" at a Jr/Sr address, sharing only the household phone, is ambiguous
    assert ["shop:a"] in groups


def test_transitive_overmerge_blocked_and_queued(engine):
    setup(engine)
    engine.ingest("crm", rows=[
        sf_row("1", "Pat", "Lee", "pat@x.com", None, "1970-01-01"),
        sf_row("2", "Pat", "Lee", "pat@x.com", "727-555-9999", None),       # bridges 1 and 3
        sf_row("3", "Pat", "Lee", None, "727-555-9999", "1990-05-05"),
    ])
    r = engine.resolve()
    assert r["refused_unions"] >= 1
    for g in profiles(engine):
        assert not ({"crm:1", "crm:3"} <= set(g))
    with engine.sf() as s:
        assert s.execute(select(Proposal).where(Proposal.kind == "cluster_conflict")).first()


def test_replay_is_hermetic_and_ledger_verifies(engine):
    setup(engine)
    engine.ingest("crm", rows=[sf_row(str(i), "Ann", f"Ng{i % 3}", f"a{i}@x.com", None, None, "1 Rd", "33755")
                               for i in range(9)])
    engine.resolve()
    rep = engine.replay()
    assert rep["ok"], rep


def test_review_flow_must_link(engine):
    setup(engine)
    engine.ingest("crm", rows=[sf_row("1", "Zed", "Quill", "z1@x.com", None, None, None, "33755")])
    engine.ingest("shop", rows=[shop_row("a", "Zed", "Quill", "z2@y.com", zip_="33756")])
    engine.resolve()
    assert len(profiles(engine)) == 2
    with engine.sf() as s:
        q = s.execute(select(Proposal).where(Proposal.outcome == "queued")).scalars().all()
    if not q:  # not a candidate pair at all -> steward links directly
        engine.add_constraint("must_link", "crm:1", "shop:a", "steward:alice", "verified by phone call")
    else:
        engine.review(q[0].id, True, "steward:alice", "verified")
    engine.resolve()
    assert profiles(engine) == [["crm:1", "shop:a"]]


def test_profile_alias_after_merge(engine):
    setup(engine)
    engine.ingest("crm", rows=[sf_row("1", "Ivy", "Stone", "ivy@x.com")])
    engine.ingest("shop", rows=[shop_row("a", "Ivy", "Stone", "ivy.s@y.com")])
    engine.resolve()
    ids_before = {g[0]: pid for pid, g in _pid_map(engine).items()}
    engine.ingest("shop", rows=[shop_row("b", "Ivy", "Stone", "ivy@x.com", phone="727-555-0001"),
                                shop_row("a", "Ivy", "Stone", "ivy.s@y.com", phone="727-555-0001")])
    engine.resolve()
    assert len(profiles(engine)) == 1
    for pid in ids_before.values():
        p = engine.get_profile(pid)
        assert p and p["id"] in _pid_map(engine)


def _pid_map(engine):
    with engine.sf() as s:
        out = {}
        for m in s.execute(select(ProfileMember)).scalars():
            out.setdefault(m.profile_id, []).append(m.record_id)
        return {k: sorted(v) for k, v in out.items()}


def test_erasure_suppression_and_ambiguity_guard(engine):
    setup(engine)
    engine.ingest("crm", rows=[sf_row("1", "Mo", "Diaz", "mo@x.com", "727-555-2222"),
                               sf_row("2", "Al", "Diaz", "fam@x.com"), sf_row("3", "Bo", "Diaz", "fam@x.com")])
    engine.resolve()
    with pytest.raises(EngineError):
        engine.erase("dpo", "DSR-1", email="fam@x.com")  # shared inbox -> must use profile id
    res = engine.erase("dpo", "DSR-2", email="MO@x.com")
    assert res["erased_records"] == 1
    out = engine.ingest("crm", rows=[sf_row("9", "Mo", "Diaz", "mo@x.com")])  # lagging source re-sends
    assert out.get("suppressed") == 1
    engine.resolve()
    with engine.sf() as s:
        for p in s.execute(select(Profile)).scalars():
            assert "mo@x.com" not in str(p.data)
    assert engine.replay()["ok"]


def test_consent_denial_propagates_but_grant_does_not(engine):
    setup(engine)
    # shared placeholder email: one person opted in, another opted out later
    engine.ingest("crm", rows=[sf_row("1", "Kim", "Ro", "noemail@store.com", "727-555-3333", "1990-01-01",
                                      optout=False, ts="2025-01-01T00:00:00.000+0000"),
                               sf_row("2", "Lee", "Park", "noemail@store.com", "727-555-4444", "1991-01-01",
                                      optout=True, ts="2025-06-01T00:00:00.000+0000"),
                               sf_row("3", "Joy", "Park", "noemail@store.com", "727-555-5555", "1992-01-01",
                                      optout=None)])
    engine.resolve()
    exported = engine.export("email_marketing")
    assert exported == []  # Kim's earlier grant is overridden by the later denial on the same address
    with engine.sf() as s:
        joy = [p for p in s.execute(select(Profile)).scalars() if p.data.get("first_name") == "Joy"][0]
        assert joy.consent.get("email_marketing", {}).get("granted") is not True  # Kim's grant didn't leak


def test_consent_event_only_on_value_change(engine):
    setup(engine)
    engine.ingest("crm", rows=[sf_row("1", "Tia", "Moss", "t@x.com", optout=True, ts="2025-01-01T00:00:00.000+0000")])
    engine.ingest("shop", rows=[shop_row("a", "Tia", "Moss", "t@x.com", consent="subscribed", ts="2025-06-01T00:00:00Z")])
    # crm record touched later for unrelated reasons; still opted out, but that is not a new denial
    engine.ingest("crm", rows=[sf_row("1", "Tia", "Moss", "t@x.com", optout=True, ts="2026-01-01T00:00:00.000+0000")])
    engine.resolve()
    assert [r["first_name"] for r in engine.export("email_marketing")] == ["Tia"]


def test_unlinked_same_name_denial_holds_marketing_export_until_newer_grant(engine):
    setup(engine)
    engine.ingest("crm", rows=[sf_row("deny", "Jane", "Doe", "deny@x.com", optout=True,
                                       ts="2025-08-01T00:00:00.000+0000", zip_="33755")])
    engine.ingest("shop", rows=[shop_row("grant", "Jane", "Doe", "grant@y.com", consent="subscribed",
                                          ts="2025-06-01T00:00:00Z", zip_="98101")])
    engine.resolve()
    assert len(profiles(engine)) == 2
    assert engine.export("email_marketing") == []

    engine.ingest("shop", rows=[
        shop_row("grant", "Jane", "Doe", "grant@y.com", consent="subscribed",
                 ts="2025-06-01T00:00:00Z", zip_="98101"),
        shop_row("grant-new", "Jane", "Doe", "grant@y.com", consent="subscribed",
                 ts="2025-09-01T00:00:00Z", zip_="98101"),
    ])
    engine.resolve()
    assert [r["primary_email"] for r in engine.export("email_marketing")] == ["grant@y.com"]


def test_conflicting_consent_on_disjoint_phones_holds_until_denial_record_is_updated(engine):
    setup(engine)
    engine.ingest("crm", rows=[sf_row(
        "deny", "Taylor", "Ng", "taylor@x.com", "727-555-2001", "1988-02-03",
        optout=True, ts="2025-01-01T00:00:00.000+0000",
    )])
    engine.ingest("shop", rows=[shop_row(
        "grant", "Taylor", "Ng", "taylor@x.com", phone="727-555-2002", consent="subscribed",
        ts="2025-06-01T00:00:00Z",
    )])
    engine.resolve()
    assert profiles(engine) == [["crm:deny", "shop:grant"]]
    assert engine.export("email_marketing") == []

    engine.ingest("crm", rows=[sf_row(
        "deny", "Taylor", "Ng", "taylor@x.com", "727-555-2001", "1988-02-03",
        optout=False, ts="2025-09-01T00:00:00.000+0000",
    )])
    engine.resolve()
    assert [r["primary_email"] for r in engine.export("email_marketing")] == ["taylor@x.com"]


def test_unlinked_denial_at_same_address_holds_export_without_merging_profiles(engine):
    setup(engine)
    engine.ingest("crm", rows=[sf_row(
        "grant", "Karen", "Rodriguez", "karen@x.com", "727-555-1001", "1970-09-02",
        "5980 Maple Lane", "33733", optout=False, ts="2024-12-10T04:53:22.000+0000",
    )])
    engine.ingest("shop", rows=[shop_row(
        "deny", "Karen", "Chen", "karen.chen@y.com", zip_="33733", street="5980 Maple Lane",
        consent="unsubscribed", ts="2025-05-21T01:27:47Z",
    )])
    engine.resolve()
    assert len(profiles(engine)) == 2
    assert engine.export("email_marketing") == []


def test_circuit_breaker_halts(settings):
    settings.policy = ResolutionPolicy(max_cluster_size=3)
    e = Engine(make_session_factory(settings.database_url), settings)
    e.register_source("crm", "memory")
    e.ingest("crm", rows=[sf_row(str(i), "Sam", "Ode", "sam@x.com", None, None) for i in range(5)])
    r = e.resolve()
    assert r["status"] == "halted" and "max_cluster_size" in r["breakers"][0]
    with e.sf() as s:
        assert s.execute(select(Profile)).first() is None
    assert e.resolve(force=True)["status"] == "published"


def test_schema_drift_triggers_remap_and_quarantine(engine):
    engine.register_source("crm", "memory")
    engine.ingest("crm", rows=[sf_row(str(i), "A", "B", f"a{i}@x.com") for i in range(3)])
    drifted = [SourceRow(str(i), {"Id": str(i), "EmailAddr": f"a{i}@x.com", "First": "A", "Last": "B"}) for i in range(3)]
    out = engine.ingest("crm", rows=drifted)
    assert out["status"] == "ok"
    with engine.sf() as s:
        assert len(s.execute(select(Proposal).where(Proposal.kind == "schema_mapping")).all()) == 2
    # same header set but garbage values in an identity column -> batch quarantined
    garbage = [SourceRow(str(i), {"Id": str(i), "EmailAddr": "n/a-" + str(i), "First": "A", "Last": "B"}) for i in range(30)]
    out = engine.ingest("crm", rows=garbage)
    assert out["status"] == "quarantined"


def test_steward_mapping_is_gated(engine):
    engine.register_source("crm", "memory")
    engine.ingest("crm", rows=[sf_row(str(i), "A", "B", f"a{i}@x.com") for i in range(5)])
    bad = {"fields": {"FirstName": {"target": "email"}}}
    assert engine.set_mapping("crm", bad, "steward:bob")["applied"] is False
    forced = engine.set_mapping("crm", {"fields": {"Email": {"target": "email"}, "FirstName": {"target": "phone"}}},
                                "admin:root", force=True)
    assert forced["applied"] and forced["forced"]


def test_secrets_must_be_env_refs(engine):
    with pytest.raises(EngineError):
        engine.register_source("hs", "hubspot", {"token": "pat-na1-abc"})


def test_mapping_timestamp_facts_use_field_names(engine):
    facts = engine.field_facts([SourceRow("1", {
        "id": 63_745_491_631,
        "created": 1_700_000_000,
        "Phone": "2067259534",
    })])
    assert facts["id"]["valid_rate"]["created_at"] == 0.0
    assert facts["created"]["valid_rate"]["created_at"] == 1.0
    assert facts["Phone"]["valid_rate"]["created_at"] == 0.0


def test_fail_closed_without_hmac_key(tmp_path):
    s = Settings(database_url=f"sqlite:///{tmp_path / 'x.db'}", pii_hmac_key=None, allow_insecure_dev=False)
    with pytest.raises(RuntimeError):
        Engine(make_session_factory(s.database_url), s)
