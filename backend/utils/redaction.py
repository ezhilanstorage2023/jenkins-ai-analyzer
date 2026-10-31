import re

REDACTED = "[REDACTED]"

_PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)

# (pattern, replacement) applied in order after private-key blocks.
# Replacements never add or remove newlines, so "[Line N]" numbering
# of the redacted log still matches the original log.
_RULES = [
    (
        re.compile(r"(?i)(authorization:\s*(?:bearer|basic|token)\s+)\S+"),
        r"\1" + REDACTED,
    ),
    (
        re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@"),
        r"\1" + REDACTED + "@",
    ),
    (
        re.compile(
            r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key"
            r"|access[_-]?key|client[_-]?secret)\s*=\s*"
            r"(\"[^\"]*\"|'[^']*'|\S+)"
        ),
        r"\1=" + REDACTED,
    ),
    (
        re.compile(r'(?i)("(?:password|secret|token|api[_-]?key)"\s*:\s*)"[^"]*"'),
        r'\1"' + REDACTED + '"',
    ),
    (re.compile(r"\bgsk_[A-Za-z0-9]{20,}"), REDACTED),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}"), REDACTED),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), REDACTED),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"), REDACTED),
]


def _replace_private_key(match):
    return "[REDACTED PRIVATE KEY]" + "\n" * match.group(0).count("\n")


def redact(text: str) -> str:
    """Remove credentials from a build log before it is sent or stored."""

    text = _PRIVATE_KEY_BLOCK.sub(_replace_private_key, text)

    for pattern, replacement in _RULES:
        text = pattern.sub(replacement, text)

    return text
