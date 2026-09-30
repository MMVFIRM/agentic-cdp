"""Canonical customer schema, the closed vocabulary agents may map into, and the
deterministic transform that turns a source row + approved mapping into a
canonical record with match keys.
"""
from __future__ import annotations

from typing import Any, Callable

from . import normalize as N

# Closed vocabulary. An agent proposing any target outside this set is rejected by gate.
MULTI_TARGETS = {"email", "phone"}
TARGETS: dict[str, str] = {
    "email": "email address",
    "phone": "phone number",
    "first_name": "given name",
    "last_name": "family name",
    "full_name": "full name in one field (requires transform split_full_name)",
    "name_suffix": "generational suffix (Jr, Sr, III)",
    "birth_date": "date of birth",
    "address_line1": "street address",
    "address_line2": "apt/suite",
    "city": "city",
    "region": "state/province",
    "postal_code": "postal/zip code",
    "country": "country",
    "company": "employer/company name",
    "job_title": "job title",
    "updated_at": "record last-modified timestamp",
    "created_at": "record creation timestamp",
    "consent.email_marketing": "email marketing opt-in (boolean)",
    "consent.sms_marketing": "SMS marketing opt-in (boolean)",
    "consent.analytics": "analytics/tracking consent (boolean)",
    "attribute": "keep as an extra attribute (non-identity)",
}
TRANSFORMS = {"default", "split_full_name", "invert_bool", "split_list"}
IDENTITY_TARGETS = {"email", "phone", "first_name", "last_name", "full_name", "birth_date", "postal_code",
                    "address_line1"}


def _validator(target: str, default_cc: str) -> Callable[[Any], Any]:
    if target == "email":
        return N.email
    if target == "phone":
        return lambda v: N.phone(v, default_cc)
    if target in {"first_name", "last_name"}:
        return lambda v: N.name_part(v) if (N.clean_str(v) and any(c.isalpha() for c in str(v))
                                             and not any(c.isdigit() or c == "@" for c in str(v))) else None
    if target == "full_name":
        return lambda v: (N.split_full_name(v)[0] or N.split_full_name(v)[1]) if (
            N.clean_str(v) and not any(c.isdigit() or c == "@" for c in str(v))) else None
    if target == "name_suffix":
        return N.suffix
    if target == "birth_date":
        return N.date
    if target in {"updated_at", "created_at"}:
        return N.timestamp
    if target == "postal_code":
        return lambda v: N.postal(v, "US") or (N.clean_str(v) if N.clean_str(v) and len(str(v)) <= 10 else None)
    if target == "country":
        return N.country
    if target.startswith("consent."):
        return N.boolean
    return N.clean_str


def validator(target: str, default_cc: str = "1") -> Callable[[Any], Any]:
    return _validator(target, default_cc)


def flatten(obj: Any, prefix: str = "", out: dict[str, Any] | None = None) -> dict[str, Any]:
    """{'a': {'b': 1}, 'c': [1,2]} -> {'a.b': 1, 'c': [1,2]}  (lists kept as values)."""
    out = {} if out is None else out
    if isinstance(obj, dict):
        for k, v in obj.items():
            flatten(v, f"{prefix}.{k}" if prefix else str(k), out)
    else:
        out[prefix] = obj
    return out


def _pre(transform: str, v: Any) -> list[Any]:
    if v is None:
        return []
    if isinstance(v, list):
        vals = v
    elif transform == "split_list" and isinstance(v, str):
        vals = [x for x in v.replace(";", ",").split(",")]
    else:
        vals = [v]
    return vals


def canonicalize(flat: dict[str, Any], mapping: dict[str, Any], default_cc: str = "1") -> dict[str, Any]:
    data: dict[str, Any] = {"emails": [], "phones": [], "address": {}, "consent": {}, "attributes": {}}
    for src_field, spec in mapping.get("fields", {}).items():
        target, transform = spec.get("target"), spec.get("transform", "default")
        if not target or src_field not in flat:
            continue
        for raw in _pre(transform, flat[src_field]):
            if target == "full_name":
                fn, ln, sfx = N.split_full_name(raw)
                data.setdefault("first_name", fn) if fn else None
                data.setdefault("last_name", ln) if ln else None
                if sfx:
                    data.setdefault("name_suffix", sfx)
                continue
            if target == "attribute":
                v = N.clean_str(raw)
                if v is not None:
                    data["attributes"][src_field] = v
                continue
            v = validator(target, default_cc)(raw)
            if v is None:
                continue
            if transform == "invert_bool" and isinstance(v, bool):
                v = not v
            if target == "email":
                if v not in data["emails"]:
                    data["emails"].append(v)
            elif target == "phone":
                if v not in data["phones"]:
                    data["phones"].append(v)
            elif target.startswith("address_") or target in {"city", "region", "postal_code", "country"}:
                data["address"].setdefault(target.replace("address_", ""), v)
            elif target.startswith("consent."):
                data["consent"].setdefault(target.split(".", 1)[1], v)
            else:
                data.setdefault(target, v)
    # Name suffix can hide inside last_name ("Smith Jr")
    ln = data.get("last_name")
    if ln and " " in ln and ln.split()[-1].lower() in N.SUFFIXES:
        data["name_suffix"] = data.get("name_suffix") or N.SUFFIXES[ln.split()[-1].lower()]
        data["last_name"] = " ".join(ln.split()[:-1])
    return data


def match_keys(data: dict[str, Any]) -> dict[str, Any]:
    addr = data.get("address", {})
    return {
        "email_keys": sorted({N.email_key(e) for e in data.get("emails", [])}),
        "phones": sorted(set(data.get("phones", []))),
        "fn": N.first_name_key(data.get("first_name")),
        "fn_raw": N.name_key(data.get("first_name").split()[0]) if data.get("first_name") else None,
        "ln": N.name_key(data.get("last_name")),
        "suffix": data.get("name_suffix"),
        "dob": data.get("birth_date"),
        "postal": addr.get("postal_code"),
        "addr": N.address_key(addr.get("line1")),
        "city": N.name_key(addr.get("city")),
    }


def has_identity(keys: dict[str, Any]) -> bool:
    """A record is resolvable only with a strong key or name + locality/dob."""
    if keys["email_keys"] or keys["phones"]:
        return True
    return bool(keys["ln"] and keys["fn"] and (keys["postal"] or keys["dob"] or keys["addr"]))
