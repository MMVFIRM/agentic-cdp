"""Agent layer. Agents *propose*; they never write state. See acdp.gates."""
from .base import Agent, AgentError
from .deterministic import DeterministicAgent
from .llm import AnthropicAgent, OpenAICompatibleAgent


def build_agent(settings) -> Agent:
    p = (settings.llm_provider or "deterministic").lower()
    if p == "deterministic":
        return DeterministicAgent()
    if p == "anthropic":
        return AnthropicAgent(settings)
    if p in {"openai", "openai_compatible", "local"}:
        return OpenAICompatibleAgent(settings)
    raise ValueError(f"unknown ACDP_LLM_PROVIDER={p!r}")


__all__ = ["Agent", "AgentError", "DeterministicAgent", "AnthropicAgent", "OpenAICompatibleAgent", "build_agent"]
