"""LLM-backed agents (Anthropic Messages API, OpenAI-compatible Chat Completions).

The LLM only ever returns a JSON proposal. Nothing it says is executed or
written without passing acdp.gates. By default (ACDP_LLM_PII_MODE=derived) the
engine sends masked value shapes and computed comparisons, never raw PII.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

import httpx

from .base import AgentError

MAPPING_SYSTEM = """You map columns from a customer-data source into a fixed canonical schema.
Return ONLY a JSON object: {"fields": {"<source field>": {"target": <one of the allowed targets or null>,
"transform": <one of the allowed transforms>, "confidence": <0..1>}}}.
Rules: use null for technical ids and irrelevant fields; use "attribute" for useful non-identity fields;
use transform "split_full_name" only with target "full_name"; "invert_bool" only for opt-OUT style consent
flags; never invent targets. Validity rates per target are computed for you — respect them."""

ADJUDICATE_SYSTEM = """You decide whether two customer records describe the same real person.
You see computed field comparisons (status: agree|partial|disagree|missing|conflict) and masked value shapes.
Return ONLY JSON: {"decision": "match"|"no_match"|"abstain", "confidence": 0..1,
"evidence": [{"field": <field>, "status": <status exactly as given>}], "rationale": "<one sentence>"}.
Cite only fields and statuses that appear in the input. Prefer abstain over guessing: a false merge is
far more costly than a missed merge. Shared phone, address or email between people with different
given names is typical of households, not the same person."""


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise AgentError("no JSON object in model output")
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError as e:
        raise AgentError(f"invalid JSON from model: {e}") from e


class _LLMBase:
    provider = "llm"

    def __init__(self, settings, client: httpx.Client | None = None):
        self.model = settings.llm_model
        if not self.model:
            raise ValueError("ACDP_LLM_MODEL must be set when using an LLM provider")
        self.api_key = os.environ.get(settings.llm_api_key_env, "")
        self.base_url = settings.llm_base_url
        self.client = client or httpx.Client(timeout=60)
        self.name = f"{self.provider}/{self.model}"

    def _complete(self, system: str, user: str) -> str:  # pragma: no cover - abstract
        raise NotImplementedError

    def propose_mapping(self, task: dict[str, Any]) -> dict[str, Any]:
        return _extract_json(self._complete(MAPPING_SYSTEM, json.dumps(task, default=str)))

    def adjudicate(self, task: dict[str, Any]) -> dict[str, Any]:
        return _extract_json(self._complete(ADJUDICATE_SYSTEM, json.dumps(task, default=str)))


class AnthropicAgent(_LLMBase):
    provider = "anthropic"

    def _complete(self, system: str, user: str) -> str:
        url = (self.base_url or "https://api.anthropic.com").rstrip("/") + "/v1/messages"
        try:
            r = self.client.post(url, headers={
                "x-api-key": self.api_key, "anthropic-version": "2023-06-01", "content-type": "application/json",
            }, json={"model": self.model, "max_tokens": 2048, "temperature": 0, "system": system,
                     "messages": [{"role": "user", "content": user}]})
            r.raise_for_status()
            body = r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise AgentError(f"anthropic call failed: {e}") from e
        return "".join(b.get("text", "") for b in body.get("content", []) if b.get("type") == "text")


class OpenAICompatibleAgent(_LLMBase):
    """Works with OpenAI and any OpenAI-compatible server (vLLM, Ollama, LM Studio)."""
    provider = "openai"

    def _complete(self, system: str, user: str) -> str:
        url = (self.base_url or "https://api.openai.com/v1").rstrip("/") + "/chat/completions"
        try:
            r = self.client.post(url, headers={"authorization": f"Bearer {self.api_key}"}, json={
                "model": self.model, "temperature": 0, "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]})
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]
        except (httpx.HTTPError, ValueError, KeyError, IndexError) as e:
            raise AgentError(f"openai-compatible call failed: {e}") from e
