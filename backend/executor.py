"""Remediation executor (P2: infra re-run).

The only module that writes to Jenkins, and only through
JenkinsClient.trigger_rebuild. Everything is deny-by-default:

    pending -> claimed -> skipped                      (a guard said no)
                       -> triggering -> triggered      (rebuild queued)
                                     -> failed         (Jenkins refused it)
                                     -> uncertain      (may have been applied)

The intent ("triggering") is committed *before* the POST. A crash or a
transport error after that point can therefore never lead to a second POST:
the execution is parked as `uncertain` for a human to look at.

Run by the worker loop (python -m worker) only when settings.mode_infra is
"auto"; in "shadow" nothing in this module is ever reached.
"""

import logging
import re
from datetime import datetime, timedelta
from fnmatch import fnmatch

from sqlalchemy import func, update
from sqlalchemy.orm import Session

from clients.jenkins import (
    JenkinsClient,
    JenkinsError,
    JenkinsUncertainError,
    build_url_for,
)
from settings import get_settings
from store.db import session_scope, utcnow
from store.models import Control, Event, Execution, RebuildOutcome

logger = logging.getLogger("executor")

KILL_SWITCH_KEY = "kill_switch"

# Executions that count against a budget (a build was, or may have been, queued).
_LIVE = ("triggering", "triggered", "uncertain")


# ---- controls -----------------------------------------------------------

def kill_switch_on(session: Session) -> bool:
    if get_settings().kill_switch:
        return True

    control = session.get(Control, KILL_SWITCH_KEY)

    return control is not None and control.value == "on"


def set_kill_switch(session: Session, enabled: bool, user: str) -> None:
    control = session.get(Control, KILL_SWITCH_KEY) or Control(key=KILL_SWITCH_KEY)
    control.value = "on" if enabled else "off"
    control.updated_by = user
    control.updated_at = utcnow()
    session.add(control)


# ---- creating work ------------------------------------------------------

def lineage_root(session: Session, event: Event) -> int:
    """The first event of the re-run chain `event` belongs to.

    An event is part of a chain when it is the build one of our executions
    queued for the same job; otherwise it starts its own chain.
    """

    parent = (
        session.query(Execution)
        .join(Event, Event.id == Execution.event_id)
        .filter(
            Event.controller_id == event.controller_id,
            Event.job == event.job,
            Execution.new_build_number == event.build_number,
        )
        .first()
    )

    return parent.root_event_id if parent else event.id


def create_execution(session: Session, event: Event, now: datetime) -> Execution:
    execution = Execution(
        event_id=event.id,
        action_type="rerun",
        status="pending",
        reason_codes=[],
        root_event_id=lineage_root(session, event),
        created_at=now,
        updated_at=now,
    )
    session.add(execution)

    return execution


# ---- guards -------------------------------------------------------------

def _gate(session: Session, execution: Execution, event: Event, now: datetime) -> str | None:
    """Reason code of the first guard that refuses, else None."""

    settings = get_settings()

    if kill_switch_on(session):
        return "kill_switch"

    if settings.mode_infra != "auto":
        return "mode_not_auto"

    if not any(fnmatch(event.job, pattern) for pattern in settings.rerun_job_allowlist):
        return "job_not_allowed"

    # Per chain: a re-run of a re-run counts against the same budget.
    in_chain = (
        session.query(func.count(Execution.id))
        .filter(
            Execution.root_event_id == execution.root_event_id,
            Execution.id != execution.id,
            Execution.status.in_(_LIVE),
        )
        .scalar()
    )

    if in_chain >= settings.rerun_budget:
        return "budget_exhausted"

    # Per job, independent of lineage: if the queue item never resolved we
    # cannot tell that a failing build is our own re-run, and a retry loop
    # must still be impossible.
    window_start = now - timedelta(hours=settings.rerun_job_window_hours)
    recent_for_job = (
        session.query(func.count(Execution.id))
        .join(Event, Event.id == Execution.event_id)
        .filter(
            Event.controller_id == event.controller_id,
            Event.job == event.job,
            Execution.id != execution.id,
            Execution.status.in_(_LIVE),
            Execution.created_at >= window_start,
        )
        .scalar()
    )

    if recent_for_job >= settings.rerun_budget:
        return "job_rate_limited"

    return None


# ---- state changes ------------------------------------------------------

def _set(execution_id: int, status: str, *, expect: tuple[str, ...], **fields) -> bool:
    """Conditional transition; False if the execution is no longer in `expect`."""

    with session_scope() as session:
        updated = session.execute(
            update(Execution)
            .where(Execution.id == execution_id, Execution.status.in_(expect))
            .values(status=status, updated_at=utcnow(), **fields)
            .execution_options(synchronize_session=False)
        ).rowcount

    return updated == 1


def _skip(execution_id: int, reason: str) -> None:
    _set(execution_id, "skipped", expect=("claimed",), reason_codes=[reason])
    logger.info("Execution skipped", extra={"execution_id": execution_id, "reason": reason})


def _describe(err: Exception) -> str:
    return f"{type(err).__name__}: {err}"[:500]


# ---- running ------------------------------------------------------------

def _sweep_stale(now: datetime) -> None:
    cutoff = now - timedelta(seconds=get_settings().execution_lease_seconds)

    with session_scope() as session:
        # Died while only checking: nothing was sent, so it is safe to fail.
        session.execute(
            update(Execution)
            .where(Execution.status == "claimed", Execution.updated_at < cutoff)
            .values(status="failed", reason_codes=["claim_expired"], updated_at=now)
            .execution_options(synchronize_session=False)
        )
        # Died around the POST: it may have been applied. Never retry.
        session.execute(
            update(Execution)
            .where(Execution.status == "triggering", Execution.updated_at < cutoff)
            .values(status="uncertain", reason_codes=["crash_after_intent"], updated_at=now)
            .execution_options(synchronize_session=False)
        )


def _claim_next(now: datetime) -> int | None:
    with session_scope() as session:
        candidate = (
            session.query(Execution.id)
            .filter(Execution.status == "pending")
            .order_by(Execution.id)
            .first()
        )

        if candidate is None:
            return None

        claimed = session.execute(
            update(Execution)
            .where(Execution.id == candidate.id, Execution.status == "pending")
            .values(status="claimed", updated_at=now)
            .execution_options(synchronize_session=False)
        ).rowcount

    return candidate.id if claimed == 1 else None


def _execute(jenkins: JenkinsClient, execution_id: int, now: datetime) -> None:
    settings = get_settings()

    with session_scope() as session:
        execution = session.get(Execution, execution_id)
        event = execution.event
        job, build_number, build_url = event.job, event.build_number, event.build_url
        reason = _gate(session, execution, event, now)

    if reason:
        _skip(execution_id, reason)
        return

    # Live checks. Nothing has been sent yet, so an error here is safe to fail.
    try:
        last_build = jenkins.get_last_build_number(job)
        info = jenkins.get_build_info(build_url)
    except JenkinsError as err:
        _set(execution_id, "failed", expect=("claimed",), error=_describe(err))
        return

    if last_build != build_number:
        return _skip(execution_id, "stale_build")

    if info.result != "FAILURE":
        return _skip(execution_id, "build_not_failed")

    # A secret/password parameter cannot be read back, so the rebuild would
    # silently run with different inputs. Refuse instead.
    if info.masked_parameters:
        return _skip(execution_id, "secret_parameters")

    # Persist the intent before the request leaves.
    if not _set(execution_id, "triggering", expect=("claimed",)):
        return

    try:
        queue_url = jenkins.trigger_rebuild(job, info.parameters)
    except JenkinsUncertainError as err:
        _set(execution_id, "uncertain", expect=("triggering",),
             reason_codes=["request_outcome_unknown"], error=_describe(err))
        return
    except JenkinsError as err:
        _set(execution_id, "failed", expect=("triggering",), error=_describe(err))
        return

    _set(execution_id, "triggered", expect=("triggering",), queue_url=queue_url)

    try:
        resolved = jenkins.resolve_queue_item(
            queue_url,
            settings.rerun_queue_timeout_seconds,
            settings.rerun_queue_poll_seconds,
        )
    except JenkinsError:
        resolved = None

    if resolved is None:
        _set(execution_id, "triggered", expect=("triggered",),
             reason_codes=["queue_unresolved"])
        return

    number, url = resolved
    _set(execution_id, "triggered", expect=("triggered",),
         new_build_number=number, new_build_url=url or None)
    logger.info(
        "Rebuild triggered",
        extra={"execution_id": execution_id, "new_build_number": number},
    )


def run_once(jenkins: JenkinsClient, now: datetime | None = None) -> bool:
    """Process at most one pending execution. True if one was claimed."""

    now = now or utcnow()
    _sweep_stale(now)
    execution_id = _claim_next(now)

    if execution_id is None:
        return False

    try:
        _execute(jenkins, execution_id, now)
    except Exception as err:
        # A bug must not turn into a retry: settle by how far we got.
        logger.exception("Execution crashed", extra={"execution_id": execution_id})
        _set(execution_id, "failed", expect=("claimed",), error=_describe(err))
        _set(execution_id, "uncertain", expect=("triggering",),
             reason_codes=["crashed_after_intent"], error=_describe(err))

    return True


# ---- following the rebuild (read-only) ----------------------------------------

_FINAL_STATES = ("finished", "cancelled", "unknown")
_QUEUE_ID = re.compile(r"/queue/item/(\d+)/")


def _write_outcome(
    execution_id: int,
    state: str,
    now: datetime,
    *,
    result: str | None = None,
    duration_ms: int | None = None,
) -> None:
    with session_scope() as session:
        outcome = (
            session.query(RebuildOutcome).filter_by(execution_id=execution_id).one_or_none()
            or RebuildOutcome(execution_id=execution_id)
        )
        outcome.state = state
        outcome.build_result = result
        outcome.duration_ms = duration_ms
        outcome.finished_at = now if state in ("finished", "cancelled") else None
        outcome.checked_at = now
        session.add(outcome)


def _link_build(execution_id: int, number: int, url: str | None) -> None:
    """Record the build number once known. Also what lets a later failure of
    that build be recognised as part of this re-run chain (the budget)."""

    with session_scope() as session:
        session.execute(
            update(Execution)
            .where(
                Execution.id == execution_id,
                Execution.status == "triggered",
                Execution.new_build_number.is_(None),
            )
            .values(new_build_number=number, new_build_url=url, updated_at=utcnow())
            .execution_options(synchronize_session=False)
        )


def _find_started_build(jenkins: JenkinsClient, job: str, queue_url: str):
    """('started', number, url) | ('cancelled', None, None) | ('queued', None, None)
    | None when Jenkins could not be asked."""

    try:
        return jenkins.queue_item_state(queue_url)
    except JenkinsError:
        pass

    # Jenkins forgets a queue item a few minutes after it leaves the queue,
    # but the build it started keeps its queueId.
    match = _QUEUE_ID.search(queue_url)

    if match is None:
        return None

    try:
        found = jenkins.find_build_by_queue_id(job, int(match.group(1)))
    except JenkinsError:
        return None

    return ("started", found[0], found[1]) if found else None


def _track_one(
    jenkins: JenkinsClient,
    execution_id: int,
    job: str,
    number: int | None,
    url: str | None,
    queue_url: str | None,
    previous: str,
    give_up: bool,
    now: datetime,
) -> None:
    def keep_waiting(state: str) -> None:
        _write_outcome(execution_id, "unknown" if give_up else state, now)

    if number is None:
        if not queue_url:
            return keep_waiting(previous)

        found = _find_started_build(jenkins, job, queue_url)

        if found is None:
            return keep_waiting(previous)

        state, number, url = found

        if state == "cancelled":
            return _write_outcome(execution_id, "cancelled", now)

        if number is None:
            return keep_waiting("queued")

        _link_build(execution_id, number, url)

    try:
        building, result, duration = jenkins.get_run_status(
            url or build_url_for(jenkins.base_url, job, number)
        )
    except JenkinsError:
        return keep_waiting(previous)

    if building or result is None:
        return keep_waiting("running")

    _write_outcome(execution_id, "finished", now, result=result, duration_ms=duration)
    logger.info(
        "Rebuild finished",
        extra={"execution_id": execution_id, "result": result},
    )


def track_outcomes(jenkins: JenkinsClient, now: datetime | None = None) -> bool:
    """Check the builds our executions queued and record how they ended.

    Read-only (GETs only), so it is safe whatever MODE_INFRA is. Each build is
    re-checked at most every rebuild_poll_seconds, and abandoned as "unknown"
    after rebuild_track_hours. True if anything was checked.
    """

    settings = get_settings()
    now = now or utcnow()
    due = now - timedelta(seconds=settings.rebuild_poll_seconds)
    abandon = now - timedelta(hours=settings.rebuild_track_hours)
    work = []

    with session_scope() as session:
        for execution in session.query(Execution).filter(Execution.status == "triggered"):
            outcome = execution.outcome

            if outcome is not None and (
                outcome.state in _FINAL_STATES or outcome.checked_at > due
            ):
                continue

            work.append((
                execution.id, execution.event.job, execution.new_build_number,
                execution.new_build_url, execution.queue_url,
                outcome.state if outcome else "queued",
                execution.created_at < abandon,
            ))

    for execution_id, job, number, url, queue_url, previous, give_up in work:
        try:
            _track_one(jenkins, execution_id, job, number, url, queue_url, previous, give_up, now)
        except Exception:
            # Tracking is best effort: one bad row must not stop the others.
            logger.exception("Tracking a rebuild failed", extra={"execution_id": execution_id})

    return bool(work)
