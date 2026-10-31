import pytest

from settings import Settings, get_settings


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch):
    # Never pick up a developer's real backend/.env or exported key.
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    get_settings.cache_clear()

    yield

    get_settings.cache_clear()
