import asyncio

from fastapi.testclient import TestClient

import main
from settings import get_settings
from tests.fakes import detected_json, root_cause_json, verdict_json

client = TestClient(main.app)

LOG = b"Compiling\n[javac] Foo.java:3: error: cannot find symbol\nBUILD FAILED"
EVID = "[Line 2] [javac] Foo.java:3: error: cannot find symbol"


def post(body):
    return client.post(
        "/analyze-log",
        files={"file": ("b.log", body, "text/plain")}
    )


def test_happy_path_200(fake_llm):
    fake_llm([
        detected_json([EVID]),
        root_cause_json("compilation", [EVID]),
        verdict_json(True),
    ])

    response = post(LOG)

    assert response.status_code == 200
    assert response.json()["error_category"] == "compilation"


def test_garbage_llm_output_returns_200_needs_triage(fake_llm):
    fake_llm(["garbage"] * 3)

    response = post(LOG)

    assert response.status_code == 200
    assert response.json()["error_category"] == "needs_triage"


def test_missing_key_returns_503():
    response = post(LOG)

    assert response.status_code == 503
    assert "GROQ_API_KEY" in response.json()["detail"]


def test_upload_over_limit_returns_413(monkeypatch):
    monkeypatch.setenv("MAX_UPLOAD_BYTES", "10")
    get_settings.cache_clear()

    assert post(b"x" * 11).status_code == 413


def test_non_utf8_upload_does_not_500(fake_llm):
    fake_llm([])

    response = post(b"\xff\xfe binary \x00 junk")

    assert response.status_code == 200
    assert response.json()["error_category"] == "needs_triage"


def test_analysis_runs_off_event_loop(monkeypatch):
    seen = {}

    def fake_analyze(text):
        try:
            asyncio.get_running_loop()
            seen["in_loop"] = True
        except RuntimeError:
            seen["in_loop"] = False

        return {}

    monkeypatch.setattr(main, "analyze_log", fake_analyze)

    assert post(LOG).status_code == 200
    assert seen["in_loop"] is False


def test_test_llm_endpoint_removed():
    assert client.get("/test-llm").status_code == 404
