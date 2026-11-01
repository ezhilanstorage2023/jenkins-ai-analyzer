from datetime import datetime

from sqlalchemy import JSON, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Event(Base):
    """One failed Jenkins build; doubles as the worker queue entry."""

    __tablename__ = "events"
    __table_args__ = (
        UniqueConstraint(
            "controller_id", "job", "build_number", name="uq_event_build"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    controller_id: Mapped[str] = mapped_column(String(100))
    job: Mapped[str] = mapped_column(String(500))
    build_number: Mapped[int]
    build_url: Mapped[str] = mapped_column(String(1000))
    result: Mapped[str | None] = mapped_column(String(50))
    source: Mapped[str] = mapped_column(String(20))

    # queued -> processing -> done | failed
    status: Mapped[str] = mapped_column(String(20), default="queued")
    attempts: Mapped[int] = mapped_column(default=0)
    next_attempt_at: Mapped[datetime]
    claimed_at: Mapped[datetime | None]
    last_error: Mapped[str | None] = mapped_column(String(500))
    received_at: Mapped[datetime]

    enrichment: Mapped["Enrichment | None"] = relationship(back_populates="event")
    analysis: Mapped["Analysis | None"] = relationship(back_populates="event")
    action: Mapped["ProposedAction | None"] = relationship(back_populates="event")
    execution: Mapped["Execution | None"] = relationship(back_populates="event")
    notification: Mapped["Notification | None"] = relationship(back_populates="event")


class Enrichment(Base):
    __tablename__ = "enrichments"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), unique=True)
    failing_stage: Mapped[str | None] = mapped_column(String(500))
    parameters: Mapped[dict] = mapped_column(JSON)
    causes: Mapped[list] = mapped_column(JSON)
    artifactory_links: Mapped[list] = mapped_column(JSON)
    gerrit_scope: Mapped[dict] = mapped_column(JSON)
    # Redacted parser excerpt only; raw logs are never stored.
    log_excerpt: Mapped[str] = mapped_column(Text)
    log_bytes: Mapped[int]
    log_truncated: Mapped[bool]

    event: Mapped[Event] = relationship(back_populates="enrichment")


class Analysis(Base):
    __tablename__ = "analyses"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), unique=True)
    response: Mapped[dict] = mapped_column(JSON)
    rule_matches: Mapped[list] = mapped_column(JSON)
    final_category: Mapped[str] = mapped_column(String(20))
    reason_codes: Mapped[list] = mapped_column(JSON)
    model: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime]

    event: Mapped[Event] = relationship(back_populates="analysis")
    label: Mapped["Label | None"] = relationship(back_populates="analysis")


class ProposedAction(Base):
    """Shadow-mode record of the remediation the system would take."""

    __tablename__ = "proposed_actions"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), unique=True)
    action_type: Mapped[str] = mapped_column(String(50))
    target: Mapped[dict] = mapped_column(JSON)
    allowed: Mapped[bool]
    reason_codes: Mapped[list] = mapped_column(JSON)
    created_at: Mapped[datetime]

    event: Mapped[Event] = relationship(back_populates="action")


class Execution(Base):
    """A remediation actually performed (or refused) for an event.

    pending -> triggering -> triggered
    pending -> skipped            (a guard said no; reason_codes say which)
    triggering -> failed          (Jenkins refused the request)
    triggering -> uncertain       (the request may or may not have been applied:
                                   transport error or crash). Never retried
                                   automatically: a blind retry could rebuild twice.
    """

    __tablename__ = "executions"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), unique=True)
    action_type: Mapped[str] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(20), default="pending")
    reason_codes: Mapped[list] = mapped_column(JSON, default=list)
    # First event of this chain of re-runs; budgets count per root.
    root_event_id: Mapped[int]
    queue_url: Mapped[str | None] = mapped_column(String(1000))
    new_build_number: Mapped[int | None]
    new_build_url: Mapped[str | None] = mapped_column(String(1000))
    error: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]

    event: Mapped[Event] = relationship(back_populates="execution")
    outcome: Mapped["RebuildOutcome | None"] = relationship(back_populates="execution")


class Notification(Base):
    """Shadow-mode record of the email that would be sent to a job's owner.

    decision: would_send | suppressed (rate limited) | blocked (no valid
    recipient, kill switch, ...). Nothing is ever sent from this record.
    """

    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), unique=True)
    recipients: Mapped[list] = mapped_column(JSON, default=list)
    recipient_source: Mapped[str | None] = mapped_column(String(20))
    subject: Mapped[str] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text)
    signature: Mapped[str] = mapped_column(String(64))
    decision: Mapped[str] = mapped_column(String(20))
    reason_codes: Mapped[list] = mapped_column(JSON, default=list)
    mode: Mapped[str] = mapped_column(String(20))
    # Reviewer feedback: was the recipient the right person? (None = unrated)
    recipient_ok: Mapped[bool | None]
    reviewed_by: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime]

    event: Mapped[Event] = relationship(back_populates="notification")
    delivery: Mapped["EmailDelivery | None"] = relationship(back_populates="notification")


class EmailDelivery(Base):
    """An email actually handed to the SMTP server (or refused), one per
    notification. Only created when MODE_DEVOPS=auto.

    pending -> claimed -> skipped            (a guard said no)
                       -> sending -> sent
                                  -> failed     (the server refused, or we never
                                                 connected: nothing was delivered)
                                  -> uncertain  (the connection broke after the
                                                 message may have been accepted:
                                                 never resent automatically)
    """

    __tablename__ = "email_deliveries"

    id: Mapped[int] = mapped_column(primary_key=True)
    notification_id: Mapped[int] = mapped_column(
        ForeignKey("notifications.id"), unique=True
    )
    status: Mapped[str] = mapped_column(String(20), default="pending")
    reason_codes: Mapped[list] = mapped_column(JSON, default=list)
    message_id: Mapped[str | None] = mapped_column(String(300))
    error: Mapped[str | None] = mapped_column(String(500))
    sent_at: Mapped[datetime | None]
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]

    notification: Mapped["Notification"] = relationship(back_populates="delivery")


class RebuildOutcome(Base):
    """What happened to the build an Execution queued (read-only tracking).

    state: queued (waiting for an executor) | running | finished | cancelled |
    unknown (never resolved within rebuild_track_hours). build_result is
    Jenkins' own verdict once finished: SUCCESS, FAILURE, UNSTABLE, ABORTED...
    """

    __tablename__ = "rebuild_outcomes"

    id: Mapped[int] = mapped_column(primary_key=True)
    execution_id: Mapped[int] = mapped_column(ForeignKey("executions.id"), unique=True)
    state: Mapped[str] = mapped_column(String(20))
    build_result: Mapped[str | None] = mapped_column(String(30))
    duration_ms: Mapped[int | None]
    finished_at: Mapped[datetime | None]
    checked_at: Mapped[datetime]

    execution: Mapped["Execution"] = relationship(back_populates="outcome")


class Control(Base):
    """Runtime switches an operator can flip without a restart."""

    __tablename__ = "controls"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(String(200))
    updated_by: Mapped[str] = mapped_column(String(200))
    updated_at: Mapped[datetime]


class Label(Base):
    __tablename__ = "labels"

    id: Mapped[int] = mapped_column(primary_key=True)
    analysis_id: Mapped[int] = mapped_column(
        ForeignKey("analyses.id"), unique=True
    )
    reviewer: Mapped[str] = mapped_column(String(200))
    # correct | incorrect
    verdict: Mapped[str] = mapped_column(String(20))
    correct_category: Mapped[str | None] = mapped_column(String(20))
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime]

    analysis: Mapped[Analysis] = relationship(back_populates="label")


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(200), unique=True)
    password_hash: Mapped[str] = mapped_column(String(300))
    # viewer < labeler < operator
    role: Mapped[str] = mapped_column(String(20))
