
import re


def extract_error_context(
    text,
    context_before=3,
    context_after=5,
    max_blocks=20
):
    """
    Extract relevant Jenkins failure contexts.

    Prioritizes specific underlying errors over generic build-failure
    messages while preserving line numbers and surrounding context.
    """

    lines = text.splitlines()

    # Specific errors that may reveal the original cause.
    root_cause_patterns = [
        r"\bcannot stat\b",
        r"\bno such file or directory\b",
        r"\bpermission denied\b",
        r"\bcommand not found\b",
        r"\bno space left on device\b",
        r"\bundefined reference\b",
        r"\bcannot find symbol\b",
        r"\bfatal error\s*:",
        r"\btraceback \(most recent call last\)",
        r"\bconnection refused\b",
        r"\bcould not resolve host\b",
        r"\bfailed to fetch\b",
        r"\bcompilation failed\b",
        r"\bsegmentation fault\b",
    ]

    # Generic indicators that a failure occurred.
    failure_keywords = [
        "ERROR",
        "EXCEPTION",
        "FAILURE",
        "FAILED",
        "FATAL",
        "CAUSED BY",
    ]

    # Collect failure contexts with priority scores.
    candidates = []

    for index, line in enumerate(lines):
        upper_line = line.upper()

        specific_match = any(
            re.search(pattern, line, re.IGNORECASE)
            for pattern in root_cause_patterns
        )

        generic_match = any(
            keyword in upper_line
            for keyword in failure_keywords
        )

        if not specific_match and not generic_match:
            continue

        start = max(0, index - context_before)
        end = min(len(lines), index + context_after + 1)

        # Specific errors receive higher priority.
        priority = 2 if specific_match else 1

        candidates.append({
            "start": start,
            "end": end,
            "priority": priority,
            "line": index,
        })

    if not candidates:
        return ""

    # Merge overlapping contexts, retaining their highest priority.
    candidates.sort(key=lambda item: item["start"])
    merged = []

    for candidate in candidates:
        if (
            merged
            and candidate["start"] <= merged[-1]["end"]
        ):
            current = merged[-1]
            current["end"] = max(
                current["end"],
                candidate["end"]
            )
            current["priority"] = max(
                current["priority"],
                candidate["priority"]
            )
            current["latest_line"] = max(
                current["latest_line"],
                candidate["line"]
            )
        else:
            merged.append({
                "start": candidate["start"],
                "end": candidate["end"],
                "priority": candidate["priority"],
                "latest_line": candidate["line"],
            })

    # Prefer specific errors first; use recency to break ties.
    ranked = sorted(
        merged,
        key=lambda block: (
            block["priority"],
            block["latest_line"]
        ),
        reverse=True
    )

    selected = ranked[:max_blocks]

    # Restore chronological order for easier log interpretation.
    selected.sort(key=lambda block: block["start"])

    blocks = []

    for block in selected:
        formatted_lines = [
            f"[Line {i + 1}] {lines[i]}"
            for i in range(block["start"], block["end"])
        ]

        blocks.append("\n".join(formatted_lines))

    return "\n\n--- ERROR CONTEXT ---\n\n".join(blocks)

MAX_LINE_LENGTH = 4096


def cap_line_length(text, limit=MAX_LINE_LENGTH):
    """Truncate overlong lines (e.g. huge compiler command lines).

    Keeps the line count, so "[Line N]" numbering is unchanged, and bounds
    the cost of every per-line regex that runs afterwards.
    """

    return "\n".join(line[:limit] for line in text.split("\n"))
