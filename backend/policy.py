"""Shadow policy: decide what remediation would be taken. Never executes."""

from dataclasses import dataclass
from fnmatch import fnmatch

from enrichment import EnrichmentData
from settings import get_settings


@dataclass
class ActionProposal:
    action_type: str
    target: dict
    allowed: bool
    reason_codes: list[str]


def _proposal(action_type: str, target: dict, ok: bool, reason: str) -> ActionProposal:
    return ActionProposal(
        action_type=action_type,
        target=target,
        allowed=ok,
        reason_codes=[] if ok else [reason],
    )


def propose_action(
    final_category: str,
    job: str,
    enrichment: EnrichmentData,
) -> ActionProposal | None:
    settings = get_settings()

    if final_category == "infra":
        allowed = any(
            fnmatch(job, pattern) for pattern in settings.rerun_job_allowlist
        )

        return _proposal(
            "rerun",
            {"job": job, "parameters": enrichment.build.parameters},
            allowed,
            "job_not_allowed",
        )

    if final_category == "devops":
        recipient = settings.owner_email_override

        return _proposal(
            "notify_owner",
            {"recipient": recipient, "source": "override"},
            recipient is not None,
            "target_unresolved",
        )

    if final_category == "compilation":
        scope = enrichment.gerrit_scope

        return _proposal(
            "exclude_culprit",
            {
                "change_number": scope.get("change_number"),
                "patchset_number": scope.get("patchset_number"),
                "topic": scope.get("topic"),
            },
            "change_number" in scope,
            "target_unresolved",
        )

    return None
