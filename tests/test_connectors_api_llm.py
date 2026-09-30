import hashlib
import hmac
import json
import os

import httpx
import pytest
from fastapi.testclient import TestClient

from acdp.agents import AnthropicAgent, OpenAICompatibleAgent
from acdp.agents.base import AgentError
from acdp.api import create_app
from acdp.config import Settings
from acdp.connectors.base import ConnectorError
from acdp.connectors.saas import HubSpotConnector, SalesforceConnector, ShopifyConnector, StripeConnector
from acdp.engine import Engine
from acdp.models import make_session_factory
from bench.generate import build_sources, build_universe
from bench.mocks import VendorMock

TOK = {"salesforce": "t1", "hubspot": "t2", "shopify": "t3", "stripe": "t4"}


@pytest.fixture(scope="module")
def data():
    people = build_universe(700, 3)
    d = build_sources(people, 5)
    d.pop("_consent_truth")
    return d


@pytest.fixture(autouse=True)
def tokens(monkeypatch):
    for k, v in TOK.items():
        monkeypatch.setenv(f"T_{k.upper()}", v)


def client(mock):
    return httpx.Client(transport=mock.transport())


@pytest.mark.parametrize("cls,cfg,vendor", [
    (SalesforceConnector, {"instance_url": "https://acme.my.salesforce.com", "token_env": "T_SALESFORCE"}, "salesforce"),
    (HubSpotConnector, {"token_env": "T_HUBSPOT"}, "hubspot"),
    (ShopifyConnector, {"shop": "acme", "token_env": "T_SHOPIFY"}, "shopify"),
    (StripeConnector, {"token_env": "T_STRIPE"}, "stripe"),
])
def test_saas_full_sync_paginates_and_increments(data, cls, cfg, vendor):
    mock = VendorMock(data, TOK)
    mock.fail_next = 1  # one 429 must be retried transparently
    c = cls(vendor, cfg, client=client(mock), sleep=lambda s: None)
    rows = list(c.read(None))
    assert len(rows) == len(data[vendor]) and len({r.key for r in rows}) == len(rows)
    assert len(mock.calls) > 2  # paginated
    cur = c.next_cursor
    assert cur
    c2 = cls(vendor, cfg, client=client(mock), sleep=lambda s: None)
    assert list(c2.read(cur)) == []  # nothing newer than the cursor


def test_egress_allowlist_and_auth_errors(data, monkeypatch):
    mock = VendorMock(data, TOK)
    bad = SalesforceConnector("sf", {"instance_url": "https://evil.example.com", "token_env": "T_SALESFORCE"},
                              client=client(mock))
    with pytest.raises(ConnectorError, match="allowlist"):
        list(bad.read(None))
    monkeypatch.setenv("T_HUBSPOT", "wrong")
    with pytest.raises(ConnectorError, match="401"):
        list(HubSpotConnector("hs", {"token_env": "T_HUBSPOT"}, client=client(mock)).read(None))
    with pytest.raises(ConnectorError, match="not set"):
        list(StripeConnector("st", {"token_env": "T_MISSING"}, client=client(mock)).read(None))


def _app(tmp_path, keys="admin-key-000000000000:admin,steward-key-0000000000:steward,viewer-key-00000000000:viewer"):
    st = Settings(database_url=f"sqlite:///{tmp_path / 'api.db'}", pii_hmac_key="k", api_keys=keys,
                  segment_secret="seg-secret")
    return TestClient(create_app(Engine(make_session_factory(st.database_url), st), st))


def test_api_fails_closed_without_keys(tmp_path):
    c = _app(tmp_path, keys=None)
    assert c.get("/stats").status_code == 503
    assert c.get("/health").status_code == 200


def test_api_roles_and_segment_webhook(tmp_path):
    c = _app(tmp_path)
    A, S, V = ({"x-api-key": k} for k in ("admin-key-000000000000", "steward-key-0000000000", "viewer-key-00000000000"))
    assert c.get("/stats").status_code == 401
    assert c.post("/sources", json={"id": "seg", "kind": "segment"}, headers=V).status_code == 403
    assert c.post("/sources", json={"id": "seg", "kind": "segment"}, headers=A).status_code == 200
    events = {"batch": [{"type": "identify", "userId": f"u{i}", "timestamp": "2026-01-01T00:00:00Z",
                         "traits": {"email": f"p{i % 5}@x.com", "firstName": "Pat", "lastName": f"L{i % 5}"}}
                        for i in range(10)] + [{"type": "track", "event": "ignored"}]}
    body = json.dumps(events).encode()
    assert c.post("/ingest/segment/seg", content=body, headers={"x-signature": "bad"}).status_code == 401
    sig = hmac.new(b"seg-secret", body, hashlib.sha1).hexdigest()
    r = c.post("/ingest/segment/seg", content=body, headers={"x-signature": sig})
    assert r.status_code == 200 and r.json()["new"] == 10
    assert c.post("/runs", json={"ingest": False}, headers=V).status_code == 403
    assert c.post("/runs", json={"ingest": False, "force": True}, headers=S).status_code == 403
    r = c.post("/runs", json={"ingest": False}, headers=S).json()
    assert r["status"] == "published" and r["clusters"] == 5
    prof = c.get("/profiles", headers=V).json()
    assert prof["total"] == 5
    pid = prof["items"][0]["id"]
    assert len(c.get(f"/profiles/{pid}", headers=V).json()["records"]) == 2
    assert c.post("/privacy/erasure", json={"profile_id": pid, "reason": "t"}, headers=S).status_code == 403
    assert c.post("/privacy/erasure", json={"profile_id": pid, "reason": "t"}, headers=A).json()["erased_records"] == 2
    assert c.get(f"/profiles/{pid}", headers=V).status_code == 410
    assert c.get("/audit/verify", headers=V).json()["ok"]
    assert c.get("/audit/replay", headers=S).json()["missing_decisions"] == 0
    assert "Agentic CDP" in c.get("/").text


def _mock_llm(reply: str, openai=False):
    def h(req):
        if openai:
            return httpx.Response(200, json={"choices": [{"message": {"content": reply}}]})
        return httpx.Response(200, json={"content": [{"type": "text", "text": reply}]})
    return httpx.Client(transport=httpx.MockTransport(h))


def test_llm_agents_parse_and_fail_safely():
    st = Settings(llm_model="test-model")
    a = AnthropicAgent(st, client=_mock_llm('Sure! {"decision":"no_match","confidence":0.9,"evidence":[],"rationale":"x"}'))
    assert a.adjudicate({"comparisons": {}})["decision"] == "no_match"
    o = OpenAICompatibleAgent(st, client=_mock_llm('{"fields":{}}', openai=True))
    assert o.propose_mapping({"fields": []}) == {"fields": {}}
    with pytest.raises(AgentError):
        AnthropicAgent(st, client=_mock_llm("I refuse")).adjudicate({})


def test_engine_falls_back_when_llm_misbehaves(tmp_path):
    st = Settings(database_url=f"sqlite:///{tmp_path / 'l.db'}", pii_hmac_key="k", llm_model="m")
    bad = AnthropicAgent(st, client=_mock_llm('{"fields": {"Email": {"target": "social_security", "confidence": 1}}}'))
    e = Engine(make_session_factory(st.database_url), st, agent=bad)
    e.register_source("crm", "memory")
    from conftest import sf_row
    out = e.ingest("crm", rows=[sf_row(str(i), "A", "B", f"a{i}@x.com") for i in range(3)])
    assert out["status"] == "ok"  # hard-gate rejection -> deterministic fallback mapping applied
    from acdp.models import Proposal
    from sqlalchemy import select
    with e.sf() as s:
        outcomes = [(p.agent, p.outcome) for p in s.execute(select(Proposal)).scalars()]
    assert outcomes == [("anthropic/m", "rejected"), ("deterministic/v1(fallback)", "auto_applied")]
