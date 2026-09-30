"""Runtime configuration. Everything is environment-driven and fail-closed.

Secrets are never stored in the database: source configs reference env var
names (e.g. ``"token_env": "HUBSPOT_TOKEN"``) and are resolved at call time.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


@dataclass
class ResolutionPolicy:
    # Fellegi-Sunter style log-likelihood bands.
    auto_match: float = 12.0      # >= : deterministic MATCH (if no hard conflict)
    non_match: float = 4.0        # <= : deterministic NON-MATCH
    # Agent gate thresholds (gray zone between the two bands).
    agent_min_confidence: float = 0.85
    # Circuit breakers — a run that trips any of these is halted, not published.
    max_cluster_size: int = 25
    max_merge_ratio_jump: float = 0.20   # fraction of profiles lost vs previous run
    min_mapping_validity: float = 0.90
    default_country_code: str = "1"


@dataclass
class Settings:
    database_url: str = field(default_factory=lambda: _env("ACDP_DATABASE_URL", "sqlite:///./acdp.db"))
    # HMAC key for hashing PII into the immutable ledger and suppression list.
    pii_hmac_key: str | None = field(default_factory=lambda: _env("ACDP_PII_HMAC_KEY"))
    # "key:role,key:role" — roles: admin, steward, viewer.
    api_keys: str | None = field(default_factory=lambda: _env("ACDP_API_KEYS"))
    llm_provider: str = field(default_factory=lambda: _env("ACDP_LLM_PROVIDER", "deterministic"))
    llm_model: str | None = field(default_factory=lambda: _env("ACDP_LLM_MODEL"))
    llm_base_url: str | None = field(default_factory=lambda: _env("ACDP_LLM_BASE_URL"))
    llm_api_key_env: str = field(default_factory=lambda: _env("ACDP_LLM_API_KEY_ENV", "ACDP_LLM_API_KEY"))
    # What the LLM may see. "derived" = only masked shapes + computed comparisons (default).
    llm_pii_mode: str = field(default_factory=lambda: _env("ACDP_LLM_PII_MODE", "derived"))
    segment_secret: str | None = field(default_factory=lambda: _env("ACDP_SEGMENT_SHARED_SECRET"))
    allow_insecure_dev: bool = field(default_factory=lambda: _env("ACDP_ALLOW_INSECURE_DEV", "0") == "1")
    policy: ResolutionPolicy = field(default_factory=ResolutionPolicy)

    def require_hmac_key(self) -> bytes:
        if self.pii_hmac_key:
            return self.pii_hmac_key.encode()
        if self.allow_insecure_dev:
            return b"insecure-dev-key-do-not-use-in-production"
        raise RuntimeError(
            "ACDP_PII_HMAC_KEY is not set. Refusing to start (set ACDP_ALLOW_INSECURE_DEV=1 for local dev only)."
        )


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def set_settings(s: Settings) -> None:
    global _settings
    _settings = s
