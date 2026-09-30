"""httpx.MockTransport that serves generated data in each vendor's documented
REST shape, with real pagination semantics and auth-header checks."""
from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx


def _strip(rows):
    return [{k: v for k, v in r.items() if k != "_truth"} for r in rows]


class VendorMock:
    def __init__(self, data: dict[str, list[dict[str, Any]]], tokens: dict[str, str]):
        self.data = {k: _strip(v) for k, v in data.items() if not k.startswith("_")}
        self.tokens = tokens
        self.calls: list[str] = []
        self.fail_next: int = 0  # inject 429s to exercise retry

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def _auth(self, req: httpx.Request, vendor: str) -> bool:
        tok = self.tokens[vendor]
        if vendor == "shopify":
            return req.headers.get("x-shopify-access-token") == tok
        return req.headers.get("authorization") == f"Bearer {tok}"

    def handle(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(f"{req.method} {req.url}")
        if self.fail_next > 0:
            self.fail_next -= 1
            return httpx.Response(429, headers={"retry-after": "0"})
        host, path = req.url.host, req.url.path
        q = parse_qs(urlparse(str(req.url)).query)
        if host.endswith("my.salesforce.com"):
            if not self._auth(req, "salesforce"):
                return httpx.Response(401, json=[{"errorCode": "INVALID_SESSION_ID"}])
            if "/query/" in path and path.rsplit("-", 1)[-1].isdigit():
                start = int(path.rsplit("-", 1)[-1])
            else:
                start = 0
                rows = self.data["salesforce"]
                soql = q.get("q", [""])[0]
                if "WHERE SystemModstamp >" in soql:
                    cur = soql.split("WHERE SystemModstamp >")[1].split("ORDER")[0].strip()
                    rows = [r for r in rows if r["SystemModstamp"] > cur]
                self._sf_rows = sorted(rows, key=lambda r: (r["SystemModstamp"], r["Id"]))
            rows = self._sf_rows
            page = rows[start:start + 200]
            done = start + 200 >= len(rows)
            body = {"totalSize": len(rows), "done": done,
                    "records": [{"attributes": {"type": "Contact"}, **{k: v for k, v in r.items() if k != "attributes"}} for r in page]}
            if not done:
                body["nextRecordsUrl"] = f"/services/data/v61.0/query/01gXX-{start + 200}"
            return httpx.Response(200, json=body)
        if host == "api.hubapi.com":
            if not self._auth(req, "hubspot"):
                return httpx.Response(401, json={"status": "error"})
            rows = sorted(self.data["hubspot"], key=lambda r: r["id"])
            if path.endswith("/search"):
                body = json.loads(req.content)
                cur = body["filterGroups"][0]["filters"][0]["value"]
                rows = sorted([r for r in rows if r["properties"]["lastmodifieddate"] > cur],
                              key=lambda r: r["properties"]["lastmodifieddate"])
                after = int(body.get("after") or 0)
            else:
                after = int(q.get("after", ["0"])[0])
            page = rows[after:after + 100]
            out = {"results": [{"id": r["id"], "properties": r["properties"],
                                "updatedAt": r["properties"]["lastmodifieddate"]} for r in page]}
            if after + 100 < len(rows):
                out["paging"] = {"next": {"after": str(after + 100)}}
            return httpx.Response(200, json=out)
        if host.endswith(".myshopify.com"):
            if not self._auth(req, "shopify"):
                return httpx.Response(401, json={"errors": "[API] Invalid API key or access token"})
            if "page_info" in q:
                start = int(q["page_info"][0])
            else:
                start = 0
                rows = sorted(self.data["shopify"], key=lambda r: (r["updated_at"], r["id"]))
                if "updated_at_min" in q:
                    rows = [r for r in rows if r["updated_at"] >= q["updated_at_min"][0]]
                self._shop_rows = rows
            rows = self._shop_rows
            page = rows[start:start + 250]
            headers = {}
            if start + 250 < len(rows):
                headers["link"] = f'<https://{host}{path}?limit=250&page_info={start + 250}>; rel="next"'
            return httpx.Response(200, json={"customers": page}, headers=headers)
        if host == "api.stripe.com":
            if not self._auth(req, "stripe"):
                return httpx.Response(401, json={"error": {"type": "invalid_request_error"}})
            rows = sorted(self.data["stripe"], key=lambda r: r["id"])
            if "created[gt]" in q:
                rows = [r for r in rows if r["created"] > int(q["created[gt]"][0])]
            start = 0
            if "starting_after" in q:
                ids = [r["id"] for r in rows]
                start = ids.index(q["starting_after"][0]) + 1
            page = rows[start:start + 100]
            return httpx.Response(200, json={"object": "list", "data": page, "has_more": start + 100 < len(rows),
                                             "url": "/v1/customers"})
        return httpx.Response(404, json={"error": f"no mock for {host}{path}"})
