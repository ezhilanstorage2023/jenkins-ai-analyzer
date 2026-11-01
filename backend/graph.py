from utils.log_processor import cap_line_length, extract_error_context
from utils.redaction import redact
from models import (
    CombinedResult,
    DetectedErrors,
    ErrorCategory,
    RootCauseResult,
    ValidationVerdict,
)
from settings import get_settings
from typing import TypedDict
from langgraph.graph import StateGraph, START, END
import json
import logging
import llm

logger = logging.getLogger("graph")

NO_ERROR_CONTEXT = "No error context found in log."

# Category definitions shared by the root-cause and merged prompts. Keep in
# sync with models.ErrorCategory (see CLAUDE.md "Cross-file invariants").
CATEGORY_RULES = """\
- Classify the root cause as exactly one of these categories:
    - infra: Jenkins agent/node offline or disconnected, executor or channel
        failure, network/DNS, storage or disk space, out-of-memory kill, cloud
        resource, or an external service being unavailable (including
        Artifactory/registry 5xx or timeouts).
    - devops: Jenkinsfile or pipeline script error, build/deploy automation,
        credentials, permissions, missing tool or toolchain, or environment
        configuration.
    - compilation: application source code fails to compile or link
        (compiler or linker errors in project source).
    - needs_triage: failing tests, mixed or unclear causes, or evidence
        insufficient to choose one of the above.
- Set error_category to exactly one of: infra, devops, compilation, needs_triage.
- Choose the category best supported by the root cause and evidence; do not
    classify based only on the fact that the failure occurred in Jenkins."""

FIX_RULES = """\
- Do not invent missing information.
- If the exact root cause cannot be determined, explicitly say so.
- Confidence must reflect the strength of the available evidence.
- Recommended fix must be directly related to the identified root cause.
- Do not invent configuration values, commands, or versions that are not supported by the evidence.
- If the exact fix cannot be determined from the log, explicitly say so.
- Do not suggest specific replacement flags unless the log explicitly identifies the intended flag.
- If the intended replacement cannot be determined from the evidence, say that it cannot be determined."""


def _retry_context(state) -> str:
    """Previous attempt + validator feedback; empty on the first attempt."""

    if not state["root_cause"]:
        return ""

    return f"""
Previous root cause:
{json.dumps(state["root_cause"], indent=2)}

Validator feedback:
{state["validation_result"]}
"""

class State(TypedDict):
    log_content: str
    parsed_log: str
    parser_metadata: dict
    detected_errors: dict
    root_cause: dict
    validation_result: dict
    attempt_count: int

def parser_node(state: State):
    logger.debug(
        "Parser received log",
        extra={"characters": len(state["log_content"])}
    )

    log = state["log_content"]

    lines = log.splitlines()

    parsed_log = extract_error_context(
        log,
        context_before=3,
        context_after=5,
        max_blocks=20
    )

    # Keep the LLM input within a safe size.
    # Instead of blindly taking the first 10,000 characters,
    # prioritize the latest error contexts.
    max_characters = get_settings().parser_max_characters

    if len(parsed_log) > max_characters:

        blocks = parsed_log.split(
            "\n\n--- ERROR CONTEXT ---\n\n"
        )

        selected_blocks = []
        current_length = 0

        # Start from the latest error block
        for block in reversed(blocks):

            block_length = len(block)

            separator_length = (
                len("\n\n--- ERROR CONTEXT ---\n\n")
                if selected_blocks
                else 0
            )

            if (
                current_length
                + separator_length
                + block_length
                > max_characters
            ):
                break

            selected_blocks.append(block)

            current_length += (
                separator_length
                + block_length
            )

        # The latest block alone exceeds the limit: keep its tail,
        # cut on line boundaries so "[Line N]" prefixes stay intact.
        if not selected_blocks:
            block_lines = blocks[-1].splitlines()
            tail = []
            tail_length = 0

            for line in reversed(block_lines):
                if tail_length + len(line) + 1 > max_characters:
                    break

                tail.append(line)
                tail_length += len(line) + 1

            if not tail:
                tail = [block_lines[-1][:max_characters]]

            tail.reverse()
            selected_blocks.append("\n".join(tail))

        # Restore chronological order
        selected_blocks.reverse()

        parsed_log = (
            "\n\n--- ERROR CONTEXT ---\n\n"
            .join(selected_blocks)
        )

    error_count = sum(
        1
        for line in lines
        if "ERROR" in line.upper()
    )

    failure_count = sum(
        1
        for line in lines
        if "FAILED" in line.upper()
        or "FAILURE" in line.upper()
    )

    parser_metadata = {
        "total_lines": len(lines),
        "error_count": error_count,
        "failure_count": failure_count,
        "extracted_characters": len(parsed_log)
    }

    logger.info("Parser finished", extra=parser_metadata)

    update = {
        "parsed_log": parsed_log,
        "parser_metadata": parser_metadata
    }

    # Nothing for the LLM to analyze: end without calling it.
    if not parsed_log:
        update["validation_result"] = {
            "valid": False,
            "reason": NO_ERROR_CONTEXT
        }

    return update


def parser_router(state: State):
    if not state["parsed_log"]:
        return "end"

    return "continue"

def validate_evidence(evidence, parsed_log):
    valid_evidence = []

    for item in evidence:

        if item in parsed_log:
            valid_evidence.append(item)

    return valid_evidence

def validate_analysis(result, parsed_log, check_fix=True):
    problems = []

    evidence = result.get("evidence", [])

    # 1. Root cause must have evidence
    if not evidence:
        problems.append(
            "Root cause has no supporting evidence."
        )

    # 2. Every evidence item must exist exactly in the parsed log,
    #    including items already filtered out by validate_evidence
    for item in evidence + result.get("rejected_evidence", []):
        if item not in parsed_log:
            problems.append(
                f"Evidence not found in parsed log: {item}"
            )

    # 3. Recommended fix must exist (only when the fix gates validation)
    recommended_fix = result.get(
        "recommended_fix",
        ""
    )

    if check_fix and not recommended_fix.strip():
        problems.append(
            "Recommended fix is empty."
        )

    return problems

def error_detector_node(state: State):
    prompt = f"""
You are an Error Detection Agent analyzing a Jenkins build log.


Analyze the following parsed log:

{state["parsed_log"]}

Identify the important build errors.

Return ONLY valid JSON in this exact structure:

{{
    "main_error": "the primary error",
    "error_messages": [
        "important error message 1",
        "important error message 2"
    ],
    "affected_component": "affected command, tool, or component",
    "evidence": [
        "exact log line or exact text copied from the log"
    ]
}}

Rules:
- Evidence must be copied exactly from the provided log.
- Do not paraphrase evidence.
- Do not modify evidence in any way.
- Preserve every character exactly, including spaces, punctuation,
  capitalization, brackets, and line numbers.
- Do not remove spaces from line numbers.
- Do not add or remove words.
- Do not normalize or "clean up" the log text.
- Do not generate evidence yourself.
- Every evidence item must be an exact substring of the provided log.
- Include the line number exactly as it appears in the provided log.
- If exact evidence cannot be identified, return an empty evidence list.
"""

    detected_errors = llm.complete_json(
        prompt,
        DetectedErrors,
        model=get_settings().model_for("detector"),
    ).model_dump(mode="json")

    detected_errors["evidence"] = validate_evidence(
        detected_errors.get("evidence", []),
        state["parsed_log"]
    )

    logger.info(
        "Error detector finished",
        extra={"evidence_count": len(detected_errors["evidence"])}
    )

    return {
        "detected_errors": detected_errors
    }

def root_cause_node(state: State):
    attempt_count = state["attempt_count"] + 1

    logger.info("Root cause attempt", extra={"attempt": attempt_count})

    detected_errors = json.dumps(
    state["detected_errors"],
    indent=2
)

    prompt = f"""
You are a Root Cause Analysis Agent for a Jenkins build failure.

Detected errors:

{detected_errors}
{_retry_context(state)}
Determine the most defensible root cause based only on
the available evidence.

Return ONLY valid JSON in this exact structure:

{{
    "root_cause": "the most defensible root cause",
    "error_category": "infra, devops, compilation, or needs_triage",
    "evidence": [
        "evidence from the detected errors"
    ],
    "affected_component": "affected command, tool, or component",
    "recommended_fix": "an actionable fix based only on the available evidence",
    "confidence": "high, medium, or low"
}}

Rules:
- Use only information supported by the evidence.
{CATEGORY_RULES}
{FIX_RULES}

- Evidence must be copied exactly from the detected errors.
- Do not modify, correct, normalize, shorten, or rephrase evidence.
- Preserve spaces, punctuation, capitalization, and line numbers exactly.
- Do not generate new evidence.
- Every evidence item must appear verbatim in the detected errors.
"""

    root_cause = llm.complete_json(
        prompt,
        RootCauseResult,
        model=get_settings().model_for("root_cause"),
    ).model_dump(mode="json")

    proposed_evidence = root_cause.get("evidence", [])

    root_cause["evidence"] = validate_evidence(
        proposed_evidence,
        state["parsed_log"]
    )

    # Keep what was dropped so the validator rejects partly invented
    # evidence instead of passing on the surviving items alone.
    root_cause["rejected_evidence"] = [
        item for item in proposed_evidence
        if item not in root_cause["evidence"]
    ]

    logger.info(
        "Root cause finished",
        extra={
            "evidence_count": len(root_cause["evidence"]),
            "category": root_cause.get("error_category"),
        }
    )


    return {
    "root_cause": root_cause,
    "attempt_count": attempt_count
    }

def validator_node(state: State):
    detected_errors = json.dumps(
        state["detected_errors"],
        indent=2
    )

    settings = get_settings()
    check_fix = settings.validator_checks_fix

    # With check_fix off the fix is advisory text: it is neither shown to the
    # validator nor allowed to veto the category.
    reviewed = (
        state["root_cause"]
        if check_fix
        else {
            key: value
            for key, value in state["root_cause"].items()
            if key != "recommended_fix"
        }
    )

    root_cause = json.dumps(
        reviewed,
        indent=2
    )

    deterministic_problems = validate_analysis(
        state["root_cause"],
        state["parsed_log"],
        check_fix=check_fix,
    )

    logger.info(
        "Deterministic validation",
        extra={"problem_count": len(deterministic_problems)}
    )

    fix_rules = (
        "- The recommended fix must be reasonable and directly related to the root cause.\n"
        if check_fix else ""
    )
    fix_rules_2 = (
        "- A recommended fix must not assume that a missing file is optional.\n"
        "- Do not recommend deleting, skipping, disabling, or bypassing a failing step\n"
        "  unless the log provides evidence that doing so is appropriate.\n"
        "- If multiple fixes are possible, distinguish evidence-supported remediation\n"
        "  from possible alternatives.\n"
        "- A fix may describe what needs to be investigated when the exact remediation\n"
        "  cannot be determined from the log.\n"
        if check_fix else ""
    )
    category_rule = (
        "- A recommended fix must not depend on information that is absent from the evidence.\n"
        if check_fix else
        "- The error_category must follow from the evidence: infra (agent/network/"
        "storage/external service), devops (pipeline script, credentials, "
        "permissions, tooling, configuration), compilation (project source fails "
        "to compile/link), needs_triage (tests, mixed or unclear).\n"
    )

    prompt = f"""
You are a Validation Agent for a Jenkins build failure analysis system.

Detected errors:

{detected_errors}

{"Proposed root cause and recommended fix:" if check_fix else "Proposed root cause and category:"}

{root_cause}

{"Determine whether BOTH the proposed root cause and the recommended fix" if check_fix else "Determine whether the proposed root cause and its error_category"}
are directly supported by the detected errors.

The validator must be conservative. It should reject {"recommendations" if check_fix else "conclusions"}
that require assumptions not established by the log.

Return ONLY valid JSON in this exact structure:

{{
    "valid": true,
    "reason": "short explanation",
    "unsupported_claims": [
        "claim that is not directly supported"
    ]
}}

Rules:
- valid must be either true or false.
- The root cause must be supported by the evidence.
{fix_rules}- Do not assume information that is not present.
- Distinguish direct evidence from speculation.
- If the root cause {"or recommended fix " if check_fix else ""}contains unsupported assumptions,
  set valid to false.
- If there are no unsupported claims, return an empty list.
{fix_rules_2}- Evidence must be distinguishable from interpretation.
- Do not treat an AI-generated explanation as direct log evidence.
- A root cause must be supported by the actual evidence provided.
{category_rule}"""

    validation_result = llm.complete_json(
        prompt,
        ValidationVerdict,
        model=settings.model_for("validator"),
    ).model_dump(mode="json")

    if deterministic_problems:
        validation_result["valid"] = False

        validation_result.setdefault(
            "unsupported_claims",
            []
        )

        validation_result["unsupported_claims"].extend(
            deterministic_problems
        )

        validation_result["reason"] = (
            "Deterministic validation failed: "
            + "; ".join(deterministic_problems)
        )

    logger.info(
        "Validator verdict",
        extra={"valid": validation_result["valid"]}
    )

    return {
        "validation_result": validation_result
    }

def validator_router(state: State):

    validation = state["validation_result"]
    attempt_count = state["attempt_count"]

    if validation["valid"]:
        return "end"

    if attempt_count >= get_settings().max_validation_attempts:
        return "end"

    return "retry"

def analyze_node(state: State):
    """Detection + root cause in one call (llm_merge_detector).

    Fills detected_errors and root_cause the same way the two-agent path
    does, so the validator, router and response builder are shared.
    """

    attempt_count = state["attempt_count"] + 1

    logger.info("Analyze attempt", extra={"attempt": attempt_count})

    prompt = f"""
You are a Jenkins build-failure analyst. Read the parsed log, find the
primary error, and determine its root cause in one pass.

Parsed log:

{state["parsed_log"]}
{_retry_context(state)}
Return ONLY valid JSON in this exact structure:

{{
    "main_error": "the primary error",
    "root_cause": "the most defensible root cause",
    "error_category": "infra, devops, compilation, or needs_triage",
    "evidence": [
        "exact log line or exact text copied from the log"
    ],
    "affected_component": "affected command, tool, or component",
    "recommended_fix": "an actionable fix based only on the available evidence",
    "confidence": "high, medium, or low"
}}

Rules:
- Use only information supported by the log.
{CATEGORY_RULES}
{FIX_RULES}

- Evidence must be copied exactly from the provided log.
- Do not modify, correct, normalize, shorten, or rephrase evidence.
- Preserve every character exactly, including spaces, punctuation,
  capitalization, brackets, and the "[Line N] " prefix.
- Do not generate evidence yourself.
- Every evidence item must be an exact substring of the provided log.
- If exact evidence cannot be identified, return an empty evidence list.
"""

    combined = llm.complete_json(
        prompt,
        CombinedResult,
        model=get_settings().model_for("root_cause"),
    ).model_dump(mode="json")

    proposed_evidence = combined.get("evidence", [])
    grounded = validate_evidence(proposed_evidence, state["parsed_log"])

    root_cause = {
        key: value
        for key, value in combined.items()
        if key != "main_error"
    }
    root_cause["evidence"] = grounded
    root_cause["rejected_evidence"] = [
        item for item in proposed_evidence if item not in grounded
    ]

    logger.info(
        "Analyze finished",
        extra={
            "evidence_count": len(grounded),
            "category": root_cause.get("error_category"),
        }
    )

    return {
        "detected_errors": {
            "main_error": combined.get("main_error", "Unknown"),
            "error_messages": [],
            "affected_component": combined.get("affected_component", "Unknown"),
            "evidence": grounded,
        },
        "root_cause": root_cause,
        "attempt_count": attempt_count,
    }


def _build_graph(merged: bool):
    """The 3-agent graph (detector -> root_cause -> validator) or, merged,
    the 2-agent graph (analyze -> validator)."""

    builder = StateGraph(State)
    analysis_node = "analyze" if merged else "root_cause"

    builder.add_node("parser", parser_node)
    builder.add_node("validator", validator_node)

    if merged:
        builder.add_node("analyze", analyze_node)
        first = "analyze"
    else:
        builder.add_node("error_detector", error_detector_node)
        builder.add_node("root_cause", root_cause_node)
        builder.add_edge("error_detector", "root_cause")
        first = "error_detector"

    builder.add_edge(START, "parser")
    builder.add_conditional_edges(
        "parser",
        parser_router,
        {
            "end": END,
            "continue": first
        }
    )
    builder.add_edge(analysis_node, "validator")

    builder.add_conditional_edges(
        "validator",
        validator_router,
        {
            "end": END,
            "retry": analysis_node
        }
    )

    return builder.compile()


# Compiled at import. `app` is the default 3-agent graph; `merged_app` is
# selected per call when settings.llm_merge_detector is on.
app = _build_graph(merged=False)
merged_app = _build_graph(merged=True)


def analyze_log(log_content: str) -> dict:
    """Run the analysis graph. Raises llm.LLMProviderError if the LLM is
    unreachable or not configured; unusable LLM output degrades to an
    unvalidated result instead of raising."""

    selected = merged_app if get_settings().llm_merge_detector else app

    try:
        return selected.invoke({
            "log_content": redact(cap_line_length(log_content)),
            "parsed_log": "",
            "parser_metadata": {},
            "detected_errors": {},
            "root_cause": {},
            "validation_result": {},
            "attempt_count": 0
        })
    except llm.LLMOutputError as err:
        logger.warning("LLM output unusable", extra={"error": str(err)})

        return {
            "parser_metadata": {},
            "detected_errors": {},
            "root_cause": {},
            "validation_result": {
                "valid": False,
                "reason": f"LLM output invalid: {err}"
            },
            "attempt_count": 0
        }

def build_analysis_response(result: dict, filename: str) -> dict:
    detected_errors = result.get("detected_errors") or {}
    root_cause = result.get("root_cause") or {}
    validation_result = result.get("validation_result") or {}

    validated = validation_result.get("valid") is True
    llm_category = root_cause.get("error_category")

    # Only a validated analysis may carry an actionable category.
    error_category = (
        llm_category
        if validated and llm_category
        else ErrorCategory.NEEDS_TRIAGE.value
    )

    return {
        "filename": filename,
        "failure_summary": detected_errors.get("main_error", "Unknown"),
        "root_cause": root_cause.get("root_cause", "Unknown"),
        "error_category": error_category,
        "llm_category": llm_category,
        "validated": validated,
        "validation_reason": validation_result.get("reason", ""),
        "attempts": result.get("attempt_count", 0),
        "affected_component": root_cause.get(
            "affected_component",
            detected_errors.get("affected_component", "Unknown")
        ),
        "evidence": (
            root_cause.get("evidence")
            or detected_errors.get("evidence")
            or []
        ),
        "recommended_fix": root_cause.get(
            "recommended_fix",
            "Not yet determined"
        ),
        "confidence": root_cause.get("confidence", "unknown"),
    }
