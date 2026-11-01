"""Following the builds an automatic rebuild queued, and showing how they ended.

Everything is read-only against a fake Jenkins: the tracker never writes."""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

import executor
import main
import worker
from api import review
from clients.jenkins import JenkinsClient, JenkinsError
from settings import get_settings
from store.db import session_scope, utcnow
from store.models import Event, Execution, RebuildOutcome
from tests.fakes import FakeJenkins

http = TestClient(main.app)

BASE = "https://ci.example.com"
JOB = "acme/build"
RUN_8 = "/job/acme/job/build/8/api/json"
QUEUE_55 = "/queue/item/55/api/json"
JOB_API = "/job/acme/job/build/api/json"
QUEUE_URL = f"{BASE}/queue/item/55/"
BUILD_8_URL = f"{BASE}/job/acme/job/build/8/"


def reader(routes):
    fake = FakeJenkins(routes)
    client = JenkinsClient(BASE, "svc", "tok", timeout=5, max_log_bytes=1000, transport=fake.transport)
    client.fake = fake
    return client


def run(building=False, result="SUCCESS", duration=61000):
    return (200, {"building": building, "result": result, "duration": duration})


def triggered(job=JOB, number=7, new_build=8, status="triggered", queue_id=55, age_hours=0):
    with session_scope() as session:
        event = Event(controller_id="default", job=job, build_number=number,
                      build_url=f"{BASE}/job/{job.replace('/', '/job/')}/{number}/", result="FAILURE",
                      source="webhook", status="done", next_attempt_at=utcnow(), received_at=utcnow())
        session.add(event)
        session.flush()
        when = utcnow() - timedelta(hours=age_hours)
        execution = Execution(
            event_id=event.id, action_type="rerun", status=status, reason_codes=[],
            root_event_id=event.id, queue_url=f"{BASE}/queue/item/{queue_id}/",
            new_build_number=new_build,
            new_build_url=(f"{BASE}/job/{job.replace('/', '/job/')}/{new_build}/" if new_build else None),
            created_at=when, updated_at=when,
        )
        session.add(execution)
        session.flush()
        return event.id, execution.id


def outcome(execution_id):
    with session_scope() as session:
        o = session.query(RebuildOutcome).filter_by(execution_id=execution_id).one_or_none()
        return o and {"state": o.state, "result": o.build_result, "duration": o.duration_ms,
                      "finished": o.finished_at is not None, "checked": o.checked_at}


def execution(execution_id):
    with session_scope() as session:
        e = session.get(Execution, execution_id)
        return {"number": e.new_build_number, "url": e.new_build_url, "status": e.status}


# ---- client: read-only status calls ----------------------------------------------

def test_get_run_status_reads_building_result_and_duration():
    client = reader({RUN_8: run(building=True, result=None, duration=0)})

    assert client.get_run_status(BUILD_8_URL) == (True, None, 0)

    client = reader({RUN_8: run(result="FAILURE", duration=1234)})
    assert client.get_run_status(BUILD_8_URL) == (False, "FAILURE", 1234)


@pytest.mark.parametrize("body, expected", [
    ({"executable": {"number": 8, "url": BUILD_8_URL}}, ("started", 8, BUILD_8_URL)),
    ({"executable": {"number": 8, "url": "https://evil.example.net/x/"}}, ("started", 8, None)),
    ({"cancelled": True}, ("cancelled", None, None)),
    ({"why": "Waiting for next available executor"}, ("queued", None, None)),
], ids=["started", "foreign-url-dropped", "cancelled", "queued"])
def test_queue_item_state(body, expected):
    assert reader({QUEUE_55: (200, body)}).queue_item_state(QUEUE_URL) == expected


@pytest.mark.parametrize("url", ["https://evil.example.net/queue/item/55/", f"{BASE}/job/x/1/", f"{BASE}/queue/item/55"])
def test_queue_item_state_refuses_foreign_or_odd_urls(url):
    client = reader({QUEUE_55: (200, {})})

    with pytest.raises(JenkinsError):
        client.queue_item_state(url)

    assert client.fake.seen == []


def test_find_build_by_queue_id():
    builds = {"builds": [{"number": 9, "url": f"{BASE}/job/acme/job/build/9/", "queueId": 99},
                         {"number": 8, "url": BUILD_8_URL, "queueId": 55}]}
    client = reader({JOB_API: (200, builds)})

    assert client.find_build_by_queue_id(JOB, 55) == (8, BUILD_8_URL)
    assert client.find_build_by_queue_id(JOB, 12345) is None


# ---- the tracker -------------------------------------------------------------------------

@pytest.mark.parametrize("result", ["SUCCESS", "FAILURE", "UNSTABLE", "ABORTED"])
def test_a_finished_rebuild_records_jenkins_verdict(result):
    _, ex = triggered()
    jenkins = reader({RUN_8: run(result=result, duration=61000)})

    assert executor.track_outcomes(jenkins) is True

    got = outcome(ex)
    assert (got["state"], got["result"], got["duration"], got["finished"]) == ("finished", result, 61000, True)
    assert jenkins.fake.posts() == []                       # read-only


def test_a_running_rebuild_is_recorded_as_running_without_a_result():
    _, ex = triggered()

    executor.track_outcomes(reader({RUN_8: run(building=True, result=None)}))

    got = outcome(ex)
    assert (got["state"], got["result"], got["finished"]) == ("running", None, False)


def test_running_then_finished_over_two_polls():
    _, ex = triggered()
    t0 = utcnow()
    executor.track_outcomes(reader({RUN_8: run(building=True, result=None)}), now=t0)

    executor.track_outcomes(reader({RUN_8: run(result="SUCCESS")}), now=t0 + timedelta(seconds=30))

    assert outcome(ex)["state"] == "finished" and outcome(ex)["result"] == "SUCCESS"


def test_polling_is_rate_limited():
    _, ex = triggered()
    t0 = utcnow()
    jenkins = reader({RUN_8: run(building=True, result=None)})

    executor.track_outcomes(jenkins, now=t0)
    first = len(jenkins.fake.seen)
    assert executor.track_outcomes(jenkins, now=t0 + timedelta(seconds=5)) is False
    assert len(jenkins.fake.seen) == first                  # too soon: not asked again

    assert executor.track_outcomes(jenkins, now=t0 + timedelta(seconds=25)) is True
    assert len(jenkins.fake.seen) > first


def test_a_finished_rebuild_is_never_asked_about_again():
    _, ex = triggered()
    t0 = utcnow()
    jenkins = reader({RUN_8: run(result="FAILURE")})
    executor.track_outcomes(jenkins, now=t0)
    asked = len(jenkins.fake.seen)

    assert executor.track_outcomes(jenkins, now=t0 + timedelta(hours=3)) is False
    assert len(jenkins.fake.seen) == asked


@pytest.mark.parametrize("status", ["pending", "claimed", "skipped", "failed", "uncertain", "sending"])
def test_only_triggered_executions_are_followed(status):
    triggered(status=status)
    jenkins = reader({RUN_8: run()})

    assert executor.track_outcomes(jenkins) is False and jenkins.fake.seen == []


def test_a_rebuild_still_in_the_queue_is_reported_as_queued_and_gets_linked_when_it_starts():
    event_id, ex = triggered(new_build=None)
    t0 = utcnow()

    executor.track_outcomes(reader({QUEUE_55: (200, {"why": "waiting for an executor"})}), now=t0)
    assert outcome(ex)["state"] == "queued" and execution(ex)["number"] is None

    executor.track_outcomes(reader({
        QUEUE_55: (200, {"executable": {"number": 8, "url": BUILD_8_URL}}),
        RUN_8: run(building=True, result=None),
    }), now=t0 + timedelta(seconds=30))

    assert outcome(ex)["state"] == "running"
    assert execution(ex) == {"number": 8, "url": BUILD_8_URL, "status": "triggered"}

    # Now a failure of build 8 is recognised as part of this chain (the budget).
    with session_scope() as session:
        later = Event(controller_id="default", job=JOB, build_number=8, build_url=BUILD_8_URL,
                      result="FAILURE", source="webhook", status="done",
                      next_attempt_at=utcnow(), received_at=utcnow())
        session.add(later)
        session.flush()
        assert executor.lineage_root(session, later) == event_id


def test_a_forgotten_queue_item_is_found_again_by_its_queue_id():
    _, ex = triggered(new_build=None)
    jenkins = reader({
        QUEUE_55: (404, b"gone"),
        JOB_API: (200, {"builds": [{"number": 8, "url": BUILD_8_URL, "queueId": 55}]}),
        RUN_8: run(result="SUCCESS"),
    })

    executor.track_outcomes(jenkins)

    assert execution(ex)["number"] == 8
    assert (outcome(ex)["state"], outcome(ex)["result"]) == ("finished", "SUCCESS")


def test_a_cancelled_queue_item_ends_as_cancelled():
    _, ex = triggered(new_build=None)

    executor.track_outcomes(reader({QUEUE_55: (200, {"cancelled": True})}))

    got = outcome(ex)
    assert (got["state"], got["result"], got["finished"]) == ("cancelled", None, True)


def test_jenkins_being_down_keeps_the_last_state_and_retries_later():
    _, ex = triggered()
    t0 = utcnow()
    executor.track_outcomes(reader({RUN_8: run(building=True, result=None)}), now=t0)

    executor.track_outcomes(reader({RUN_8: (503, b"down")}), now=t0 + timedelta(seconds=30))
    assert outcome(ex)["state"] == "running"                # not reset, not failed

    executor.track_outcomes(reader({RUN_8: run(result="FAILURE")}), now=t0 + timedelta(seconds=60))
    assert (outcome(ex)["state"], outcome(ex)["result"]) == ("finished", "FAILURE")


def test_an_unresolvable_rebuild_is_given_up_as_unknown_after_a_day():
    _, ex = triggered(new_build=None, age_hours=30)
    jenkins = reader({QUEUE_55: (404, b"gone"), JOB_API: (200, {"builds": []})})

    executor.track_outcomes(jenkins)

    assert outcome(ex)["state"] == "unknown"
    assert executor.track_outcomes(jenkins, now=utcnow() + timedelta(hours=1)) is False


def test_a_rebuild_that_never_ends_is_eventually_unknown():
    _, ex = triggered(age_hours=30)

    executor.track_outcomes(reader({RUN_8: run(building=True, result=None)}))

    assert outcome(ex)["state"] == "unknown"


def test_one_bad_row_does_not_stop_the_others(monkeypatch):
    _, first = triggered(job="acme/build", number=7, new_build=8)
    _, second = triggered(job="acme/other", number=3, new_build=4, queue_id=77)
    real = executor._track_one

    def flaky(jenkins, execution_id, *args):
        if execution_id == first:
            raise RuntimeError("bug")

        return real(jenkins, execution_id, *args)

    monkeypatch.setattr(executor, "_track_one", flaky)

    executor.track_outcomes(reader({"/job/acme/job/other/4/api/json": run(result="SUCCESS")}))

    assert outcome(first) is None and outcome(second)["result"] == "SUCCESS"


def test_the_worker_loop_runs_the_tracker_in_every_mode(monkeypatch):
    monkeypatch.setenv("JENKINS_URL", BASE)
    get_settings.cache_clear()
    calls = []
    monkeypatch.setattr(worker, "jenkins_client_from_settings", lambda write=False: reader({}))
    monkeypatch.setattr(worker, "run_once", lambda jenkins: False)
    monkeypatch.setattr(worker.executor, "track_outcomes", lambda jenkins: calls.append(jenkins) or False)
    monkeypatch.setattr(worker.time, "sleep", lambda seconds: (_ for _ in ()).throw(SystemExit))

    with pytest.raises(SystemExit):
        worker.main()

    assert len(calls) == 1


# ---- API and UI ---------------------------------------------------------------------------

def test_event_detail_and_list_report_the_rebuild(viewer):
    event_id, ex = triggered()
    executor.track_outcomes(reader({RUN_8: run(result="FAILURE", duration=2000)}))

    detail = http.get(f"/api/events/{event_id}", auth=viewer).json()["execution"]["rebuild"]
    summary = [e for e in http.get("/api/events", auth=viewer).json() if e["id"] == event_id][0]["rebuild"]

    for rebuild in (detail, summary):
        assert (rebuild["state"], rebuild["result"], rebuild["build_number"]) == ("finished", "FAILURE", 8)
    assert detail["duration_ms"] == 2000 and detail["build_url"] == BUILD_8_URL


def test_a_triggered_rebuild_not_yet_checked_reads_as_queued(viewer):
    event_id, _ = triggered()

    rebuild = http.get(f"/api/events/{event_id}", auth=viewer).json()["execution"]["rebuild"]

    assert rebuild["state"] == "queued" and rebuild["result"] is None


@pytest.mark.parametrize("status", ["pending", "skipped", "failed", "uncertain"])
def test_no_rebuild_is_reported_unless_one_was_queued(viewer, status):
    event_id, _ = triggered(status=status)

    assert http.get(f"/api/events/{event_id}", auth=viewer).json()["execution"]["rebuild"] is None
    assert [e for e in http.get("/api/events", auth=viewer).json() if e["id"] == event_id][0]["rebuild"] is None


def test_events_polled_by_id_carry_the_rebuild_so_the_ui_updates_live(viewer):
    event_id, _ = triggered()
    executor.track_outcomes(reader({RUN_8: run(building=True, result=None)}))

    row = http.get(f"/api/events?ids={event_id}", auth=viewer).json()[0]

    assert row["rebuild"]["state"] == "running"


def test_live_failing_jobs_rows_show_the_rebuild(monkeypatch, viewer):
    monkeypatch.setenv("JENKINS_URL", BASE)
    monkeypatch.delenv("JENKINS_JOBS", raising=False)
    get_settings.cache_clear()
    now_ms = int(utcnow().timestamp() * 1000)
    triggered(job="acme/build", number=7, new_build=8)
    executor.track_outcomes(reader({RUN_8: run(result="SUCCESS")}))
    listing = reader({"/api/json": (200, {"jobs": [
        {"fullName": "acme/build", "color": "red",
         "lastCompletedBuild": {"number": 7, "result": "FAILURE", "timestamp": now_ms}},
        {"fullName": "other/job", "color": "red",
         "lastCompletedBuild": {"number": 2, "result": "FAILURE", "timestamp": now_ms}},
    ]})})
    main.app.dependency_overrides[review.get_jenkins] = lambda: listing

    try:
        rows = {r["job"]: r for r in http.get("/api/jenkins/failed-builds", auth=viewer).json()}
    finally:
        main.app.dependency_overrides.pop(review.get_jenkins, None)

    assert rows["acme/build"]["rebuild"]["result"] == "SUCCESS" and rows["acme/build"]["rebuild"]["build_number"] == 8
    assert rows["other/job"]["rebuild"] is None


def test_ui_shows_the_rebuild_result_everywhere_it_matters():
    from pathlib import Path

    static = Path(main.__file__).parent / "static" / "review"
    page = (static / "index.html").read_text(encoding="utf-8")
    script = (static / "review.js").read_text(encoding="utf-8")

    assert "<th>Rebuild</th>" in page
    assert "function rebuildBadge" in script and "Rebuild result" in script
    assert "rebuildPending(event.rebuild)" in script          # keeps polling until the rebuild ends
    assert "innerHTML" not in script
