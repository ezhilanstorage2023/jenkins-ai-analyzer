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
from pipeline import process_event
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
        .values(status="processing", claimed_at=now)
        .execution_options(synchronize_session=False)
    ).rowcount

    if claimed != 1:
        return None

    return session.get(Event, candidate.id, populate_existing=True)


def _describe(err: Exception) -> str:
    # Our own error types carry sanitized messages; others only their type.
    if isinstance(err, (JenkinsError, LLMError)):
        return f"{type(err).__name__}: {err}"

    return type(err).__name__


def _record_failure(event_id: int, err: Exception, now: datetime) -> None:
    settings = get_settings()

    with session_scope() as session:
        event = session.get(Event, event_id)
        event.attempts += 1
        event.last_error = _describe(err)[:500]
        event.claimed_at = None

        if event.attempts >= settings.worker_max_attempts:
            event.status = "failed"
        else:
            event.status = "queued"
            event.next_attempt_at = now + timedelta(
                seconds=settings.worker_backoff_seconds * 2 ** (event.attempts - 1)
            )

        status, attempts = event.status, event.attempts

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

    if event_id is None:
        return False

    try:
        with session_scope() as session:
            process_event(session, session.get(Event, event_id), jenkins)
    except Exception as err:
        _record_failure(event_id, err, now)

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

    logger.info("Worker started")

    while True:
        if not run_once(jenkins):
            time.sleep(settings.worker_poll_seconds)


if __name__ == "__main__":
    main()
