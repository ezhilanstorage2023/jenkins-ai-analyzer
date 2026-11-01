import json

import pytest

from graph import analyze_log, build_analysis_response
from tests.fakes import detected_json, root_cause_json, verdict_json

LOG = (
    "Started\n"
    "Compiling\n"
    "[javac] Foo.java:3: error: cannot find symbol\n"
    "BUILD FAILED\n"
    "Finished: FAILURE"
)
EVID = "[Line 3] [javac] Foo.java:3: error: cannot find symbol"


def test_happy_path_three_llm_calls(fake_llm):
    fake = fake_llm([
        detected_json([EVID]),
        root_cause_json("compilation", [EVID]),
        verdict_json(True),
    ])

    response = build_analysis_response(analyze_log(LOG), "b.log")

    assert response["error_category"] == "compilation"
    assert response["validated"] is True
    assert len(fake.calls) == 3


def test_ungrounded_evidence_exhausts_retries_to_needs_triage(fake_llm):
    bad = root_cause_json("compilation", ["[Line 99] invented"])
    fake = fake_llm([detected_json([EVID])] + [bad, verdict_json(True)] * 3)

    response = build_analysis_response(analyze_log(LOG), "b.log")

    assert response["attempts"] == 3
    assert response["validated"] is False
    assert response["error_category"] == "needs_triage"
    assert response["llm_category"] == "compilation"
    assert len(fake.calls) == 7


def test_no_error_context_short_circuits_without_llm(fake_llm):
    fake = fake_llm([])

    response = build_analysis_response(
        analyze_log("all good\nFinished: SUCCESS"),
        "b.log"
    )

    assert response["error_category"] == "needs_triage"
    assert response["validation_reason"] == "No error context found in log."
    assert fake.calls == []


def test_unparseable_llm_output_degrades(fake_llm):
    fake_llm(["garbage"] * 3)

    response = build_analysis_response(analyze_log(LOG), "b.log")

    assert response["error_category"] == "needs_triage"
    assert response["validation_reason"].startswith("LLM output invalid")


def test_secrets_never_reach_llm_prompt(fake_llm, caplog):
    log = "Authorization: Bearer SECRETTOKEN123\n" + LOG
    evid = "[Line 4] [javac] Foo.java:3: error: cannot find symbol"
    fake = fake_llm([
        detected_json([evid]),
        root_cause_json("compilation", [evid]),
        verdict_json(True),
    ])
    caplog.set_level("DEBUG")

    analyze_log(log)

    assert all("SECRETTOKEN123" not in str(call["messages"]) for call in fake.calls)
    assert "SECRETTOKEN123" not in caplog.text


def test_oversized_single_error_block_is_truncated_not_dropped(fake_llm):
    lines = ["[INFO] BUILD START"] + [
        f"[ERROR] /src/com/acme/A{i}.java:[10,5] cannot find symbol"
        for i in range(150)
    ]
    log = "\n".join(lines)
    last = "[Line 151] [ERROR] /src/com/acme/A149.java:[10,5] cannot find symbol"
    fake = fake_llm([
        detected_json([last]),
        root_cause_json("compilation", [last]),
        verdict_json(True),
    ])

    response = build_analysis_response(analyze_log(log), "b.log")

    assert response["validation_reason"] != "No error context found in log."
    assert response["error_category"] == "compilation"
    assert len(fake.calls) == 3

    prompt = fake.calls[0]["messages"][0]["content"]
    assert last in prompt


def test_partly_invented_evidence_fails_validation(fake_llm):
    mixed = root_cause_json(
        "compilation",
        [EVID, "[Line 2] Change 12345 by alice introduced the bug"]
    )
    fake_llm([detected_json([EVID])] + [mixed, verdict_json(True)] * 3)

    response = build_analysis_response(analyze_log(LOG), "b.log")

    assert response["validated"] is False
    assert response["error_category"] == "needs_triage"


def test_analyze_log_fast_on_long_lines(fake_llm):
    import time

    fake_llm(["garbage"] * 3)
    log = "a-" * 500_000 + "\nERROR: build failed"

    start = time.monotonic()
    analyze_log(log)

    assert time.monotonic() - start < 3.0


# ---- options: merged agent, validator gate, per-agent models ---------------

def combined_json(category, evidence, confidence="high", main_error="Build failed"):
    return json.dumps({
        "main_error": main_error,
        "root_cause": "r",
        "error_category": category,
        "evidence": evidence,
        "affected_component": "c",
        "recommended_fix": "f",
        "confidence": confidence,
    })


@pytest.fixture
def merged(monkeypatch):
    from settings import get_settings

    monkeypatch.setenv("LLM_MERGE_DETECTOR", "true")
    get_settings.cache_clear()


def test_merged_agent_uses_two_calls(fake_llm, merged):
    fake = fake_llm([combined_json("infra", [EVID]), verdict_json(True)])

    response = build_analysis_response(analyze_log(LOG), "b.log")

    assert len(fake.calls) == 2
    assert response["error_category"] == "infra"
    assert response["validated"] is True
    assert response["evidence"] == [EVID]
    assert response["failure_summary"] == "Build failed"


def test_merged_agent_ungrounded_evidence_never_validates(fake_llm, merged):
    bad = combined_json("infra", ["[Line 99] invented"])
    fake_llm([bad, verdict_json(True)] * 3)

    response = build_analysis_response(analyze_log(LOG), "b.log")

    assert response["validated"] is False
    assert response["error_category"] == "needs_triage"


def test_merged_agent_retry_carries_validator_feedback(fake_llm, merged):
    fake = fake_llm([
        combined_json("devops", [EVID]), verdict_json(False, "unsupported"),
        combined_json("infra", [EVID]), verdict_json(True),
    ])

    response = build_analysis_response(analyze_log(LOG), "b.log")

    assert response["attempts"] == 2 and response["error_category"] == "infra"
    first, second = fake.calls[0]["messages"][0]["content"], fake.calls[2]["messages"][0]["content"]
    assert "Validator feedback" not in first
    assert "Validator feedback" in second and "unsupported" in second


def test_first_attempt_prompt_has_no_empty_previous_block(fake_llm):
    fake = fake_llm([
        detected_json([EVID]), root_cause_json("infra", [EVID]), verdict_json(True),
    ])

    analyze_log(LOG)

    root_cause_prompt = fake.calls[1]["messages"][0]["content"]
    assert "Previous root cause" not in root_cause_prompt


def test_fix_does_not_gate_validation_when_disabled(fake_llm, monkeypatch):
    from settings import get_settings

    monkeypatch.setenv("VALIDATOR_CHECKS_FIX", "false")
    get_settings.cache_clear()
    payload = json.loads(root_cause_json("infra", [EVID]))
    payload["recommended_fix"] = " "
    fake = fake_llm([detected_json([EVID]), json.dumps(payload), verdict_json(True)])

    response = build_analysis_response(analyze_log(LOG), "b.log")

    assert response["validated"] is True and response["error_category"] == "infra"
    validator_prompt = fake.calls[2]["messages"][0]["content"]
    assert "recommended_fix" not in validator_prompt  # fix is not shown to it


def test_empty_fix_still_blocks_by_default(fake_llm):
    payload = json.loads(root_cause_json("infra", [EVID]))
    payload["recommended_fix"] = " "
    fake_llm([detected_json([EVID])] + [json.dumps(payload), verdict_json(True)] * 3)

    response = build_analysis_response(analyze_log(LOG), "b.log")

    assert response["validated"] is False


def test_each_agent_uses_its_own_model(fake_llm, monkeypatch):
    from settings import get_settings

    monkeypatch.setenv("LLM_MODEL_DETECTOR", "m-detect")
    monkeypatch.setenv("LLM_MODEL_VALIDATOR", "m-validate")
    get_settings.cache_clear()
    fake = fake_llm([
        detected_json([EVID]), root_cause_json("infra", [EVID]), verdict_json(True),
    ])

    analyze_log(LOG)

    assert [c["model"] for c in fake.calls] == [
        "m-detect", "openai/gpt-oss-120b", "m-validate",
    ]
