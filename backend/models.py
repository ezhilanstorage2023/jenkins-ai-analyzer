from enum import Enum
from typing import Literal

from pydantic import BaseModel, field_validator


class ErrorCategory(str, Enum):
    INFRA = "infra"
    DEVOPS = "devops"
    COMPILATION = "compilation"
    NEEDS_TRIAGE = "needs_triage"


# LLM output schemas


class DetectedErrors(BaseModel):
    main_error: str
    error_messages: list[str] = []
    affected_component: str = "Unknown"
    evidence: list[str] = []


class RootCauseResult(BaseModel):
    root_cause: str
    error_category: ErrorCategory
    evidence: list[str] = []
    affected_component: str = "Unknown"
    recommended_fix: str
    confidence: Literal["high", "medium", "low"]

    @field_validator("error_category", "confidence", mode="before")
    @classmethod
    def normalize_label(cls, value):
        if isinstance(value, str):
            return value.strip().lower()
        return value


class CombinedResult(RootCauseResult):
    """Detection + root cause in one call (llm_merge_detector)."""

    main_error: str = "Unknown"


class ValidationVerdict(BaseModel):
    valid: bool
    reason: str = ""
    unsupported_claims: list[str] = []


# API response


class AnalysisResponse(BaseModel):
    filename: str
    failure_summary: str
    root_cause: str
    error_category: ErrorCategory
    llm_category: ErrorCategory | None
    validated: bool
    validation_reason: str
    attempts: int
    affected_component: str
    evidence: list[str]
    recommended_fix: str
    confidence: Literal["high", "medium", "low", "unknown"]
