"""P2 infra re-run executor. Every test runs against a fake Jenkins: nothing
here can reach a real controller."""

from datetime import timedelta
from urllib.parse import parse_qs

import httpx
import pytest

import executor
import worker
from clients.jenkins import JenkinsClient, JenkinsError
from settings import get_settings
from store.db import session_scope, utcnow
from store.models import Event, Execution
from tests.conftest import FIXTURE_LOGS, make_jenkins
from tests.fakes import FakeJenkins, responses_for

BASE = "https://ci.example.com"
JOB = "acme/build"
JOB_API = "/job/acme/job/build/api/json"
BUILD_API = "/job/acme/job/build/7/api/json"
REBUILD = "/job/acme/job/build/buildWithParameters"
QUEUE = "https://ci.example.com/queue/item/55/"


@pytest.fixture
def auto(monkeypatch):
    monkeypatch.setenv("JENKINS_URL", BASE)
    monkeypatch.setenv("MODE_INFRA", "auto")
    monkeypatch.setenv("RERUN_JOB_ALLOWLIST", '["acme/*"]')
    monkeypatch.setenv("RERUN_QUEUE_TIMEOUT_SECONDS", "0")
    get_settings.cache_clear()


def routes(*, last=7, result="FAILURE", params=None, post=None, queue=None):
    params = [{"name": "BRANCH", "value": "main"}] if params is None else params
    return {
        JOB_API: (200, {"lastBuild": {"number": last}}),
        BUILD_API: (200, {"result": result, "actions": [{"parameters": params}]}),
        ("POST", REBUILD): post or (201, b"", {"Location": QUEUE}),
        "/queue/item/55/api/json": queue or (200, {
            "executable": {"number": 8, "url": f"{BASE}/job/acme/job/build/8/"}
        }),
    }


def writer(table):
    fake = FakeJenkins(table)
    client = JenkinsClient(
        BASE, "svc", "tok", timeout=5, max_log_bytes=1000,
        transport=fake.transport, allow_writes=True,
    )
    client.fake = fake
    return client


def add_execution(job=JOB, number=7, status="pending", root=None, new_build=None, age_minutes=0):
    with session_scope() as session:
        event = Event(
            controller_id="default", job=job, build_number=number,
            build_url=f"{BASE}/job/{job.replace('/', '/job/')}/{number}/",
            result="FAILURE", source="webhook", status="done",
            next_attempt_at=utcnow(), received_at=utcnow(),
        )
        session.add(event)
        session.flush()
        when = utcnow() - timedelta(minutes=age_minutes)
        execution = Execution(
            event_id=event.id, action_type="rerun", status=status, reason_codes=[],
            root_event_id=root or event.id, new_build_number=new_build,
            created_at=when, updated_at=when,
        )
        session.add(execution)
        session.flush()
        return event.id, execution.id


def state(execution_id):
    with session_scope() as session:
        ex = session.get(Execution, execution_id)
        return {
            "status": ex.status, "reasons": list(ex.reason_codes or []),
            "new_build": ex.new_build_number, "queue_url": ex.queue_url, "error": ex.error,
        }


# ---- happy path -----------------------------------------------------------

def test_rerun_posts_original_parameters_and_records_the_new_build(auto):
    _, ex = add_execution()
    jenkins = writer(routes())

    assert executor.run_once(jenkins) is True

    assert state(ex) == {
        "status": "triggered", "reasons": [], "new_build": 8,
        "queue_url": QUEUE, "error": None,
    }
    [post] = jenkins.fake.posts()
    assert post.url.path == REBUILD
    assert parse_qs(post.content.decode()) == {"BRANCH": ["main"]}


def test_parameterless_job_uses_the_plain_build_endpoint(auto):
    _, ex = add_execution()
    table = routes(params=[])
    table[("POST", "/job/acme/job/build/build")] = (201, b"", {"Location": QUEUE})
    jenkins = writer(table)

    executor.run_once(jenkins)

    assert state(ex)["status"] == "triggered"
    assert [p.url.path for p in jenkins.fake.posts()] == ["/job/acme/job/build/build"]


def test_idle_when_nothing_pending(auto):
    assert executor.run_once(writer(routes())) is False


def test_queue_that_never_starts_is_still_triggered_but_flagged(auto):
    _, ex = add_execution()
    jenkins = writer(routes(queue=(200, {"executable": None})))

    executor.run_once(jenkins)

    result = state(ex)
    assert result["status"] == "triggered" and result["new_build"] is None
    assert result["reasons"] == ["queue_unresolved"]
    assert len(jenkins.fake.posts()) == 1


# ---- guards: nothing is sent --------------------------------------------

def test_kill_switch_from_settings_blocks_everything(auto, monkeypatch):
    monkeypatch.setenv("KILL_SWITCH", "true")
    get_settings.cache_clear()
    _, ex = add_execution()
    jenkins = writer(routes())

    executor.run_once(jenkins)

    assert state(ex)["status"] == "skipped" and state(ex)["reasons"] == ["kill_switch"]
    assert jenkins.fake.seen == []          # not even a read


def test_runtime_kill_switch_blocks_and_can_be_cleared(auto):
    with session_scope() as session:
        executor.set_kill_switch(session, True, "op")

    _, blocked = add_execution()
    jenkins = writer(routes())
    executor.run_once(jenkins)

    assert state(blocked)["reasons"] == ["kill_switch"] and jenkins.fake.seen == []

    with session_scope() as session:
        executor.set_kill_switch(session, False, "op")
        assert executor.kill_switch_on(session) is False


@pytest.mark.parametrize("mode", ["shadow", "off"])
def test_executor_refuses_when_mode_is_not_auto(auto, monkeypatch, mode):
    monkeypatch.setenv("MODE_INFRA", mode)
    get_settings.cache_clear()
    _, ex = add_execution()
    jenkins = writer(routes())

    executor.run_once(jenkins)

    assert state(ex)["reasons"] == ["mode_not_auto"] and jenkins.fake.seen == []


def test_job_outside_the_allowlist_is_skipped(auto, monkeypatch):
    monkeypatch.setenv("RERUN_JOB_ALLOWLIST", '["other/*"]')
    get_settings.cache_clear()
    _, ex = add_execution()
    jenkins = writer(routes())

    executor.run_once(jenkins)

    assert state(ex)["reasons"] == ["job_not_allowed"] and jenkins.fake.seen == []


def test_empty_allowlist_allows_nothing(auto, monkeypatch):
    monkeypatch.setenv("RERUN_JOB_ALLOWLIST", "[]")
    get_settings.cache_clear()
    _, ex = add_execution()

    executor.run_once(writer(routes()))

    assert state(ex)["reasons"] == ["job_not_allowed"]


def test_budget_counts_a_rerun_of_a_rerun_against_the_same_chain(auto):
    first_event, first_ex = add_execution(status="triggered", new_build=8)
    # Build 8 (our re-run) failed and arrived as its own event.
    with session_scope() as session:
        rerun_event = Event(
            controller_id="default", job=JOB, build_number=8,
            build_url=f"{BASE}/job/acme/job/build/8/", result="FAILURE",
            source="webhook", status="done", next_attempt_at=utcnow(), received_at=utcnow(),
        )
        session.add(rerun_event)
        session.flush()
        assert executor.lineage_root(session, rerun_event) == first_event
        execution = executor.create_execution(session, rerun_event, utcnow())
        session.flush()
        second_ex = execution.id

    jenkins = writer(routes())
    executor.run_once(jenkins)

    assert state(second_ex)["reasons"] == ["budget_exhausted"]
    assert jenkins.fake.posts() == []


def test_job_rate_limit_holds_even_when_the_lineage_link_is_lost(auto):
    # Earlier re-run on the same job whose queue item never resolved, so its
    # build number is unknown and the next failure cannot be linked to it.
    add_execution(number=3, status="triggered", new_build=None, age_minutes=30)
    _, ex = add_execution(number=7)
    jenkins = writer(routes())

    executor.run_once(jenkins)

    assert state(ex)["reasons"] == ["job_rate_limited"] and jenkins.fake.posts() == []


def test_old_rerun_outside_the_window_does_not_block(auto):
    add_execution(number=3, status="triggered", new_build=None, age_minutes=60 * 24)
    _, ex = add_execution(number=7)

    executor.run_once(writer(routes()))

    assert state(ex)["status"] == "triggered"


# ---- guards: live checks, still nothing is POSTed -----------------------------

def test_stale_build_is_skipped(auto):
    _, ex = add_execution()
    jenkins = writer(routes(last=9))          # a newer build exists

    executor.run_once(jenkins)

    assert state(ex)["reasons"] == ["stale_build"] and jenkins.fake.posts() == []


def test_build_that_is_not_failed_any_more_is_skipped(auto):
    _, ex = add_execution()
    jenkins = writer(routes(result="SUCCESS"))

    executor.run_once(jenkins)

    assert state(ex)["reasons"] == ["build_not_failed"] and jenkins.fake.posts() == []


def test_secret_parameter_means_no_rebuild(auto):
    _, ex = add_execution()
    # Jenkins reports a password parameter without its value.
    jenkins = writer(routes(params=[{"name": "BRANCH", "value": "main"}, {"name": "API_TOKEN"}]))

    executor.run_once(jenkins)

    assert state(ex)["reasons"] == ["secret_parameters"] and jenkins.fake.posts() == []


def test_jenkins_unreachable_before_the_post_fails_safely(auto):
    _, ex = add_execution()
    jenkins = writer({JOB_API: (503, b"down")})

    executor.run_once(jenkins)

    assert state(ex)["status"] == "failed" and jenkins.fake.posts() == []


# ---- the POST itself ------------------------------------------------------

def test_jenkins_refusing_the_post_is_failed_not_uncertain(auto):
    _, ex = add_execution()
    jenkins = writer(routes(post=(403, b"forbidden")))

    executor.run_once(jenkins)

    assert state(ex)["status"] == "failed"
    assert "HTTP 403" in state(ex)["error"]


def test_transport_error_on_the_post_is_uncertain_and_never_retried(auto):
    _, ex = add_execution()
    table = routes()

    def boom(request):
        raise httpx.ReadTimeout("slow", request=request)

    table[("POST", REBUILD)] = boom
    jenkins = writer(table)

    executor.run_once(jenkins)
    result = state(ex)
    assert result["status"] == "uncertain"
    assert result["reasons"] == ["request_outcome_unknown"]

    # Another pass (even a much later one) must not POST again.
    executor.run_once(jenkins, now=utcnow() + timedelta(days=1))
    assert len(jenkins.fake.posts()) == 1
    assert state(ex)["status"] == "uncertain"


def test_queued_but_no_queue_url_is_uncertain(auto):
    _, ex = add_execution()
    jenkins = writer(routes(post=(201, b"")))     # no Location header

    executor.run_once(jenkins)

    assert state(ex)["status"] == "uncertain"
    assert len(jenkins.fake.posts()) == 1


def test_queue_url_outside_the_base_is_not_followed(auto):
    _, ex = add_execution()
    evil = (201, b"", {"Location": "https://evil.example.net/queue/item/1/"})
    jenkins = writer(routes(post=evil))

    executor.run_once(jenkins)

    assert state(ex)["status"] == "uncertain"
    assert all("evil" not in str(r.url) for r in jenkins.fake.seen)


# ---- crash recovery -------------------------------------------------------

def test_stuck_triggering_becomes_uncertain_without_a_new_post(auto):
    _, ex = add_execution(status="triggering", age_minutes=60)
    jenkins = writer(routes())

    executor.run_once(jenkins)

    assert state(ex)["status"] == "uncertain"
    assert state(ex)["reasons"] == ["crash_after_intent"]
    assert jenkins.fake.seen == []


def test_stuck_claimed_fails_because_nothing_was_sent(auto):
    _, ex = add_execution(status="claimed", age_minutes=60)

    executor.run_once(writer(routes()))

    assert state(ex)["status"] == "failed" and state(ex)["reasons"] == ["claim_expired"]


def test_recent_in_flight_work_is_left_alone(auto):
    _, ex = add_execution(status="triggering", age_minutes=0)

    executor.run_once(writer(routes()))

    assert state(ex)["status"] == "triggering"


def test_a_bug_after_the_post_does_not_cause_a_second_post(auto, monkeypatch):
    _, ex = add_execution()
    jenkins = writer(routes())

    def explode(*args, **kwargs):
        raise RuntimeError("bug")

    monkeypatch.setattr(jenkins, "resolve_queue_item", explode)

    executor.run_once(jenkins)

    # POST happened and was recorded before the bug: stays triggered, one POST.
    assert state(ex)["status"] == "triggered"
    assert len(jenkins.fake.posts()) == 1


def test_unexpected_crash_before_the_intent_fails(auto, monkeypatch):
    _, ex = add_execution()
    jenkins = writer(routes())
    monkeypatch.setattr(jenkins, "get_last_build_number", lambda job: 1 / 0)

    executor.run_once(jenkins)

    assert state(ex)["status"] == "failed" and jenkins.fake.posts() == []


# ---- the client's own write guard ---------------------------------------------

def test_default_client_cannot_trigger_a_rebuild():
    client = make_jenkins()

    with pytest.raises(JenkinsError, match="read-only"):
        client.trigger_rebuild(JOB, {"A": "b"})

    assert client.fake.posts() == []


@pytest.mark.parametrize("path", [
    "job/acme/job/build/7/stop",
    "job/acme/job/build/doDelete",
    "job/acme/job/build/config.xml",
    "scriptText",
    "job/acme/job/build/7/submitDescription",
])
def test_write_client_still_refuses_every_other_post(path):
    client = writer(routes())

    with pytest.raises(JenkinsError, match="refused"):
        client._http.post(f"{BASE}/{path}")

    assert client.fake.posts() == []


def test_write_client_refuses_a_url_outside_the_base():
    client = writer(routes())

    with pytest.raises(JenkinsError):
        client.resolve_queue_item("https://evil.example.net/queue/item/1/", 0, 0)


def test_crumb_is_sent_when_jenkins_offers_one(auto):
    _, ex = add_execution()
    table = routes()
    table["/crumbIssuer/api/json"] = (200, {"crumbRequestField": "Jenkins-Crumb", "crumb": "abc"})
    jenkins = writer(table)

    executor.run_once(jenkins)

    assert jenkins.fake.posts()[0].headers["Jenkins-Crumb"] == "abc"
    assert state(ex)["status"] == "triggered"


# ---- creating executions from the pipeline ----------------------------------------

INFRA_LOG = (FIXTURE_LOGS / "infra_agent_offline.log").read_text()
INFRA_SIGNATURE = "ChannelClosedException"


def process(fake_llm, queued_event):
    fake_llm(responses_for(INFRA_LOG, INFRA_SIGNATURE, "infra"))
    assert worker.run_once(make_jenkins(console=INFRA_LOG)) is True


def executions():
    with session_scope() as session:
        return session.query(Execution).count()


def test_auto_mode_queues_an_execution_for_an_allowed_rerun(auto, fake_llm, queued_event):
    process(fake_llm, queued_event)

    assert executions() == 1
    with session_scope() as session:
        execution = session.query(Execution).one()
        assert execution.status == "pending" and execution.root_event_id == queued_event


def test_shadow_mode_never_queues_an_execution(auto, fake_llm, queued_event, monkeypatch):
    monkeypatch.setenv("MODE_INFRA", "shadow")
    get_settings.cache_clear()

    process(fake_llm, queued_event)

    assert executions() == 0
    with session_scope() as session:
        assert session.get(Event, queued_event).action.action_type == "rerun"


def test_off_mode_proposes_nothing(auto, fake_llm, queued_event, monkeypatch):
    monkeypatch.setenv("MODE_INFRA", "off")
    get_settings.cache_clear()

    process(fake_llm, queued_event)

    assert executions() == 0
    with session_scope() as session:
        assert session.get(Event, queued_event).action is None


def test_job_not_allowlisted_is_proposed_denied_and_not_queued(
    auto, fake_llm, queued_event, monkeypatch
):
    monkeypatch.setenv("RERUN_JOB_ALLOWLIST", '["other/*"]')
    get_settings.cache_clear()

    process(fake_llm, queued_event)

    assert executions() == 0
    with session_scope() as session:
        assert session.get(Event, queued_event).action.allowed is False


def test_worker_main_only_builds_a_write_client_in_auto_mode(monkeypatch):
    built = []

    def fake_factory(write=False):
        built.append(write)

        if len(built) > 2:
            raise SystemExit
        return make_jenkins()

    monkeypatch.setattr(worker, "jenkins_client_from_settings", fake_factory)
    monkeypatch.setattr(worker, "run_once", lambda jenkins: False)
    monkeypatch.setattr(worker.time, "sleep", lambda s: (_ for _ in ()).throw(SystemExit))
    monkeypatch.setenv("JENKINS_URL", BASE)
    get_settings.cache_clear()

    with pytest.raises(SystemExit):
        worker.main()

    assert built == [False]                      # shadow: read-only client only

    built.clear()
    monkeypatch.setenv("MODE_INFRA", "auto")
    get_settings.cache_clear()
    monkeypatch.setattr(worker.executor, "run_once", lambda writer: False)

    with pytest.raises(SystemExit):
        worker.main()

    assert built == [False, True]                # auto: plus the write client


# ---- controls API and execution in the event detail ------------------------------

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402

http = TestClient(main.app)


def test_controls_are_readable_by_viewers(viewer):
    data = http.get("/api/controls", auth=viewer).json()

    assert data == {
        "kill_switch": False, "kill_switch_from_settings": False,
        "mode_infra": "shadow", "mode_devops": "shadow",
    }
    assert http.get("/api/controls").status_code == 401


def test_only_operators_can_flip_the_kill_switch(viewer, labeler, operator):
    body = {"enabled": True}

    assert http.post("/api/controls/kill-switch", json=body, auth=viewer).status_code == 403
    assert http.post("/api/controls/kill-switch", json=body, auth=labeler).status_code == 403
    assert http.post("/api/controls/kill-switch", json=body, auth=operator).json()["kill_switch"] is True
    assert http.get("/api/controls", auth=viewer).json()["kill_switch"] is True

    cleared = http.post("/api/controls/kill-switch", json={"enabled": False}, auth=operator)
    assert cleared.json()["kill_switch"] is False


def test_runtime_toggle_cannot_clear_a_kill_switch_set_in_settings(operator, monkeypatch):
    monkeypatch.setenv("KILL_SWITCH", "true")
    get_settings.cache_clear()

    data = http.post("/api/controls/kill-switch", json={"enabled": False}, auth=operator).json()

    assert data["kill_switch"] is True and data["kill_switch_from_settings"] is True


def test_the_kill_switch_stops_a_pending_execution(auto, operator):
    http.post("/api/controls/kill-switch", json={"enabled": True}, auth=operator)
    _, ex = add_execution()
    jenkins = writer(routes())

    executor.run_once(jenkins)

    assert state(ex)["reasons"] == ["kill_switch"] and jenkins.fake.seen == []


def test_event_detail_reports_the_execution(auto, viewer):
    event_id, ex = add_execution()
    executor.run_once(writer(routes()))

    detail = http.get(f"/api/events/{event_id}", auth=viewer).json()

    assert detail["execution"]["status"] == "triggered"
    assert detail["execution"]["new_build_number"] == 8
    assert detail["execution"]["queue_url"] == QUEUE


def test_event_detail_without_execution_says_none_and_not_executed(viewer, queued_event):
    detail = http.get(f"/api/events/{queued_event}", auth=viewer).json()

    assert detail["execution"] is None
