from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    supabase_db_url: str = ""  # postgres connection string, plain Postgres
    # Phase 2 — Surplus Intelligence marketplace, OpenAI-compatible
    # chat completions (api.surplusintelligence.ai). Variable names kept
    # per project owner; empty = agent layer raises a clear config error.
    openai_api_key: str = ""
    openai_model: str = ""  # e.g. "deepseek-v4.1-flash-1017" (Surplus catalog id)
    openai_base_url: str = "https://api.surplusintelligence.ai/v1"


settings = Settings()
