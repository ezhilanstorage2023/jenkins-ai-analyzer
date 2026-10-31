import logging
import secrets

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from pydantic import BaseModel, Field
from urllib.parse import urlsplit

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from clients.jenkins import build_url_for
from settings import get_settings
from store.db import get_session, utcnow
from store.models import Event
from utils.urls import url_under_base

logger = logging.getLogger("webhooks")

router = APIRouter()


class JenkinsFailureHook(BaseModel):
    controller_id: str = Field(default="default", max_length=100)
    job: str = Field(min_length=1, max_length=500)
    build_number: int = Field(ge=1, le=2**31 - 1)
    build_url: str = Field(max_length=1000)
    result: str | None = None


def _find_event(session: Session, hook: JenkinsFailureHook) -> Event | None:
    return session.query(Event).filter_by(
        controller_id=hook.controller_id,
        job=hook.job,
        build_number=hook.build_number,
    ).one_or_none()


@router.post("/webhooks/jenkins", status_code=202)
def receive_jenkins_failure(
    hook: JenkinsFailureHook,
    response: Response,
    x_webhook_token: str | None = Header(default=None),
    session: Session = Depends(get_session),
):
    settings = get_settings()
    expected = settings.jenkins_webhook_token

    if expected is None:
        raise HTTPException(503, "Webhook intake is not configured.")

    if x_webhook_token is None or not secrets.compare_digest(
        x_webhook_token.encode(),
        expected.get_secret_value().encode(),
    ):
        raise HTTPException(401, "Invalid webhook token.")

    if hook.controller_id != settings.jenkins_controller_id:
        raise HTTPException(422, "Unknown controller_id.")

    base = settings.jenkins_base_url

    if base is None or not url_under_base(hook.build_url, base):
        raise HTTPException(
            422,
            "build_url is not under the configured Jenkins URL."
        )

    # The URL must be exactly this job's build: otherwise a payload could
    # make the service account read another job's console, or poison the
    # dedupe key for the real build.
    canonical = build_url_for(base, hook.job, hook.build_number)
    sent = urlsplit(hook.build_url)

    if sent.query or sent.fragment or sent.path != urlsplit(canonical).path:
        raise HTTPException(
            422,
            "build_url does not match job and build_number."
        )

    if hook.result is not None and hook.result.upper() != "FAILURE":
        response.status_code = 200
        return {"status": "ignored"}

    existing = _find_event(session, hook)

    if existing is not None:
        response.status_code = 200
        return {"event_id": existing.id, "duplicate": True}

    now = utcnow()
    event = Event(
        controller_id=hook.controller_id,
        job=hook.job,
        build_number=hook.build_number,
        build_url=canonical,
        result=hook.result,
        source="webhook",
        next_attempt_at=now,
        received_at=now,
    )

    try:
        session.add(event)
        # Commit before answering: 202 must mean the event is stored.
        session.commit()
    except IntegrityError:
        # A concurrent delivery of the same build won the insert.
        session.rollback()
        existing = _find_event(session, hook)
        response.status_code = 200
        return {"event_id": existing.id, "duplicate": True}

    logger.info(
        "Webhook event queued",
        extra={"event_id": event.id, "job": hook.job, "build": hook.build_number},
    )

    return {"event_id": event.id, "duplicate": False}
