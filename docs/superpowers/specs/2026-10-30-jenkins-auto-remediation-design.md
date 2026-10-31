# Jenkins Failure Auto-Remediation — Design & Phased Plan

Status: **Draft for review** · 2026-10-30

## 1. Intent

Extend the existing Jenkins AI Analyzer (FastAPI + LangGraph + Groq) from a manual "upload a log, read the analysis" tool into a system that **receives Jenkins failures automatically, classifies them, and executes a bounded remediation** per category — with every decision auditable and every action gated.

Success = fewer human touches per failed build, without the system ever making a wrong Gerrit vote, re-run storm, or misdirected email that a human has to clean up.

## 2. Goals / Non-goals / Anti-goals

**Goals**
- Webhook intake of Jenkins build failures; enrichment from Jenkins API (console log, failing stage, parameters, Artifactory links).
- Classification into `infra` | `devops` | `compilation` | `needs_triage`.
- Remediation:
  - `infra` → re-run the Jenkins job with original parameters.
  - `devops` → email the job owner (owner field in job config) using a template.
  - `compilation` → fetch Gerrit diff/author/topic, exclude the culprit change (remove +2, bot posts Code-Review -1 with evidence), re-trigger the pipeline.
- Infra and DevOps: shadow mode first; auto-execution enabled after accuracy is measured.
- Compilation (culprit identification and exclusion): fully automatic from go-live, no human approval; gated by an offline backtest and deterministic safeguards (§6.5).
- Full audit trail and a UI view of events → classification → action → outcome.

**Non-goals (v1)**
- Fixing code or config automatically.
- Remediating test failures (routed to `needs_triage`).
- Replacing Jenkins/Gerrit notification systems.

**Anti-goals (failure even if it "works")**
- LLM-generated text used as an action target (change number, email, job name).
- Unbounded re-runs or re-triggers.
- A Gerrit vote on a change that did not cause the failure.
- Acting on an analysis the validator rejected.

## 3. Decisions made

| Decision | Source |
|---|---|
| Three remediation categories as specified | User |
| Gerrit exclusion: remove reviewer's Code-Review +2, bot posts its own Code-Review -1 with evidence comment | User |
| Shadow first, then auto — infra and devops categories | User |
| Compilation remediation (culprit identification, +2 removal, bot -1, re-trigger) runs automatically from go-live with no human approval | User (2026-10-31) |
| Compensating controls for automatic compilation remediation: offline backtest go-live bar, initial Gerrit project allowlist, post-action audit sampling, wrong-vote circuit breaker, one-click restore | Claude's call |
| DevOps recipient = owner field in job configuration | User |
| Add 4th outcome `needs_triage` (test failures, low confidence, rule/LLM disagreement, ambiguous culprit) | Claude's call |
| Test failures → `needs_triage`, no action | Claude's call |
| Artifactory links → captured as evidence; Artifactory 4xx/5xx/timeout → infra signal | Claude's call |
| Email via corporate SMTP relay, Jinja2 templates in repo | Claude's call |
| Build-scope resolver supports both single-change and batch (see §6.4) | Claude's call — user unsure of current pipeline behavior |
| Shadow mode is read-only: approve/reject only labels the proposed action; nothing executes until the category is promoted to `auto` | Claude's call |
| Re-run / re-trigger only for jobs on an explicit allowlist (folder or name pattern); default deny. Deploy/release/publish jobs excluded unless listed | Claude's call |
| Batch defined by parameter/manifest (§6.4 path 2): vote + comment only, no re-trigger in v1 | Claude's call |
| `needs_triage`: UI queue from P1; email to a configured triage distribution list once the mailer lands (P3) | Claude's call |
| Topic handling: culprit gets bot Code-Review -1 + evidence; topic siblings get +2 removed (no negative vote) with a "held back: topic sibling X failed" comment | User |
| Hosting + UI auth not decided: P1 ships a pluggable auth layer with a local-account fallback; real identity provider chosen before production | User (undecided) → Claude's call for interim |
| Eval labeling by DevOps team, a few hours/week, via the shadow-mode UI | User |
| `controller_id` included in schema, settings, dedupe key and HMAC secret lookup from P1 (supports multiple Jenkins controllers) | Claude's call |

## 4. Current-state findings (must fix before automation)

From `backend/graph.py`, `backend/main.py`, `backend/models.py`:

1. **Unvalidated results are returned.** `validator_router` ends after 3 attempts even when `valid` is false; `validation_result` is not exposed. → Must surface `validated: bool` and treat unvalidated as `needs_triage`.
2. **Fragile LLM parsing.** Bare `json.loads` on raw completions; no JSON mode, no parse retry. `build_analysis_response` reads `root_cause["error_category"]` without default; off-list categories fail the `Literal` → HTTP 500.
3. **Taxonomy mismatch.** Current: Infrastructure / Developer-Side (incl. failing tests) / DevOps-Side. Target: infra / devops / compilation / needs_triage.
4. **Blocking I/O.** Synchronous multi-call LLM pipeline inside an `async` endpoint.
5. **Log leakage.** Full parsed logs and LLM outputs `print`ed to stdout; no secret redaction before sending to the LLM provider.
6. **No auth, persistence, queue, or config layer.** Model name hardcoded 3×; two Groq clients.

## 5. Architecture

```
Jenkins (post{failure} hook / Notification plugin)
   │  POST /webhooks/jenkins  {job, build_number, build_url}  + HMAC header
   ▼
Intake API (FastAPI) ── verify signature ── dedupe (job, build#) ── persist Event ── 202
   ▼
Worker (separate process, DB-backed queue)
   1. Enrich      Jenkins API: consoleText (tail-bounded), wfapi/describe (failing stage),
                  build params, causes, Artifactory URLs (regex over log)
                  Gerrit API: changes in build (via Build-Scope Resolver)
   2. Redact      strip tokens/passwords/keys before LLM + storage
   3. Classify    a) deterministic rules (signatures + failing stage)
                  b) LangGraph pipeline (existing, hardened, new taxonomy)
                  c) reconcile → category + confidence + validated + evidence
   4. Policy gate mode (shadow/auto per category) · validated · confidence ·
                  rule/LLM agreement · budgets · kill switch
   5. Execute     InfraRerun | DevOpsNotify | CompilationExclude+Retrigger
                  (shadow → record ProposedAction only)
   6. Audit       every step persisted; outcome linked back to Event
   ▼
UI: event list, detail (evidence, classification, proposed/executed actions, links)
    approve/reject proposed actions during shadow period
```

### 5.1 Components (new modules under `backend/`)

| Module | Responsibility |
|---|---|
| `api/webhooks.py` | Signature check, dedupe, enqueue, 202 |
| `worker.py` | Pull queue, run pipeline steps, retries with backoff |
| `clients/jenkins.py` | consoleText, wfapi, params, config.xml, buildWithParameters, queue-item → build resolution |
| `clients/gerrit.py` | Change detail, files/diff, topic query, delete vote, post review. Strips `)]}'` XSSI prefix. |
| `clients/mailer.py` | SMTP relay + Jinja2 rendering |
| `classification/rules.py` | Deterministic signatures → category hints |
| `classification/` (existing `graph.py`) | LangGraph pipeline, hardened, new taxonomy |
| `scope/resolver.py` | Determine Gerrit change(s) in a build (§6.4) |
| `policy.py` | Gate decision, budgets, kill switches |
| `actions/` | One executor per category; each idempotent, records request/response |
| `store/` | SQLAlchemy models + migrations; SQLite dev, Postgres prod |
| `settings.py` | pydantic-settings; all URLs, creds, thresholds, modes |

Library choices (Claude's call): `httpx` (async clients), SQLAlchemy 2 + Alembic, `pydantic-settings`, `Jinja2`, `aiosmtplib`. No Redis/Celery in v1 — DB-backed queue is sufficient at Jenkins failure volumes and keeps one fewer moving part.

### 5.2 Data model (core)

- `events` — controller_id, job, build#, build_url, received_at, status, dedupe key `(controller_id, job, build#)` (unique).
- `enrichments` — failing stage, params, causes, artifactory_links, gerrit_changes (JSON), log excerpt (redacted).
- `analyses` — rule hints, LLM output, category, confidence, validated, evidence, model + prompt version.
- `actions` — type, mode (shadow/auto), status (proposed/approved/rejected/executed/failed/skipped), target, request, response, actor, timestamps.
- `lineage` — parent build → triggered build, for budget enforcement.

## 6. Detailed behavior

### 6.1 Intake
- Jenkins has no native failure webhook. Preferred: shared-library step in `post { failure { ... } }` sending minimal payload. Alternative: Notification plugin.
- Payload carries identifiers only; the worker pulls logs via API (avoids multi-MB payloads, and the worker sees the authoritative log).
- Auth: HMAC-SHA256 of body with shared secret; reject on mismatch or stale timestamp.
- Dedupe key `(controller_id, job_full_name, build_number)`; duplicates return 200 with existing event id.

### 6.2 Classification
- **Rules first** (deterministic, auditable): agent offline / `ChannelClosedException` / `RemovedNodeListener` / no space left / OOM-killed / DNS / connection refused / Artifactory 5xx / timeout → `infra`. Credentials / permission / missing tool / Jenkinsfile syntax / `NoSuchMethodError` in pipeline script → `devops`. Compiler signatures (`error:` with file:line, `cannot find symbol`, `undefined reference`, `compilation failed`) in a build/compile stage → `compilation`. Test-report failures → `needs_triage`.
- **LLM** (existing graph, hardened): JSON mode / schema-validated output, parse retry, new 4-class taxonomy, prompt includes failing stage name.
- **Reconcile**: final category requires `validated == true` AND confidence `high` AND (rules agree OR rules silent). Disagreement or anything weaker → `needs_triage`.
- LLM output is used **only** for category, explanation, and evidence (evidence still must be exact substrings). Never for action targets.

### 6.3 Remediation workflows

**Infra → re-run**
1. Fetch original build parameters.
2. Budget check: ≤1 automatic re-run per original build lineage (configurable). If a re-run fails and classifies `infra` again → `needs_triage` + notify.
3. `buildWithParameters` with original params; resolve queue item → new build number; record lineage.

**DevOps → notify owner**
1. Fetch `config.xml`; extract owner email via configured XPath (location TBD — §8).
2. Validate email against allowed domain list; if absent/invalid → `needs_triage`.
3. Render template (job, build link, failing stage, evidence, analysis, recommended next step); send via SMTP relay.
4. Rate limit: one email per (job, failure signature) per N hours to avoid storms.

**Compilation → exclude culprit + re-trigger**
1. Resolve change(s) in the build (§6.4). Fetch for each: current revision files + diff, owner, topic, Code-Review votes.
2. **Culprit identification (deterministic):** match file paths in compiler errors against files modified by each change. Exactly one matching change (or one topic) → culprit. Zero or multiple → `needs_triage`, no vote.
3. **Topic handling:** if culprit has a topic, all open changes in the topic leave the batch together (excluding one part of a topic breaks atomic submission).
4. Exclusion:
   - Culprit: for each +2 vote, `DELETE /changes/{id}/reviewers/{account}/votes/Code-Review`; then `POST .../revisions/current/review` with `Code-Review: -1` and a comment containing evidence, build link, and how to restore (re-apply +2 after fix).
   - Topic siblings: +2 votes removed the same way, **no -1**; comment "held back: topic sibling <culprit> failed compilation in <build link>; re-apply +2 once the culprit is fixed."
   - Read-back: confirm no +2 remains on any change in the unit before re-trigger.
5. Re-trigger:
   - **Batch build** → re-trigger pipeline; resolver query no longer includes the culprit. Budget: ≤2 exclusion+retrigger cycles per pipeline run.
   - **Single-change build** → **no re-trigger** (it would rebuild the same failing change). Vote + comment only.
   - **Parameter/manifest batch** (§6.4 path 2) → vote + comment only in v1; no re-trigger.
6. Before re-trigger, verify the culprit is actually absent from the new scope (read back from Gerrit).

### 6.4 Build-scope resolver
User is unsure how the pipeline selects changes. Resolver tries, in order:
1. Gerrit Trigger parameters (`GERRIT_CHANGE_NUMBER`, `GERRIT_PATCHSET_NUMBER`, `GERRIT_TOPIC`) → single change (+ topic expansion).
2. Configured batch parameter or manifest artifact listing change numbers → batch.
3. Configured Gerrit query (e.g., `status:open label:Code-Review=+2 branch:X`) evaluated at build start time → batch.
4. None → `needs_triage`.

How to find out which applies: inspect the job's build parameters page and Jenkinsfile for `GERRIT_*` variables, a change-list parameter, or a Gerrit query in a checkout/cherry-pick step.

### 6.5 Policy gate & safety
- Per-category mode: `off` | `shadow` | `auto`. Default `shadow` for infra and devops; `auto` for compilation.
- **Compilation runs without human approval.** Every automatic safeguard still applies: validated + high confidence, rule agreement, unique culprit match, patchset staleness check, topic handling, read-back, budgets, kill switch. Anything ambiguous → `needs_triage` (no vote).
- **Compilation go-live bar (replaces the shadow period):** offline backtest of the classifier + culprit matcher against historical failed builds whose real culprit is known (DevOps-labeled); zero wrong-culprit identifications required. Go live first on an allowlist of Gerrit projects, then widen.
- **Compilation post-action controls:** DevOps reviews a sample of executed exclusions after the fact (not an approval step); one-click restore in the UI removes the bot's -1 and asks the original reviewers to re-apply +2; any confirmed wrong-culprit vote trips the compilation kill switch until reviewed.
- Global and per-category kill switch (setting + UI toggle).
- Shadow: action recorded as `proposed`; UI approve/reject **labels only** (no execution, no write credentials needed). Labels form the eval dataset.
- Job eligibility: re-run/re-trigger executors check a per-controller allowlist (folder/name patterns); non-listed jobs → `needs_triage`.
- In `auto`, executors re-check staleness before acting: build is still the latest for that job, change's current patchset matches the one built.
- **Promotion to auto** for infra and devops (proposed thresholds, user to confirm): ≥50 shadow events in that category, ≥95% reviewer-approved, zero wrong-target incidents.
- Service accounts with least privilege: Jenkins (Build + Read on target folders), Gerrit (Code-Review -1..+1, Remove Reviewer on target projects), SMTP (send-only).
- Secret redaction before LLM calls and storage; structured logging replaces `print`.

## 7. Phased plan

| Phase | Scope | Exit criteria |
|---|---|---|
| **P0 Harden** | Fix §4 items: schema-validated LLM output + retry, `validated` flag, 4-class taxonomy, async/offloaded LLM calls, settings module, structured logging + redaction, single Groq client. Update existing tests; add fixtures for each category. | All tests pass; malformed LLM output never yields HTTP 500; upload UI works with new taxonomy. |
| **P1 Intake + shadow classify** | Webhook endpoint, DB + migrations, worker, Jenkins client (log, wfapi, params), rules engine, reconcile, UI event list/detail + approve/reject labeling + `needs_triage` queue, pluggable auth (local accounts interim). Shared-library `post{failure}` snippet. Historical backfill: classify last N failed builds for an eval set labeled by DevOps team. | Real failures flow end-to-end into DB/UI; classification accuracy measured on labeled set; hosting + identity provider decided. |
| **P2 Infra re-run** | Jenkins rebuild executor, lineage, budgets. Shadow → auto promotion. | Promotion criteria met; no re-run loops in soak. |
| **P3 DevOps notify** | config.xml owner extraction, mailer, templates, rate limiting. | Promotion criteria met; zero misdirected emails. |
| **P4 Compilation + Gerrit** | Gerrit client, scope resolver, culprit matcher, topic handling, vote removal/-1, conditional re-trigger, read-back verification, restore action, wrong-vote circuit breaker. Offline backtest on historical failed builds; then live directly in `auto` (no human approval) on allowlisted Gerrit projects. | Backtest bar met (zero wrong-culprit identifications); Gerrit write permissions granted; auto live on initial projects with zero wrong-change votes. |
| **P5 Operate** | Metrics (events, category mix, action success, MTTR delta), alerting on worker backlog/failures, runbook, docs/README update. | Dashboards live; runbook reviewed. |

Testing approach: unit tests per client against recorded API fixtures; integration tests with Jenkins and Gerrit in Docker (official images) for P2–P4; golden-set classification eval run in CI on prompt/model changes.

## 8. Open items (need owner input)

1. **Build scope** — single-change vs batch vs topic (resolver handles all; confirm which applies so the right path is tested first).
2. **Owner field location** in job config (XML element/parameter name, e.g., a `<description>` convention or a parameter default).
3. **Gerrit permissions** — can a service account be granted Remove Reviewer + Code-Review -1 on target projects?
4. **Jenkins setup** — version, Gerrit Trigger plugin present?, can a shared library add `post{failure}` globally?
5. **LLM data handling** — build logs are sent to Groq's hosted API. Confirm this is acceptable for these logs, or require a self-hosted/approved provider.
6. **SMTP relay** host and email template wording/sender identity.
7. **Promotion thresholds** in §6.5.
8. **Hosting target + identity provider** for the UI (needed before production; P1 uses interim local accounts).
9. **Re-run allowlist contents** — which job folders/patterns are eligible.
10. **Compilation go-live scope** — initial Gerrit project allowlist, backtest sample size, and post-action audit sampling rate.

## 9. Left to the builder
Module layout details, migration tooling specifics, UI styling, exact rule-signature list (seeded from §6.2 and expanded from the eval set), retry/backoff parameters.
