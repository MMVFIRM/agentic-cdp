"""Storage model. SQLite for dev/tests, Postgres in production (same schema).

PII lives only in raw_records / canonical_records / profiles. The ledger holds
HMAC digests only, so GDPR erasure can delete PII without breaking the chain.
"""
from __future__ import annotations

import datetime as dt
import json
from typing import Any

from sqlalchemy import (
    JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text,
    UniqueConstraint, create_engine, event,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON}


class Source(Base):
    __tablename__ = "sources"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    trust_rank: Mapped[int] = mapped_column(Integer, default=50)  # higher = more trusted
    cursor: Mapped[str | None] = mapped_column(Text, nullable=True)
    active_mapping_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    schema_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class SchemaMapping(Base):
    __tablename__ = "schema_mappings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"))
    version: Mapped[int] = mapped_column(Integer)
    mapping: Mapped[dict[str, Any]] = mapped_column(JSON)
    digest: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))  # proposed|approved|rejected|superseded
    proposal_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class RawRecord(Base):
    __tablename__ = "raw_records"
    __table_args__ = (UniqueConstraint("source_id", "source_key"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), index=True)
    source_key: Mapped[str] = mapped_column(String(256))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    digest: Mapped[str] = mapped_column(String(64))
    source_updated_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ingested_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    erased: Mapped[bool] = mapped_column(Boolean, default=False)


class CanonicalRecord(Base):
    __tablename__ = "canonical_records"
    id: Mapped[str] = mapped_column(String(320), primary_key=True)  # "<source_id>:<source_key>"
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), index=True)
    mapping_id: Mapped[int] = mapped_column(Integer)
    data: Mapped[dict[str, Any]] = mapped_column(JSON)
    keys: Mapped[dict[str, Any]] = mapped_column(JSON)  # normalized match keys
    digest: Mapped[str] = mapped_column(String(64))
    observed_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    erased: Mapped[bool] = mapped_column(Boolean, default=False, index=True)


class Profile(Base):
    __tablename__ = "profiles"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSON)
    lineage: Mapped[dict[str, Any]] = mapped_column(JSON)
    consent: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    digest: Mapped[str] = mapped_column(String(64))
    member_count: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class ProfileMember(Base):
    __tablename__ = "profile_members"
    record_id: Mapped[str] = mapped_column(String(320), primary_key=True)
    profile_id: Mapped[str] = mapped_column(String(64), index=True)


class ProfileAlias(Base):
    """Retired profile id -> surviving id, so downstream systems holding old ids never 404."""
    __tablename__ = "profile_aliases"
    old_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    new_id: Mapped[str] = mapped_column(String(64), index=True)
    reason: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class PairDecision(Base):
    """Cache of every pairwise decision, keyed by the evidence digest.

    If either record changes, the evidence digest changes and the pair is
    re-decided. This cache is what makes replay hermetic: replay never calls
    an agent, it only re-reads decisions recorded here.
    """
    __tablename__ = "pair_decisions"
    __table_args__ = (UniqueConstraint("a", "b", "evidence_digest"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    a: Mapped[str] = mapped_column(String(320), index=True)
    b: Mapped[str] = mapped_column(String(320), index=True)
    evidence_digest: Mapped[str] = mapped_column(String(64))
    score: Mapped[float] = mapped_column(Float)
    band: Mapped[str] = mapped_column(String(16))        # match|gray|non_match|conflict
    decision: Mapped[str] = mapped_column(String(16))    # match|no_match|review
    decided_by: Mapped[str] = mapped_column(String(128))  # rule|agent:<p>/<m>|steward:<id>
    proposal_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class Constraint(Base):
    __tablename__ = "constraints"
    __table_args__ = (UniqueConstraint("kind", "a", "b"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(16))  # must_link|cannot_link
    a: Mapped[str] = mapped_column(String(320))
    b: Mapped[str] = mapped_column(String(320))
    actor: Mapped[str] = mapped_column(String(128))
    reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class Proposal(Base):
    """Every AI (or rule) proposal, its gate verdicts, and its fate.

    outcome: auto_applied | queued | rejected | approved | overridden
    Open review queue == outcome 'queued'.
    """
    __tablename__ = "proposals"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    subject: Mapped[str] = mapped_column(String(700))
    agent: Mapped[str] = mapped_column(String(128))
    input_digest: Mapped[str] = mapped_column(String(64))
    input: Mapped[dict[str, Any]] = mapped_column(JSON)
    output: Mapped[dict[str, Any]] = mapped_column(JSON)
    gates: Mapped[list[Any]] = mapped_column(JSON)
    outcome: Mapped[str] = mapped_column(String(16), index=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    resolved_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    resolved_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class ConsentEvent(Base):
    __tablename__ = "consent_events"
    __table_args__ = (UniqueConstraint("record_id", "purpose", "observed_at"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    record_id: Mapped[str] = mapped_column(String(320), index=True)
    purpose: Mapped[str] = mapped_column(String(64))
    granted: Mapped[bool] = mapped_column(Boolean)
    observed_at: Mapped[str] = mapped_column(String(64))
    source_id: Mapped[str] = mapped_column(String(64))


class Suppression(Base):
    __tablename__ = "suppressions"
    hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class LedgerEvent(Base):
    __tablename__ = "ledger"
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[str] = mapped_column(String(40))
    type: Mapped[str] = mapped_column(String(48), index=True)
    actor: Mapped[str] = mapped_column(String(128))
    payload: Mapped[str] = mapped_column(Text)  # canonical JSON string (hash input)
    prev_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64))

    def payload_obj(self) -> dict[str, Any]:
        return json.loads(self.payload)


class Run(Base):
    __tablename__ = "runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    started_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(16))  # running|published|halted|failed
    stats: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    state_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    actor: Mapped[str] = mapped_column(String(128), default="system")


def make_engine(url: str):
    kwargs: dict[str, Any] = {}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    engine = create_engine(url, future=True, **kwargs)
    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _fk(dbapi_conn, _):  # pragma: no cover - trivial
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA secure_delete=ON")  # erased PII is overwritten, not left in free pages
            cur.close()
    return engine


def make_session_factory(url: str):
    engine = make_engine(url)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)
