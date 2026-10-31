"""Authenticated review API for shadow-mode events and labels."""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from auth import require_role
from clients.jenkins import (
    JenkinsError,
    build_url_for,
    jenkins_client_from_settings,
)
from models import ErrorCategory
from settings import get_settings
from store.db import get_session, utcnow
from store.models import Analysis, Event, Label, User

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
        "received_at": _time(event.received_at),
        "final_category": analysis.final_category if analysis else None,
        "llm_category": analysis.response.get("llm_category") if analysis else None,
        "validated": analysis.response.get("validated") if analysis else None,
        "reason_codes": analysis.reason_codes if analysis else [],
        "label_verdict": label.verdict if label else None,
    }


@router.get("/events")
def list_events(
    status: str | None = None,
    category: str | None = None,
    unlabeled: bool = False,
    limit: int = Query(50, ge=1),
    user: User = Depends(require_role("viewer")),
    session: Session = Depends(get_session),
):
    query = (
        session.query(Event)
        .outerjoin(Analysis, Analysis.event_id == Event.id)
        .outerjoin(Label, Label.analysis_id == Analysis.id)
    )

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
            "executed": False,
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


@router.get("/jenkins/failed-builds")
def failed_builds(
    count: int = Query(20, ge=1, le=100),
    user: User = Depends(require_role("viewer")),
    session: Session = Depends(get_session),
    jenkins=Depends(get_jenkins),
):
    """Failed builds of the configured jobs, read-only against Jenkins.

    Each row carries any existing Event's id/status/final category, so the
    UI can tell new builds apart from ones already queued or classified.
    """

    settings = get_settings()
    controller_id = settings.jenkins_controller_id

    if not settings.jenkins_jobs:
        raise HTTPException(409, "No jobs configured (set JENKINS_JOBS).")

    rows = []

    for job in settings.jenkins_jobs:
        try:
            builds = jenkins.list_builds(job, count)
        except JenkinsError as err:
            raise HTTPException(503, f"Jenkins request failed: {err}")

        failed = [b for b in builds if b.result == "FAILURE"]

        if not failed:
            continue

        existing = {
            event.build_number: event
            for event in session.query(Event).filter(
                Event.controller_id == controller_id,
                Event.job == job,
                Event.build_number.in_([b.number for b in failed]),
            )
        }

        for build in failed:
            event = existing.get(build.number)
            analysis = event.analysis if event else None

            rows.append({
                "job": job,
                "build_number": build.number,
                "build_url": build_url_for(jenkins.base_url, job, build.number),
                "result": build.result,
                "timestamp": build.timestamp,
                "event_id": event.id if event else None,
                "event_status": event.status if event else None,
                "final_category": analysis.final_category if analysis else None,
            })

    return rows


class AnalyzeIn(BaseModel):
    job: str
    build_number: int = Field(ge=1)


@router.post("/jenkins/analyze")
def analyze_build(
    body: AnalyzeIn,
    user: User = Depends(require_role("operator")),
    session: Session = Depends(get_session),
):
    """Enqueue one failed build for shadow classification by the worker.

    Idempotent: an existing event for the build is returned unchanged.
    Only configured jobs may be enqueued.
    """

    settings = get_settings()

    if body.job not in settings.jenkins_jobs:
        raise HTTPException(422, "Job is not in the configured job list.")

    controller_id = settings.jenkins_controller_id

    existing = session.query(Event).filter_by(
        controller_id=controller_id,
        job=body.job,
        build_number=body.build_number,
    ).one_or_none()

    if existing is not None:
        return {"event_id": existing.id, "status": existing.status}

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
