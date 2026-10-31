from datetime import timedelta

from store.db import session_scope, utcnow
from store.models import Event
from tests.conftest import COMPILATION_LOG, make_jenkins
from tests.fakes import responses_for
from worker import claim_next, run_once


def test_run_once_processes_event_end_to_end(fake_llm, queued_event, jenkins):
    fake_llm(responses_for(COMPILATION_LOG, "cannot find symbol", "compilation"))

    assert run_once(jenkins) is True

    with session_scope() as session:
        event = session.get(Event, queued_event)
        assert event.status == "done"
        assert event.analysis.final_category == "compilation"
        assert event.analysis.reason_codes == []
        assert event.enrichment.failing_stage == "Compile"
        assert event.enrichment.parameters["GERRIT_CHANGE_NUMBER"] == "48213"
        assert event.action.action_type == "exclude_culprit"
        assert event.action.target["change_number"] == "48213"
        assert {m["rule_id"] for m in event.analysis.rule_matches} == {
            "compilation.compiler_error"
        }

    assert {r.method for r in jenkins.fake.seen} == {"GET"}


def test_idle_returns_false(jenkins):
    assert run_once(jenkins) is False


def test_jenkins_error_retries_then_fails(queued_event, broken_jenkins):
    t0 = utcnow()

    run_once(broken_jenkins, now=t0)

    with session_scope() as session:
        event = session.get(Event, queued_event)
        assert (event.status, event.attempts) == ("queued", 1)
        assert event.next_attempt_at == t0 + timedelta(seconds=60)
        assert "503" in event.last_error

    run_once(broken_jenkins, now=t0 + timedelta(seconds=61))
    run_once(broken_jenkins, now=t0 + timedelta(seconds=10_000))

    with session_scope() as session:
        event = session.get(Event, queued_event)
        assert (event.status, event.attempts) == ("failed", 3)


def test_backoff_not_due_is_not_claimed(queued_event, broken_jenkins):
    t0 = utcnow()
    run_once(broken_jenkins, now=t0)

    assert run_once(broken_jenkins, now=t0 + timedelta(seconds=30)) is False


def test_expired_lease_is_reclaimed(queued_event):
    t0 = utcnow()

    with session_scope() as session:
        assert claim_next(session, t0).id == queued_event

    with session_scope() as session:
        assert claim_next(session, t0 + timedelta(seconds=601)).id == queued_event


def test_live_claim_not_double_claimed(queued_event):
    t0 = utcnow()

    with session_scope() as session:
        claim_next(session, t0)

    with session_scope() as session:
        assert claim_next(session, t0 + timedelta(seconds=5)) is None


def test_stored_excerpt_is_redacted(fake_llm, queued_event):
    console = "Authorization: Bearer abc.def.123\n" + COMPILATION_LOG
    fake_llm(responses_for(console, "cannot find symbol", "compilation"))

    run_once(make_jenkins(console=console))

    with session_scope() as session:
        event = session.get(Event, queued_event)
        assert event.status == "done"
        assert "abc.def.123" not in event.enrichment.log_excerpt
        assert "abc.def.123" not in str(event.analysis.response)


def test_unusable_llm_output_still_completes_as_triage(fake_llm, queued_event, jenkins):
    fake_llm(["garbage"] * 3)

    run_once(jenkins)

    with session_scope() as session:
        event = session.get(Event, queued_event)
        assert event.status == "done"
        assert event.analysis.final_category == "needs_triage"
        assert event.action is None


def test_main_exits_cleanly_when_jenkins_unconfigured(caplog, monkeypatch):
    import worker

    # configure_logging would replace pytest's capture handler.
    monkeypatch.setattr(worker, "configure_logging", lambda level: None)
    caplog.set_level("ERROR")

    worker.main()  # must return, not raise or loop

    assert "Jenkins is not configured" in caplog.text
