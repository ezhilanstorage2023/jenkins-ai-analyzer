from typing import TypedDict
from langgraph.graph import StateGraph, START, END
from dotenv import load_dotenv
from groq import Groq
import os

load_dotenv()

client = Groq(
    api_key=os.getenv("GROQ_API_KEY")
)


class State(TypedDict):
    log_content: str
    parsed_log: str
    detected_errors: str
    root_cause: str
    validation_result: str


def parser_node(state: State):
    print("Parser received the log")

    log = state["log_content"]

    lines = log.splitlines()

    relevant_lines = []

    keywords = [
        "ERROR",
        "EXCEPTION",
        "FAILED",
        "FAILURE",
        "FATAL"
    ]

    for line in lines:
        if any(keyword in line.upper() for keyword in keywords):
            relevant_lines.append(line)

    parsed_log = "\n".join(relevant_lines)

    return {
        "parsed_log": parsed_log
    }


def error_detector_node(state: State):
    print("Error Detector received:", state["parsed_log"])

    prompt = f"""
You are an Error Detection Agent analyzing a Jenkins build log.

Analyze the following parsed log:

{state["parsed_log"]}

Identify the important build errors.

Return:
1. The main error
2. Any important error messages
3. The affected command or component

Do not invent information that is not present in the log.
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

    return {
        "detected_errors": detected_errors
    }


def root_cause_node(state: State):
    print("Root Cause Agent received:", state["detected_errors"])

    validation_feedback = state["validation_result"]

    prompt = f"""
You are a Root Cause Analysis Agent for a Jenkins build failure.

Detected errors:
{state["detected_errors"]}

Previous root cause:
{state["root_cause"]}

Validator feedback:
{validation_feedback}

Determine the most defensible root cause based only on the available evidence.

Do not invent information.
If the evidence is insufficient to determine the exact root cause,
say so explicitly.

Return the root cause and briefly explain the evidence supporting it.
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

    return {
        "root_cause": root_cause
    }


def validator_node(state: State):
    print("Validator received:", state["root_cause"])

    prompt = f"""
You are a Validation Agent for a Jenkins build failure analysis system.

Detected errors:
{state["detected_errors"]}

Proposed root cause:
{state["root_cause"]}

Determine whether the proposed root cause is directly supported
by the detected errors.

Rules:
- Do not assume information that is not present.
- Distinguish direct evidence from speculation.
- If the root cause contains unsupported assumptions, say so.
- Explain briefly why it is valid or invalid.

Return:

VALID: YES or NO
REASON: <short explanation>
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

    return {
        "validation_result": validation_result
    }

def validator_router(state: State):
    validation = state["validation_result"].upper()

    if "VALID: YES" in validation:
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


result = app.invoke({
    "log_content": """
Starting Jenkins build

Running application tests...

ERROR: connection failed

Build FAILURE
""",
    "parsed_log": "",
    "detected_errors": "",
    "root_cause": "",
    "validation_result": ""
})

print("\nFINAL STATE:")
print(result)