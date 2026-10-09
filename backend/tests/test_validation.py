from graph import build_analysis_response, validate_analysis


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


def test_build_analysis_response_includes_error_category():
    result = {
        "detected_errors": {
            "main_error": "Compilation failed"
        },
        "root_cause": {
            "root_cause": "Application source does not compile",
            "error_category": "Developer-Side Error",
            "affected_component": "compiler",
            "evidence": ["ERROR: Compilation failed"],
            "recommended_fix": "Correct the source compilation error",
            "confidence": "high",
        },
    }

    response = build_analysis_response(result, "build.log")

    assert response["error_category"] == "Developer-Side Error"