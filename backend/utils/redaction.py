import re

REDACTED = "[REDACTED]"

# A BEGIN line with no END (truncated log) is redacted to the end of text.
_PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"(?:.*?-----END [A-Z ]*PRIVATE KEY-----|.*\Z)",
    re.DOTALL,
)

_SECRET_NAME = (
    r"(?:password|passwd|pwd|secret|token|api[_-]?key"
    r"|access[_-]?key|client[_-]?secret|auth[_-]?token)"
)

# Names may carry prefixes/suffixes: DB_PASSWORD, GROQ_API_KEY,
# aws_secret_access_key, _authToken.
_SECRET_IDENTIFIER = r"[A-Za-z0-9_-]*?" + _SECRET_NAME + r"[A-Za-z0-9_]*"

# (pattern, replacement) applied in order after private-key blocks.
# Patterns never match across a newline, so "[Line N]" numbering of
# the redacted log still matches the original log.
_RULES = [
    (
        re.compile(r"(?i)(authorization:[ \t]*(?:bearer|basic|token)[ \t]+)\S+"),
        r"\1" + REDACTED,
    ),
    (
        # Bare bearer tokens; require a digit or dot so prose such as
        # "Bearer token required" is left alone.
        re.compile(
            r"(?i)\b(bearer[ \t]+)"
            r"(?=[A-Za-z0-9._~+/=-]*[0-9.])[A-Za-z0-9._~+/=-]{8,}"
        ),
        r"\1" + REDACTED,
    ),
    (
        re.compile(r"(?i)\b([a-z][a-z0-9+.-]{0,30}://)[^/\s:@]+:[^/\s@]+@"),
        r"\1" + REDACTED + "@",
    ),
    (
        # CLI flags: --password hunter2
        re.compile(
            r"(?i)(--?(?:password|passwd|pass|token|api[_-]?key|secret)[ \t]+)"
            r"(?!-)\S+"
        ),
        r"\1" + REDACTED,
    ),
    (
        # name=value
        re.compile(
            r"(?i)(?<![A-Za-z0-9_-])(" + _SECRET_IDENTIFIER + r")[ \t]*=[ \t]*"
            r"(\"[^\"\n]*\"|'[^'\n]*'|\S+)"
        ),
        r"\1=" + REDACTED,
    ),
    (
        # "name": "value"
        re.compile(
            r'(?i)("' + _SECRET_IDENTIFIER + r'"[ \t]*:[ \t]*)"[^"\n]*"'
        ),
        r'\1"' + REDACTED + '"',
    ),
    (
        # YAML-style "password: value" at line start. "token" is excluded
        # so compiler text like "Unexpected token: '}'" is untouched.
        re.compile(
            r"(?im)^([ \t-]*(?:[A-Za-z0-9_]+[_-])?"
            r"(?:password|passwd|secret|api[_-]?key|client[_-]?secret)"
            r"[A-Za-z0-9_]*[ \t]*:[ \t]+)\S.*$"
        ),
        r"\1" + REDACTED,
    ),
    (re.compile(r"\bgsk_[A-Za-z0-9]{20,}"), REDACTED),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}"), REDACTED),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), REDACTED),
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


# Parameter *names* that mark the value as secret. Broad on purpose: a
# false positive only hides a value; a miss stores and shows a credential.
_SECRET_PARAM_NAME = re.compile(
    r"pass|pwd|secret|token|auth|cred|api[_-]?key|private[_-]?key"
    r"|ssh[_-]?key|keystore|cert",
    re.IGNORECASE,
)


def redact_mapping(values: dict[str, str]) -> dict[str, str]:
    """Redact a name -> value mapping such as Jenkins build parameters.

    Values of secret-looking names are replaced outright; all other values
    still go through redact() for embedded credentials.
    """

    return {
        name: REDACTED if _SECRET_PARAM_NAME.search(name) else redact(value)
        for name, value in values.items()
    }
