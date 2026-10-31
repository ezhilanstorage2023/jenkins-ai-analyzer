"""Deterministic failure signatures, evaluated before the LLM result is trusted."""

import re
from dataclasses import dataclass

RULES_VERSION = "2026-10-31.1"

# Compiler errors only count when the failing stage looks like a build.
_BUILD_STAGE = re.compile(r"compil|build|make|assembl", re.IGNORECASE)


@dataclass(frozen=True)
class RuleMatch:
    rule_id: str
    category: str
    line_number: int
    line: str


def _rule(rule_id: str, category: str, pattern: str):
    return rule_id, category, re.compile(pattern, re.IGNORECASE)


RULES = [
    _rule("infra.channel_closed", "infra",
          r"ChannelClosedException|RemovedNodeListener"),
    _rule("infra.agent_offline", "infra",
          r"agent .* (was marked offline|went offline|is offline)"
          r"|Cannot contact [\w.-]+:"),
    _rule("infra.disk_full", "infra", r"No space left on device"),
    _rule("infra.oom", "infra",
          r"OOMKilled|Out of memory: Killed process|java\.lang\.OutOfMemoryError"),
    _rule("infra.network", "infra",
          r"Could not resolve host|Temporary failure in name resolution"
          r"|Connection refused|Connection timed out|Read timed out"),
    _rule("infra.artifactory_5xx", "infra",
          r"artifactory\S*.*\b(500|502|503|504)\b"),
    _rule("devops.missing_credentials", "devops",
          r"Could not find credentials entry"),
    _rule("devops.permission_denied", "devops", r"Permission denied"),
    _rule("devops.command_not_found", "devops", r"command not found"),
    _rule("devops.no_tool", "devops", r"No tool named"),
    _rule("devops.pipeline_script", "devops",
          r"WorkflowScript: \d+:|No such DSL method"
          r"|groovy\.lang\.MissingPropertyException"),
    _rule("compilation.compiler_error", "compilation",
          r"\S+\.(java|kt|scala|c|cc|cpp|cxx|h|hpp|go|cs|ts|rs):\d+(:\d+)?:?\s+(fatal )?error"
          r"|error: cannot find symbol|undefined reference to"
          r"|COMPILATION ERROR|Compilation failed"),
    _rule("tests.failures", "needs_triage",
          r"Tests run: \d+, Failures: [1-9]|There (were|are) test failures"
          r"|FAILED: \S*Test"),
]


def evaluate_rules(log: str, failing_stage: str | None) -> list[RuleMatch]:
    skip_compilation = (
        failing_stage is not None
        and not _BUILD_STAGE.search(failing_stage)
    )
    lines = log.splitlines()
    matches = []

    for rule_id, category, pattern in RULES:
        if skip_compilation and category == "compilation":
            continue

        for number, line in enumerate(lines, start=1):
            if pattern.search(line):
                matches.append(RuleMatch(rule_id, category, number, line))
                break

    return matches
