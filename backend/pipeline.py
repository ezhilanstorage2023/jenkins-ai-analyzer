"""Process one queued failure: enrich, classify, decide, persist (shadow only)."""

import logging
from dataclasses import asdict

from sqlalchemy.orm import Session

from classification.reconcile import reconcile
from classification.rules import RULES_VERSION, evaluate_rules
from clients.jenkins import JenkinsClient
from enrichment import enrich
from graph import analyze_log, build_analysis_response
from policy import propose_action
from settings import get_settings
from store.db import utcnow
from store.models import Analysis, Enrichment, Event, ProposedAction

logger = logging.getLogger("pipeline")


def process_event(session: Session, event: Event, jenkins: JenkinsClient) -> None:
    """Raises JenkinsError / LLMProviderError unchanged for the worker to retry."""

    data = enrich(jenkins, event.build_url)

    result = analyze_log(data.console.text)
    analysis = build_analysis_response(
        result,
        f"{event.job}#{event.build_number}"
    )

    matches = evaluate_rules(data.console.text, data.failing_stage)
    decision = reconcile(analysis, matches)
    proposal = propose_action(decision.final_category, event.job, data)

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

    event.status = "done"
    event.claimed_at = None
    event.last_error = None

    logger.info(
        "Event processed",
        extra={
            "event_id": event.id,
            "final_category": decision.final_category,
            "reason_codes": decision.reason_codes,
            "proposed_action": proposal.action_type if proposal else None,
        },
    )
