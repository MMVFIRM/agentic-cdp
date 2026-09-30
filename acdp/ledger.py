"""Append-only, hash-chained audit ledger.

hash_n = sha256(prev_hash || seq || type || actor || payload_json)

Timestamps are recorded but are *not* part of the replay state digest.
Payloads must never contain raw PII — use util.pii_hash.
"""
from __future__ import annotations

import datetime as dt
import hashlib
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from .models import LedgerEvent
from .util import canonical_json

GENESIS = "0" * 64


def _h(prev: str, seq: int, typ: str, actor: str, payload: str, ts: str) -> str:
    return hashlib.sha256(f"{prev}|{seq}|{typ}|{actor}|{ts}|{payload}".encode()).hexdigest()


def append(s: Session, typ: str, actor: str, payload: dict[str, Any]) -> LedgerEvent:
    if s.get_bind().dialect.name == "postgresql":
        # Serialize appenders across processes; released at transaction end.
        s.execute(text("SELECT pg_advisory_xact_lock(724201)"))
    last =s.execute(select(LedgerEvent).order_by(LedgerEvent.seq.desc()).limit(1)).scalar_one_or_none()
    prev = last.hash if last else GENESIS
    seq = (last.seq + 1) if last else 1
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    body = canonical_json(payload)
    ev = LedgerEvent(seq=seq, ts=ts, type=typ, actor=actor, payload=body, prev_hash=prev,
                     hash=_h(prev, seq, typ, actor, body, ts))
    s.add(ev)
    s.flush()
    return ev


def verify(s: Session) -> dict[str, Any]:
    prev = GENESIS
    n = 0
    for ev in s.execute(select(LedgerEvent).order_by(LedgerEvent.seq)).scalars():
        n += 1
        if ev.seq != n:
            return {"ok": False, "events": n, "error": f"sequence gap at {ev.seq} (expected {n})"}
        if ev.prev_hash != prev:
            return {"ok": False, "events": n, "error": f"broken link at seq {ev.seq}"}
        if _h(prev, ev.seq, ev.type, ev.actor, ev.payload, ev.ts) != ev.hash:
            return {"ok": False, "events": n, "error": f"hash mismatch at seq {ev.seq}"}
        prev = ev.hash
    return {"ok": True, "events": n, "head": prev}


def head(s: Session) -> str:
    last = s.execute(select(LedgerEvent.hash).order_by(LedgerEvent.seq.desc()).limit(1)).scalar_one_or_none()
    return last or GENESIS


def count(s: Session) -> int:
    return s.execute(select(func.count()).select_from(LedgerEvent)).scalar_one()
