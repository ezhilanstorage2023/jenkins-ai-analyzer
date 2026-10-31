from utils.log_processor import cap_line_length, extract_error_context
from utils.redaction import redact
from models import (
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

def validate_analysis(result, parsed_log):
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

    # 3. Recommended fix must exist
    recommended_fix = result.get(
        "recommended_fix",
        ""
    )

    if not recommended_fix.strip():
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
        DetectedErrors
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
    previous_root_cause = json.dumps(
    state["root_cause"],
    indent=2
)

    prompt = f"""
You are a Root Cause Analysis Agent for a Jenkins build failure.

Detected errors:

{detected_errors}

Previous root cause:
{previous_root_cause}

Validator feedback:
{state["validation_result"]}

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
    classify based only on the fact that the failure occurred in Jenkins.
- Do not invent missing information.
- If the exact root cause cannot be determined, explicitly say so.
- Confidence must reflect the strength of the available evidence.
- Recommended fix must be directly related to the identified root cause.
- Do not invent configuration values, commands, or versions that are not supported by the evidence.
- If the exact fix cannot be determined from the log, explicitly say so.
- Do not suggest specific replacement flags unless the log explicitly identifies the intended flag.
- If the intended replacement cannot be determined from the evidence, say that it cannot be determined.

- Evidence must be copied exactly from the detected errors.
- Do not modify, correct, normalize, shorten, or rephrase evidence.
- Preserve spaces, punctuation, capitalization, and line numbers exactly.
- Do not generate new evidence.
- Every evidence item must appear verbatim in the detected errors.
"""

    root_cause = llm.complete_json(
        prompt,
        RootCauseResult
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

    root_cause = json.dumps(
        state["root_cause"],
        indent=2
    )

    deterministic_problems = validate_analysis(
        state["root_cause"],
        state["parsed_log"]
    )

    logger.info(
        "Deterministic validation",
        extra={"problem_count": len(deterministic_problems)}
    )


    prompt = f"""
You are a Validation Agent for a Jenkins build failure analysis system.

Detected errors:

{detected_errors}

Proposed root cause and recommended fix:

{root_cause}

Determine whether BOTH the proposed root cause and the recommended fix
are directly supported by the detected errors.

The validator must be conservative. It should reject recommendations
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
- The recommended fix must be reasonable and directly related to the root cause.
- Do not assume information that is not present.
- Distinguish direct evidence from speculation.
- If the root cause or recommended fix contains unsupported assumptions,
  set valid to false.
- If there are no unsupported claims, return an empty list.
- A recommended fix must not assume that a missing file is optional.
- Do not recommend deleting, skipping, disabling, or bypassing a failing step
  unless the log provides evidence that doing so is appropriate.
- If multiple fixes are possible, distinguish evidence-supported remediation
  from possible alternatives.
- A fix may describe what needs to be investigated when the exact remediation
  cannot be determined from the log.
  - Evidence must be distinguishable from interpretation.
- Do not treat an AI-generated explanation as direct log evidence.
- A root cause must be supported by the actual evidence provided.
- A recommended fix must not depend on information that is absent from the evidence.
"""

    validation_result = llm.complete_json(
        prompt,
        ValidationVerdict
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

graph = StateGraph(State)

graph.add_node("parser", parser_node)
graph.add_node("error_detector", error_detector_node)
graph.add_node("root_cause", root_cause_node)
graph.add_node("validator", validator_node)

graph.add_edge(START, "parser")
graph.add_conditional_edges(
    "parser",
    parser_router,
    {
        "end": END,
        "continue": "error_detector"
    }
)
graph.add_edge("error_detector", "root_cause")
graph.add_edge("root_cause", "validator")

graph.add_conditional_edges(
    "validator",
    validator_router,
    {
        "end": END,
        "retry": "root_cause"
    }
)

app = graph.compile()    

def analyze_log(log_content: str) -> dict:
    """Run the analysis graph. Raises llm.LLMProviderError if the LLM is
    unreachable or not configured; unusable LLM output degrades to an
    unvalidated result instead of raising."""

    try:
        return app.invoke({
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
