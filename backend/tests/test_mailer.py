"""P3b: automatic DevOps email over SMTP. Runs against a real SMTP server on
localhost (tests/smtp_fake.py), so the actual smtplib code path is exercised,
including the failures that decide "failed" versus "uncertain"."""

import itertools
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

import mailer
import main
import worker
from settings import get_settings
from store.db import session_scope, utcnow
from store.models import EmailDelivery, Event, Notification
from tests.conftest import make_jenkins
from tests.fakes import responses_for
from tests.smtp_fake import FakeSmtp, closed_port
from tests.test_notify import (
    ANALYSIS, DEVOPS_LOG, INFRA_LOG, configure, make_event, preview,
)

http = TestClient(main.app)
numbers = itertools.count(100)

SIGNATURE_LINE = "Could not find credentials entry"


@pytest.fixture
def smtp():
    started = []

    def make(**behaviour):
        server = FakeSmtp(**behaviour).start()
        started.append(server)
        return server

    yield make

    for server in started:
        server.stop()


def mail_env(monkeypatch, server=None, **extra):
    env = dict(
        MODE_DEVOPS="auto",
        FROM_EMAIL_ID="bot@example.com",
        SMTP_PASSWORD="s3cret-pass-123",
        SMTP_HOST="127.0.0.1",
        SMTP_PORT=str(server.port if server else closed_port()),
        SMTP_STARTTLS="false",
        SMTP_TIMEOUT_SECONDS="5",
        EMAIL_ID="owner@example.com",
    )
    env.update(extra)
    configure(monkeypatch, **env)


def sender():
    return mailer.SmtpSender(get_settings())


def process(fake_llm, log=DEVOPS_LOG, signature=SIGNATURE_LINE, category="devops"):
    fake_llm(responses_for(log, signature, category))
    assert worker.run_once(make_jenkins(console=log)) is True


def delivery_of(event_id):
    with session_scope() as session:
        note = session.query(Notification).filter_by(event_id=event_id).one_or_none()
        d = note.delivery if note else None
        return d and {
            "id": d.id, "status": d.status, "reasons": list(d.reason_codes or []),
            "error": d.error, "message_id": d.message_id, "sent_at": d.sent_at,
        }


def add_delivery(status="pending", age_minutes=0, recipients=("owner@example.com",),
                 decision="would_send"):
    event = make_event(number=next(numbers))

    with session_scope() as session:
        note = Notification(
            event_id=event.id, recipients=list(recipients), recipient_source="override",
            subject="[Jenkins] acme/build failed (devops)", body="Root cause: x\n",
            signature=f"sig{event.id}", decision=decision, reason_codes=[], mode="auto",
            recipient_ok=None, created_at=utcnow(),
        )
        session.add(note)
        session.flush()
        when = utcnow() - timedelta(minutes=age_minutes)
        delivery = EmailDelivery(notification_id=note.id, status=status, reason_codes=[],
                                 created_at=when, updated_at=when)
        session.add(delivery)
        session.flush()
        return delivery.id


def status_of(delivery_id):
    with session_scope() as session:
        d = session.get(EmailDelivery, delivery_id)
        return d.status, list(d.reason_codes or []), d.error


# ---- the message ----------------------------------------------------------

def test_message_has_the_right_headers_and_utf8_body():
    message = mailer.build_message("bot@example.com", ["a@example.com", "b@example.com"],
                                   "[Jenkins] job #1 failed (devops)", "Wurzel: Fehler ü\n")

    assert message["From"] == "bot@example.com"
    assert message["To"] == "a@example.com, b@example.com"
    assert message["Subject"] == "[Jenkins] job #1 failed (devops)"
    assert message["Message-ID"].endswith("@example.com>")
    assert message["Auto-Submitted"] == "auto-generated"
    assert message["X-Auto-Response-Suppress"] == "All"
    assert message["Date"]
    assert "Fehler ü" in message.get_content()


@pytest.mark.parametrize("subject, recipients", [
    ("subject\r\nBcc: x@evil.net", ["a@example.com"]),
    ("subject", ["a@example.com\r\nBcc: x@evil.net"]),
], ids=["subject", "recipient"])
def test_header_injection_is_rejected(subject, recipients):
    with pytest.raises(ValueError):
        mailer.build_message("bot@example.com", recipients, subject, "body")


# ---- queueing in the pipeline -----------------------------------------------------

def test_auto_mode_queues_a_delivery_for_a_devops_failure(monkeypatch, fake_llm, queued_event, smtp):
    mail_env(monkeypatch, smtp())

    process(fake_llm)

    assert delivery_of(queued_event)["status"] == "pending"
    with session_scope() as session:
        assert session.query(Notification).one().mode == "auto"


def test_shadow_mode_queues_no_delivery(monkeypatch, fake_llm, queued_event, smtp):
    mail_env(monkeypatch, smtp(), MODE_DEVOPS="shadow")

    process(fake_llm)

    assert delivery_of(queued_event) is None
    with session_scope() as session:
        assert session.query(EmailDelivery).count() == 0


def test_an_infra_failure_sends_nothing(monkeypatch, fake_llm, queued_event, smtp):
    server = smtp()
    mail_env(monkeypatch, server)

    process(fake_llm, INFRA_LOG, "ChannelClosedException", "infra")

    with session_scope() as session:
        assert session.query(EmailDelivery).count() == 0 and session.query(Notification).count() == 0
    assert mailer.run_once(sender()) is False and server.messages == []


def test_a_blocked_or_suppressed_preview_queues_no_delivery(monkeypatch, fake_llm, queued_event, smtp):
    mail_env(monkeypatch, smtp(), EMAIL_ID="")           # no recipient -> blocked

    process(fake_llm)

    with session_scope() as session:
        assert session.query(Notification).one().decision == "blocked"
        assert session.query(EmailDelivery).count() == 0


# ---- sending over the wire ----------------------------------------------------------

def test_delivery_reaches_the_server_with_the_right_envelope_and_login(monkeypatch, fake_llm, queued_event, smtp):
    server = smtp()
    mail_env(monkeypatch, server)
    process(fake_llm)

    assert mailer.run_once(sender()) is True

    assert len(server.messages) == 1
    assert server.messages[0]["mail_from"] == "bot@example.com"
    assert server.messages[0]["rcpt_to"] == ["owner@example.com"]
    assert server.logins == [("bot@example.com", "s3cret-pass-123")]

    received = server.parsed()
    assert received["From"] == "bot@example.com" and received["To"] == "owner@example.com"
    assert received["Subject"] == "[Jenkins] acme/build #7 failed (devops)"
    body = received.get_content()
    assert "Could not find credentials entry" in body and "Build: https://ci.example.com/job/acme/job/build/7/" in body

    done = delivery_of(queued_event)
    assert done["status"] == "sent" and done["reasons"] == [] and done["error"] is None
    assert done["message_id"] == received["Message-ID"] and done["sent_at"] is not None


def test_what_is_sent_is_exactly_the_stored_preview(monkeypatch, fake_llm, queued_event, smtp):
    server = smtp()
    mail_env(monkeypatch, server)
    process(fake_llm)
    mailer.run_once(sender())

    with session_scope() as session:
        stored = session.query(Notification).one()
        subject, body = stored.subject, stored.body

    received = server.parsed()
    assert received["Subject"] == subject
    assert received.get_content().replace("\r\n", "\n").strip() == body.strip()


def test_nothing_pending_means_nothing_sent(monkeypatch, smtp):
    server = smtp()
    mail_env(monkeypatch, server)

    assert mailer.run_once(sender()) is False and server.data_attempts == 0


def test_a_sent_delivery_is_never_sent_again(monkeypatch, fake_llm, queued_event, smtp):
    server = smtp()
    mail_env(monkeypatch, server)
    process(fake_llm)

    mailer.run_once(sender())
    assert mailer.run_once(sender(), now=utcnow() + timedelta(days=2)) is False

    assert len(server.messages) == 1


# ---- guards: nothing is offered to the server -----------------------------------------------

def test_kill_switch_stops_the_email(monkeypatch, fake_llm, queued_event, smtp):
    server = smtp()
    mail_env(monkeypatch, server)
    process(fake_llm)
    monkeypatch.setenv("KILL_SWITCH", "true")
    get_settings.cache_clear()

    mailer.run_once(sender())

    assert delivery_of(queued_event)["status"] == "skipped"
    assert delivery_of(queued_event)["reasons"] == ["kill_switch"]
    assert server.data_attempts == 0 and server.logins == []


def test_runtime_kill_switch_stops_the_email(monkeypatch, smtp):
    import executor

    server = smtp()
    mail_env(monkeypatch, server)
    delivery = add_delivery()
    with session_scope() as session:
        executor.set_kill_switch(session, True, "op")

    mailer.run_once(sender())

    assert status_of(delivery)[:2] == ("skipped", ["kill_switch"]) and server.data_attempts == 0


def test_switching_back_to_shadow_skips_what_was_queued(monkeypatch, smtp):
    server = smtp()
    mail_env(monkeypatch, server)
    delivery = add_delivery()
    mail_env(monkeypatch, server, MODE_DEVOPS="shadow")

    mailer.run_once(sender())

    assert status_of(delivery)[:2] == ("skipped", ["mode_not_auto"]) and server.data_attempts == 0


@pytest.mark.parametrize("decision, recipients, reason", [
    ("suppressed", ["owner@example.com"], "not_sendable"),
    ("blocked", [], "not_sendable"),
    ("would_send", [], "no_recipients"),
])
def test_only_a_would_send_preview_with_recipients_is_mailed(monkeypatch, smtp, decision, recipients, reason):
    server = smtp()
    mail_env(monkeypatch, server)
    delivery = add_delivery(decision=decision, recipients=recipients)

    mailer.run_once(sender())

    assert status_of(delivery)[:2] == ("skipped", [reason]) and server.data_attempts == 0


# ---- failures where the message was NOT delivered ----------------------------------------

def test_wrong_password_is_failed_and_the_secret_is_not_recorded(monkeypatch, smtp):
    server = smtp(auth_ok=False)
    mail_env(monkeypatch, server)
    delivery = add_delivery()

    mailer.run_once(sender())

    status, reasons, error = status_of(delivery)
    assert (status, reasons) == ("failed", ["not_delivered"])
    assert "535" in error and "s3cret-pass-123" not in error and "bot@example.com" not in error
    assert server.data_attempts == 0


def test_connection_refused_is_failed(monkeypatch):
    mail_env(monkeypatch, None)                           # nothing is listening
    delivery = add_delivery()

    mailer.run_once(sender())

    assert status_of(delivery)[:2] == ("failed", ["not_delivered"])


def test_all_recipients_refused_is_failed(monkeypatch, smtp):
    server = smtp(reject_rcpt=["owner@example.com"])
    mail_env(monkeypatch, server)
    delivery = add_delivery()

    mailer.run_once(sender())

    status, reasons, error = status_of(delivery)
    assert (status, reasons) == ("failed", ["not_delivered"]) and "550" in error
    assert server.messages == []


def test_message_rejected_after_data_is_failed(monkeypatch, smtp):
    server = smtp(data_reply=b"554 5.7.1 rejected as spam")
    mail_env(monkeypatch, server)
    delivery = add_delivery()

    mailer.run_once(sender())

    assert status_of(delivery)[:2] == ("failed", ["not_delivered"]) and server.messages == []


def test_one_refused_recipient_among_several_is_sent_and_flagged(monkeypatch, smtp):
    server = smtp(reject_rcpt=["b@example.com"])
    mail_env(monkeypatch, server)
    delivery = add_delivery(recipients=["a@example.com", "b@example.com"])

    mailer.run_once(sender())

    status, reasons, error = status_of(delivery)
    assert (status, reasons) == ("sent", ["partial_refusal"]) and "b@example.com" in error
    assert server.messages[0]["rcpt_to"] == ["a@example.com"]


# ---- the outcome is unknown: never resend --------------------------------------------------

def test_connection_dropped_after_data_is_uncertain_and_never_resent(monkeypatch, smtp):
    server = smtp(drop_after_data=True)
    mail_env(monkeypatch, server)
    delivery = add_delivery()

    mailer.run_once(sender())
    assert status_of(delivery)[:2] == ("uncertain", ["send_outcome_unknown"])
    assert server.data_attempts == 1

    # Neither an immediate nor a much later pass may offer it again.
    mailer.run_once(sender())
    mailer.run_once(sender(), now=utcnow() + timedelta(days=3))
    assert server.data_attempts == 1 and status_of(delivery)[0] == "uncertain"


def test_stale_claimed_fails_because_nothing_was_offered(monkeypatch, smtp):
    server = smtp()
    mail_env(monkeypatch, server)
    delivery = add_delivery(status="claimed", age_minutes=60)

    mailer.run_once(sender())

    assert status_of(delivery)[:2] == ("failed", ["claim_expired"]) and server.data_attempts == 0


def test_stale_sending_is_uncertain_and_not_resent(monkeypatch, smtp):
    server = smtp()
    mail_env(monkeypatch, server)
    delivery = add_delivery(status="sending", age_minutes=60)

    mailer.run_once(sender())

    assert status_of(delivery)[:2] == ("uncertain", ["crash_after_intent"]) and server.data_attempts == 0


def test_recent_in_flight_work_is_left_alone(monkeypatch, smtp):
    server = smtp()
    mail_env(monkeypatch, server)
    delivery = add_delivery(status="sending", age_minutes=0)

    mailer.run_once(sender())

    assert status_of(delivery)[0] == "sending" and server.data_attempts == 0


def test_a_bug_before_the_send_fails_and_a_bug_after_it_is_uncertain(monkeypatch, smtp):
    server = smtp()
    mail_env(monkeypatch, server)

    class Exploding:
        sender = "bot@example.com"

        def send(self, message, recipients):
            raise RuntimeError("bug while sending")

    delivery = add_delivery()
    mailer.run_once(Exploding())
    assert status_of(delivery)[:2] == ("uncertain", ["crashed_after_intent"])
    assert server.data_attempts == 0


# ---- rate limit counts only mail that reached (or may have reached) the owner -----------------

@pytest.mark.parametrize("delivery_status, expected", [
    ("sent", "suppressed"),
    ("pending", "suppressed"),
    ("uncertain", "suppressed"),       # it may have gone out: do not risk a duplicate
    ("failed", "would_send"),          # never delivered: the next failure may try again
    ("skipped", "would_send"),
])
def test_rate_limit_ignores_deliveries_that_never_reached_the_owner(monkeypatch, delivery_status, expected):
    configure(monkeypatch, EMAIL_ID="owner@example.com")
    assert preview(make_event(number=1))[0] == "would_send"
    with session_scope() as session:
        note = session.query(Notification).order_by(Notification.id.desc()).first()
        session.add(EmailDelivery(notification_id=note.id, status=delivery_status, reason_codes=[],
                                  created_at=utcnow(), updated_at=utcnow()))

    assert preview(make_event(number=2))[0] == expected


# ---- end to end -------------------------------------------------------------------------------------

def test_end_to_end_failed_build_to_message_in_the_inbox(monkeypatch, fake_llm, queued_event, smtp, viewer):
    server = smtp()
    mail_env(monkeypatch, server)

    # 1. a build fails and is classified devops by the worker
    process(fake_llm)
    with session_scope() as session:
        event = session.get(Event, queued_event)
        assert event.status == "done" and event.analysis.final_category == "devops"

    # 2. nothing is sent until the mailer runs
    assert server.messages == []

    # 3. the mailer delivers it
    assert mailer.run_once(sender()) is True

    # 4. it is in the inbox, from the configured sender to the configured owner
    received = server.parsed()
    assert (received["From"], received["To"]) == ("bot@example.com", "owner@example.com")
    assert "devops" in received["Subject"]

    # 5. the API/UI report it as sent
    detail = http.get(f"/api/events/{queued_event}", auth=viewer).json()["notification"]
    assert detail["sent"] is True and detail["delivery"]["status"] == "sent"
    assert detail["delivery"]["message_id"] == received["Message-ID"]
    metrics = http.get("/api/metrics/notifications", auth=viewer).json()
    assert metrics["by_delivery"] == {"sent": 1}


def test_the_same_failure_again_does_not_send_a_second_email(monkeypatch, smtp):
    server = smtp()
    mail_env(monkeypatch, server)

    first = preview(make_event(number=1))
    with session_scope() as session:
        note = session.query(Notification).one()
        session.add(EmailDelivery(notification_id=note.id, status="pending", reason_codes=[],
                                  created_at=utcnow(), updated_at=utcnow()))
    mailer.run_once(sender())
    second = preview(make_event(number=2))

    assert first[0] == "would_send" and second[:2] == ("suppressed", ["rate_limited"])
    assert len(server.messages) == 1


def test_api_shows_a_failed_delivery_and_its_reason(monkeypatch, fake_llm, queued_event, smtp, viewer):
    server = smtp(auth_ok=False)
    mail_env(monkeypatch, server)
    process(fake_llm)
    mailer.run_once(sender())

    detail = http.get(f"/api/events/{queued_event}", auth=viewer).json()["notification"]

    assert detail["sent"] is False and detail["delivery"]["status"] == "failed"
    assert "535" in detail["delivery"]["error"]
    assert "s3cret-pass-123" not in str(detail)


# ---- worker wiring ----------------------------------------------------------------------------------

@pytest.mark.parametrize("mode, mailer_runs", [("shadow", 0), ("auto", 1)])
def test_worker_runs_the_mailer_only_in_auto_mode(monkeypatch, smtp, mode, mailer_runs):
    mail_env(monkeypatch, smtp(), MODE_DEVOPS=mode, JENKINS_URL="https://ci.example.com")
    calls = []

    monkeypatch.setattr(worker, "jenkins_client_from_settings", lambda write=False: make_jenkins())
    monkeypatch.setattr(worker, "run_once", lambda jenkins: False)
    monkeypatch.setattr(worker.mailer, "run_once", lambda s: calls.append(s) or False)
    monkeypatch.setattr(worker.time, "sleep", lambda seconds: (_ for _ in ()).throw(SystemExit))

    with pytest.raises(SystemExit):
        worker.main()

    assert len(calls) == mailer_runs


# ---- TLS: never fall back to cleartext ------------------------------------------------------

def test_starttls_requested_but_not_offered_refuses_and_never_sends_the_password(monkeypatch, smtp):
    server = smtp()                                       # speaks plain SMTP only
    mail_env(monkeypatch, server, SMTP_STARTTLS="true")
    delivery = add_delivery()

    mailer.run_once(sender())

    status, reasons, error = status_of(delivery)
    assert (status, reasons) == ("failed", ["not_delivered"])
    assert server.logins == [] and server.data_attempts == 0      # nothing in cleartext
    assert "s3cret-pass-123" not in (error or "")


def test_implicit_ssl_against_a_plaintext_server_fails_without_leaking(monkeypatch, smtp):
    server = smtp()
    mail_env(monkeypatch, server, SMTP_USE_SSL="true", SMTP_STARTTLS="false")
    delivery = add_delivery()

    mailer.run_once(sender())

    assert status_of(delivery)[:2] == ("failed", ["not_delivered"])
    assert server.logins == [] and server.data_attempts == 0


def test_default_settings_target_gmail_with_starttls(monkeypatch):
    configure(monkeypatch)
    settings = get_settings()

    assert (settings.smtp_host, settings.smtp_port, settings.smtp_starttls) == ("smtp.gmail.com", 587, True)
    assert settings.smtp_use_ssl is False


def test_login_name_defaults_to_the_from_address(monkeypatch, smtp):
    mail_env(monkeypatch, smtp())

    assert sender().username == "bot@example.com"

    mail_env(monkeypatch, smtp(), SMTP_USERNAME="login-name")
    assert sender().username == "login-name" and sender().sender == "bot@example.com"


# ---- python -m mailer test ----------------------------------------------------------------

def test_cli_test_mail_reaches_the_server(monkeypatch, smtp, capsys):
    server = smtp()
    mail_env(monkeypatch, server, MODE_DEVOPS="shadow")        # works without auto mode

    mailer.main(["test"])

    assert "Sent. Message-ID" in capsys.readouterr().out
    received = server.parsed()
    assert (received["From"], received["To"]) == ("bot@example.com", "owner@example.com")
    assert received["Subject"] == "[Jenkins AI Analyzer] SMTP test"
    assert server.logins == [("bot@example.com", "s3cret-pass-123")]


@pytest.mark.parametrize("env, text", [
    ({"FROM_EMAIL_ID": ""}, "FROM_EMAIL_ID and SMTP_PASSWORD"),
    ({"SMTP_PASSWORD": ""}, "FROM_EMAIL_ID and SMTP_PASSWORD"),
    ({"EMAIL_ID": ""}, "EMAIL_ID"),
], ids=["no-from", "no-password", "no-recipient"])
def test_cli_test_mail_explains_missing_settings(monkeypatch, smtp, env, text):
    mail_env(monkeypatch, smtp(), MODE_DEVOPS="shadow", **env)

    with pytest.raises(SystemExit, match=text):
        mailer.main(["test"])


def test_cli_test_mail_reports_a_refusal_without_the_password(monkeypatch, smtp):
    mail_env(monkeypatch, smtp(auth_ok=False), MODE_DEVOPS="shadow")

    with pytest.raises(SystemExit) as raised:
        mailer.main(["test"])

    assert "NOT delivered" in str(raised.value) and "535" in str(raised.value)
    assert "s3cret-pass-123" not in str(raised.value)


def test_cli_test_mail_says_when_the_outcome_is_unknown(monkeypatch, smtp):
    mail_env(monkeypatch, smtp(drop_after_data=True), MODE_DEVOPS="shadow")

    with pytest.raises(SystemExit, match="Outcome unknown"):
        mailer.main(["test"])
