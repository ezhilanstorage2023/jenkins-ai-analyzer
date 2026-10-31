from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
    )

    groq_api_key: SecretStr | None = None
    llm_model: str = "openai/gpt-oss-120b"

    # Extra attempts after the first when LLM output fails to parse.
    llm_parse_retries: int = 2

    max_validation_attempts: int = 3
    parser_max_characters: int = 10000
    max_upload_bytes: int = 20_000_000

    cors_origins: list[str] = [
        "http://127.0.0.1:5500",
        "http://localhost:5500",
    ]

    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
