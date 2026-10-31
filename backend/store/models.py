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
