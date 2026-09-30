"""File and SQL connectors."""
from __future__ import annotations

import csv
import json
import os
from typing import Iterator

from sqlalchemy import create_engine, text

from .base import Connector, ConnectorError, SourceRow


class CSVConnector(Connector):
    """config: path, key_field (optional; row index otherwise), updated_field (optional), delimiter, encoding"""
    kind = "csv"

    def read(self, cursor: str | None) -> Iterator[SourceRow]:
        path = self.config["path"]
        if not os.path.exists(path):
            raise ConnectorError(f"file not found: {path}")
        kf, uf = self.config.get("key_field"), self.config.get("updated_field")
        best = cursor
        with open(path, newline="", encoding=self.config.get("encoding", "utf-8-sig")) as fh:
            for i, row in enumerate(csv.DictReader(fh, delimiter=self.config.get("delimiter", ","))):
                row = {k.strip(): v for k, v in row.items() if k is not None}
                key = str(row.get(kf) if kf else i)
                upd = row.get(uf) if uf else None
                if cursor and upd and upd <= cursor:
                    continue
                if upd and (best is None or upd > best):
                    best = upd
                yield SourceRow(key, row, upd)
        self.next_cursor = best


class JSONLConnector(Connector):
    kind = "jsonl"

    def read(self, cursor: str | None) -> Iterator[SourceRow]:
        path = self.config["path"]
        kf, uf = self.config.get("key_field", "id"), self.config.get("updated_field")
        best = cursor
        with open(path, encoding="utf-8") as fh:
            for i, line in enumerate(fh):
                if not line.strip():
                    continue
                row = json.loads(line)
                upd = row.get(uf) if uf else None
                if cursor and upd and str(upd) <= cursor:
                    continue
                if upd and (best is None or str(upd) > best):
                    best = str(upd)
                yield SourceRow(str(row.get(kf, i)), row, str(upd) if upd else None)
        self.next_cursor = best


class SQLConnector(Connector):
    """config: url_env (env var with SQLAlchemy URL), table, key_column, updated_column (optional)"""
    kind = "sql"

    def read(self, cursor: str | None) -> Iterator[SourceRow]:
        url = os.environ.get(self.config["url_env"])
        if not url:
            raise ConnectorError(f"env var {self.config['url_env']} not set")
        table = self.config["table"]
        if not table.replace("_", "").replace(".", "").isalnum():
            raise ConnectorError("table name must be alphanumeric/underscore")
        kc, uc = self.config["key_column"], self.config.get("updated_column")
        for c in filter(None, (kc, uc)):
            if not c.replace("_", "").isalnum():
                raise ConnectorError("column names must be alphanumeric/underscore")
        q = f"SELECT * FROM {table}"
        params = {}
        if uc and cursor:
            q += f" WHERE {uc} > :cur"
            params["cur"] = cursor
        if uc:
            q += f" ORDER BY {uc}"
        best = cursor
        eng = create_engine(url)
        with eng.connect() as conn:
            for row in conn.execute(text(q), params).mappings():
                d = {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in row.items()}
                upd = str(d.get(uc)) if uc and d.get(uc) is not None else None
                if upd and (best is None or upd > best):
                    best = upd
                yield SourceRow(str(d[kc]), d, upd)
        self.next_cursor = best
