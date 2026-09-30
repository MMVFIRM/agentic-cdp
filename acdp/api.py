"""HTTP API + operator console.

Auth: X-API-Key header; keys configured as ACDP_API_KEYS="key:role,...".
Roles: viewer < steward < admin. No keys configured -> every request is refused
(unless ACDP_ALLOW_INSECURE_DEV=1, which grants admin to all and says so in /health).
"""
from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from . import __version__, ledger
from .config import Settings, get_settings
from .connectors import SegmentConnector
from .engine import Engine, EngineError
from .models import (
    CanonicalRecord, LedgerEvent, Profile, ProfileMember, Proposal, Run, SchemaMapping, Source,
    make_session_factory,
)

ROLES = {"viewer": 0, "steward": 1, "admin": 2}


class SourceIn(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9_\-]{1,64}$")
    kind: str
    config: dict[str, Any] = {}
    trust_rank: int = Field(50, ge=0, le=100)


class ReviewIn(BaseModel):
    approve: bool
    note: str = ""


class ConstraintIn(BaseModel):
    kind: str
    a: str
    b: str
    reason: str = ""


class SplitIn(BaseModel):
    record_ids: list[str]
    reason: str = ""


class ErasureIn(BaseModel):
    email: str | None = None
    phone: str | None = None
    profile_id: str | None = None
    reason: str
    allow_multiple: bool = False


class MappingIn(BaseModel):
    mapping: dict[str, Any]
    force: bool = False


class RunIn(BaseModel):
    force: bool = False
    ingest: bool = True


def create_app(engine: Engine | None = None, settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    if engine is None:
        engine = Engine(make_session_factory(settings.database_url), settings)
    sf = engine.sf
    keys: dict[str, str] = {}
    for part in (settings.api_keys or "").split(","):
        if ":" in part:
            k, r = part.rsplit(":", 1)
            if r.strip() in ROLES and len(k.strip()) >= 16:
                keys[hashlib.sha256(k.strip().encode()).hexdigest()] = r.strip()

    app = FastAPI(title="Agentic CDP", version=__version__)
    app.state.engine = engine

    def auth(min_role: str):
        def dep(x_api_key: str | None = Header(default=None)) -> str:
            if not keys:
                if settings.allow_insecure_dev:
                    return "admin:insecure-dev"
                raise HTTPException(503, "no API keys configured (ACDP_API_KEYS); refusing all requests")
            if not x_api_key:
                raise HTTPException(401, "missing X-API-Key")
            h = hashlib.sha256(x_api_key.encode()).hexdigest()
            role = next((r for kh, r in keys.items() if hmac.compare_digest(kh, h)), None)
            if role is None:
                raise HTTPException(401, "invalid API key")
            if ROLES[role] < ROLES[min_role]:
                raise HTTPException(403, f"requires {min_role}")
            return f"{role}:{h[:8]}"
        return dep

    viewer, steward, admin = auth("viewer"), auth("steward"), auth("admin")

    def guard(fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except EngineError as e:
            raise HTTPException(409, str(e))

    @app.get("/health")
    def health():
        with sf() as s:
            chain = ledger.verify(s)
        return {"ok": chain["ok"], "version": __version__, "agent": engine.agent.name,
                "insecure_dev": settings.allow_insecure_dev and not keys, "ledger_events": chain["events"]}

    @app.get("/", response_class=HTMLResponse)
    def console():
        return (Path(__file__).parent / "console" / "index.html").read_text()

    # ---- overview
    @app.get("/stats")
    def stats(actor: str = Depends(viewer)):
        with sf() as s:
            count = lambda m: s.execute(select(func.count()).select_from(m)).scalar_one()  # noqa: E731
            q = s.execute(select(Proposal.kind, func.count()).where(Proposal.outcome == "queued")
                          .group_by(Proposal.kind)).all()
            outcomes = s.execute(select(Proposal.agent, Proposal.outcome, func.count())
                                 .group_by(Proposal.agent, Proposal.outcome)).all()
            runs = s.execute(select(Run).order_by(Run.id.desc()).limit(10)).scalars().all()
            return {
                "sources": count(Source), "records": s.execute(select(func.count()).select_from(CanonicalRecord)
                                                            .where(CanonicalRecord.erased.is_(False))).scalar_one(),
                "profiles": count(Profile), "ledger_events": count(LedgerEvent),
                "review_queue": {k: n for k, n in q},
                "agent_outcomes": [{"agent": a, "outcome": o, "n": n} for a, o, n in outcomes],
                "runs": [{"id": r.id, "status": r.status, "started_at": r.started_at.isoformat(),
                          "stats": r.stats, "state_digest": r.state_digest, "actor": r.actor} for r in runs],
            }

    # ---- sources
    @app.get("/sources")
    def list_sources(actor: str = Depends(viewer)):
        with sf() as s:
            out = []
            for src in s.execute(select(Source).order_by(Source.id)).scalars():
                sm = s.get(SchemaMapping, src.active_mapping_id) if src.active_mapping_id else None
                out.append({"id": src.id, "kind": src.kind, "trust_rank": src.trust_rank, "cursor": src.cursor,
                            "config": src.config, "mapping": {"id": sm.id, "version": sm.version, "fields": sm.mapping["fields"]} if sm else None})
            return out

    @app.post("/sources")
    def add_source(body: SourceIn, actor: str = Depends(admin)):
        guard(engine.register_source, body.id, body.kind, body.config, body.trust_rank, actor)
        return {"ok": True}

    @app.put("/sources/{source_id}/mapping")
    def put_mapping(source_id: str, body: MappingIn, actor: str = Depends(steward)):
        if body.force and not actor.startswith("admin"):
            raise HTTPException(403, "force requires admin")
        return guard(engine.set_mapping, source_id, body.mapping, actor, body.force)

    @app.post("/sources/{source_id}/ingest")
    def ingest(source_id: str, actor: str = Depends(steward)):
        return guard(engine.ingest, source_id, None, actor)

    # ---- runs
    @app.post("/runs")
    def run(body: RunIn, actor: str = Depends(steward)):
        if body.force and not actor.startswith("admin"):
            raise HTTPException(403, "overriding circuit breakers requires admin")
        if body.ingest:
            return guard(engine.run_all, actor, body.force)
        return guard(engine.resolve, actor, body.force)

    # ---- review queue
    @app.get("/review")
    def review_queue(kind: str | None = None, limit: int = Query(100, le=500), actor: str = Depends(viewer)):
        with sf() as s:
            q = select(Proposal).where(Proposal.outcome == "queued")
            if kind:
                q = q.where(Proposal.kind == kind)
            items = s.execute(q.order_by(Proposal.id).limit(limit)).scalars().all()
            out = []
            for p in items:
                item = {"id": p.id, "kind": p.kind, "subject": p.subject, "agent": p.agent, "output": p.output,
                        "gates": p.gates, "created_at": p.created_at.isoformat()}
                if p.kind in {"match_adjudication", "cluster_conflict"}:
                    a, _, b = p.subject.partition("|")
                    ra, rb = s.get(CanonicalRecord, a), s.get(CanonicalRecord, b)
                    item["records"] = [{"id": r.id, "source": r.source_id, "data": r.data} for r in (ra, rb) if r]
                    item["comparisons"] = p.input.get("comparisons")
                if p.kind == "schema_mapping":
                    item["input"] = p.input
                out.append(item)
            return out

    @app.post("/review/{proposal_id}")
    def decide(proposal_id: int, body: ReviewIn, actor: str = Depends(steward)):
        return guard(engine.review, proposal_id, body.approve, actor, body.note)

    # ---- profiles
    @app.get("/profiles")
    def profiles(q: str | None = None, limit: int = Query(50, le=500), offset: int = 0, actor: str = Depends(viewer)):
        with sf() as s:
            stmt = select(Profile).order_by(Profile.member_count.desc(), Profile.id)
            rows = s.execute(stmt).scalars().all()
            if q:
                ql = q.lower()
                rows = [p for p in rows if ql in json.dumps(p.data).lower() or ql == p.id]
            return {"total": len(rows), "items": [{"id": p.id, "members": p.member_count, "data": p.data,
                                                   "consent": p.consent} for p in rows[offset:offset + limit]]}

    @app.get("/profiles/{pid}")
    def profile(pid: str, actor: str = Depends(viewer)):
        p = engine.get_profile(pid)
        if p is None:
            raise HTTPException(404, "not found")
        if p.get("status") == "erased_or_retired":
            raise HTTPException(410, "profile erased or retired")
        with sf() as s:
            p["records"] = [{"id": r.id, "source": r.source_id, "observed_at": r.observed_at, "data": r.data}
                            for r in (s.get(CanonicalRecord, m) for m in p["members"]) if r]
        return p

    @app.post("/profiles/{pid}/split")
    def split(pid: str, body: SplitIn, actor: str = Depends(steward)):
        return {"constraints_added": guard(engine.split_profile, pid, body.record_ids, actor, body.reason)}

    @app.post("/constraints")
    def constraint(body: ConstraintIn, actor: str = Depends(steward)):
        guard(engine.add_constraint, body.kind, body.a, body.b, actor, body.reason)
        return {"ok": True}

    # ---- privacy
    @app.post("/privacy/erasure")
    def erasure(body: ErasureIn, actor: str = Depends(admin)):
        if not (body.email or body.phone or body.profile_id):
            raise HTTPException(422, "one of email, phone, profile_id required")
        return guard(engine.erase, actor, body.reason, body.email, body.phone, body.profile_id, body.allow_multiple)

    @app.get("/export/{purpose}")
    def export(purpose: str, limit: int = Query(10000, le=100000), actor: str = Depends(steward)):
        return engine.export(purpose, limit)

    # ---- audit
    @app.get("/audit/verify")
    def verify(actor: str = Depends(viewer)):
        with sf() as s:
            return ledger.verify(s)

    @app.get("/audit/replay")
    def replay(actor: str = Depends(steward)):
        return engine.replay()

    @app.get("/audit/events")
    def events(after: int = 0, type: str | None = None, limit: int = Query(100, le=1000), actor: str = Depends(viewer)):
        with sf() as s:
            q = select(LedgerEvent).where(LedgerEvent.seq > after)
            if type:
                q = q.where(LedgerEvent.type == type)
            return [{"seq": e.seq, "ts": e.ts, "type": e.type, "actor": e.actor, "payload": e.payload_obj(),
                     "hash": e.hash} for e in s.execute(q.order_by(LedgerEvent.seq).limit(limit)).scalars()]

    @app.get("/proposals/{pid}")
    def proposal(pid: int, actor: str = Depends(viewer)):
        with sf() as s:
            p = s.get(Proposal, pid)
            if not p:
                raise HTTPException(404)
            return {"id": p.id, "kind": p.kind, "subject": p.subject, "agent": p.agent, "input": p.input,
                    "output": p.output, "gates": p.gates, "outcome": p.outcome, "resolved_by": p.resolved_by,
                    "note": p.note}

    # ---- Segment webhook (HMAC-SHA1 X-Signature over raw body, per Segment webhook destination)
    @app.post("/ingest/segment/{source_id}")
    async def segment(source_id: str, request: Request):
        if not settings.segment_secret:
            raise HTTPException(503, "ACDP_SEGMENT_SHARED_SECRET not configured")
        body = await request.body()
        sig = request.headers.get("x-signature", "")
        want = hmac.new(settings.segment_secret.encode(), body, hashlib.sha1).hexdigest()
        if not hmac.compare_digest(sig, want):
            raise HTTPException(401, "bad signature")
        data = json.loads(body)
        events = data.get("batch", [data]) if isinstance(data, dict) else data
        rows = SegmentConnector.rows_from_events(events)
        if not rows:
            return {"accepted": 0}
        with sf() as s:
            src = s.get(Source, source_id)
            if not src or src.kind != "segment":
                raise HTTPException(404, "unknown segment source")
        return guard(engine.ingest, source_id, rows, "segment-webhook")

    return app


def app_factory() -> FastAPI:  # uvicorn --factory acdp.api:app_factory
    return create_app()
