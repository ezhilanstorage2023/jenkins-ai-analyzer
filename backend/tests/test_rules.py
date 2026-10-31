from pathlib import Path

import pytest

from classification.reconcile import Reconciliation, reconcile
from classification.rules import RuleMatch, evaluate_rules

FIX = Path(__file__).parent / "fixtures" / "logs"


@pytest.mark.parametrize("name, category", [
    ("infra_agent_offline.log", "infra"),
    ("devops_missing_credentials.log", "devops"),
    ("compilation_java_symbol.log", "compilation"),
    ("test_failure.log", "needs_triage"),
])
def test_fixture_rule_category(name, category):
    log = (FIX / name).read_text()

    categories = {m.category for m in evaluate_rules(log, None)}

    # Each fixture must be unambiguous: exactly its own category.
    assert categories == {category}


def test_compilation_rules_skipped_outside_build_stage():
    assert evaluate_rules("Foo.java:3: error: x", "Deploy") == []


def test_compilation_rules_apply_in_compile_stage():
    matches = evaluate_rules("Foo.java:3: error: x", "Compile")

    assert [m.rule_id for m in matches] == ["compilation.compiler_error"]


def test_rule_match_reports_line_number():
    match = evaluate_rules("ok\nNo space left on device\n", None)[0]

    assert (match.rule_id, match.line_number) == ("infra.disk_full", 2)


def test_one_match_per_rule():
    log = "No space left on device\nNo space left on device\n"

    assert len(evaluate_rules(log, None)) == 1


def A(category, validated=True, confidence="high"):
    return {"llm_category": category, "validated": validated, "confidence": confidence}


def M(category):
    return RuleMatch(f"{category}.x", category, 1, "l")


def test_agreement_keeps_category():
    assert reconcile(A("infra"), [M("infra")]) == Reconciliation("infra", [])


def test_rules_silent_keeps_validated_high_category():
    assert reconcile(A("devops"), []).final_category == "devops"


def test_conflict_routes_to_triage():
    assert reconcile(A("compilation"), [M("infra")]) == Reconciliation(
        "needs_triage", ["rule_llm_conflict"]
    )


def test_ambiguous_rules_route_to_triage():
    assert reconcile(A("infra"), [M("infra"), M("devops")]).reason_codes == [
        "rule_ambiguous"
    ]


def test_low_confidence_and_unvalidated_reasons():
    result = reconcile(A("infra", validated=False, confidence="medium"), [])

    assert result == Reconciliation("needs_triage", ["unvalidated", "low_confidence"])


def test_missing_llm_category_is_triage():
    assert reconcile(A(None), []).final_category == "needs_triage"


@pytest.mark.parametrize("line, stage", [
    ("artifactory" + "x" * 20000, None),
    ("y" * 20000 + ".javaz", "Compile"),
    ("z" * 20000, "Compile"),
])
def test_rules_are_fast_on_long_lines(line, stage):
    import time

    start = time.monotonic()
    evaluate_rules(line, stage)

    assert time.monotonic() - start < 1.0


def test_compiler_rule_still_matches_without_path_prefix():
    assert evaluate_rules("src/Foo.java:3: error: x", "Compile")[0].rule_id == (
        "compilation.compiler_error"
    )
