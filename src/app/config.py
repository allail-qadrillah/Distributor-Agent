from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # Deployment environment. Default production = the safe side: every
    # fail-closed rule below applies unless development is set explicitly.
    app_environment: Literal["development", "production"] = "production"

    supabase_db_url: str = ""  # postgres connection string, plain Postgres
    # Phase 2 — Surplus Intelligence marketplace, OpenAI-compatible
    # chat completions (api.surplusintelligence.ai). Variable names kept
    # per project owner; empty = agent layer raises a clear config error.
    openai_api_key: str = ""
    openai_model: str = ""  # e.g. "deepseek-v4.1-flash-1017" (Surplus catalog id)
    openai_base_url: str = "https://api.surplusintelligence.ai/v1"

    # Phase 3 — intent classifier, a SEPARATE OpenAI-compatible provider.
    # Never falls back to the agent provider: base_url/api_key/model must
    # be set when enabled (fail fast, same convention as the agent layer).
    # enabled=false bypasses the intent gate; PRODUCTION REFUSES TO RUN
    # with it disabled (see _production_safety). Development must set it
    # explicitly to get the documented bypass.
    intent_classifier_enabled: bool = True
    intent_classifier_base_url: str = ""
    intent_classifier_api_key: str = ""
    intent_classifier_model: str = ""
    intent_classifier_confidence_threshold: float = Field(default=0.85, ge=0.0, le=1.0)
    intent_classifier_timeout_seconds: float = Field(default=5.0, gt=0.0, le=60.0)
    intent_classifier_max_context_messages: int = Field(default=10, ge=0, le=100)

    # Phase 3 — WhatsApp bridge (external Baileys/Node transport service).
    # empty bridge URL = outbound delivery disabled (webhooks still accept
    # inbound and persist; replies stay pending in the outbox).
    # Production with a URL but no token is refused (never send
    # unauthenticated bridge requests): see _production_safety.
    whatsapp_bridge_url: str = ""
    whatsapp_bridge_token: str = ""
    whatsapp_bridge_timeout_seconds: float = Field(default=15.0, gt=0.0, le=120.0)

    # Phase 3 — webhook/operator authentication. Shared secrets compared in
    # constant time. Empty secret/token = every request rejected in
    # production (fail closed); development allows (documented dev bypass).
    # These are boundaries, not an identity system: no per-operator
    # identity, roles, or audit trail yet.
    whatsapp_webhook_secret: str = ""
    operator_api_token: str = ""

    # Outbox recovery: a 'sending' row older than this many seconds is
    # crashed-mid-send; it is reconciled to 'unknown' (never resent).
    outbox_stale_sending_seconds: float = Field(default=60.0, gt=0.0, le=3600.0)

    # Bounded agent context: the agent replay input holds at most the
    # newest N transcript messages (tool-call/result pairing preserved;
    # the full transcript stays persisted untouched for audit).
    agent_transcript_max_messages: int = Field(default=200, ge=10, le=1000)

    @model_validator(mode="after")
    def _production_safety(self) -> "Settings":
        if self.app_environment == "production":
            if not self.intent_classifier_enabled:
                raise ValueError(
                    "INTENT_CLASSIFIER_ENABLED=false is not allowed in production; "
                    "the intent gate must never be bypassed there"
                )
            if self.whatsapp_bridge_url and not self.whatsapp_bridge_token:
                raise ValueError(
                    "WHATSAPP_BRIDGE_TOKEN is required when WHATSAPP_BRIDGE_URL "
                    "is set (production never sends unauthenticated bridge requests)"
                )
        return self


settings = Settings()
