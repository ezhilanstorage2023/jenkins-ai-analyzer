"""Browse failed Jenkins builds and human-trigger shadow classification."""

import pytest
from fastapi.testclient import TestClient

import main
from api import review
from clients.jenkins import JenkinsClient
from settings import get_settings
from store.db import session_scope, utcnow
from store.models import Event
from tests.fakes import FakeJenkins

client = TestClient(main.app)

JENKINS_URL = "https://ci.example.com"
JOB = "acme/build"
JOB_LIST_PATH = "/job/acme/job/build/api/json"


@pytest.fixture
def jobs_configured(monkeypatch):
    monkeypatch.setenv("JENKINS_URL", JENKINS_URL)
    monkeypatch.setenv("JENKINS_JOBS", '["acme/build"]')
    get_settings.cache_clear()


def _override_jenkins(builds=None, status=200):
    body = {"builds": builds} if builds is not None else b"boom"
    fake = FakeJenkins({JOB_LIST_PATH: (status, body)})
    fake_client = JenkinsClient(
        JENKINS_URL, "svc", "tok",
        timeout=5, max_log_bytes=5_000_000, transport=fake.transport,
    )
    fake_client.fake = fake
    main.app.dependency_overrides[review.get_jenkins] = lambda: fake_client
    return fake_client


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    main.app.dependency_overrides.pop(review.get_jenkins, None)


# ---- failed-builds -------------------------------------------------------

def test_failed_builds_requires_auth(jobs_configured):
    assert client.get("/api/jenkins/failed-builds").status_code == 401


def test_failed_builds_lists_only_failures(jobs_configured, viewer):
    _override_jenkins(builds=[
        {"number": 9, "url": "x", "result": "FAILURE", "timestamp": 1700000000000},
        {"number": 8, "url": "x", "result": "SUCCESS"},
        {"number": 7, "url": "x", "result": "FAILURE"},
    ])

    rows = client.get("/api/jenkins/failed-builds", auth=viewer).json()

    assert [r["build_number"] for r in rows] == [9, 7]
    assert all(r["result"] == "FAILURE" for r in rows)
    assert rows[0]["event_id"] is None
    assert rows[0]["timestamp"] == 1700000000000
    assert rows[0]["build_url"] == "https://ci.example.com/job/acme/job/build/9/"


def test_failed_builds_marks_existing_event(jobs_configured, viewer):
    _override_jenkins(builds=[{"number": 7, "url": "x", "result": "FAILURE"}])

    with session_scope() as session:
        session.add(Event(
            controller_id="default", job=JOB, build_number=7,
            build_url="https://ci.example.com/job/acme/job/build/7/",
            result="FAILURE", source="manual", status="queued",
            next_attempt_at=utcnow(), received_at=utcnow(),
        ))

    rows = client.get("/api/jenkins/failed-builds", auth=viewer).json()

    assert rows[0]["event_id"] is not None
    assert rows[0]["event_status"] == "queued"


def test_failed_builds_jenkins_error_is_503(jobs_configured, viewer):
    _override_jenkins(status=503)

    assert client.get("/api/jenkins/failed-builds", auth=viewer).status_code == 503


def test_failed_builds_no_jobs_configured(monkeypatch, viewer):
    monkeypatch.setenv("JENKINS_URL", JENKINS_URL)
    monkeypatch.delenv("JENKINS_JOBS", raising=False)
    get_settings.cache_clear()
    _override_jenkins(builds=[])

    assert client.get("/api/jenkins/failed-builds", auth=viewer).status_code == 409


def test_failed_builds_is_read_only(jobs_configured, viewer):
    fake_client = _override_jenkins(builds=[
        {"number": 7, "url": "x", "result": "FAILURE"},
    ])

    client.get("/api/jenkins/failed-builds", auth=viewer)

    assert fake_client.fake.seen
    assert all(req.method == "GET" for req in fake_client.fake.seen)


# ---- analyze (enqueue) ---------------------------------------------------

def test_analyze_requires_operator(jobs_configured, viewer, labeler):
    body = {"job": JOB, "build_number": 7}

    assert client.post("/api/jenkins/analyze", json=body, auth=viewer).status_code == 403
    assert client.post("/api/jenkins/analyze", json=body, auth=labeler).status_code == 403


def test_analyze_enqueues_manual_event(jobs_configured, operator):
    response = client.post(
        "/api/jenkins/analyze", json={"job": JOB, "build_number": 7}, auth=operator
    )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "queued"

    with session_scope() as session:
        event = session.get(Event, data["event_id"])
        assert event.source == "manual"
        assert event.status == "queued"
        assert event.build_url == "https://ci.example.com/job/acme/job/build/7/"


def test_analyze_is_idempotent(jobs_configured, operator):
    first = client.post(
        "/api/jenkins/analyze", json={"job": JOB, "build_number": 7}, auth=operator
    ).json()
    second = client.post(
        "/api/jenkins/analyze", json={"job": JOB, "build_number": 7}, auth=operator
    ).json()

    assert first["event_id"] == second["event_id"]

    with session_scope() as session:
        count = session.query(Event).filter_by(job=JOB, build_number=7).count()
        assert count == 1


def test_analyze_rejects_unconfigured_job(jobs_configured, operator):
    response = client.post(
        "/api/jenkins/analyze",
        json={"job": "other/job", "build_number": 7},
        auth=operator,
    )

    assert response.status_code == 422
