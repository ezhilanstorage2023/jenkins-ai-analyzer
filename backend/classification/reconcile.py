"""Combine the LLM analysis with rule matches into the final category."""

from dataclasses import dataclass

from classification.rules import RuleMatch


@dataclass
class Reconciliation:
    final_category: str
    reason_codes: list[str]


def reconcile(analysis: dict, matches: list[RuleMatch]) -> Reconciliation:
    """analysis is the build_analysis_response dict."""

    llm_category = analysis.get("llm_category")
    reasons = []

    if analysis.get("validated") is not True:
        reasons.append("unvalidated")

    if analysis.get("confidence") != "high":
        reasons.append("low_confidence")

    rule_categories = {match.category for match in matches}

    if len(rule_categories) > 1:
        reasons.append("rule_ambiguous")
    elif rule_categories and llm_category not in rule_categories:
        reasons.append("rule_llm_conflict")

    if reasons or not llm_category:
        return Reconciliation("needs_triage", reasons)

    return Reconciliation(llm_category, reasons)
