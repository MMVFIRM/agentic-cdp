from __future__ import annotations

from typing import Any, Protocol


class AgentError(RuntimeError):
    """Raised when an agent cannot produce a well-formed proposal. Gates treat it as a rejection."""


class Agent(Protocol):
    name: str

    def propose_mapping(self, task: dict[str, Any]) -> dict[str, Any]:
        """task: {source_id, fields:[{name, samples, stats}], targets, transforms}
        returns: {fields: {name: {target|None, transform, confidence}}}"""
        ...

    def adjudicate(self, task: dict[str, Any]) -> dict[str, Any]:
        """task: {pair, score, comparisons:{field:{status, similarity, ...}}}
        returns: {decision: match|no_match|abstain, confidence, evidence:[{field,status}], rationale}"""
        ...
