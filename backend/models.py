from typing import Literal

from pydantic import BaseModel


class AnalysisResponse(BaseModel):
    filename: str
    failure_summary: str
    root_cause: str
    error_category: Literal[
        "Infrastructure Error",
        "Developer-Side Error",
        "DevOps-Side Error",
    ]
    affected_component: str
    evidence: list[str]
    recommended_fix: str
    confidence: str