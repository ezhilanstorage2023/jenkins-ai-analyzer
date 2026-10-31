"""Webhook -> worker -> review API, all in-process with fakes."""

from fastapi.testclient import TestClient

import main
from tests.conftest import BUILD_URL, COMPILATION_LOG, WEBHOOK_TOKEN
from tests.fakes import responses_for
from worker import run_once

client = TestClient(main.app)

HOOK = {
    "job": "acme/build",
    "build_number": 7,
    "build_url": BUILD_URL,
    "result": "FAILURE",
}


def test_webhook_to_review_end_to_end(webhook_env, fake_llm, jenkins, labeler):
    response = client.post(
        "/webhooks/jenkins",
        json=HOOK,
        headers={"X-Webhook-Token": WEBHOOK_TOKEN},
    )
    event_id = response.json()["event_id"]

    fake_llm(responses_for(COMPILATION_LOG, "cannot find symbol", "compilation"))
    assert run_once(jenkins) is True

    detail = client.get(f"/api/events/{event_id}", auth=labeler).json()

    assert detail["event"]["status"] == "done"
    assert detail["analysis"]["final_category"] == "compilation"
    assert detail["action"]["action_type"] == "exclude_culprit"
    assert detail["action"]["allowed"] is True
    assert detail["action"]["executed"] is False

    labeled = client.post(
        f"/api/events/{event_id}/label",
        json={"verdict": "correct"},
        auth=labeler,
    )
    assert labeled.status_code == 200

    # Shadow mode: the worker only ever read from Jenkins.
    assert {request.method for request in jenkins.fake.seen} == {"GET"}
