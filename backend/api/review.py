"""Authenticated review API for shadow-mode events and labels."""

import time
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from auth import require_role
from clients.jenkins import (
    JenkinsError,
    build_url_for,
    jenkins_client_from_settings,
)
from executor import kill_switch_on, set_kill_switch
from models import ErrorCategory
from settings import get_settings
from store.db import get_session, utcnow
from store.models import Analysis, EmailDelivery, Event, Label, Notification, User

router = APIRouter(prefix="/api")

_MAX_LIMIT = 200


def get_jenkins():
    """Request-scoped read-only Jenkins client; overridable in tests.

    Always the live Jenkins (from settings) — no local/mock data path in
    production; the fake is injected only via dependency override in tests.
    """

    try:
        client = jenkins_client_from_settings()
    except JenkinsError as err:
        raise HTTPException(503, f"Jenkins is not configured: {err}")

    try:
        yield client
    finally:
        client.close()


def _time(value):
    return value.isoformat() if value else None


def _rebuild(execution) -> dict | None:
    """How the build an execution queued ended, for the UI. None if nothing was
    queued (shadow mode, skipped, failed...)."""

    if execution is None or execution.status != "triggered":
        return None

    outcome = execution.outcome

    return {
        "state": outcome.state if outcome else "queued",
        "result": outcome.build_result if outcome else None,
        "duration_ms": outcome.duration_ms if outcome else None,
        "finished_at": _time(outcome.finished_at) if outcome else None,
        "checked_at": _time(outcome.checked_at) if outcome else None,
        "build_number": execution.new_build_number,
        "build_url": execution.new_build_url,
    }


def _event_summary(event: Event) -> dict:
    analysis = event.analysis
    label = analysis.label if analysis else None

    return {
        "id": event.id,
        "job": event.job,
        "build_number": event.build_number,
        "build_url": event.build_url,
        "source": event.source,
        "status": event.status,
        "attempts": event.attempts,
        "last_error": event.last_error,
        "received_at": _time(event.received_at),
        "final_category": analysis.final_category if analysis else None,
        "llm_category": analysis.response.get("llm_category") if analysis else None,
        "validated": analysis.response.get("validated") if analysis else None,
        "reason_codes": analysis.reason_codes if analysis else [],
        "label_verdict": label.verdict if label else None,
        "rebuild": _rebuild(event.execution),
    }


@router.get("/events")
def list_events(
    status: str | None = None,
    category: str | None = None,
    unlabeled: bool = False,
    ids: str | None = Query(None, description="comma-separated event ids"),
    limit: int = Query(50, ge=1),
    user: User = Depends(require_role("viewer")),
    session: Session = Depends(get_session),
):
    query = (
        session.query(Event)
        .outerjoin(Analysis, Analysis.event_id == Event.id)
        .outerjoin(Label, Label.analysis_id == Analysis.id)
    )

    if ids is not None:
        try:
            wanted = [int(part) for part in ids.split(",") if part.strip()]
        except ValueError:
            raise HTTPException(422, "ids must be comma-separated integers.")

        if len(wanted) > _MAX_LIMIT:
            raise HTTPException(422, f"At most {_MAX_LIMIT} ids per request.")

        query = query.filter(Event.id.in_(wanted))

    if status:
        query = query.filter(Event.status == status)

    if category:
        query = query.filter(Analysis.final_category == category)

    if unlabeled:
        query = query.filter(Analysis.id.isnot(None), Label.id.is_(None))

    events = (
        query.order_by(Event.received_at.desc(), Event.id.desc())
        .limit(min(limit, _MAX_LIMIT))
        .all()
    )

    return [_event_summary(event) for event in events]


@router.get("/events/{event_id}")
def event_detail(
    event_id: int,
    user: User = Depends(require_role("viewer")),
    session: Session = Depends(get_session),
):
    event = session.get(Event, event_id)

    if event is None:
        raise HTTPException(404, "Event not found.")

    enrichment, analysis, action = event.enrichment, event.analysis, event.action
    execution = event.execution
    notification = event.notification
    label = analysis.label if analysis else None

    return {
        "event": {
            "id": event.id,
            "controller_id": event.controller_id,
            "job": event.job,
            "build_number": event.build_number,
            "build_url": event.build_url,
            "source": event.source,
            "status": event.status,
            "attempts": event.attempts,
            "last_error": event.last_error,
            "received_at": _time(event.received_at),
        },
        "enrichment": enrichment and {
            "failing_stage": enrichment.failing_stage,
            "parameters": enrichment.parameters,
            "causes": enrichment.causes,
            "artifactory_links": enrichment.artifactory_links,
            "gerrit_scope": enrichment.gerrit_scope,
            "log_excerpt": enrichment.log_excerpt,
            "log_bytes": enrichment.log_bytes,
            "log_truncated": enrichment.log_truncated,
        },
        "analysis": analysis and {
            "final_category": analysis.final_category,
            "reason_codes": analysis.reason_codes,
            "response": analysis.response,
            "rule_matches": analysis.rule_matches,
            "model": analysis.model,
            "created_at": _time(analysis.created_at),
        },
        "action": action and {
            "action_type": action.action_type,
            "target": action.target,
            "allowed": action.allowed,
            "reason_codes": action.reason_codes,
            "executed": bool(execution and execution.status == "triggered"),
        },
        "notification": notification and {
            "decision": notification.decision,
            "reason_codes": notification.reason_codes,
            "recipients": notification.recipients,
            "recipient_source": notification.recipient_source,
            "subject": notification.subject,
            "body": notification.body,
            "mode": notification.mode,
            "sent": bool(
                notification.delivery and notification.delivery.status == "sent"
            ),
            "delivery": notification.delivery and {
                "status": notification.delivery.status,
                "reason_codes": notification.delivery.reason_codes,
                "message_id": notification.delivery.message_id,
                "error": notification.delivery.error,
                "sent_at": _time(notification.delivery.sent_at),
            },
            "recipient_ok": notification.recipient_ok,
            "reviewed_by": notification.reviewed_by,
        },
        "execution": execution and {
            "status": execution.status,
            "reason_codes": execution.reason_codes,
            "queue_url": execution.queue_url,
            "new_build_number": execution.new_build_number,
            "new_build_url": execution.new_build_url,
            "error": execution.error,
            "updated_at": _time(execution.updated_at),
            "rebuild": _rebuild(execution),
        },
        "label": label and {
            "reviewer": label.reviewer,
            "verdict": label.verdict,
            "correct_category": label.correct_category,
            "note": label.note,
            "created_at": _time(label.created_at),
        },
    }


class LabelIn(BaseModel):
    verdict: Literal["correct", "incorrect"]
    correct_category: ErrorCategory | None = None
    note: str | None = Field(default=None, max_length=2000)


@router.post("/events/{event_id}/label")
def label_event(
    event_id: int,
    body: LabelIn,
    user: User = Depends(require_role("labeler")),
    session: Session = Depends(get_session),
):
    event = session.get(Event, event_id)

    if event is None:
        raise HTTPException(404, "Event not found.")

    if event.analysis is None:
        raise HTTPException(422, "Event has no analysis to label yet.")

    if body.verdict == "incorrect" and body.correct_category is None:
        raise HTTPException(422, "correct_category is required when incorrect.")

    label = event.analysis.label or Label(analysis_id=event.analysis.id)
    label.reviewer = user.username
    label.verdict = body.verdict
    label.correct_category = (
        body.correct_category.value if body.verdict == "incorrect" else None
    )
    label.note = body.note
    label.created_at = utcnow()
    session.add(label)
    # Commit before answering: "saved" must mean saved.
    session.commit()

    return {"status": "saved"}


@router.get("/metrics/accuracy")
def accuracy(
    user: User = Depends(require_role("viewer")),
    session: Session = Depends(get_session),
):
    totals: dict[str, dict] = {}

    for final_category, verdict in (
        session.query(Analysis.final_category, Label.verdict)
        .join(Label, Label.analysis_id == Analysis.id)
    ):
        bucket = totals.setdefault(final_category, {"labeled": 0, "correct": 0})
        bucket["labeled"] += 1
        bucket["correct"] += verdict == "correct"

    for bucket in totals.values():
        bucket["accuracy"] = round(bucket["correct"] / bucket["labeled"], 3)

    return totals


def _is_recent(timestamp_ms, max_age_days):
    """Unknown times are kept; max_age_days == 0 means no limit."""

    if not max_age_days or timestamp_ms is None:
        return True

    return timestamp_ms >= (time.time() - max_age_days * 86400) * 1000


def _matches(name, q):
    return not q or q.strip().lower() in name.lower()


def _failed_row(jenkins, job, number, result, timestamp, event):
    analysis = event.analysis if event else None

    return {
        "job": job,
        "build_number": number,
        "build_url": build_url_for(jenkins.base_url, job, number),
        "result": result,
        "timestamp": timestamp,
        "event_id": event.id if event else None,
        "event_status": event.status if event else None,
        "final_category": analysis.final_category if analysis else None,
        "rebuild": _rebuild(event.execution) if event else None,
    }


def _existing_events(session, controller_id, pairs):
    """{(job, build_number): Event} for the given (job, build_number) pairs."""

    jobs = {job for job, _ in pairs}
    wanted = set(pairs)

    if not wanted:
        return {}

    query = session.query(Event).filter(
        Event.controller_id == controller_id,
        Event.job.in_(jobs),
    )

    return {
        (event.job, event.build_number): event
        for event in query
        if (event.job, event.build_number) in wanted
    }


@router.get("/metrics/notifications")
def notification_metrics(
    user: User = Depends(require_role("viewer")),
    session: Session = Depends(get_session),
):
    """What the shadow email stage would have done, and how reviewers rate
    the recipients (the evidence for the 'zero misdirected emails' bar)."""

    rows = session.query(
        Notification.decision, Notification.recipient_source,
        Notification.recipient_ok, Notification.signature, Event.job,
    ).join(Event, Event.id == Notification.event_id).all()

    by_delivery: dict[str, int] = {}
    for (status,) in session.query(EmailDelivery.status):
        by_delivery[status] = by_delivery.get(status, 0) + 1

    by_decision: dict[str, int] = {}
    by_source: dict[str, int] = {}
    suppressed: dict[tuple, int] = {}
    feedback = {"ok": 0, "wrong": 0, "unrated": 0}

    for decision, source, ok, signature, job in rows:
        by_decision[decision] = by_decision.get(decision, 0) + 1

        if source:
            by_source[source] = by_source.get(source, 0) + 1

        if decision == "suppressed":
            key = (job, signature)
            suppressed[key] = suppressed.get(key, 0) + 1

        if decision == "would_send":
            feedback["unrated" if ok is None else ("ok" if ok else "wrong")] += 1

    top = sorted(suppressed.items(), key=lambda item: -item[1])[:10]

    return {
        "total": len(rows),
        "by_decision": by_decision,
        "by_source": by_source,
        "by_delivery": by_delivery,
        "recipient_feedback": feedback,
        "top_suppressed": [
            {"job": job, "signature": signature, "count": count}
            for (job, signature), count in top
        ],
    }


class RecipientFeedbackIn(BaseModel):
    recipient_ok: bool


@router.post("/events/{event_id}/notification-label")
def label_notification(
    event_id: int,
    body: RecipientFeedbackIn,
    user: User = Depends(require_role("labeler")),
    session: Session = Depends(get_session),
):
    event = session.get(Event, event_id)

    if event is None or event.notification is None:
        raise HTTPException(404, "No email preview for this event.")

    if event.notification.decision != "would_send":
        raise HTTPException(422, "Only a would-send preview can be rated.")

    event.notification.recipient_ok = body.recipient_ok
    event.notification.reviewed_by = user.username
    session.commit()

    return {"status": "saved"}


@router.get("/jenkins/failed-builds")
def failed_builds(
    response: Response,
    count: int | None = Query(None, ge=1, le=500),
    include_disabled: bool = False,
    max_age_days: int = Query(30, ge=0, le=3650),
    sort: Literal["timestamp", "job"] = "timestamp",
    offset: int = Query(0, ge=0),
    q: str | None = Query(None, max_length=200),
    user: User = Depends(require_role("viewer")),
    session: Session = Depends(get_session),
    jenkins=Depends(get_jenkins),
):
    """Currently failing jobs, read-only against Jenkins.

    JENKINS_JOBS unset: every job on the controller whose latest completed
    build failed (one tree request). Disabled jobs are hidden unless
    `include_disabled`. A page of at most `count` rows (default 200) starting
    at `offset` is returned; the size of the whole filtered set is in the
    `X-Total-Count` header.

    JENKINS_JOBS set: only those jobs, with each job's last `count` (default
    20, max 100) builds that failed.

    Both: failures older than `max_age_days` (default 30; 0 = no limit) are
    left out, builds with an unknown time are kept, `q` keeps jobs whose
    name contains it (case-insensitive), and `sort` is newest failure first
    (`timestamp`) or by job name (`job`).

    Each row carries any existing Event's id/status/final category, so the
    UI can tell new builds apart from ones already queued or classified.
    """

    settings = get_settings()
    controller_id = settings.jenkins_controller_id

    if not settings.jenkins_jobs:
        limit = count or 200

        try:
            jobs = jenkins.list_jobs()
        except JenkinsError as err:
            raise HTTPException(503, f"Jenkins request failed: {err}")

        failing = [
            job for job in jobs
            if job.last_result == "FAILURE"
            and job.last_number
            and (include_disabled or not job.disabled)
            and _is_recent(job.last_timestamp, max_age_days)
            and _matches(job.full_name, q)
        ]

        if sort == "job":
            failing.sort(key=lambda job: job.full_name.lower())
        else:
            failing.sort(key=lambda job: job.last_timestamp or 0, reverse=True)

        response.headers["X-Total-Count"] = str(len(failing))
        failing = failing[offset:offset + limit]

        existing = _existing_events(
            session, controller_id,
            [(job.full_name, job.last_number) for job in failing],
        )

        return [
            _failed_row(
                jenkins, job.full_name, job.last_number, job.last_result,
                job.last_timestamp,
                existing.get((job.full_name, job.last_number)),
            )
            for job in failing
        ]

    per_job = min(count or 20, 100)
    rows = []

    for job in settings.jenkins_jobs:
        try:
            builds = jenkins.list_builds(job, per_job)
        except JenkinsError as err:
            raise HTTPException(503, f"Jenkins request failed: {err}")

        failed = [b for b in builds if b.result == "FAILURE"]

        if not failed:
            continue

        existing = _existing_events(
            session, controller_id, [(job, b.number) for b in failed]
        )

        for build in failed:
            if not _is_recent(build.timestamp, max_age_days) or not _matches(job, q):
                continue

            rows.append(_failed_row(
                jenkins, job, build.number, build.result, build.timestamp,
                existing.get((job, build.number)),
            ))

    if sort == "job":
        rows.sort(key=lambda row: (row["job"].lower(), -row["build_number"]))
    else:
        rows.sort(key=lambda row: row["timestamp"] or 0, reverse=True)

    response.headers["X-Total-Count"] = str(len(rows))

    return rows


class AnalyzeIn(BaseModel):
    job: str
    build_number: int = Field(ge=1)


def _enqueue_build(session: Session, jenkins, body: AnalyzeIn) -> dict:
    """Enqueue one failed build for shadow classification by the worker.
    Raises HTTPException (422 invalid/unknown/not failed, 503 Jenkins down).

    Idempotent: an existing event for the build is returned unchanged.
    With JENKINS_JOBS set only those jobs may be enqueued; otherwise any job
    on the controller, provided Jenkins confirms the build exists and failed.
    """

    settings = get_settings()

    parts = body.job.split("/")

    if any(part in ("", ".", "..") for part in parts):
        raise HTTPException(422, "Invalid job path.")

    if settings.jenkins_jobs and body.job not in settings.jenkins_jobs:
        raise HTTPException(422, "Job is not in the configured job list.")

    controller_id = settings.jenkins_controller_id

    existing = session.query(Event).filter_by(
        controller_id=controller_id,
        job=body.job,
        build_number=body.build_number,
    ).one_or_none()

    if existing is not None:
        return {"event_id": existing.id, "status": existing.status}

    if not settings.jenkins_jobs:
        # No allowlist: Jenkins is the authority on what exists and failed.
        url = build_url_for(jenkins.base_url, body.job, body.build_number)

        try:
            info = jenkins.get_build_info(url)
        except JenkinsError as err:
            if "HTTP 404" in str(err):
                raise HTTPException(422, "Build not found in Jenkins.")

            raise HTTPException(503, f"Jenkins request failed: {err}")

        if info.result != "FAILURE":
            raise HTTPException(422, "Build did not fail.")

    now = utcnow()
    event = Event(
        controller_id=controller_id,
        job=body.job,
        build_number=body.build_number,
        # Canonical URL from our configured base, never caller-supplied.
        build_url=build_url_for(settings.jenkins_base_url, body.job, body.build_number),
        result="FAILURE",
        source="manual",
        next_attempt_at=now,
        received_at=now,
    )
    session.add(event)
    session.commit()

    return {"event_id": event.id, "status": event.status}


@router.post("/jenkins/analyze")
def analyze_build(
    body: AnalyzeIn,
    user: User = Depends(require_role("operator")),
    session: Session = Depends(get_session),
    jenkins=Depends(get_jenkins),
):
    return _enqueue_build(session, jenkins, body)


class AnalyzeBatchIn(BaseModel):
    builds: list[AnalyzeIn] = Field(min_length=1, max_length=50)


@router.post("/jenkins/analyze-batch")
def analyze_batch(
    body: AnalyzeBatchIn,
    user: User = Depends(require_role("operator")),
    session: Session = Depends(get_session),
    jenkins=Depends(get_jenkins),
):
    """Enqueue up to 50 builds; one result per build, in request order.

    A bad build (invalid, unknown, did not fail) reports its own error and
    does not stop the others. If Jenkins itself is unavailable the remaining
    builds are not attempted (each would otherwise wait out its timeout).
    """

    results = []
    jenkins_down = False

    for item in body.builds:
        entry = {"job": item.job, "build_number": item.build_number}

        if jenkins_down:
            entry.update(error="Jenkins unavailable; not attempted.", status_code=503)
            results.append(entry)
            continue

        try:
            entry.update(_enqueue_build(session, jenkins, item))
        except HTTPException as err:
            entry.update(error=err.detail, status_code=err.status_code)
            jenkins_down = err.status_code == 503

        results.append(entry)

    return results


class KillSwitchIn(BaseModel):
    enabled: bool


def _controls(session: Session) -> dict:
    settings = get_settings()

    return {
        "kill_switch": kill_switch_on(session),
        "kill_switch_from_settings": settings.kill_switch,
        "mode_infra": settings.mode_infra,
        "mode_devops": settings.mode_devops,
    }


@router.get("/controls")
def controls(
    user: User = Depends(require_role("viewer")),
    session: Session = Depends(get_session),
):
    return _controls(session)


@router.post("/controls/kill-switch")
def kill_switch(
    body: KillSwitchIn,
    user: User = Depends(require_role("operator")),
    session: Session = Depends(get_session),
):
    """Stop (or resume) every executor immediately. Resuming cannot override
    a kill switch set in settings; it only clears the runtime toggle."""

    set_kill_switch(session, body.enabled, user.username)
    session.commit()

    return _controls(session)


@router.get("/me")
def whoami(user: User = Depends(require_role("viewer"))):
    return {"username": user.username, "role": user.role}
