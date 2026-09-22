from graph import validate_analysis


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