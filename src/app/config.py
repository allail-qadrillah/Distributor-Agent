from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    app_environment: Literal["development", "production"] = "production"
    log_level: str = "INFO"
    supabase_db_url: str = ""
    openai_api_key: str = ""
    openai_model: str = ""
    openai_base_url: str = "https://api.surplusintelligence.ai/v1"

    intent_classifier_enabled: bool = True
    intent_classifier_base_url: str = ""
    intent_classifier_api_key: str = ""
    intent_classifier_model: str = ""
    intent_classifier_confidence_threshold: float = Field(default=0.85, ge=0.0, le=1.0)
    intent_classifier_timeout_seconds: float = Field(default=5.0, gt=0.0, le=60.0)
    intent_classifier_max_context_messages: int = Field(default=10, ge=0, le=100)

    whatsapp_bridge_url: str = ""
    whatsapp_bridge_token: str = ""
    whatsapp_bridge_timeout_seconds: float = Field(default=15.0, gt=0.0, le=120.0)

    whatsapp_webhook_secret: str = ""
    operator_api_token: str = ""

    outbox_stale_sending_seconds: float = Field(default=60.0, gt=0.0, le=3600.0)
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
