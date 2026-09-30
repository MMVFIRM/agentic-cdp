"""Adversarial and null agents used to prove the gates, not the agent, bound correctness."""
from __future__ import annotations

from typing import Any

from acdp.agents import DeterministicAgent


class FabricatingAgent:
    """Always says MATCH at confidence 1.0 and cites every field as 'agree' (mostly lies).
    Mapping: tries to map every column to email."""
    name = "adversary/fabricator"

    def propose_mapping(self, task: dict[str, Any]) -> dict[str, Any]:
        return {"fields": {f["name"]: {"target": "email", "transform": "default", "confidence": 1.0} for f in task["fields"]}}

    def adjudicate(self, task: dict[str, Any]) -> dict[str, Any]:
        return {"decision": "match", "confidence": 1.0,
                "evidence": [{"field": f, "status": "agree"} for f in task["comparisons"]],
                "rationale": "trust me"}


class OvereagerAgent:
    """Cites only TRUE evidence, but says MATCH whenever any name field agrees — the realistic
    failure mode of an over-confident LLM. Only the sufficiency gate (A4) can stop it."""
    name = "adversary/overeager"

    def __init__(self):
        self.base = DeterministicAgent()

    def propose_mapping(self, task):
        return self.base.propose_mapping(task)

    def adjudicate(self, task: dict[str, Any]) -> dict[str, Any]:
        ev = [{"field": f, "status": c["status"]} for f, c in task["comparisons"].items()
              if c["status"] in {"agree", "partial"}]
        return {"decision": "match", "confidence": 0.99, "evidence": ev, "rationale": "names look the same"}


class NullAgent:
    """Abstains on everything: measures what the gray zone is worth."""
    name = "null/abstain"

    def __init__(self):
        self.base = DeterministicAgent()

    def propose_mapping(self, task):
        return self.base.propose_mapping(task)

    def adjudicate(self, task):
        return {"decision": "abstain", "confidence": 0.0, "evidence": [], "rationale": "null"}
