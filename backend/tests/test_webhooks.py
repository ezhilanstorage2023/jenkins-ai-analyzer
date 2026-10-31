from fastapi.testclient import TestClient

import main
from settings import get_settings
from store.db import session_scope
from store.models import Event

client = TestClient(main.app)

HOOK = {
    "job": "acme/build",
    "build_number": 7,
    "build_url": "https://ci.example.com/job/acme/job/build/7/",
    "result": "FAILURE",
}


def post(body, token="t0k"):
    headers = {"X-Webhook-Token": token} if token else {}
    return client.post("/webhooks/jenkins", json=body, headers=headers)


def test_new_failure_queued(webhook_env):
    response = post(HOOK)

    assert response.status_code == 202
    assert response.json()["duplicate"] is False

    with session_scope() as session:
        event = session.query(Event).one()
        assert (event.status, event.source, event.controller_id) == (
            "queued", "webhook", "default"
        )


def test_duplicate_webhook_returns_existing_event(webhook_env):
    first = post(HOOK).json()["event_id"]

    response = post(HOOK)

    assert response.status_code == 200
    assert response.json() == {"event_id": first, "duplicate": True}


def test_webhook_token_required(webhook_env):
    assert post(HOOK, token=None).status_code == 401
    assert post(HOOK, token="wrong").status_code == 401


def test_webhook_disabled_without_configured_token(monkeypatch):
    monkeypatch.setenv("JENKINS_URL", "https://ci.example.com")
    get_settings.cache_clear()

    assert post(HOOK).status_code == 503


def test_build_url_outside_jenkins_rejected(webhook_env):
    response = post(HOOK | {"build_url": "http://169.254.169.254/latest/"})

    assert response.status_code == 422


def test_build_url_prefix_trick_rejected(webhook_env):
    # Host "ci.example.com.evil.net" must not pass a naive prefix check.
    response = post(HOOK | {"build_url": "https://ci.example.com.evil.net/job/x/1/"})

    assert response.status_code == 422


def test_non_failure_result_ignored(webhook_env):
    response = post(HOOK | {"result": "SUCCESS"})

    assert response.status_code == 200
    assert response.json() == {"status": "ignored"}


import pytest  # noqa: E402

from utils.urls import url_under_base  # noqa: E402


@pytest.mark.parametrize("url, base, expected", [
    ("https://ci.example.com/job/a/1/", "https://ci.example.com/", True),
    ("https://CI.example.com/job/a/1/", "https://ci.example.com/", True),
    ("http://ci.example.com/job/a/1/", "https://ci.example.com/", False),
    ("https://ci.example.com.evil.net/job/a/1/", "https://ci.example.com/", False),
    ("https://ci.example.com@evil.net/job/a/1/", "https://ci.example.com/", False),
    ("https://ci.example.com/jenkins/job/a/1/", "https://ci.example.com/jenkins/", True),
    ("https://ci.example.com/jenkinsx/job/a/", "https://ci.example.com/jenkins/", False),
    ("https://ci.example.com/jenkins/../admin/", "https://ci.example.com/jenkins/", False),
    ("file:///etc/passwd", "https://ci.example.com/", False),
])
def test_url_under_base(url, base, expected):
    assert url_under_base(url, base) is expected
