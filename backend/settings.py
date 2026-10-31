from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # Anchored to backend/, independent of the working directory.
        env_file=BACKEND_DIR / ".env",
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

    # as_posix(): forward slashes, so the URL is valid on Windows too.
    database_url: str = f"sqlite:///{(BACKEND_DIR / 'data' / 'remediation.db').as_posix()}"

    jenkins_url: str | None = None
    # Single controller in P1; webhooks for any other id are rejected.
    jenkins_controller_id: str = "default"
    jenkins_username: str | None = None
    jenkins_api_token: SecretStr | None = None
    jenkins_webhook_token: SecretStr | None = None
    jenkins_max_log_bytes: int = 5_000_000
    jenkins_timeout_seconds: float = 30.0

    # Test-period recipient for proposed DevOps notifications.
    owner_email_override: str | None = Field(
        default=None,
        validation_alias=AliasChoices("OWNER_EMAIL_OVERRIDE", "EMAIL_ID"),
    )

    # fnmatch patterns of jobs eligible for an infra re-run.
    rerun_job_allowlist: list[str] = []

    worker_poll_seconds: float = 5.0
    worker_max_attempts: int = 3
    worker_lease_seconds: int = 600
    worker_backoff_seconds: int = 60

    @property
    def jenkins_base_url(self) -> str | None:
        if not self.jenkins_url:
            return None

        return self.jenkins_url.rstrip("/") + "/"


@lru_cache
def get_settings() -> Settings:
    return Settings()
