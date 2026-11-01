"""DevOps owner notification, shadow mode (P3).

Builds the email that *would* be sent and records the decision. There is no
SMTP code in this project: nothing here, or anywhere, sends mail.
"""

import hashlib
import re
from datetime import datetime, timedelta

from sqlalchemy import or_
from sqlalchemy.orm import Session

import owners
from clients.jenkins import JenkinsClient
from enrichment import EnrichmentData
from executor import kill_switch_on
from settings import get_settings
from store.models import EmailDelivery, Event, Notification
from utils.redaction import redact

_LINE_PREFIX = re.compile(r"^\[Line \d+\]\s*")
_TIMESTAMP = re.compile(r"\[\d{4}-\d{2}-\d{2}[T ][\d:.]+Z?\]\s*")
_DIGITS = re.compile(r"\d+")
_CONTROL = re.compile(r"[\r\n\t\x00-\x1f\x7f]+")

_MAX_EVIDENCE_LINES = 5
_MAX_LINE = 300
_MAX_TEXT = 800
_MAX_BODY = 4000


def failure_signature(job: str, category: str, evidence: list[str]) -> str:
    """Stable id for 'the same failure again': the job, the category and the
    first evidence line with line numbers, timestamps and digits removed."""

    first = evidence[0] if evidence else ""
    first = _LINE_PREFIX.sub("", first)
    first = _TIMESTAMP.sub("", first)
    first = _DIGITS.sub("#", first).strip().lower()

    return hashlib.sha256(f"{job}|{category}|{first}".encode()).hexdigest()[:16]


def _one_line(text: str, cap: int) -> str:
    return _CONTROL.sub(" ", text or "").strip()[:cap]


def _block(text: str, cap: int) -> str:
    return (text or "").strip()[:cap] or "(not available)"


def render(
    event: Event,
    category: str,
    analysis: dict,
    data: EnrichmentData,
    review_url: str | None,
) -> tuple[str, str]:
    subject = _one_line(f"[Jenkins] {event.job} #{event.build_number} failed ({category})", 200)

    evidence = [
        _one_line(line, _MAX_LINE)
        for line in (analysis.get("evidence") or [])[:_MAX_EVIDENCE_LINES]
    ]
    lines = [
        f"Job: {_one_line(event.job, 300)}",
        f"Build: {event.build_url}",
        f"Failing stage: {_one_line(data.failing_stage or 'unknown', 200)}",
        f"Category: {category}",
        "",
        "Root cause:",
        _block(analysis.get("root_cause"), _MAX_TEXT),
        "",
        "Evidence (verbatim from the build log):",
        *(evidence or ["(none)"]),
        "",
        "Suggested next step (advisory, not verified):",
        _block(analysis.get("recommended_fix"), _MAX_TEXT),
    ]

    if review_url:
        lines += ["", f"Review: {review_url.rstrip('/')}/ui/"]

    lines += ["", "This message was generated automatically from the build log."]

    return subject, redact("\n".join(lines))[:_MAX_BODY]


def build_preview(
    session: Session,
    event: Event,
    category: str,
    analysis: dict,
    data: EnrichmentData,
    jenkins: JenkinsClient | None,
    now: datetime,
) -> Notification:
    """The Notification to record for this event. Not added to the session
    here, so a failure while building it cannot disturb the caller."""

    settings = get_settings()
    resolution = owners.resolve(event.job, jenkins)
    signature = failure_signature(event.job, category, analysis.get("evidence") or [])
    subject, body = render(event, category, analysis, data, settings.review_ui_url)

    decision = "would_send"
    reasons = list(resolution.reasons)

    if not resolution.recipients:
        decision = "blocked"
    elif kill_switch_on(session):
        decision, reasons = "blocked", reasons + ["kill_switch"]
    else:
        window_start = now - timedelta(hours=settings.notify_rate_window_hours)
        earlier = (
            session.query(Notification)
            .join(Event, Event.id == Notification.event_id)
            .outerjoin(EmailDelivery, EmailDelivery.notification_id == Notification.id)
            .filter(
                # An email that failed or was skipped never reached the owner:
                # it must not silence the next occurrence. (Shadow previews have
                # no delivery row and count, as before.)
                or_(
                    EmailDelivery.id.is_(None),
                    EmailDelivery.status.notin_(("failed", "skipped")),
                ),
                Event.controller_id == event.controller_id,
                Event.job == event.job,
                Notification.signature == signature,
                Notification.decision == "would_send",
                Notification.created_at >= window_start,
            )
            .count()
        )

        if earlier:
            decision, reasons = "suppressed", reasons + ["rate_limited"]

    return Notification(
        event_id=event.id,
        recipients=resolution.recipients,
        recipient_source=resolution.source,
        subject=subject,
        body=body,
        signature=signature,
        decision=decision,
        reason_codes=reasons,
        mode=settings.mode_devops,
        recipient_ok=None,
        created_at=now,
    )
