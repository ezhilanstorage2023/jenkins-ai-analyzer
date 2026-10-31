"""Gather and sanitize what the classifier needs from a failed build."""

import re
from dataclasses import dataclass, replace

from clients.jenkins import BuildInfo, ConsoleLog, JenkinsClient, Stage
from utils.redaction import redact, redact_mapping

_STAGE_MARKER = re.compile(r"^\[Pipeline\] \{ \((.+)\)$", re.MULTILINE)
_ARTIFACTORY_LINK = re.compile(
    r"https?://[^\s\"'<>]*artifactory[^\s\"'<>]*",
    re.IGNORECASE,
)
_MAX_ARTIFACTORY_LINKS = 20

_GERRIT_KEYS = (
    "CHANGE_NUMBER",
    "PATCHSET_NUMBER",
    "CHANGE_ID",
    "PROJECT",
    "BRANCH",
    "TOPIC",
    "CHANGE_OWNER_EMAIL",
    "PATCHSET_UPLOADER_EMAIL",
)


@dataclass
class EnrichmentData:
    build: BuildInfo
    console: ConsoleLog
    failing_stage: str | None
    artifactory_links: list[str]
    gerrit_scope: dict[str, str]


def failing_stage(stages: list[Stage] | None, console: str) -> str | None:
    for stage in stages or []:
        if stage.status in ("FAILED", "ABORTED"):
            return stage.name

    # No Stage View data: the last stage opened in the console.
    names = [
        name for name in _STAGE_MARKER.findall(console)
        if not name.startswith("Declarative: ")
    ]

    return names[-1] if names else None


def artifactory_links(console: str) -> list[str]:
    links = []

    for link in _ARTIFACTORY_LINK.findall(console):
        if link not in links:
            links.append(link)

            if len(links) == _MAX_ARTIFACTORY_LINKS:
                break

    return links


def gerrit_scope(parameters: dict[str, str]) -> dict[str, str]:
    return {
        key.lower(): parameters[f"GERRIT_{key}"]
        for key in _GERRIT_KEYS
        if f"GERRIT_{key}" in parameters
    }


def enrich(client: JenkinsClient, build_url: str) -> EnrichmentData:
    build = client.get_build_info(build_url)
    console = client.get_console(build_url)
    stages = client.get_stages(build_url)

    redacted_console = redact(console.text)

    return EnrichmentData(
        build=replace(build, parameters=redact_mapping(build.parameters)),
        console=replace(console, text=redacted_console),
        failing_stage=failing_stage(stages, redacted_console),
        artifactory_links=artifactory_links(redacted_console),
        # Gerrit trigger keys never match secret names; use raw values.
        gerrit_scope=gerrit_scope(build.parameters),
    )
