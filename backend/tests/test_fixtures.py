from pathlib import Path

import pytest

from graph import analyze_log, build_analysis_response
from tests.fakes import detected_json, root_cause_json, verdict_json
from utils.log_processor import extract_error_context

FIXTURES = Path(__file__).parent / "fixtures" / "logs"

CASES = [
    ("infra_agent_offline.log", "ChannelClosedException", "infra"),
    ("devops_missing_credentials.log", "Could not find credentials entry", "devops"),
    ("compilation_java_symbol.log", "cannot find symbol", "compilation"),
    ("test_failure.log", "FAILED: com.acme.FooTest.testParse", "needs_triage"),
]


@pytest.mark.parametrize("name, signature, category", CASES)
def test_parser_keeps_signature_line(name, signature, category):
    assert signature in extract_error_context((FIXTURES / name).read_text())


@pytest.mark.parametrize("name, signature, category", CASES)
def test_pipeline_end_to_end_per_category(fake_llm, name, signature, category):
    log = (FIXTURES / name).read_text()
    parsed = extract_error_context(log)
    evidence = next(line for line in parsed.splitlines() if signature in line)
    fake_llm([
        detected_json([evidence]),
        root_cause_json(category, [evidence]),
        verdict_json(True),
    ])

    response = build_analysis_response(analyze_log(log), name)

    assert response["error_category"] == category
    assert response["evidence"] == [evidence]
