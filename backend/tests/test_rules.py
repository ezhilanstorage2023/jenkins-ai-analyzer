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
], ids=["artifactory-prefix", "java-suffix", "z-run"])
def test_rules_are_fast_on_long_lines(line, stage):
    import time

    start = time.monotonic()
    evaluate_rules(line, stage)

    assert time.monotonic() - start < 1.0


def test_compiler_rule_still_matches_without_path_prefix():
    assert evaluate_rules("src/Foo.java:3: error: x", "Compile")[0].rule_id == (
        "compilation.compiler_error"
    )


@pytest.mark.parametrize("analysis, matches, expected", [
    # validated + high + rules silent -> LLM category stands
    (A("infra"), [], Reconciliation("infra", [])),
    # two matches of the *same* category are not ambiguous
    (A("infra"), [M("infra"), M("infra")], Reconciliation("infra", [])),
    # medium confidence never auto-acts, even when rules agree
    (A("infra", confidence="medium"), [M("infra")],
     Reconciliation("needs_triage", ["low_confidence"])),
    # unknown confidence behaves like low
    (A("infra", confidence="unknown"), [],
     Reconciliation("needs_triage", ["low_confidence"])),
    # unvalidated, high confidence, rules agree -> still triage, one reason
    (A("devops", validated=False), [M("devops")],
     Reconciliation("needs_triage", ["unvalidated"])),
    # everything wrong at once reports every reason, in a stable order
    (A("compilation", validated=False, confidence="low"),
     [M("infra"), M("devops")],
     Reconciliation("needs_triage", ["unvalidated", "low_confidence", "rule_ambiguous"])),
    # rules point elsewhere and the LLM has no category
    (A(None), [M("infra")], Reconciliation("needs_triage", ["rule_llm_conflict"])),
    # needs_triage from the LLM stays triage, with no reasons to report
    (A("needs_triage"), [], Reconciliation("needs_triage", [])),
])
def test_reconcile_matrix(analysis, matches, expected):
    assert reconcile(analysis, matches) == expected


# ---- false positives seen on real Jenkins logs ----------------------------

@pytest.mark.parametrize("line", [
    "HEAD is now at b0aa956 Added fix for compilation error during test stage",
    "[2026-10-31T11:14:19.384Z] ++ raise_error_record 'Compilation error'",
    "++ egrep 'error:|fatal:|Connection refused|FAILED:' -A 5",
    "[2026-10-31T11:14:19.384Z] + grep 'No space left on device' build.log",
])
def test_prose_and_shell_trace_lines_are_not_failures(line):
    assert evaluate_rules(line, None) == []


@pytest.mark.parametrize("line, rule_id", [
    ("[ERROR] COMPILATION ERROR :", "compilation.compiler_error"),
    ("Compilation failed; see the compiler error output for details.",
     "compilation.compiler_error"),
    ("fatal: write error: No space left on device", "infra.disk_full"),
    ("Caused: hudson.remoting.ChannelClosedException: Channel closed",
     "infra.channel_closed"),
])
def test_real_failure_lines_still_match(line, rule_id):
    assert [m.rule_id for m in evaluate_rules(line, None)] == [rule_id]


@pytest.mark.parametrize("line, rule_id", [
    ("Timeout waiting for agent to come back", "infra.agent_timeout"),
    ("[2026-10-31T09:32:23.623Z] All nodes of label ‘build_servers’ are offline",
     "infra.nodes_offline"),
    ("All nodes of label 'x' are offline", "infra.nodes_offline"),
    ("org.jenkinsci.plugins.workflow.steps.SynchronousResumeNotSupportedException: "
     "The Pipeline step `checkout` cannot be resumed after a controller restart.",
     "infra.controller_restart"),
])
def test_jenkins_infra_signatures(line, rule_id):
    assert [m.rule_id for m in evaluate_rules(line, None)] == [rule_id]


def test_offline_label_rule_is_not_fooled_by_unrelated_text():
    assert evaluate_rules("All nodes are fine; label X is offline later", None) == []


def test_offline_label_rule_is_fast_on_long_lines():
    import time

    start = time.monotonic()
    evaluate_rules("All nodes of label " + "x" * 50000, None)

    assert time.monotonic() - start < 1.0
