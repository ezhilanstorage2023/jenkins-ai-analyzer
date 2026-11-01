"""P3 devops owner notification, shadow mode: nothing is ever sent."""

import json
import re
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

import main
import notify
import owners
import pipeline
import worker
from clients.jenkins import JenkinsClient
from settings import get_settings
from store.db import session_scope, utcnow
from store.models import Event, Notification
from tests.conftest import FIXTURE_LOGS, make_jenkins
from tests.fakes import FakeJenkins, responses_for

http = TestClient(main.app)

BASE = "https://ci.example.com"
CONFIG_PATH = "/job/acme/job/build/config.xml"
DEVOPS_LOG = (FIXTURE_LOGS / "devops_missing_credentials.log").read_text()
INFRA_LOG = (FIXTURE_LOGS / "infra_agent_offline.log").read_text()


def configure(monkeypatch, **env):
    for key in ("EMAIL_ID", "OWNER_EMAIL_OVERRIDE", "OWNER_MAP", "OWNER_PARAMETER_NAME",
                "ALLOWED_EMAIL_DOMAINS", "MODE_DEVOPS", "NOTIFY_RATE_WINDOW_HOURS",
                "REVIEW_UI_URL"):
        monkeypatch.delenv(key, raising=False)

    for key, value in env.items():
        monkeypatch.setenv(key, value if isinstance(value, str) else json.dumps(value))

    get_settings.cache_clear()


def config_client(xml=None, status=200):
    fake = FakeJenkins({CONFIG_PATH: (status, xml if xml is not None else "")})
    client = JenkinsClient(BASE, "svc", "tok", timeout=5, max_log_bytes=1000,
                           transport=fake.transport)
    client.fake = fake
    return client


def param_xml(name="NOTIFY_EMAIL", default="owner@example.com"):
    return f"""<?xml version='1.1' encoding='UTF-8'?>
<flow-definition><properties><hudson.model.ParametersDefinitionProperty>
<parameterDefinitions>
  <hudson.model.StringParameterDefinition><name>OTHER</name><defaultValue>x</defaultValue>
  </hudson.model.StringParameterDefinition>
  <hudson.model.StringParameterDefinition><name>{name}</name><defaultValue>{default}</defaultValue>
  </hudson.model.StringParameterDefinition>
</parameterDefinitions></hudson.model.ParametersDefinitionProperty></properties></flow-definition>"""


# ---- owner resolution -------------------------------------------------------

def test_first_matching_owner_map_pattern_wins(monkeypatch):
    configure(monkeypatch, OWNER_MAP={"acme/*": ["Team@Example.com"], "*": ["other@example.com"]},
              ALLOWED_EMAIL_DOMAINS=["example.com"])

    res = owners.resolve("acme/build")

    assert (res.recipients, res.source, res.reasons) == (["team@example.com"], "map", [])


def test_owner_map_domain_must_be_allowed_and_does_not_fall_through(monkeypatch):
    configure(monkeypatch, OWNER_MAP={"*": ["a@evil.net"]}, ALLOWED_EMAIL_DOMAINS=["example.com"],
              EMAIL_ID="me@example.com")

    res = owners.resolve("acme/build")

    # An explicit-but-wrong mapping is blocked, not quietly sent to the override.
    assert res.recipients == [] and res.reasons == ["domain_not_allowed"] and res.source == "map"


def test_no_domain_is_accepted_when_none_are_configured(monkeypatch):
    configure(monkeypatch, OWNER_MAP={"*": ["a@example.com"]})

    assert owners.resolve("x").recipients == []


def test_override_is_used_without_a_domain_check(monkeypatch):
    configure(monkeypatch, EMAIL_ID="Tester@Anywhere.org")

    res = owners.resolve("acme/build")

    assert (res.recipients, res.source) == (["tester@anywhere.org"], "override")


def test_nothing_configured_is_unresolved(monkeypatch):
    configure(monkeypatch)

    res = owners.resolve("acme/build")

    assert res.recipients == [] and res.reasons == ["target_unresolved"] and res.source is None


def test_parameter_default_from_config_xml(monkeypatch):
    configure(monkeypatch, OWNER_PARAMETER_NAME="NOTIFY_EMAIL", ALLOWED_EMAIL_DOMAINS=["example.com"],
              EMAIL_ID="me@example.com")

    res = owners.resolve("acme/build", config_client(param_xml()))

    assert (res.recipients, res.source) == (["owner@example.com"], "config")


def test_parameter_lookup_is_skipped_unless_configured(monkeypatch):
    configure(monkeypatch, EMAIL_ID="me@example.com")
    client = config_client(param_xml())

    res = owners.resolve("acme/build", client)

    assert res.source == "override" and client.fake.seen == []


@pytest.mark.parametrize("xml, status", [
    ('<!DOCTYPE x [<!ENTITY a "b">]><x/>', 200),
    ("<not-xml", 200),
    ("x" * 1_100_000, 200),
    ("", 404),
], ids=["dtd", "malformed", "oversize", "not-found"])
def test_unusable_config_falls_back_to_override_and_says_why(monkeypatch, xml, status):
    configure(monkeypatch, OWNER_PARAMETER_NAME="NOTIFY_EMAIL", EMAIL_ID="me@example.com")

    res = owners.resolve("acme/build", config_client(xml, status))

    assert res.source == "override" and "config_unavailable" in res.reasons
    assert res.recipients == ["me@example.com"]


def test_config_parameter_in_a_forbidden_domain_is_blocked(monkeypatch):
    configure(monkeypatch, OWNER_PARAMETER_NAME="NOTIFY_EMAIL", ALLOWED_EMAIL_DOMAINS=["example.com"],
              EMAIL_ID="me@example.com")

    res = owners.resolve("acme/build", config_client(param_xml(default="x@evil.net")))

    assert res.recipients == [] and res.reasons == ["domain_not_allowed"]


@pytest.mark.parametrize("value", [
    "a@example.com\nBcc: b@example.com",
    "a@example.com\r\nSubject: hi",
    "not-an-email",
    "a@b",
    "<a@example.com>",
])
def test_malformed_or_injected_addresses_are_blocked(monkeypatch, value):
    configure(monkeypatch, OWNER_MAP={"*": [value]}, ALLOWED_EMAIL_DOMAINS=["example.com"])

    res = owners.resolve("x")

    assert res.recipients == [] and "invalid_recipient" in res.reasons


def test_a_partly_bad_list_is_blocked_not_trimmed(monkeypatch):
    configure(monkeypatch, OWNER_MAP={"*": ["a@example.com, junk"]}, ALLOWED_EMAIL_DOMAINS=["example.com"])

    assert owners.resolve("x").recipients == []


def test_recipient_list_is_deduped_and_capped(monkeypatch):
    configure(monkeypatch, OWNER_MAP={"*": ["a@example.com;A@example.com,b@example.com"]},
              ALLOWED_EMAIL_DOMAINS=["example.com"])
    assert owners.resolve("x").recipients == ["a@example.com", "b@example.com"]

    configure(monkeypatch, OWNER_MAP={"*": ["a@example.com,b@example.com,c@example.com,d@example.com"]},
              ALLOWED_EMAIL_DOMAINS=["example.com"])
    res = owners.resolve("x")
    assert res.recipients == [] and res.reasons == ["too_many_recipients"]


@pytest.mark.parametrize("xml", ['<!DOCTYPE a><a/>', '<!ENTITY x "y"><a/>', '<!doctype a><a/>'])
def test_parameter_default_refuses_dtds(xml):
    with pytest.raises(ValueError):
        owners.parameter_default(xml, "X")


def test_parameter_default_missing_or_empty_is_none():
    assert owners.parameter_default(param_xml(), "NOPE") is None
    assert owners.parameter_default(param_xml(default="  "), "NOTIFY_EMAIL") is None


def test_mode_auto_is_refused_without_a_sender_login(monkeypatch):
    configure(monkeypatch, MODE_DEVOPS="auto")

    with pytest.raises(ValidationError, match="FROM_EMAIL_ID and SMTP_PASSWORD"):
        get_settings()

    configure(monkeypatch, MODE_DEVOPS="auto", FROM_EMAIL_ID="bot@example.com")
    with pytest.raises(ValidationError, match="SMTP_PASSWORD"):
        get_settings()

    configure(monkeypatch, MODE_DEVOPS="auto", FROM_EMAIL_ID="bot@example.com", SMTP_PASSWORD="x")
    assert get_settings().mode_devops == "auto"


# ---- signature and rendering --------------------------------------------------

def test_signature_ignores_line_numbers_timestamps_and_digits():
    a = ["[Line 12] [2026-10-31T09:00:01.123Z] ERROR: build 57 failed on node 3"]
    b = ["[Line 903] [2026-11-02T18:44:59.001Z] ERROR: build 61 failed on node 9"]

    assert notify.failure_signature("j", "devops", a) == notify.failure_signature("j", "devops", b)


@pytest.mark.parametrize("other", [("k", "devops", "ERROR: x"), ("j", "infra", "ERROR: x"),
                                   ("j", "devops", "ERROR: y")])
def test_signature_differs_by_job_category_and_text(other):
    base = notify.failure_signature("j", "devops", ["ERROR: x"])
    job, category, text = other

    assert notify.failure_signature(job, category, [text]) != base


def make_event(job="acme/build", number=7):
    with session_scope() as session:
        event = Event(controller_id="default", job=job, build_number=number,
                      build_url=f"{BASE}/job/{job}/{number}/", result="FAILURE", source="webhook",
                      status="done", next_attempt_at=utcnow(), received_at=utcnow())
        session.add(event)
        session.flush()
        session.expunge(event)
        return event


ANALYSIS = {
    "root_cause": "Credentials entry is missing.",
    "recommended_fix": "Add the credentials entry to Jenkins.",
    "evidence": ["[Line 4] Could not find credentials entry with ID 'deploy-key'"],
}
DATA = SimpleNamespace(failing_stage="Deploy")


def test_render_has_the_facts_verbatim_and_no_send_claims():
    event = make_event()

    subject, body = notify.render(event, "devops", ANALYSIS, DATA, "https://review.example.com/")

    assert subject == "[Jenkins] acme/build #7 failed (devops)"
    assert f"Build: {BASE}/job/acme/build/7/" in body
    assert "Failing stage: Deploy" in body
    assert "[Line 4] Could not find credentials entry with ID 'deploy-key'" in body
    assert "Review: https://review.example.com/ui/" in body
    assert "advisory, not verified" in body


def test_render_strips_header_injection_from_the_subject():
    event = make_event(job="acme/build\r\nBcc: x@evil.net")

    subject, _ = notify.render(event, "devops", ANALYSIS, DATA, None)

    assert "\r" not in subject and "\n" not in subject


def test_render_redacts_secrets_and_caps_size():
    analysis = dict(ANALYSIS, evidence=["export PASSWORD=s3cr3t-value"] + ["x" * 5000] * 20,
                    root_cause="r" * 20000)

    _, body = notify.render(make_event(), "devops", analysis, DATA, None)

    assert "s3cr3t-value" not in body and "[REDACTED" in body
    assert len(body) <= 4000
    assert "x" * 301 not in body and "r" * 801 not in body      # each field is capped
    assert body.count("x" * 300) <= 5                            # at most 5 evidence lines


def test_render_without_review_url_has_no_link():
    _, body = notify.render(make_event(), "devops", ANALYSIS, DATA, None)

    assert "Review:" not in body


# ---- decisions, rate limit ----------------------------------------------------------

def preview(event, now=None, jenkins=None):
    with session_scope() as session:
        note = notify.build_preview(session, event, "devops", ANALYSIS, DATA, jenkins, now or utcnow())
        session.add(note)
        session.flush()
        return note.decision, list(note.reason_codes), list(note.recipients)


def test_same_failure_again_is_suppressed_but_another_job_is_not(monkeypatch):
    configure(monkeypatch, EMAIL_ID="me@example.com")
    first, again, other = make_event(number=7), make_event(number=8), make_event(job="other/job", number=1)

    assert preview(first)[0] == "would_send"
    assert preview(again)[:2] == ("suppressed", ["rate_limited"])
    assert preview(other)[0] == "would_send"


def test_a_suppressed_preview_does_not_extend_the_window(monkeypatch):
    configure(monkeypatch, EMAIL_ID="me@example.com", NOTIFY_RATE_WINDOW_HOURS="6")
    t0 = utcnow()

    preview(make_event(number=1), now=t0)
    assert preview(make_event(number=2), now=t0 + timedelta(hours=5))[0] == "suppressed"
    # 7h after the only real would_send: allowed again.
    assert preview(make_event(number=3), now=t0 + timedelta(hours=7))[0] == "would_send"


def test_no_recipient_is_blocked(monkeypatch):
    configure(monkeypatch)

    decision, reasons, recipients = preview(make_event())

    assert (decision, reasons, recipients) == ("blocked", ["target_unresolved"], [])


def test_kill_switch_blocks_the_preview(monkeypatch):
    configure(monkeypatch, EMAIL_ID="me@example.com")
    monkeypatch.setenv("KILL_SWITCH", "true")
    get_settings.cache_clear()

    decision, reasons, _ = preview(make_event())

    assert decision == "blocked" and "kill_switch" in reasons


# ---- pipeline integration -----------------------------------------------------------------

def process(fake_llm, log, signature, category):
    fake_llm(responses_for(log, signature, category))
    assert worker.run_once(make_jenkins(console=log)) is True


def only_notification():
    with session_scope() as session:
        notes = session.query(Notification).all()
        return [(n.decision, n.recipients, n.recipient_source, n.mode, n.reason_codes) for n in notes]


def test_a_devops_failure_records_a_preview_for_the_configured_email(
    monkeypatch, fake_llm, queued_event
):
    configure(monkeypatch, EMAIL_ID="tester@example.com")

    process(fake_llm, DEVOPS_LOG, "Could not find credentials entry", "devops")

    assert only_notification() == [
        ("would_send", ["tester@example.com"], "override", "shadow", [])
    ]


def test_an_infra_failure_gets_no_email_preview(monkeypatch, fake_llm, queued_event):
    configure(monkeypatch, EMAIL_ID="tester@example.com")

    process(fake_llm, INFRA_LOG, "ChannelClosedException", "infra")

    assert only_notification() == []


def test_mode_off_records_nothing_and_proposes_nothing(monkeypatch, fake_llm, queued_event):
    configure(monkeypatch, EMAIL_ID="tester@example.com", MODE_DEVOPS="off")

    process(fake_llm, DEVOPS_LOG, "Could not find credentials entry", "devops")

    assert only_notification() == []
    with session_scope() as session:
        assert session.get(Event, queued_event).action is None


def test_unreadable_job_config_is_recorded_not_fatal(monkeypatch, fake_llm, queued_event):
    configure(monkeypatch, EMAIL_ID="tester@example.com", OWNER_PARAMETER_NAME="NOTIFY_EMAIL")

    process(fake_llm, DEVOPS_LOG, "Could not find credentials entry", "devops")

    [(decision, recipients, source, _, reasons)] = only_notification()
    assert decision == "would_send" and source == "override" and "config_unavailable" in reasons


def test_a_bug_in_the_preview_never_fails_the_event(monkeypatch, fake_llm, queued_event):
    configure(monkeypatch, EMAIL_ID="tester@example.com")

    def boom(*args, **kwargs):
        raise RuntimeError("bug")

    monkeypatch.setattr(pipeline, "build_preview", boom)

    process(fake_llm, DEVOPS_LOG, "Could not find credentials entry", "devops")

    assert only_notification() == []
    with session_scope() as session:
        event = session.get(Event, queued_event)
        assert event.status == "done" and event.analysis.final_category == "devops"


# ---- API ------------------------------------------------------------------------------

def seed_preview(monkeypatch, fake_llm):
    configure(monkeypatch, EMAIL_ID="tester@example.com")
    process(fake_llm, DEVOPS_LOG, "Could not find credentials entry", "devops")


def test_event_detail_shows_the_preview_as_not_sent(monkeypatch, fake_llm, queued_event, viewer):
    seed_preview(monkeypatch, fake_llm)

    note = http.get(f"/api/events/{queued_event}", auth=viewer).json()["notification"]

    assert note["decision"] == "would_send" and note["sent"] is False
    assert note["recipients"] == ["tester@example.com"] and note["subject"].startswith("[Jenkins]")
    assert "Could not find credentials entry" in note["body"]


def test_event_detail_without_a_preview_has_none(queued_event, viewer):
    assert http.get(f"/api/events/{queued_event}", auth=viewer).json()["notification"] is None


def test_recipient_feedback_needs_labeler_and_feeds_the_metrics(
    monkeypatch, fake_llm, queued_event, viewer, labeler
):
    seed_preview(monkeypatch, fake_llm)
    url = f"/api/events/{queued_event}/notification-label"

    assert http.post(url, json={"recipient_ok": True}, auth=viewer).status_code == 403
    assert http.post(url, json={"recipient_ok": False}, auth=labeler).json() == {"status": "saved"}

    metrics = http.get("/api/metrics/notifications", auth=viewer).json()
    assert metrics["total"] == 1 and metrics["by_decision"] == {"would_send": 1}
    assert metrics["by_source"] == {"override": 1}
    assert metrics["recipient_feedback"] == {"ok": 0, "wrong": 1, "unrated": 0}
    assert http.get(f"/api/events/{queued_event}", auth=viewer).json()["notification"]["reviewed_by"]


def test_only_a_would_send_preview_can_be_rated(monkeypatch, fake_llm, queued_event, labeler):
    configure(monkeypatch)                                  # no recipient -> blocked
    process(fake_llm, DEVOPS_LOG, "Could not find credentials entry", "devops")

    response = http.post(f"/api/events/{queued_event}/notification-label",
                         json={"recipient_ok": True}, auth=labeler)

    assert response.status_code == 422


def test_rating_an_event_without_a_preview_is_404(queued_event, labeler):
    response = http.post(f"/api/events/{queued_event}/notification-label",
                         json={"recipient_ok": True}, auth=labeler)

    assert response.status_code == 404


def test_metrics_report_the_top_suppressed_failures(monkeypatch, viewer):
    configure(monkeypatch, EMAIL_ID="me@example.com")

    for number in (1, 2, 3):
        preview(make_event(number=number))

    metrics = http.get("/api/metrics/notifications", auth=viewer).json()

    assert metrics["by_decision"] == {"would_send": 1, "suppressed": 2}
    assert metrics["top_suppressed"][0]["job"] == "acme/build"
    assert metrics["top_suppressed"][0]["count"] == 2
    assert http.get("/api/metrics/notifications").status_code == 401


# ---- the guarantee: only mailer.py can send email ------------------------------------------

def test_only_mailer_py_can_send_email():
    backend = Path(__file__).resolve().parents[1]
    forbidden = re.compile(
        r"^\s*(import|from)\s+(smtplib|aiosmtplib|yagmail|sendgrid|mailjet|boto3|email\.mime|email\.message)\b",
        re.MULTILINE,
    )
    offenders = []

    for path in backend.rglob("*.py"):
        parts = set(path.relative_to(backend).parts)

        if parts & {"venv", ".venv", "tests", "__pycache__"}:
            continue

        if forbidden.search(path.read_text(encoding="utf-8")):
            offenders.append(str(path.relative_to(backend)))

    # mailer.py is the one place, and the worker the one caller (test_mailer.py).
    assert offenders == ["mailer.py"]
