"""Background worker: claim queued failures with a lease and process them.

Run with: python -m worker
"""

import logging
import time
from datetime import datetime, timedelta

from sqlalchemy import and_, or_, update
from sqlalchemy.orm import Session

from clients.jenkins import JenkinsClient, JenkinsError, jenkins_client_from_settings
from llm import LLMError
from logging_config import configure_logging
import executor
import mailer
from pipeline import StaleClaimError, process_event
from settings import get_settings
from store.db import session_scope, utcnow
from store.models import Event

logger = logging.getLogger("worker")


def claim_next(session: Session, now: datetime) -> Event | None:
    lease_cutoff = now - timedelta(seconds=get_settings().worker_lease_seconds)

    candidate = (
        session.query(Event)
        .filter(or_(
            and_(Event.status == "queued", Event.next_attempt_at <= now),
            # A worker died mid-event: its lease has expired.
            and_(Event.status == "processing", Event.claimed_at < lease_cutoff),
        ))
        .order_by(Event.id)
        .first()
    )

    if candidate is None:
        return None

    # Reclaiming an expired lease means the previous attempt died or hung:
    # count it, so a poison event cannot loop forever.
    reclaim = candidate.status == "processing"
    values = {"status": "processing", "claimed_at": now}

    if reclaim:
        values["attempts"] = Event.attempts + 1

    seen_claimed = (
        Event.claimed_at.is_(None)
        if candidate.claimed_at is None
        else Event.claimed_at == candidate.claimed_at
    )

    # Conditional update: only one worker can win the claim.
    claimed = session.execute(
        update(Event)
        .where(
            Event.id == candidate.id,
            Event.status == candidate.status,
            seen_claimed,
        )
        .values(**values)
        .execution_options(synchronize_session=False)
    ).rowcount

    if claimed != 1:
        return None

    event = session.get(Event, candidate.id, populate_existing=True)

    if reclaim and event.attempts >= get_settings().worker_max_attempts:
        event.status = "failed"
        event.claimed_at = None
        event.last_error = "Worker lease expired (crashed or exceeded worker_lease_seconds)"
        logger.warning(
            "Event failed after repeated lease expiry",
            extra={"event_id": event.id, "attempts": event.attempts},
        )
        return None

    return event


def _describe(err: Exception) -> str:
    # Our own error types carry sanitized messages; others only their type.
    if isinstance(err, (JenkinsError, LLMError)):
        return f"{type(err).__name__}: {err}"

    return type(err).__name__


def _record_failure(
    event_id: int,
    err: Exception,
    now: datetime,
    claimed_at: datetime,
) -> None:
    settings = get_settings()

    with session_scope() as session:
        event = session.get(Event, event_id)
        attempts = event.attempts + 1

        if attempts >= settings.worker_max_attempts:
            values = {"status": "failed"}
        else:
            values = {
                "status": "queued",
                "next_attempt_at": now + timedelta(
                    seconds=settings.worker_backoff_seconds * 2 ** (attempts - 1)
                ),
            }

        # Fenced: a worker whose claim was taken over must not touch the event.
        updated = session.execute(
            update(Event)
            .where(
                Event.id == event_id,
                Event.status == "processing",
                Event.claimed_at == claimed_at,
            )
            .values(
                attempts=attempts,
                last_error=_describe(err)[:500],
                claimed_at=None,
                **values,
            )
            .execution_options(synchronize_session=False)
        ).rowcount

    if updated != 1:
        logger.warning(
            "Stale worker result discarded",
            extra={"event_id": event_id, "error_type": type(err).__name__},
        )
        return

    status = values["status"]

    logger.warning(
        "Event processing failed",
        extra={
            "event_id": event_id,
            "error_type": type(err).__name__,
            "attempts": attempts,
            "status": status,
        },
    )


def run_once(jenkins: JenkinsClient, now: datetime | None = None) -> bool:
    now = now or utcnow()

    with session_scope() as session:
        event = claim_next(session, now)
        event_id = event.id if event else None
        claimed_at = event.claimed_at if event else None

    if event_id is None:
        return False

    try:
        with session_scope() as session:
            process_event(
                session,
                session.get(Event, event_id),
                jenkins,
                claimed_at,
            )
    except StaleClaimError:
        logger.warning("Stale claim; result discarded", extra={"event_id": event_id})
    except Exception as err:
        _record_failure(event_id, err, now, claimed_at)

    return True


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)

    try:
        jenkins = jenkins_client_from_settings()
    except JenkinsError as err:
        # Exit cleanly (no restart loop) on installs without Jenkins settings.
        logger.error("Worker not started: %s", err)
        return

    # Writes to Jenkins exist only in "auto" mode; otherwise the write-capable
    # client is never even built.
    writer = None

    if settings.mode_infra == "auto":
        try:
            writer = jenkins_client_from_settings(write=True)
        except JenkinsError as err:
            logger.error("Executor disabled: %s", err)

    # The mail sender exists only in MODE_DEVOPS=auto (settings already refused
    # auto without FROM_EMAIL_ID / SMTP_PASSWORD).
    sender = mailer.SmtpSender(settings) if settings.mode_devops == "auto" else None

    logger.info(
        "Worker started",
        extra={"executor": writer is not None, "mailer": sender is not None},
    )

    while True:
        busy = run_once(jenkins)

        if writer is not None:
            busy = executor.run_once(writer) or busy

        if sender is not None:
            busy = mailer.run_once(sender) or busy

        # Read-only: records how the rebuilds we triggered ended.
        busy = executor.track_outcomes(jenkins) or busy

        if not busy:
            time.sleep(settings.worker_poll_seconds)


if __name__ == "__main__":
    main()
