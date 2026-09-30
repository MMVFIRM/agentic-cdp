from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterator
from urllib.parse import urlparse

import httpx


@dataclass
class SourceRow:
    key: str
    payload: dict[str, Any]
    updated_at: str | None = None


class ConnectorError(RuntimeError):
    pass


class Connector:
    kind = "base"
    push_only = False

    def __init__(self, source_id: str, config: dict[str, Any], client: httpx.Client | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        self.source_id = source_id
        self.config = config
        self.client = client
        self.sleep = sleep
        self.next_cursor: str | None = None

    def read(self, cursor: str | None) -> Iterator[SourceRow]:  # pragma: no cover - abstract
        raise NotImplementedError

    def discover(self, sample_size: int = 200) -> list[SourceRow]:
        out = []
        for row in self.read(None):
            out.append(row)
            if len(out) >= sample_size:
                break
        self.next_cursor = None  # discovery never advances the cursor
        return out


def secret(config: dict[str, Any], key: str = "token_env") -> str:
    name = config.get(key)
    if not name:
        raise ConnectorError(f"config.{key} (name of env var holding the secret) is required")
    val = os.environ.get(name)
    if not val:
        raise ConnectorError(f"environment variable {name} is not set")
    return val


class HttpConnector(Connector):
    ALLOWED_HOSTS: tuple[str, ...] = ()  # regexes

    def _check_host(self, url: str) -> None:
        u = urlparse(url)
        if u.scheme != "https":
            raise ConnectorError(f"refusing non-HTTPS egress: {url}")
        if not any(re.fullmatch(p, u.hostname or "") for p in self.ALLOWED_HOSTS):
            raise ConnectorError(f"host {u.hostname!r} not in egress allowlist for {self.kind}")

    def request(self, method: str, url: str, **kw) -> httpx.Response:
        self._check_host(url)
        client = self.client or httpx.Client(timeout=60)
        for attempt in range(6):
            try:
                r = client.request(method, url, **kw)
            except httpx.TransportError as e:
                if attempt == 5:
                    raise ConnectorError(f"{self.kind}: transport error {e}") from e
                self.sleep(min(2 ** attempt, 30))
                continue
            if r.status_code == 429 or r.status_code >= 500:
                if attempt == 5:
                    raise ConnectorError(f"{self.kind}: HTTP {r.status_code} after retries")
                ra = r.headers.get("retry-after")
                self.sleep(float(ra) if ra and ra.replace(".", "").isdigit() else min(2 ** attempt, 30))
                continue
            if r.status_code >= 400:
                raise ConnectorError(f"{self.kind}: HTTP {r.status_code}: {r.text[:300]}")
            return r
        raise ConnectorError("unreachable")  # pragma: no cover
