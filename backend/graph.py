from utils.log_processor import extract_error_context
from typing import TypedDict
from langgraph.graph import StateGraph, START, END
from dotenv import load_dotenv
from groq import Groq
import json
import os

load_dotenv()

api_key = os.getenv("GROQ_API_KEY")

client = Groq(
    api_key=api_key
) if api_key else None

class State(TypedDict):
    log_content: str
    parsed_log: str
    parser_metadata: dict
    detected_errors: dict
    root_cause: dict
    validation_result: dict
    attempt_count: int

def parser_node(state: State):
    print("Parser received the log")

    log = state["log_content"]

    lines = log.splitlines()

    parsed_log = extract_error_context(
        log,
        context_before=3,
        context_after=5
    )
    print("\n===== PARSER OUTPUT =====")
    print(parsed_log)
    print("===== END PARSER OUTPUT =====\n")

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

    # Keep request within the model token budget
    parsed_log = parsed_log[:10000]

    print("Parser metadata:", parser_metadata)
    print("Parser extracted:", len(parsed_log), "characters")

    return {
        "parsed_log": parsed_log,
        "parser_metadata": parser_metadata
    }

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

    # 2. Every evidence item must exist exactly in the parsed log
    for item in evidence:
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
    print("Error Detector received:", state["parsed_log"])

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

    response = client.chat.completions.create(
        model="openai/gpt-oss-120b",
        messages=[
            {
                "role": "user",
                "content": prompt
            }
        ]
    )

    detected_errors = response.choices[0].message.content

    detected_errors = json.loads(detected_errors)

    detected_errors["evidence"] = validate_evidence(
        detected_errors.get("evidence", []),
        state["parsed_log"]
    )

    print(
    "Validated evidence:",
    detected_errors["evidence"]
    )

    return {
        "detected_errors": detected_errors
    }

def root_cause_node(state: State):
    print("Root Cause Agent received:", state["detected_errors"])

    attempt_count = state["attempt_count"] + 1

    print("Root Cause Agent attempt:", attempt_count)

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
    "evidence": [
        "evidence from the detected errors"
    ],
    "affected_component": "affected command, tool, or component",
    "recommended_fix": "an actionable fix based only on the available evidence",
    "confidence": "high, medium, or low"
}}

Rules:
- Use only information supported by the evidence.
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

    response = client.chat.completions.create(
        model="openai/gpt-oss-120b",
        messages=[
            {
                "role": "user",
                "content": prompt
            }
        ]
    )

    root_cause = response.choices[0].message.content

    root_cause = json.loads(root_cause)

    root_cause["evidence"] = validate_evidence(
    root_cause.get("evidence", []),
    state["parsed_log"]
    )

    print(
    "Validated root cause evidence:",
    root_cause["evidence"]
    )


    return {
    "root_cause": root_cause,
    "attempt_count": attempt_count
    }

def validator_node(state: State):
    print("Validator received:", state["root_cause"])

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

    print(
        "Deterministic validation:",
        deterministic_problems
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

    response = client.chat.completions.create(
        model="openai/gpt-oss-120b",
        messages=[
            {
                "role": "user",
                "content": prompt
            }
        ]
    )

    validation_result = response.choices[0].message.content

    validation_result = json.loads(validation_result)

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

    return {
        "validation_result": validation_result
    }

def validator_router(state: State):

    validation = state["validation_result"]
    attempt_count = state["attempt_count"]

    if validation["valid"]:
        return "end"

    if attempt_count >= 3:
        return "end"

    return "retry"

graph = StateGraph(State)

graph.add_node("parser", parser_node)
graph.add_node("error_detector", error_detector_node)
graph.add_node("root_cause", root_cause_node)
graph.add_node("validator", validator_node)

graph.add_edge(START, "parser")
graph.add_edge("parser", "error_detector")
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

def analyze_log(log_content: str):
    return app.invoke({
        "log_content": log_content,
        "parsed_log": "",
        "parser_metadata": {},
        "detected_errors": {},
        "root_cause": {},
        "validation_result": {},
        "attempt_count": 0
    })

def build_analysis_response(result, filename):
    detected_errors = result["detected_errors"]
    root_cause = result["root_cause"]

    return {
        "filename": filename,

        "failure_summary": detected_errors.get(
            "main_error",
            "Unknown"
        ),

        "root_cause": root_cause.get(
            "root_cause",
            "Unknown"
        ),

        "affected_component": root_cause.get(
            "affected_component",
            detected_errors.get(
                "affected_component",
                "Unknown"
            )
        ),

        "evidence": root_cause.get(
            "evidence",
            detected_errors.get(
                "evidence",
                []
            )
        ),

        "recommended_fix": root_cause.get(
    "recommended_fix",
    "Not yet determined"
),

        "confidence": root_cause.get(
            "confidence",
            "unknown"
        )
    }