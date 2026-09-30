"""acdp CLI: serve | source add | run | replay | verify | erase"""
from __future__ import annotations

import argparse
import json
import sys


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="acdp")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sv = sub.add_parser("serve")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8080)
    sa = sub.add_parser("source-add")
    sa.add_argument("id")
    sa.add_argument("kind")
    sa.add_argument("--config", default="{}", help="JSON; secrets by env-var name, e.g. {\"token_env\":\"HUBSPOT_TOKEN\"}")
    sa.add_argument("--trust", type=int, default=50)
    rn = sub.add_parser("run")
    rn.add_argument("--force", action="store_true")
    sub.add_parser("replay")
    sub.add_parser("verify")
    er = sub.add_parser("erase")
    er.add_argument("--email")
    er.add_argument("--phone")
    er.add_argument("--profile")
    er.add_argument("--reason", required=True)
    a = ap.parse_args(argv)

    if a.cmd == "serve":
        import uvicorn
        uvicorn.run("acdp.api:app_factory", factory=True, host=a.host, port=a.port)
        return 0

    from .config import get_settings
    from .engine import Engine
    from .models import make_session_factory
    st = get_settings()
    eng = Engine(make_session_factory(st.database_url), st)
    if a.cmd == "source-add":
        eng.register_source(a.id, a.kind, json.loads(a.config), a.trust, actor="cli")
        out = {"ok": True}
    elif a.cmd == "run":
        out = eng.run_all(actor="cli", force=a.force)
    elif a.cmd == "replay":
        out = eng.replay()
    elif a.cmd == "verify":
        from . import ledger
        with eng.sf() as s:
            out = ledger.verify(s)
    else:
        out = eng.erase("cli", a.reason, a.email, a.phone, a.profile)
    print(json.dumps(out, indent=2, default=str))
    return 0 if out.get("ok", True) else 1


if __name__ == "__main__":
    sys.exit(main())
