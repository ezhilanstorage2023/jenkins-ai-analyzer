import pytest

import llm
from settings import Settings, get_settings
from store.db import reset_engine_cache
from tests.fakes import FakeLLMClient


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch):
    # Never pick up a developer's real backend/.env or exported key.
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    # Fresh in-memory database per test.
    monkeypatch.setenv("DATABASE_URL", "sqlite://")
    get_settings.cache_clear()
    llm.reset_client_cache()
    reset_engine_cache()

    yield

    get_settings.cache_clear()
    llm.reset_client_cache()
    reset_engine_cache()


@pytest.fixture
def fake_llm(monkeypatch):
    def make(responses):
        fake = FakeLLMClient(responses)
        monkeypatch.setattr(llm, "get_llm_client", lambda: fake)
        return fake

    return make
