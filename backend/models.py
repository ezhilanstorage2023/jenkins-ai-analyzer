from pydantic import BaseModel


class AnalysisResponse(BaseModel):
    filename: str
    failure_summary: str
    root_cause: str
    affected_component: str
    evidence: list[str]
    recommended_fix: str
    confidence: str