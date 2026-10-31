import pytest
from sqlalchemy.exc import IntegrityError

from store.db import session_scope, utcnow
from store.models import Analysis, Event


def _event(**overrides):
    base = dict(
        controller_id="default",
        job="acme/build",
        build_number=7,
        build_url="https://ci.example.com/job/acme/job/build/7/",
        source="webhook",
        next_attempt_at=utcnow(),
        received_at=utcnow(),
    )
    return Event(**base | overrides)


def test_event_roundtrip_defaults():
    with session_scope() as session:
        session.add(_event())

    with session_scope() as session:
        event = session.query(Event).one()
        assert event.status == "queued"
        assert event.attempts == 0


def test_duplicate_build_rejected():
    with pytest.raises(IntegrityError):
        with session_scope() as session:
            session.add(_event())
            session.add(_event())


def test_json_columns_roundtrip():
    with session_scope() as session:
        event = _event()
        session.add(event)
        session.flush()
        session.add(Analysis(
            event_id=event.id,
            response={"validated": True},
            rule_matches=[],
            final_category="infra",
            reason_codes=[],
            model="m",
            created_at=utcnow(),
        ))

    with session_scope() as session:
        assert session.query(Analysis).one().response == {"validated": True}


def test_utcnow_is_naive():
    assert utcnow().tzinfo is None
