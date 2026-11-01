from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr, model_validator
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

    # Per-agent model overrides; unset means llm_model.
    llm_model_detector: str | None = None
    llm_model_root_cause: str | None = None
    llm_model_validator: str | None = None

    # "low" | "medium" | "high" (gpt-oss, qwen3.8) or "none" (qwen3.x);
    # unset = the provider's default for the model.
    llm_reasoning_effort: str | None = None

    # Extra attempts after the first when LLM output fails to parse.
    llm_parse_retries: int = 2

    # On HTTP 429, wait the provider's retry-after (capped) and try again.
    llm_rate_limit_retries: int = 2
    llm_rate_limit_max_wait_seconds: float = 30.0

    # Graph shape. Defaults keep the original 3-agent behaviour; flip only
    # after the benchmark (backend/eval) shows no accuracy loss.
    llm_merge_detector: bool = False
    validator_checks_fix: bool = True

    def model_for(self, agent: str) -> str:
        """Model for 'detector' | 'root_cause' | 'validator'."""

        return getattr(self, f"llm_model_{agent}") or self.llm_model

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

    # ---- P3: DevOps owner notification (shadow only: nothing is ever sent) --
    # off: no preview; shadow: record a "would send" preview; auto: also send
    # it by SMTP (needs FROM_EMAIL_ID and SMTP_PASSWORD, checked at startup).
    mode_devops: Literal["off", "shadow", "auto"] = "shadow"

    # Sender mailbox and its SMTP login. For Gmail the password must be an App
    # Password (Google Account > Security > 2-Step Verification > App passwords).
    from_email_id: str | None = None
    smtp_password: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "SMTP_PASSWORD", "FROM_EMAIL_PASSWORD", "GMAIL_APP_PASSWORD"
        ),
    )
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    # Login name; defaults to from_email_id.
    smtp_username: str | None = None
    smtp_starttls: bool = True      # port 587
    smtp_use_ssl: bool = False      # port 465 (implicit TLS); then starttls is unused
    smtp_timeout_seconds: float = 30.0

    # fnmatch job pattern -> recipients, first match wins, e.g.
    # OWNER_MAP='{"team-a/*": ["a@example.com"], "*": ["fallback@example.com"]}'
    owner_map: dict[str, list[str]] = {}

    # Optional: a job parameter whose default value holds the owner address.
    owner_parameter_name: str | None = None

    # Recipients from owner_map / the parameter must be in these domains. The
    # explicit owner_email_override is the operator's own choice and is not
    # domain-checked. Empty = no domain is accepted.
    allowed_email_domains: list[str] = []

    # One mail per (job, failure signature) per window.
    notify_rate_window_hours: int = 6

    # Base URL of the review UI, for the link in the email (omitted if unset).
    review_ui_url: str | None = None

    @model_validator(mode="after")
    def _auto_needs_a_sender(self):
        if self.mode_devops == "auto" and not (
            self.from_email_id and self.smtp_password
        ):
            raise ValueError(
                "MODE_DEVOPS=auto needs FROM_EMAIL_ID and SMTP_PASSWORD "
                "(for Gmail: an App Password) in backend/.env"
            )

        return self

    # fnmatch patterns of jobs eligible for an infra re-run.
    rerun_job_allowlist: list[str] = []

    # Per-category mode. off: nothing proposed; shadow: proposals recorded,
    # never executed; auto: allowed proposals are executed (writes to Jenkins).
    mode_infra: Literal["off", "shadow", "auto"] = "shadow"

    # Hard stop for every executor. A runtime toggle is also kept in the
    # database (api /controls); either one stops execution.
    kill_switch: bool = False

    # Automatic re-runs allowed per build lineage (a re-run of a re-run
    # counts against the same budget).
    rerun_budget: int = 1
    # Also at most this many live re-runs per *job* inside this window, even
    # if the lineage link is lost (queue item never resolved).
    rerun_job_window_hours: int = 6
    rerun_queue_timeout_seconds: float = 60.0
    rerun_queue_poll_seconds: float = 2.0
    # An execution stuck "triggering" this long is presumed to have crashed.
    execution_lease_seconds: int = 300
    # How often the worker re-checks a rebuilt build (read-only), and how long
    # it keeps following one before giving up as "unknown".
    rebuild_poll_seconds: int = 20
    rebuild_track_hours: int = 24

    # Job paths whose failed builds can be browsed and classified from the UI.
    jenkins_jobs: list[str] = []

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
