"""Sends the DevOps owner notification by SMTP (P3b). The only module that
imports an email library, and it runs only when MODE_DEVOPS=auto.

State of one delivery:

    pending -> claimed -> skipped                    (a guard said no)
                       -> sending -> sent
                                  -> failed          (refused, or never connected)
                                  -> uncertain       (may have been accepted)

The intent ("sending") is committed *before* the first byte of the message is
offered, and an outcome we cannot determine is never retried: a second send
could mail the owner twice. Same discipline as executor.py.
"""

import logging
import smtplib
import ssl
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from sqlalchemy import update
from sqlalchemy.orm import Session

from executor import kill_switch_on
from settings import Settings, get_settings
from store.db import session_scope, utcnow
from store.models import EmailDelivery, Notification

logger = logging.getLogger("mailer")

# A delivery stuck this long in claimed/sending is presumed crashed.
LEASE_SECONDS = 300


class SendRefused(Exception):
    """Nothing was delivered: we never connected/authenticated, or the server
    answered with a refusal."""


class SendUncertain(Exception):
    """The message may have been accepted (connection broke mid-send)."""


# ---- message ------------------------------------------------------------

def build_message(
    sender: str, recipients: list[str], subject: str, body: str
) -> EmailMessage:
    """Raises ValueError for anything that could inject a header."""

    message = EmailMessage()
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=False)
    message["Message-ID"] = make_msgid(domain=sender.rsplit("@", 1)[-1])
    # Tells auto-responders (out-of-office etc.) not to answer this.
    message["Auto-Submitted"] = "auto-generated"
    message["X-Auto-Response-Suppress"] = "All"
    message.set_content(body, charset="utf-8")

    return message


# ---- smtp ---------------------------------------------------------------

def _describe(err: Exception) -> str:
    # Never include the message, credentials or addresses: the error type and
    # the server's reply codes only.
    if isinstance(err, smtplib.SMTPRecipientsRefused):
        codes = sorted({str(code) for code, _ in err.recipients.values()})
        return f"SMTPRecipientsRefused {','.join(codes)}"

    reply = getattr(err, "smtp_error", b"")

    if isinstance(reply, bytes):
        reply = reply.decode("utf-8", "replace")

    code = getattr(err, "smtp_code", "")

    return f"{type(err).__name__} {code} {str(reply)[:120]}".strip()[:300]


class SmtpSender:
    def __init__(self, settings: Settings):
        self.host = settings.smtp_host
        self.port = settings.smtp_port
        self.username = settings.smtp_username or settings.from_email_id
        self.password = (
            settings.smtp_password.get_secret_value() if settings.smtp_password else None
        )
        self.sender = settings.from_email_id
        self.starttls = settings.smtp_starttls
        self.use_ssl = settings.smtp_use_ssl
        self.timeout = settings.smtp_timeout_seconds

    def _connect(self) -> smtplib.SMTP:
        context = ssl.create_default_context()

        if self.use_ssl:
            smtp = smtplib.SMTP_SSL(self.host, self.port, timeout=self.timeout, context=context)
        else:
            smtp = smtplib.SMTP(self.host, self.port, timeout=self.timeout)

        try:
            smtp.ehlo()

            if self.starttls and not self.use_ssl:
                smtp.starttls(context=context)
                smtp.ehlo()

            if self.password:
                smtp.login(self.username, self.password)
        except Exception:
            try:
                smtp.close()
            except Exception:
                pass

            raise

        return smtp

    def send(self, message: EmailMessage, recipients: list[str]) -> tuple[str, list[str]]:
        """Returns (message id, recipients the server refused).

        SendRefused: nothing was delivered. SendUncertain: do not retry.
        """

        # Phase 1: connect, TLS, login. Nothing about the message has been
        # offered yet, so any failure here means it was not sent.
        try:
            smtp = self._connect()
        except (smtplib.SMTPException, OSError, ssl.SSLError) as err:
            raise SendRefused(_describe(err)) from err

        # Phase 2: offer the message.
        try:
            refused = smtp.send_message(
                message, from_addr=self.sender, to_addrs=recipients
            )
        except (
            smtplib.SMTPRecipientsRefused,
            smtplib.SMTPSenderRefused,
            smtplib.SMTPDataError,
            smtplib.SMTPHeloError,
        ) as err:
            # The server answered with a refusal.
            raise SendRefused(_describe(err)) from err
        except (smtplib.SMTPException, OSError, ssl.SSLError) as err:
            # Disconnected / timed out / garbled: it may have been accepted.
            raise SendUncertain(_describe(err)) from err
        finally:
            try:
                smtp.quit()
            except Exception:
                pass

        return message["Message-ID"], sorted(refused or {})


# ---- state machine ------------------------------------------------------

def _set(delivery_id: int, status: str, *, expect: tuple[str, ...], **fields) -> bool:
    with session_scope() as session:
        updated = session.execute(
            update(EmailDelivery)
            .where(EmailDelivery.id == delivery_id, EmailDelivery.status.in_(expect))
            .values(status=status, updated_at=utcnow(), **fields)
            .execution_options(synchronize_session=False)
        ).rowcount

    return updated == 1


def _skip(delivery_id: int, reason: str) -> None:
    _set(delivery_id, "skipped", expect=("claimed",), reason_codes=[reason])
    logger.info("Email skipped", extra={"delivery_id": delivery_id, "reason": reason})


def _gate(session: Session, notification: Notification) -> str | None:
    if kill_switch_on(session):
        return "kill_switch"

    if get_settings().mode_devops != "auto":
        return "mode_not_auto"

    if notification.decision != "would_send":
        return "not_sendable"

    if not notification.recipients:
        return "no_recipients"

    return None


def _sweep_stale(now: datetime) -> None:
    cutoff = now - timedelta(seconds=LEASE_SECONDS)

    with session_scope() as session:
        # Died while only checking: nothing was offered to the server.
        session.execute(
            update(EmailDelivery)
            .where(EmailDelivery.status == "claimed", EmailDelivery.updated_at < cutoff)
            .values(status="failed", reason_codes=["claim_expired"], updated_at=now)
            .execution_options(synchronize_session=False)
        )
        # Died around the send: it may have gone out. Never resend.
        session.execute(
            update(EmailDelivery)
            .where(EmailDelivery.status == "sending", EmailDelivery.updated_at < cutoff)
            .values(status="uncertain", reason_codes=["crash_after_intent"], updated_at=now)
            .execution_options(synchronize_session=False)
        )


def _claim_next(now: datetime) -> int | None:
    with session_scope() as session:
        candidate = (
            session.query(EmailDelivery.id)
            .filter(EmailDelivery.status == "pending")
            .order_by(EmailDelivery.id)
            .first()
        )

        if candidate is None:
            return None

        claimed = session.execute(
            update(EmailDelivery)
            .where(EmailDelivery.id == candidate.id, EmailDelivery.status == "pending")
            .values(status="claimed", updated_at=now)
            .execution_options(synchronize_session=False)
        ).rowcount

    return candidate.id if claimed == 1 else None


def _deliver(sender: SmtpSender, delivery_id: int) -> None:
    with session_scope() as session:
        delivery = session.get(EmailDelivery, delivery_id)
        notification = delivery.notification
        recipients = list(notification.recipients)
        subject, body = notification.subject, notification.body
        reason = _gate(session, notification)

    if reason:
        return _skip(delivery_id, reason)

    try:
        message = build_message(sender.sender, recipients, subject, body)
    except ValueError as err:
        _set(delivery_id, "failed", expect=("claimed",),
             reason_codes=["invalid_message"], error=str(err)[:200])
        return

    # Persist the intent before anything is offered to the server.
    if not _set(delivery_id, "sending", expect=("claimed",)):
        return

    try:
        message_id, refused = sender.send(message, recipients)
    except SendUncertain as err:
        _set(delivery_id, "uncertain", expect=("sending",),
             reason_codes=["send_outcome_unknown"], error=str(err)[:500])
        logger.warning("Email outcome unknown", extra={"delivery_id": delivery_id})
        return
    except SendRefused as err:
        _set(delivery_id, "failed", expect=("sending",),
             reason_codes=["not_delivered"], error=str(err)[:500])
        logger.warning("Email not delivered", extra={"delivery_id": delivery_id})
        return

    _set(
        delivery_id, "sent", expect=("sending",),
        message_id=message_id,
        sent_at=datetime.now(timezone.utc).replace(tzinfo=None),
        reason_codes=["partial_refusal"] if refused else [],
        error=(f"refused: {', '.join(refused)}"[:500] if refused else None),
    )
    logger.info(
        "Email sent",
        extra={"delivery_id": delivery_id, "recipients": len(recipients) - len(refused)},
    )


def run_once(sender: SmtpSender, now: datetime | None = None) -> bool:
    """Process at most one pending delivery. True if one was claimed."""

    now = now or utcnow()
    _sweep_stale(now)
    delivery_id = _claim_next(now)

    if delivery_id is None:
        return False

    try:
        _deliver(sender, delivery_id)
    except Exception as err:
        # A bug must not become a resend: settle by how far we got.
        logger.exception("Delivery crashed", extra={"delivery_id": delivery_id})
        detail = f"{type(err).__name__}: {err}"[:500]
        _set(delivery_id, "failed", expect=("claimed",), error=detail)
        _set(delivery_id, "uncertain", expect=("sending",),
             reason_codes=["crashed_after_intent"], error=detail)

    return True


# ---- one-off check of the SMTP settings ------------------------------------

def send_test(settings: Settings | None = None) -> str:
    """Send one test mail to EMAIL_ID using the real settings; returns the
    Message-ID. Touches no database. SystemExit with a plain message on any
    problem, so it can be run by hand: python -m mailer test"""

    settings = settings or get_settings()

    if not (settings.from_email_id and settings.smtp_password):
        raise SystemExit("Set FROM_EMAIL_ID and SMTP_PASSWORD in backend/.env first.")

    if not settings.owner_email_override:
        raise SystemExit("Set EMAIL_ID (the test recipient) in backend/.env first.")

    sender = SmtpSender(settings)
    message = build_message(
        sender.sender,
        [settings.owner_email_override],
        "[Jenkins AI Analyzer] SMTP test",
        "If you can read this, the Jenkins AI Analyzer can send mail from "
        f"{sender.sender} through {sender.host}:{sender.port}.\n",
    )

    try:
        message_id, refused = sender.send(message, [settings.owner_email_override])
    except SendRefused as err:
        raise SystemExit(f"NOT delivered: {err}")
    except SendUncertain as err:
        raise SystemExit(f"Outcome unknown, check the inbox before retrying: {err}")

    if refused:
        raise SystemExit("NOT delivered: the server refused the recipient.")

    return message_id


def main(argv: list[str] | None = None) -> None:
    import argparse

    parser = argparse.ArgumentParser(prog="python -m mailer")
    parser.add_argument("command", choices=["test"], help="send one test mail to EMAIL_ID")
    parser.parse_args(argv)

    message_id = send_test()
    print(f"Sent. Message-ID {message_id}. Check the inbox (and spam) of EMAIL_ID.")


if __name__ == "__main__":
    main()
