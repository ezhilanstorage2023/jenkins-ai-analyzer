"""Who to notify about a failed job (P3, shadow: nothing is ever sent).

Sources, first hit wins, and an explicit source that turns out invalid is
*blocked*, never silently replaced by a later one:

  1. OWNER_MAP         job pattern -> recipients (the reliable source)
  2. OWNER_PARAMETER_NAME  that parameter's default value in the job's config.xml
  3. OWNER_EMAIL_OVERRIDE  (EMAIL_ID) the operator's own test recipient

Addresses are never scraped from arbitrary config text: a real survey of
failing jobs found emails in property files, URLs and scripts, with no
standard owner field, so guessing would misdirect mail.
"""

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from fnmatch import fnmatch

from clients.jenkins import JenkinsClient, JenkinsError
from settings import get_settings

MAX_RECIPIENTS = 3

# No whitespace or control characters can match, so header injection
# ("a@b.com\nBcc: x@y.com") is rejected by construction.
_EMAIL = re.compile(
    r"[A-Za-z0-9._%+\-]+@([A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,})"
)
_SPLIT = re.compile(r"[,;]+")
_DTD = re.compile(r"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)


@dataclass
class Resolution:
    recipients: list[str]
    source: str | None            # map | config | override
    reasons: list[str] = field(default_factory=list)


def parameter_default(xml: str, name: str) -> str | None:
    """Default value of the job parameter called `name`, or None.

    A document with a DTD/entities is refused outright (no entity expansion
    attacks), and only the one value is returned, never the document.
    """

    if _DTD.search(xml):
        raise ValueError("DTD or entity declarations are not accepted")

    root = ET.fromstring(xml)

    for node in root.iter():
        name_node = node.find("name")
        default = node.find("defaultValue")

        if (
            name_node is not None
            and default is not None
            and (name_node.text or "").strip() == name
        ):
            return (default.text or "").strip() or None

    return None


def _validate(raw: list[str], source: str, domains: list[str] | None) -> Resolution:
    reasons: list[str] = []
    good: list[str] = []
    allowed = None if domains is None else {d.lower().lstrip("@") for d in domains}

    candidates = [
        part.strip()
        for value in raw
        for part in _SPLIT.split(value)
        if part.strip()
    ]

    for candidate in candidates:
        match = _EMAIL.fullmatch(candidate)

        if match is None:
            reasons.append("invalid_recipient")
            continue

        if allowed is not None and match.group(1).lower() not in allowed:
            reasons.append("domain_not_allowed")
            continue

        address = candidate.lower()

        if address not in good:
            good.append(address)

    if len(good) > MAX_RECIPIENTS:
        return Resolution([], source, reasons + ["too_many_recipients"])

    if not good:
        return Resolution([], source, reasons or ["target_unresolved"])

    # Some candidates were bad: still blocked, an incomplete list could
    # leave the real owner out.
    if reasons:
        return Resolution([], source, reasons)

    return Resolution(good, source, [])


def resolve(job: str, jenkins: JenkinsClient | None = None) -> Resolution:
    settings = get_settings()
    domains = settings.allowed_email_domains
    reasons: list[str] = []

    for pattern, addresses in settings.owner_map.items():
        if fnmatch(job, pattern):
            return _validate(addresses, "map", domains)

    if settings.owner_parameter_name and jenkins is not None:
        value = None

        try:
            value = parameter_default(
                jenkins.get_config_xml(job), settings.owner_parameter_name
            )
        except (JenkinsError, ValueError, SyntaxError):
            # Unreadable config is informational: fall through to the override.
            reasons.append("config_unavailable")

        if value:
            return _validate([value], "config", domains)

    if settings.owner_email_override:
        # The operator chose this address explicitly: no domain check.
        resolution = _validate([settings.owner_email_override], "override", None)
        resolution.reasons = reasons + resolution.reasons
        return resolution

    return Resolution([], None, reasons + ["target_unresolved"])
