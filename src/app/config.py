from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    supabase_db_url: str = ""  # postgres connection string, plain Postgres
    # ponytail: phase 2 — unused until agent layer exists; default empty
    openai_api_key: str = ""


settings = Settings()
