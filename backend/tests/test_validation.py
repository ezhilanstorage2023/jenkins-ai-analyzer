from graph import build_analysis_response, validate_analysis
from models import AnalysisResponse


def test_validation_rejects_missing_evidence():

    parsed_log = """
    [Line 10] ERROR: Compilation failed
    """

    result = {
        "root_cause": "Compilation failure",
        "evidence": [],
        "recommended_fix": "Check compilation errors"
    }

    problems = validate_analysis(
        result,
        parsed_log
    )

    assert len(problems) > 0


def test_validation_accepts_valid_evidence():

    parsed_log = """
    [Line 10] ERROR: Compilation failed
    """

    result = {
        "root_cause": "Compilation failure",
        "evidence": [
            "[Line 10] ERROR: Compilation failed"
        ],
        "recommended_fix": "Check compilation errors"
    }

    problems = validate_analysis(
        result,
        parsed_log
    )

    assert problems == []


def test_validation_rejects_empty_fix():

    parsed_log = """
    [Line 10] ERROR: Compilation failed
    """

    result = {
        "root_cause": "Compilation failure",
        "evidence": [
            "[Line 10] ERROR: Compilation failed"
        ],
        "recommended_fix": ""
    }

    problems = validate_analysis(
        result,
        parsed_log
    )

    assert len(problems) > 0


VALID_ROOT_CAUSE = {
    "root_cause": "src does not compile",
    "error_category": "compilation",
    "evidence": ["[Line 3] error: cannot find symbol"],
    "recommended_fix": "fix import",
    "confidence": "high",
    "affected_component": "javac",
}


def test_validated_result_keeps_llm_category():
    response = build_analysis_response(
        {
            "detected_errors": {"main_error": "javac failed"},
            "root_cause": VALID_ROOT_CAUSE,
            "validation_result": {"valid": True, "reason": "ok"},
            "attempt_count": 1,
        },
        "b.log"
    )

    assert response["error_category"] == "compilation"
    assert response["validated"] is True
    assert response["attempts"] == 1
    AnalysisResponse(**response)


def test_unvalidated_result_forced_to_needs_triage():
    response = build_analysis_response(
        {
            "detected_errors": {},
            "root_cause": VALID_ROOT_CAUSE,
            "validation_result": {"valid": False, "reason": "unsupported"},
            "attempt_count": 3,
        },
        "b.log"
    )

    assert response["error_category"] == "needs_triage"
    assert response["llm_category"] == "compilation"
    assert response["validated"] is False
    assert response["validation_reason"] == "unsupported"


def test_empty_result_does_not_raise():
    response = build_analysis_response({}, "b.log")

    assert response["error_category"] == "needs_triage"
    assert response["failure_summary"] == "Unknown"
    assert response["confidence"] == "unknown"
    AnalysisResponse(**response)