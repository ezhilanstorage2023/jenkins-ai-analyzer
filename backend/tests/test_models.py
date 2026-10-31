import pytest
from pydantic import ValidationError

from models import ErrorCategory, RootCauseResult

BASE = {"root_cause": "x", "recommended_fix": "y", "confidence": "high"}


def test_category_values():
    assert {c.value for c in ErrorCategory} == {
        "infra",
        "devops",
        "compilation",
        "needs_triage",
    }


def test_root_cause_normalizes_case_and_whitespace():
    r = RootCauseResult(**BASE | {"error_category": " Compilation ", "confidence": "High"})

    assert r.error_category is ErrorCategory.COMPILATION
    assert r.confidence == "high"


def test_root_cause_rejects_legacy_category():
    with pytest.raises(ValidationError):
        RootCauseResult(**BASE | {"error_category": "Developer-Side Error"})
