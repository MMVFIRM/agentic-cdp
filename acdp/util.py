from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any


def canonical_json(obj: Any) -> str:
    """Stable JSON: sorted keys, no whitespace, UTF-8 preserved."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def digest(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj).encode()).hexdigest()


def pii_hash(key: bytes, kind: str, value: str) -> str:
    return hmac.new(key, f"{kind}:{value}".encode(), hashlib.sha256).hexdigest()


def mask_value(v: Any) -> str:
    """Shape-preserving mask: letters->a/A, digits->9, keeps punctuation.

    Lets an LLM recognise *what kind* of value a column holds without seeing it.
    """
    s = str(v)
    out = []
    for ch in s[:64]:
        if ch.isdigit():
            out.append("9")
        elif ch.isalpha():
            out.append("A" if ch.isupper() else "a")
        else:
            out.append(ch)
    return "".join(out)
