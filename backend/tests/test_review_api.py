import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import main
from store.db import session_scope, utcnow
from store.models import Analysis, Enrichment, Event, ProposedAction

client = TestClient(main.app)


def _add_event(session, number, final_category=None, minutes_ago=0):
    received = utcnow() - timedelta(minutes=minutes_ago)
    event = Event(
        controller_id="default",
        job="acme/build",
        build_number=number,
        build_url=f"https://ci.example.com/job/acme/job/build/{number}/",
        result="FAILURE",
        source="webhook",
        status="done" if final_category else "queued",
        next_attempt_at=received,
        received_at=received,
    )
    session.add(event)
    session.flush()

    if final_category:
        session.add(Enrichment(
            event_id=event.id,
            failing_stage="Build",
            parameters={"BRANCH": "main"},
            causes=[],
            artifactory_links=[],
            gerrit_scope={},
            log_excerpt="[Line 3] No space left on device",
            log_bytes=100,
            log_truncated=False,
        ))
        session.add(Analysis(
            event_id=event.id,
            response={
                "llm_category": final_category,
                "validated": True,
                "root_cause": "disk full",
            },
            rule_matches=[],
            final_category=final_category,
            reason_codes=[],
            model="m",
            created_at=received,
        ))

    if final_category == "infra":
        session.add(ProposedAction(
            event_id=event.id,
            action_type="rerun",
            target={"job": "acme/build", "parameters": {}},
            allowed=False,
            reason_codes=["job_not_allowed"],
            created_at=received,
        ))

    return event.id


@pytest.fixture
def seeded():
    with session_scope() as session:
        return SimpleNamespace(
            infra=_add_event(session, 1, "infra", minutes_ago=30),
            triage=_add_event(session, 2, "needs_triage", minutes_ago=20),
            queued=_add_event(session, 3, None, minutes_ago=10),
        )


def label(event_id, body, auth):
    return client.post(f"/api/events/{event_id}/label", json=body, auth=auth)


def test_events_require_auth():
    assert client.get("/api/events").status_code == 401


def test_list_newest_first(seeded, viewer):
    rows = client.get("/api/events", auth=viewer).json()

    assert [row["id"] for row in rows] == [seeded.queued, seeded.triage, seeded.infra]
    assert rows[2]["final_category"] == "infra"
    assert rows[0]["final_category"] is None


def test_list_filters_unlabeled_triage(seeded, viewer):
    rows = client.get(
        "/api/events?category=needs_triage&unlabeled=true", auth=viewer
    ).json()

    assert [row["id"] for row in rows] == [seeded.triage]


def test_detail_sections(seeded, viewer):
    detail = client.get(f"/api/events/{seeded.infra}", auth=viewer).json()

    assert detail["event"]["job"] == "acme/build"
    assert detail["analysis"]["final_category"] == "infra"
    assert detail["action"]["action_type"] == "rerun"
    assert detail["label"] is None
    assert "log_excerpt" in detail["enrichment"]
    assert "console" not in json.dumps(detail)


def test_detail_missing_is_404(viewer):
    assert client.get("/api/events/999", auth=viewer).status_code == 404


def test_viewer_cannot_label(seeded, viewer):
    assert label(seeded.infra, {"verdict": "correct"}, viewer).status_code == 403


def test_incorrect_requires_category(seeded, labeler):
    assert label(seeded.infra, {"verdict": "incorrect"}, labeler).status_code == 422


def test_label_requires_analysis(seeded, labeler):
    assert label(seeded.queued, {"verdict": "correct"}, labeler).status_code == 422


def test_label_upsert_and_accuracy(seeded, labeler):
    label(seeded.infra, {"verdict": "incorrect", "correct_category": "devops"}, labeler)
    response = label(seeded.infra, {"verdict": "correct", "note": "rechecked"}, labeler)

    assert response.status_code == 200

    detail = client.get(f"/api/events/{seeded.infra}", auth=labeler).json()
    assert detail["label"]["verdict"] == "correct"
    assert detail["label"]["reviewer"] == "labeler1"
    assert detail["label"]["correct_category"] is None

    metrics = client.get("/api/metrics/accuracy", auth=labeler).json()
    assert metrics == {"infra": {"labeled": 1, "correct": 1, "accuracy": 1.0}}


def test_limit_is_capped(seeded, viewer):
    assert client.get("/api/events?limit=1000", auth=viewer).status_code == 200
