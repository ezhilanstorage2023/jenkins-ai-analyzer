import pytest

from backfill import backfill
from clients.jenkins import JenkinsClient
from store.db import session_scope
from store.models import Event
from tests.fakes import FakeJenkins

BASE = "https://ci.example.com/"


@pytest.fixture
def jenkins_builds():
    fake = FakeJenkins({"/job/acme/job/build/api/json": (200, {"builds": [
        {"number": 7, "url": BASE + "job/acme/job/build/7/", "result": "FAILURE"},
        {"number": 6, "url": BASE + "job/acme/job/build/6/", "result": "SUCCESS"},
        {"number": 5, "url": BASE + "job/acme/job/build/5/", "result": "FAILURE"},
    ]})})

    return JenkinsClient(
        BASE, "svc", "tok",
        timeout=5, max_log_bytes=1000, transport=fake.transport,
    )


def test_backfill_queues_failures_only(jenkins_builds):
    assert backfill(jenkins_builds, "acme/build", 3) == (2, 0)

    with session_scope() as session:
        events = session.query(Event).order_by(Event.build_number).all()
        assert [(e.build_number, e.source, e.status) for e in events] == [
            (5, "backfill", "queued"),
            (7, "backfill", "queued"),
        ]


def test_backfill_skips_existing_events(jenkins_builds):
    backfill(jenkins_builds, "acme/build", 3)

    assert backfill(jenkins_builds, "acme/build", 3) == (0, 2)
