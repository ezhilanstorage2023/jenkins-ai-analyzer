def extract_error_context(
    text,
    context_before=3,
    context_after=5,
    max_blocks=20
):
    """
    Extract useful error/failure contexts from a Jenkins log.

    The function scans the complete log, finds important failure-related
    lines, adds surrounding context, merges overlapping sections, and
    returns the most relevant sections.

    Later error contexts are prioritized because Jenkins logs often contain
    intermediate warnings/errors followed by the actual final failure.
    """

    lines = text.splitlines()

    keywords = [
        "ERROR",
        "EXCEPTION",
        "FAILURE",
        "FAILED",
        "FATAL",
        "CAUSED BY"
    ]

    ranges = []

    for index, line in enumerate(lines):

        upper_line = line.upper()

        if any(keyword in upper_line for keyword in keywords):

            start = max(
                0,
                index - context_before
            )

            end = min(
                len(lines),
                index + context_after + 1
            )

            ranges.append((start, end))

    # No failure-related lines found
    if not ranges:
        return ""

    # Sort ranges by their location in the log
    ranges.sort()

    # Merge overlapping or adjacent ranges
    merged_ranges = []

    for start, end in ranges:

        if not merged_ranges:
            merged_ranges.append([start, end])
            continue

        previous_start, previous_end = merged_ranges[-1]

        if start <= previous_end:
            merged_ranges[-1][1] = max(
                previous_end,
                end
            )
        else:
            merged_ranges.append([start, end])

    # Jenkins logs commonly contain the real failure near the end.
    # Keep the latest blocks first, while still limiting the total number.
    selected_ranges = merged_ranges[-max_blocks:]

    # Restore chronological order
    selected_ranges.sort()

    blocks = []

    for start, end in selected_ranges:

        block = "\n".join(
            f"[Line {i + 1}] {lines[i]}"
            for i in range(start, end)
        )

        blocks.append(block)

    return "\n\n--- ERROR CONTEXT ---\n\n".join(blocks)