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


WEBHOOK_TOKEN = "t0k"
JENKINS_URL = "https://ci.example.com"


@pytest.fixture
def webhook_env(monkeypatch):
    monkeypatch.setenv("JENKINS_WEBHOOK_TOKEN", WEBHOOK_TOKEN)
    monkeypatch.setenv("JENKINS_URL", JENKINS_URL)
    get_settings.cache_clear()


from pathlib import Path  # noqa: E402

from clients.jenkins import JenkinsClient  # noqa: E402
from store.db import session_scope, utcnow  # noqa: E402
from store.models import Event  # noqa: E402
from tests.fakes import FakeJenkins  # noqa: E402

FIXTURE_LOGS = Path(__file__).parent / "fixtures" / "logs"
BUILD_URL = "https://ci.example.com/job/acme/job/build/7/"
BUILD_PATH = "/job/acme/job/build/7/"
COMPILATION_LOG = (FIXTURE_LOGS / "compilation_java_symbol.log").read_text()


def make_jenkins(console=COMPILATION_LOG, console_status=200):
    fake = FakeJenkins({
        BUILD_PATH + "api/json": (200, {"result": "FAILURE", "actions": [
            {"parameters": [
                {"name": "GERRIT_CHANGE_NUMBER", "value": "48213"},
                {"name": "GERRIT_PATCHSET_NUMBER", "value": "2"},
            ]},
            {"causes": [{"shortDescription": "Triggered by Gerrit"}]},
        ]}),
        BUILD_PATH + "consoleText": (console_status, console),
    })
    client = JenkinsClient(
        JENKINS_URL, "svc", "tok",
        timeout=5, max_log_bytes=5_000_000, transport=fake.transport,
    )
    client.fake = fake
    return client


@pytest.fixture
def jenkins():
    return make_jenkins()


@pytest.fixture
def broken_jenkins():
    return make_jenkins(console="busy", console_status=503)


@pytest.fixture
def queued_event():
    with session_scope() as session:
        event = Event(
            controller_id="default",
            job="acme/build",
            build_number=7,
            build_url=BUILD_URL,
            result="FAILURE",
            source="webhook",
            next_attempt_at=utcnow(),
            received_at=utcnow(),
        )
        session.add(event)
        session.flush()
        return event.id
