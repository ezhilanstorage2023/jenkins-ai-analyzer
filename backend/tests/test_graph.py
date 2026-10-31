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
