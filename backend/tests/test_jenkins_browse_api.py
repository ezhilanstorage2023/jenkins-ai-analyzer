"""Browse failed Jenkins builds and human-trigger shadow classification."""

import time

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

DAY_MS = 86_400_000


def ago_ms(minutes=0, days=0):
    """Epoch ms for a time `days`/`minutes` in the past (recent unless told)."""

    return int(time.time() * 1000) - days * DAY_MS - minutes * 60_000

JOB_LIST_PATH = "/job/acme/job/build/api/json"


@pytest.fixture
def jobs_configured(monkeypatch):
    monkeypatch.setenv("JENKINS_URL", JENKINS_URL)
    monkeypatch.setenv("JENKINS_JOBS", '["acme/build"]')
    get_settings.cache_clear()
    _override_jenkins(builds=[])


@pytest.fixture
def discovery(monkeypatch):
    """No JENKINS_JOBS allowlist: every failing job is discoverable."""

    monkeypatch.setenv("JENKINS_URL", JENKINS_URL)
    monkeypatch.delenv("JENKINS_JOBS", raising=False)
    get_settings.cache_clear()


def _override_jenkins(builds=None, status=200, extra=None):
    body = {"builds": builds} if builds is not None else b"boom"
    routes = {JOB_LIST_PATH: (status, body)}
    routes.update(extra or {})
    fake = FakeJenkins(routes)
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
    RECENT_TS = ago_ms(minutes=5)
    _override_jenkins(builds=[
        {"number": 9, "url": "x", "result": "FAILURE", "timestamp": RECENT_TS},
        {"number": 8, "url": "x", "result": "SUCCESS"},
        {"number": 7, "url": "x", "result": "FAILURE"},
    ])

    rows = client.get("/api/jenkins/failed-builds", auth=viewer).json()

    assert [r["build_number"] for r in rows] == [9, 7]
    assert all(r["result"] == "FAILURE" for r in rows)
    assert rows[0]["event_id"] is None
    assert rows[0]["timestamp"] == RECENT_TS
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


def _job(name, result, number, timestamp, color="red"):
    last = {"number": number, "result": result, "timestamp": timestamp}
    return {"fullName": name, "color": color, "lastCompletedBuild": last}


ROOT = {
    "jobs": [
        _job("old-fail", "FAILURE", 4, ago_ms(minutes=40)),
        _job("new-fail", "FAILURE", 9, ago_ms(minutes=10)),
        _job("fine", "SUCCESS", 2, ago_ms(minutes=8), color="blue"),
        _job("unstable", "UNSTABLE", 1, ago_ms(minutes=6), color="yellow"),
        _job("never-built", None, None, None, color="notbuilt"),
        _job("off", "FAILURE", 6, ago_ms(minutes=30), color="disabled"),
        {"fullName": "team", "jobs": [_job("team/nested", "FAILURE", 3, ago_ms(minutes=20))]},
    ]
}


def test_discovery_lists_every_failing_job_newest_first(discovery, viewer):
    _override_jenkins(extra={"/api/json": (200, ROOT)})

    rows = client.get("/api/jenkins/failed-builds", auth=viewer).json()

    assert [r["job"] for r in rows] == ["new-fail", "team/nested", "old-fail"]
    assert rows[0]["build_number"] == 9
    assert rows[1]["build_url"] == "https://ci.example.com/job/team/job/nested/3/"


def test_discovery_include_disabled(discovery, viewer):
    _override_jenkins(extra={"/api/json": (200, ROOT)})

    rows = client.get(
        "/api/jenkins/failed-builds?include_disabled=true", auth=viewer
    ).json()

    assert "off" in [r["job"] for r in rows]


def test_discovery_count_limits_rows(discovery, viewer):
    _override_jenkins(extra={"/api/json": (200, ROOT)})

    rows = client.get("/api/jenkins/failed-builds?count=1", auth=viewer).json()

    assert [r["job"] for r in rows] == ["new-fail"]


def test_discovery_marks_existing_event(discovery, viewer):
    _override_jenkins(extra={"/api/json": (200, ROOT)})

    with session_scope() as session:
        session.add(Event(
            controller_id="default", job="new-fail", build_number=9,
            build_url="https://ci.example.com/job/new-fail/9/",
            result="FAILURE", source="manual", status="queued",
            next_attempt_at=utcnow(), received_at=utcnow(),
        ))

    rows = client.get("/api/jenkins/failed-builds", auth=viewer).json()
    by_job = {r["job"]: r for r in rows}

    assert by_job["new-fail"]["event_status"] == "queued"
    assert by_job["old-fail"]["event_id"] is None


def test_discovery_jenkins_error_is_503(discovery, viewer):
    _override_jenkins(extra={"/api/json": (503, b"down")})

    assert client.get("/api/jenkins/failed-builds", auth=viewer).status_code == 503


def test_discovery_is_read_only(discovery, viewer):
    fake_client = _override_jenkins(extra={"/api/json": (200, ROOT)})

    client.get("/api/jenkins/failed-builds", auth=viewer)

    assert fake_client.fake.seen
    assert all(req.method == "GET" for req in fake_client.fake.seen)


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


# ---- analyze without an allowlist ---------------------------------------

BUILD_PATH = "/job/any/job/job/7/api/json"


def test_analyze_discovery_enqueues_failed_build(discovery, operator):
    _override_jenkins(extra={BUILD_PATH: (200, {"result": "FAILURE", "actions": []})})

    response = client.post(
        "/api/jenkins/analyze", json={"job": "any/job", "build_number": 7}, auth=operator
    )

    assert response.status_code == 200
    assert response.json()["status"] == "queued"


def test_analyze_discovery_rejects_build_that_did_not_fail(discovery, operator):
    _override_jenkins(extra={BUILD_PATH: (200, {"result": "SUCCESS", "actions": []})})

    response = client.post(
        "/api/jenkins/analyze", json={"job": "any/job", "build_number": 7}, auth=operator
    )

    assert response.status_code == 422


def test_analyze_discovery_unknown_build_is_422(discovery, operator):
    _override_jenkins()

    response = client.post(
        "/api/jenkins/analyze", json={"job": "any/job", "build_number": 7}, auth=operator
    )

    assert response.status_code == 422


def test_analyze_discovery_jenkins_outage_is_503(discovery, operator):
    _override_jenkins(extra={BUILD_PATH: (500, b"boom")})

    response = client.post(
        "/api/jenkins/analyze", json={"job": "any/job", "build_number": 7}, auth=operator
    )

    assert response.status_code == 503


@pytest.mark.parametrize("job", ["../x", "a/../b", "a//b", "/a", "."])
def test_analyze_rejects_bad_job_paths(discovery, operator, job):
    _override_jenkins()

    response = client.post(
        "/api/jenkins/analyze", json={"job": job, "build_number": 7}, auth=operator
    )

    assert response.status_code == 422


# ---- age filter, sort, paging ---------------------------------------------

AGED = {
    "jobs": [
        _job("fresh", "FAILURE", 3, ago_ms(minutes=5)),
        _job("week-old", "FAILURE", 4, ago_ms(days=7)),
        _job("ancient-b", "FAILURE", 5, ago_ms(days=200)),
        _job("ancient-a", "FAILURE", 6, ago_ms(days=100)),
        {"fullName": "unknown-time", "color": "red",
         "lastCompletedBuild": {"number": 7, "result": "FAILURE"}},
    ]
}


def _jobs_of(path, viewer):
    response = client.get(path, auth=viewer)
    assert response.status_code == 200
    return [r["job"] for r in response.json()], response


def test_default_age_filter_is_30_days_and_keeps_unknown_times(discovery, viewer):
    _override_jenkins(extra={"/api/json": (200, AGED)})

    jobs, response = _jobs_of("/api/jenkins/failed-builds", viewer)

    assert set(jobs) == {"fresh", "week-old", "unknown-time"}
    assert response.headers["X-Total-Count"] == "3"


def test_max_age_days_zero_means_no_limit(discovery, viewer):
    _override_jenkins(extra={"/api/json": (200, AGED)})

    jobs, response = _jobs_of("/api/jenkins/failed-builds?max_age_days=0", viewer)

    assert len(jobs) == 5 and response.headers["X-Total-Count"] == "5"


def test_max_age_days_custom_window(discovery, viewer):
    _override_jenkins(extra={"/api/json": (200, AGED)})

    jobs, _ = _jobs_of("/api/jenkins/failed-builds?max_age_days=120", viewer)

    assert set(jobs) == {"fresh", "week-old", "ancient-a", "unknown-time"}


def test_sort_by_job_name(discovery, viewer):
    _override_jenkins(extra={"/api/json": (200, AGED)})

    jobs, _ = _jobs_of("/api/jenkins/failed-builds?max_age_days=0&sort=job", viewer)

    assert jobs == ["ancient-a", "ancient-b", "fresh", "unknown-time", "week-old"]


def test_paging_reports_total_of_the_whole_set(discovery, viewer):
    _override_jenkins(extra={"/api/json": (200, AGED)})
    base = "/api/jenkins/failed-builds?max_age_days=0&sort=job&count=2"

    first, r1 = _jobs_of(base, viewer)
    second, r2 = _jobs_of(base + "&offset=2", viewer)
    last, _ = _jobs_of(base + "&offset=4", viewer)
    beyond, _ = _jobs_of(base + "&offset=9", viewer)

    assert first == ["ancient-a", "ancient-b"] and second == ["fresh", "unknown-time"]
    assert last == ["week-old"] and beyond == []
    assert r1.headers["X-Total-Count"] == r2.headers["X-Total-Count"] == "5"


def test_q_filters_by_job_name_before_paging(discovery, viewer):
    _override_jenkins(extra={"/api/json": (200, AGED)})

    jobs, response = _jobs_of("/api/jenkins/failed-builds?max_age_days=0&q=ANCIENT&count=1", viewer)

    assert len(jobs) == 1 and jobs[0].startswith("ancient")
    assert response.headers["X-Total-Count"] == "2"   # total is of the filtered set


def test_q_blank_matches_everything(discovery, viewer):
    _override_jenkins(extra={"/api/json": (200, AGED)})

    jobs, _ = _jobs_of("/api/jenkins/failed-builds?max_age_days=0&q=%20", viewer)

    assert len(jobs) == 5


def test_allowlist_mode_applies_age_filter_and_total(jobs_configured, viewer):
    _override_jenkins(builds=[
        {"number": 9, "url": "x", "result": "FAILURE", "timestamp": ago_ms(minutes=5)},
        {"number": 8, "url": "x", "result": "FAILURE", "timestamp": ago_ms(days=90)},
    ])

    jobs, response = _jobs_of("/api/jenkins/failed-builds", viewer)

    assert len(jobs) == 1 and response.headers["X-Total-Count"] == "1"


def test_bad_paging_params_are_422(discovery, viewer):
    _override_jenkins(extra={"/api/json": (200, AGED)})

    for query in ("offset=-1", "max_age_days=-1", "sort=bogus", "count=0"):
        assert client.get(f"/api/jenkins/failed-builds?{query}", auth=viewer).status_code == 422


# ---- analyze-batch --------------------------------------------------------

def _build_route(job_path, result):
    return {f"/job/{job_path}/7/api/json": (200, {"result": result, "actions": []})}


def test_batch_requires_operator(discovery, viewer, labeler):
    body = {"builds": [{"job": "a", "build_number": 7}]}

    assert client.post("/api/jenkins/analyze-batch", json=body, auth=viewer).status_code == 403
    assert client.post("/api/jenkins/analyze-batch", json=body, auth=labeler).status_code == 403


def test_batch_reports_each_build_independently(discovery, operator):
    extra = {}
    extra.update(_build_route("good", "FAILURE"))
    extra.update(_build_route("passed", "SUCCESS"))
    _override_jenkins(extra=extra)
    body = {"builds": [
        {"job": "good", "build_number": 7},
        {"job": "passed", "build_number": 7},
        {"job": "../x", "build_number": 7},
        {"job": "missing", "build_number": 7},
    ]}

    results = client.post("/api/jenkins/analyze-batch", json=body, auth=operator).json()

    assert [r["job"] for r in results] == ["good", "passed", "../x", "missing"]
    assert results[0]["status"] == "queued" and "error" not in results[0]
    assert [r["status_code"] for r in results[1:]] == [422, 422, 422]

    with session_scope() as session:
        assert session.query(Event).count() == 1   # only the failed build queued


def test_batch_is_idempotent_with_single_analyze(discovery, operator):
    _override_jenkins(extra=_build_route("good", "FAILURE"))
    single = client.post(
        "/api/jenkins/analyze", json={"job": "good", "build_number": 7}, auth=operator
    ).json()

    batch = client.post(
        "/api/jenkins/analyze-batch",
        json={"builds": [{"job": "good", "build_number": 7}]}, auth=operator,
    ).json()

    assert batch[0]["event_id"] == single["event_id"]


def test_batch_stops_after_jenkins_outage(discovery, operator):
    fake_client = _override_jenkins(extra={
        "/job/a/7/api/json": (500, b"boom"),
        **_build_route("b", "FAILURE"),
    })
    body = {"builds": [{"job": "a", "build_number": 7}, {"job": "b", "build_number": 7}]}

    results = client.post("/api/jenkins/analyze-batch", json=body, auth=operator).json()

    assert [r["status_code"] for r in results] == [503, 503]
    assert len(fake_client.fake.seen) == 1       # second build never contacted Jenkins


@pytest.mark.parametrize("body", [
    {"builds": []},
    {"builds": [{"job": "a", "build_number": 7}] * 51},
    {"builds": [{"job": "a", "build_number": 0}]},
    {},
])
def test_batch_validates_body(discovery, operator, body):
    _override_jenkins()

    assert client.post("/api/jenkins/analyze-batch", json=body, auth=operator).status_code == 422


def test_batch_with_allowlist_rejects_other_jobs(jobs_configured, operator):
    response = client.post(
        "/api/jenkins/analyze-batch",
        json={"builds": [{"job": "other/job", "build_number": 7}]}, auth=operator,
    ).json()

    assert response[0]["status_code"] == 422


# ---- /api/me and events by id ---------------------------------------------

def test_me_requires_auth_and_reports_role(viewer, operator):
    assert client.get("/api/me").status_code == 401
    assert client.get("/api/me", auth=viewer).json()["role"] == "viewer"
    assert client.get("/api/me", auth=operator).json()["role"] == "operator"


def _add_events(count):
    with session_scope() as session:
        for number in range(1, count + 1):
            session.add(Event(
                controller_id="default", job=f"j{number}", build_number=number,
                build_url=f"https://ci.example.com/job/j{number}/{number}/",
                result="FAILURE", source="manual", status="queued",
                next_attempt_at=utcnow(), received_at=utcnow(),
            ))

        session.flush()
        return [event.id for event in session.query(Event).order_by(Event.id)]


def test_events_by_ids_returns_only_those_with_status_and_error(viewer):
    ids = _add_events(4)

    rows = client.get(f"/api/events?ids={ids[1]},{ids[3]}", auth=viewer).json()

    assert sorted(r["id"] for r in rows) == [ids[1], ids[3]]
    assert {"status", "attempts", "last_error"} <= set(rows[0])


@pytest.mark.parametrize("ids", ["abc", "1,x", "1;2"])
def test_events_ids_must_be_integers(viewer, ids):
    assert client.get(f"/api/events?ids={ids}", auth=viewer).status_code == 422


def test_events_ids_empty_returns_nothing(viewer):
    _add_events(2)

    assert client.get("/api/events?ids=", auth=viewer).json() == []


def test_events_ids_cap(viewer):
    ids = ",".join(str(n) for n in range(1, 202))

    assert client.get(f"/api/events?ids={ids}", auth=viewer).status_code == 422
