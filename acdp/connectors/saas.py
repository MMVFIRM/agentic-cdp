"""SaaS connectors. Request/response shapes follow each vendor's public REST API.

Verified here only against recorded-shape mock transports (tests/ and bench/);
not yet exercised against live tenants — see README "Known limitations".
"""
from __future__ import annotations

import re
from typing import Any, Iterator
from urllib.parse import quote

from .base import HttpConnector, SourceRow, secret

SF_DEFAULT_FIELDS = ["Id", "FirstName", "LastName", "Suffix", "Email", "Phone", "MobilePhone", "Birthdate",
                     "MailingStreet", "MailingCity", "MailingState", "MailingPostalCode", "MailingCountry",
                     "Title", "HasOptedOutOfEmail", "SystemModstamp"]


class SalesforceConnector(HttpConnector):
    """config: instance_url, token_env, object (Contact|Lead), api_version, fields"""
    kind = "salesforce"
    ALLOWED_HOSTS = (r"[a-z0-9\-.]+\.my\.salesforce\.com", r"[a-z0-9\-.]+\.salesforce\.com", r"[a-z0-9\-.]+\.force\.com")

    def read(self, cursor: str | None) -> Iterator[SourceRow]:
        base = self.config["instance_url"].rstrip("/")
        ver = self.config.get("api_version", "v61.0")
        obj = self.config.get("object", "Contact")
        if not re.fullmatch(r"[A-Za-z_]+", obj):
            raise ValueError("invalid object name")
        fields = self.config.get("fields", SF_DEFAULT_FIELDS)
        if not all(re.fullmatch(r"[A-Za-z_]+", f) for f in fields):
            raise ValueError("invalid field name")
        soql = f"SELECT {', '.join(fields)} FROM {obj}"
        if cursor:
            if not re.fullmatch(r"[0-9T:\-.+Z]+", cursor):
                raise ValueError("invalid cursor")
            soql += f" WHERE SystemModstamp > {cursor}"
        soql += " ORDER BY SystemModstamp ASC"
        headers = {"authorization": f"Bearer {secret(self.config)}"}
        url = f"{base}/services/data/{ver}/query?q={quote(soql)}"
        best = cursor
        while url:
            body = self.request("GET", url, headers=headers).json()
            for rec in body.get("records", []):
                rec = {k: v for k, v in rec.items() if k != "attributes"}
                upd = rec.get("SystemModstamp")
                if upd and (best is None or upd > best):
                    best = upd
                yield SourceRow(rec["Id"], rec, upd)
            nxt = body.get("nextRecordsUrl")
            url = f"{base}{nxt}" if nxt and not body.get("done", True) else None
        self.next_cursor = best


HS_DEFAULT_PROPS = ["email", "firstname", "lastname", "phone", "mobilephone", "address", "city", "state", "zip",
                    "country", "company", "jobtitle", "date_of_birth", "hs_email_optout", "lastmodifieddate"]


class HubSpotConnector(HttpConnector):
    """config: token_env, properties. Full sync via list API, incremental via search API."""
    kind = "hubspot"
    ALLOWED_HOSTS = (r"api\.hubapi\.com",)
    BASE = "https://api.hubapi.com"

    def read(self, cursor: str | None) -> Iterator[SourceRow]:
        props = self.config.get("properties", HS_DEFAULT_PROPS)
        headers = {"authorization": f"Bearer {secret(self.config)}"}
        best = cursor
        after = None
        while True:
            if cursor:
                body: dict[str, Any] = {
                    "limit": 100, "properties": props,
                    "filterGroups": [{"filters": [{"propertyName": "lastmodifieddate", "operator": "GT", "value": cursor}]}],
                    "sorts": [{"propertyName": "lastmodifieddate", "direction": "ASCENDING"}],
                }
                if after:
                    body["after"] = after
                data = self.request("POST", f"{self.BASE}/crm/v3/objects/contacts/search", headers=headers, json=body).json()
            else:
                params = {"limit": 100, "properties": ",".join(props)}
                if after:
                    params["after"] = after
                data = self.request("GET", f"{self.BASE}/crm/v3/objects/contacts", headers=headers, params=params).json()
            for r in data.get("results", []):
                payload = {"id": r["id"], **(r.get("properties") or {})}
                upd = payload.get("lastmodifieddate") or r.get("updatedAt")
                if upd and (best is None or upd > best):
                    best = upd
                yield SourceRow(str(r["id"]), payload, upd)
            after = ((data.get("paging") or {}).get("next") or {}).get("after")
            if not after:
                break
        self.next_cursor = best


class ShopifyConnector(HttpConnector):
    """config: shop (subdomain), token_env, api_version"""
    kind = "shopify"
    ALLOWED_HOSTS = (r"[a-z0-9\-]+\.myshopify\.com",)

    def read(self, cursor: str | None) -> Iterator[SourceRow]:
        shop = self.config["shop"]
        if not re.fullmatch(r"[a-z0-9\-]+", shop):
            raise ValueError("invalid shop")
        ver = self.config.get("api_version", "2024-07")
        headers = {"x-shopify-access-token": secret(self.config)}
        url: str | None = f"https://{shop}.myshopify.com/admin/api/{ver}/customers.json"
        params: dict[str, Any] | None = {"limit": 250, "order": "updated_at asc"}
        if cursor:
            params["updated_at_min"] = cursor
        best = cursor
        while url:
            r = self.request("GET", url, headers=headers, params=params)
            for c in r.json().get("customers", []):
                upd = c.get("updated_at")
                if cursor and upd and upd <= cursor:
                    continue
                if upd and (best is None or upd > best):
                    best = upd
                yield SourceRow(str(c["id"]), c, upd)
            m = re.search(r'<([^>]+)>;\s*rel="next"', r.headers.get("link", ""))
            url, params = (m.group(1), None) if m else (None, None)
        self.next_cursor = best


class StripeConnector(HttpConnector):
    """config: token_env. Incremental by `created`; updates to existing customers need a full resync
    (or a customer.updated webhook — roadmap)."""
    kind = "stripe"
    ALLOWED_HOSTS = (r"api\.stripe\.com",)

    def read(self, cursor: str | None) -> Iterator[SourceRow]:
        headers = {"authorization": f"Bearer {secret(self.config)}"}
        params: dict[str, Any] = {"limit": 100}
        if cursor:
            params["created[gt]"] = cursor
        best = cursor
        while True:
            data = self.request("GET", "https://api.stripe.com/v1/customers", headers=headers, params=params).json()
            items = data.get("data", [])
            for c in items:
                created = str(c.get("created"))
                if best is None or int(created) > int(best):
                    best = created
                yield SourceRow(c["id"], c, created)
            if not data.get("has_more") or not items:
                break
            params = {**params, "starting_after": items[-1]["id"]}
        self.next_cursor = best


class SegmentConnector(HttpConnector):
    """Push-only: Segment webhook destination posts identify calls to /ingest/segment/{source_id}."""
    kind = "segment"
    push_only = True

    def read(self, cursor):  # pragma: no cover - push only
        return iter(())

    @staticmethod
    def rows_from_events(events: list[dict[str, Any]]) -> list[SourceRow]:
        rows = []
        for e in events:
            if e.get("type") != "identify":
                continue
            key = e.get("userId") or e.get("anonymousId")
            if not key:
                continue
            payload = {"userId": e.get("userId"), "timestamp": e.get("timestamp"), "traits": e.get("traits") or {}}
            rows.append(SourceRow(str(key), payload, e.get("timestamp")))
        return rows
