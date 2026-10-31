from pathlib import Path

import settings as settings_module
from settings import Settings, get_settings

# Captured at import, before the autouse fixture disables .env loading.
ENV_FILE_DEFAULT = Settings.model_config["env_file"]


def test_defaults():
    s = get_settings()
    assert s.llm_model == "openai/gpt-oss-120b"
    assert s.max_validation_attempts == 3
    assert s.parser_max_characters == 10000
    assert s.groq_api_key is None


def test_env_override(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    monkeypatch.setenv("MAX_UPLOAD_BYTES", "100")
    get_settings.cache_clear()
    s = get_settings()
    assert s.groq_api_key.get_secret_value() == "gsk_test"
    assert "gsk_test" not in repr(s)
    assert s.max_upload_bytes == 100


def test_env_file_anchored_to_backend_dir():
    assert Path(ENV_FILE_DEFAULT) == Path(settings_module.__file__).parent / ".env"


def test_email_id_alias_sets_owner_override(monkeypatch):
    monkeypatch.setenv("EMAIL_ID", "owner@example.com")
    get_settings.cache_clear()

    assert get_settings().owner_email_override == "owner@example.com"


def test_jenkins_base_url_gets_trailing_slash(monkeypatch):
    monkeypatch.setenv("JENKINS_URL", "https://ci.example.com")
    get_settings.cache_clear()

    assert get_settings().jenkins_base_url == "https://ci.example.com/"


def test_default_database_is_absolute_sqlite_path(monkeypatch):
    # conftest points DATABASE_URL at in-memory SQLite; check the default.
    monkeypatch.delenv("DATABASE_URL")
    get_settings.cache_clear()

    url = get_settings().database_url
    path = url.removeprefix("sqlite:///")

    # Portable: POSIX "/home/..." or Windows "C:/..." both count as absolute.
    assert url.startswith("sqlite:///")
    assert "\\" not in url
    assert Path(path).is_absolute()
    assert path.endswith("/data/remediation.db")
