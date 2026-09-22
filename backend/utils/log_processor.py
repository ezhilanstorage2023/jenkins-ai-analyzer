def extract_error_context(text, context_before=3, context_after=5, max_blocks=20):
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

            start = max(0, index - context_before)
            end = min(
                len(lines),
                index + context_after + 1
            )

            ranges.append((start, end))

    # Sort ranges by their starting position
    ranges.sort()

    # Merge overlapping ranges
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

    # Build the final context blocks
    blocks = []

    for start, end in merged_ranges[:max_blocks]:

        block = "\n".join(
            f"[Line {i + 1}] {lines[i]}"
            for i in range(start, end)
        )

        blocks.append(block)

    return "\n\n--- ERROR CONTEXT ---\n\n".join(blocks)