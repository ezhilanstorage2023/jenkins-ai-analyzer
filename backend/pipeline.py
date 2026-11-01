"""Process one queued failure: enrich, classify, decide, persist (shadow only)."""

import logging
from dataclasses import asdict
from datetime import datetime

from sqlalchemy import update
from sqlalchemy.orm import Session

from classification.reconcile import reconcile
from classification.rules import RULES_VERSION, evaluate_rules
from executor import create_execution
from clients.jenkins import JenkinsClient
from enrichment import enrich
from graph import analyze_log, build_analysis_response
from notify import build_preview
from policy import propose_action
from settings import get_settings
from store.db import utcnow
from store.models import Analysis, EmailDelivery, Enrichment, Event, ProposedAction

logger = logging.getLogger("pipeline")


class StaleClaimError(Exception):
    """This worker's lease expired and another worker took the event over."""


def process_event(
    session: Session,
    event: Event,
    jenkins: JenkinsClient,
    claimed_at: datetime,
) -> None:
    """Raises JenkinsError / LLMProviderError unchanged for the worker to retry,
    and StaleClaimError when this worker no longer holds the claim."""

    data = enrich(jenkins, event.build_url)

    result = analyze_log(data.console.text)
    analysis = build_analysis_response(
        result,
        f"{event.job}#{event.build_number}"
    )

    matches = evaluate_rules(data.console.text, data.failing_stage)
    decision = reconcile(analysis, matches)
    proposal = propose_action(
        decision.final_category,
        event.job,
        event.build_url,
        data,
    )

    # Fence: finish only if this worker still holds the claim. Done first so
    # a stale worker writes nothing (the transaction rolls back on raise).
    finished = session.execute(
        update(Event)
        .where(
            Event.id == event.id,
            Event.status == "processing",
            Event.claimed_at == claimed_at,
        )
        .values(status="done", claimed_at=None, last_error=None)
        .execution_options(synchronize_session=False)
    ).rowcount

    if finished != 1:
        raise StaleClaimError(f"event {event.id} was claimed by another worker")

    now = utcnow()

    session.add(Enrichment(
        event_id=event.id,
        failing_stage=data.failing_stage,
        parameters=data.build.parameters,
        causes=data.build.causes,
        artifactory_links=data.artifactory_links,
        gerrit_scope=data.gerrit_scope,
        # Degraded (unparseable LLM output) results carry no parsed_log.
        log_excerpt=result.get("parsed_log", ""),
        log_bytes=data.console.total_bytes,
        log_truncated=data.console.truncated,
    ))

    session.add(Analysis(
        event_id=event.id,
        response=analysis,
        rule_matches=[
            asdict(match) | {"rules_version": RULES_VERSION}
            for match in matches
        ],
        final_category=decision.final_category,
        reason_codes=decision.reason_codes,
        model=get_settings().llm_model,
        created_at=now,
    ))

    if proposal is not None:
        session.add(ProposedAction(
            event_id=event.id,
            action_type=proposal.action_type,
            target=proposal.target,
            allowed=proposal.allowed,
            reason_codes=proposal.reason_codes,
            created_at=now,
        ))

        # Only "auto" executes. In "shadow" the proposal above is all there is.
        if (
            proposal.action_type == "rerun"
            and proposal.allowed
            and get_settings().mode_infra == "auto"
        ):
            create_execution(session, event, now)

    # DevOps owner notification preview (shadow: nothing is ever sent). Built
    # before touching the session, and a bug here must not fail the event.
    if decision.final_category == "devops" and get_settings().mode_devops != "off":
        try:
            note = build_preview(
                session, event, decision.final_category, analysis, data,
                jenkins, now,
            )
            session.add(note)

            # Only "auto" sends; "shadow" stops at the preview.
            if get_settings().mode_devops == "auto" and note.decision == "would_send":
                session.add(EmailDelivery(
                    notification=note, status="pending", reason_codes=[],
                    created_at=now, updated_at=now,
                ))
        except Exception:
            logger.exception(
                "Notification preview failed", extra={"event_id": event.id}
            )

    logger.info(
        "Event processed",
        extra={
            "event_id": event.id,
            "final_category": decision.final_category,
            "reason_codes": decision.reason_codes,
            "proposed_action": proposal.action_type if proposal else None,
        },
    )
