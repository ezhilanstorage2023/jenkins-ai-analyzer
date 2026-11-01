# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Jenkins AI Analyzer: FastAPI backend + LangGraph multi-agent pipeline (Groq LLM, default model `openai/gpt-oss-120b`) that classifies a Jenkins build-log failure, extracts verbatim evidence, and proposes a fix. Static HTML/JS frontend posts a log file to the backend.

It is being extended into an automated remediation system. Design, architecture and plans live in `docs/superpowers/specs/`, `docs/architecture/`, and `docs/superpowers/plans/` — read the design doc before changing classification or adding remediation code.

## Commands

All backend commands run from `backend/` — tests import top-level modules (`graph`, `llm`, `settings`, `models`, `utils.*`) and test helpers as `tests.fakes`.

```bash
# Setup (Python 3.12; CI and Docker use 3.12)
cd backend
python3.12 -m venv .venv && . .venv/bin/activate     # if ensurepip is missing: uv venv --python 3.12 .venv
pip install -r requirements.txt

# Run API locally
uvicorn main:app --reload --port 8000

# Frontend (separate terminal, from repo root) — must be served on port 5500 for CORS
python -m http.server 5500 --directory frontend

# Tests (never call the real LLM)
pytest tests -q
pytest tests/test_graph.py::test_happy_path_three_llm_calls

# P1 shadow pipeline
python -m worker                                   # claims queued events, processes them
python -m backfill --job "folder/name" --count 20  # queue recent failed builds (read-only)
python -m manage create-user <name> --role labeler # review UI account (viewer|labeler|operator)
# review UI: http://127.0.0.1:8000/ui/  (HTTP Basic); lists live failing jobs, classifies, labels
python -m eval.benchmark run --configs 20b-low     # LLM benchmark on eval-data/ (calls the real LLM)
python -m mailer test                              # send ONE test mail from FROM_EMAIL_ID to EMAIL_ID (checks the SMTP login; no DB)

# P2 (default shadow; auto only after the real allowlist/thresholds are agreed)
# MODE_INFRA=auto RERUN_JOB_ALLOWLIST='["folder/*"]' python -m worker   -- WRITES to Jenkins

# Full stack
docker compose up --build    # backend :8000, worker, frontend :5500; compose reads backend/.env
```

No linter or formatter is configured. CI (`.github/workflows/ci.yml`) runs `pytest tests` and `docker build ./backend`.

## Architecture

Request flow: `POST /analyze-log` (`main.py`) → `run_in_threadpool(analyze_log)` (`graph.py`) → `build_analysis_response()` → `AnalysisResponse` (`models.py`).

LangGraph state machine in `graph.py` (compiled at import as module-level `app` — distinct from FastAPI's `app` in `main.py`):

```
redact → parser ─┬─ no error context → END (needs_triage, no LLM call)
                 └─ error_detector → root_cause → validator ─┬─ valid or attempts >= max → END
                                         ^                    └─ invalid → root_cause (retry with feedback)
```

- **settings.py**: single pydantic-settings `Settings`, `get_settings()` is `lru_cache`d. `.env` is always `backend/.env` (anchored to the module, not the working directory). `EMAIL_ID` populates `owner_email_override`.
- **llm.py**: the only Groq client. `complete_json(prompt, schema, *, model=None, reasoning_effort=None)` forces JSON mode, strips code fences / surrounding prose, validates against a Pydantic schema, retries `llm_parse_retries` times, then raises `LLMOutputError`. Provider failures raise `LLMProviderError`; no key raises `LLMUnavailableError`. HTTP 429 waits the provider's `retry-after` (capped by `llm_rate_limit_max_wait_seconds`, up to `llm_rate_limit_retries`; a wait does not use a parse attempt) then raises `LLMRateLimitError` (a `LLMProviderError`, so still 503 / worker retry). `Settings.model_for("detector"|"root_cause"|"validator")` picks a per-agent model (`LLM_MODEL_*`, default `llm_model`); `LLM_REASONING_EFFORT` is sent only when set.
- **Graph options (off by default, original 3-agent behaviour):** `LLM_MERGE_DETECTOR=true` runs `analyze_node` (detector + root cause in one call; `graph.merged_app`, chosen per call in `analyze_log`) so a log costs 2 calls instead of 3. `VALIDATOR_CHECKS_FIX=false` stops the recommended fix from gating the category (the validator no longer sees it). Neither has been adopted: a benchmark of the merged variant was inconclusive (rate-limited), so measure with `python -m eval.benchmark` first. The benchmark reads `eval-data/` (gitignored: internal consoles + provisional labels); it is a harness only, no data is committed.
- **utils/redaction.py**: `redact()` runs on the raw log before parsing; it never changes line count.
- **utils/log_processor.py**: `extract_error_context()` scores lines (specific regex patterns = priority 2, generic keywords = priority 1), merges context windows, prefixes each line `[Line N] `.
- **logging_config.py**: JSON log lines; graph logs metadata only (counts, attempts, validity), never log text, prompts, or LLM output.

### P1 shadow pipeline

```
Jenkins post{failure} ─POST /webhooks/jenkins (X-Webhook-Token)─▶ Event(queued)   ◀─ backfill.py
worker.run_once: claim_next (lease) → pipeline.process_event:
    enrichment.enrich (clients/jenkins.py, GET only) → graph.analyze_log → build_analysis_response
    → classification.rules.evaluate_rules → classification.reconcile → policy.propose_action
    → Enrichment / Analysis / ProposedAction rows; Event(done)   (errors → retry w/ backoff → failed)
api/review.py (/api/*) + api/ui.py (/ui/, static/review/*) ── HTTP Basic via auth.py
```

- **Writes are deny-by-default.** `JenkinsClient` installs a GET-only request hook and never follows redirects; `policy.py` only returns proposals. The one exception is P2 (below): a client built with `allow_writes=True` (`jenkins_client_from_settings(write=True)`, built only by `worker.main` when `MODE_INFRA=auto`) may additionally POST to a job's `build`/`buildWithParameters` and nothing else (`_write_guard`). Any new write needs its own explicit guard, executor and tests; `grep` for `.post(` outside `executor.py` must stay empty (inbound FastAPI routes aside).
- **URL scoping:** every Jenkins URL (webhook `build_url`, client calls) must pass `utils.urls.url_under_base(url, settings.jenkins_base_url)` — parsed scheme/host/normalized path, not string prefix.
- **Queue states** on `Event.status`: `queued` → `processing` (lease `worker_lease_seconds`; expired leases are reclaimed) → `done` | `failed` (after `worker_max_attempts`, backoff `worker_backoff_seconds * 2**(n-1)`).
- **Final category** = `classification.reconcile` over the analysis and rule matches: validated + `high` confidence + rules agree-or-silent, else `needs_triage` with reason codes (`unvalidated`, `low_confidence`, `rule_ambiguous`, `rule_llm_conflict`). Fixture logs must each match exactly one rule category (`test_fixture_rule_category`).
- **Webhook binding:** `build_url` must equal `clients.jenkins.build_url_for(JENKINS_URL, job, build_number)` (no query/fragment) and `controller_id` must equal `settings.jenkins_controller_id`; the stored URL is the canonical one. Backfill also builds URLs this way.
- **Worker fencing:** `process_event(..., claimed_at)` and `_record_failure(..., claimed_at)` only finish/fail an event whose `status='processing' AND claimed_at` still matches (conditional UPDATE); otherwise `StaleClaimError` / discarded. Reclaiming an expired lease counts as an attempt; at `worker_max_attempts` the event fails. `python -m manage requeue --status failed` resets failed events.
- **Jenkins responses:** any non-2xx (including redirects — usually an SSO page) or non-JSON body raises `JenkinsError`.
- **Long lines:** `utils.log_processor.cap_line_length` (4096 chars, line count kept) runs before redaction/rules in `enrichment.enrich` and `graph.analyze_log`. Keep every per-line regex linear — no unanchored `\S+`/`.*` prefixes; perf tests guard this (`test_*_fast_on_long_lines`).
- **Rerun proposals** store `job`, `build_url`, `parameter_names` only; the executor re-fetches the real parameter values from Jenkins at execution time (stored parameters are redacted).
- **Browse & classify from the UI** (`api/review.py`): `GET /api/jenkins/failed-builds` lists failing jobs from one Jenkins tree query (`JenkinsClient.list_jobs`, folders flattened; "failing" = `lastCompletedBuild.result == FAILURE`, never the color). `JENKINS_JOBS` is now an optional allowlist; unset = every job. Query params: `max_age_days` (default 30, 0 = any; unknown times kept), `sort`, `offset`, `count`, `q`, `include_disabled`; the filtered total is in the `X-Total-Count` header. `POST /api/jenkins/analyze[-batch]` (operator, batch ≤ 50) share `_enqueue_build`: without an allowlist Jenkins must confirm the build exists and failed; job paths with empty/`.`/`..` parts are rejected. `GET /api/events?ids=` feeds the UI's live status poll; `GET /api/me` drives role-aware controls.
- **Rules** (`classification/rules.py`): shell xtrace lines (`+ cmd`, `++ cmd`) are skipped by every rule (they echo commands, not output); the Maven/Gradle compile markers are case-sensitive (`(?-i:...)`) so prose like a commit message cannot match. Bump `RULES_VERSION` when rules change.

### P3 devops owner notification (`owners.py`, `notify.py`, `mailer.py`)

For a `devops` final category the pipeline records a `Notification` preview: who would be emailed and what the message says. `MODE_DEVOPS`: `off` records nothing and proposes nothing; `shadow` (default) stops at the preview; `auto` also sends it. **`mailer.py` is the only module that imports an email library** (`test_only_mailer_py_can_send_email`), and `worker.main` builds its `SmtpSender` only when `MODE_DEVOPS=auto`. `auto` is refused at settings load unless `FROM_EMAIL_ID` and `SMTP_PASSWORD` (aliases `FROM_EMAIL_PASSWORD`, `GMAIL_APP_PASSWORD`) are set; for Gmail the password must be an App Password.

- **Sending (`mailer.py`):** in `auto`, a `would_send` preview gets an `EmailDelivery` row: `pending → claimed → skipped | sending → sent | failed | uncertain`. SMTP defaults are `smtp.gmail.com:587` + STARTTLS (`SMTP_HOST/PORT/STARTTLS/USE_SSL/USERNAME`, login name defaults to `FROM_EMAIL_ID`). If STARTTLS is requested but not offered, or the TLS handshake fails, nothing is sent and the password is never transmitted. Guards re-checked at send time: kill switch, mode, decision `would_send`, recipients present.
- **Never a blind resend:** `sending` is committed before the message is offered. A refusal (auth failure, connect failure, rejected recipient, a non-250 reply to DATA) is `failed`; a break after the message may have been accepted is `uncertain` and is never retried; stale `claimed` → `failed`, stale `sending` → `uncertain` (`mailer.LEASE_SECONDS`). Partial recipient refusal is `sent` with `partial_refusal`. Stored errors contain the error type and reply code only, never the password, the message or addresses.
- **Rate limit interplay:** a preview whose delivery is `failed`/`skipped` does not count toward the window (a wrong password must not silence a failure for hours); `sent`, `pending` and `uncertain` do.
- **Tests:** `tests/smtp_fake.py` is a real SMTP server on localhost (AUTH, rejected recipients, refused DATA, connection dropped after DATA) so the actual `smtplib` path runs; `tests/test_mailer.py` covers every transition end to end.

- **Recipient sources, first hit wins** (`owners.resolve`): `OWNER_MAP` (fnmatch job pattern → addresses) → the default of the job parameter named `OWNER_PARAMETER_NAME` in `config.xml` → `OWNER_EMAIL_OVERRIDE`/`EMAIL_ID` (the test recipient). An explicit source that is invalid is *blocked*, never replaced by a later one. Addresses are never scraped from arbitrary config text (a survey of real failing jobs found emails in property files/URLs/scripts and no standard owner field).
- **Checks:** map/config addresses must be in `ALLOWED_EMAIL_DOMAINS` (empty = none accepted; the override is not domain-checked); max 3 recipients; any invalid entry blocks the whole list; CR/LF/whitespace can never match the address pattern (header injection). `config.xml` is fetched read-only (`JenkinsClient.get_config_xml`, ≤ 1 MB), a DTD/entity declaration is refused, and only the one parameter value is kept, never the document.
- **Decisions:** `would_send`, `suppressed` (`rate_limited`: one per (job, failure signature) per `NOTIFY_RATE_WINDOW_HOURS`, default 6; only `would_send` rows count, so suppressed ones do not extend the window), `blocked` (`target_unresolved`, `domain_not_allowed`, `invalid_recipient`, `too_many_recipients`, `kill_switch`). `failure_signature` = hash of job + category + first evidence line with line numbers/timestamps/digits removed.
- **Message:** built from stored, already-redacted data, re-redacted, capped (≤ 4000 chars, ≤ 5 evidence lines); the recommended fix is labelled advisory; the link is the canonical Jenkins URL (+ the review UI if `REVIEW_UI_URL` is set).
- **Safety:** `build_preview` computes everything before touching the DB session, and the pipeline logs (not raises) a failure, so a preview bug never fails an event. A bug in the mailer settles as `failed` (before the intent) or `uncertain` (after), never as a retry.
- **API/UI:** event detail has `notification` with `sent` (true only when its `delivery.status == "sent"`) and the `delivery` object; `GET /api/metrics/notifications` (now with `by_delivery`); `POST /api/events/{id}/notification-label` (labeler) rates a `would_send` recipient right/wrong, which is the evidence for the "zero misdirected emails" promotion bar.

### P2 infra re-run executor (`executor.py`) — built, default OFF

Per-category mode `MODE_INFRA` = `off` (nothing proposed) | `shadow` (default; proposals only) | `auto` (allowed `rerun` proposals get an `Execution` row and the worker runs `executor.run_once`). Nothing in `executor.py` is reachable in `shadow`/`off`.

```
Execution: pending → claimed → skipped (reason_codes: kill_switch, mode_not_auto, job_not_allowed,
                                  budget_exhausted, job_rate_limited, stale_build, build_not_failed,
                                  secret_parameters)
                       claimed → triggering → triggered | failed | uncertain
```

- **Order of checks (all before any write):** kill switch (setting `KILL_SWITCH` OR the DB toggle `Control('kill_switch')`, flipped via `POST /api/controls/kill-switch`, operator only; the toggle cannot clear a settings-level switch) → mode → `rerun_job_allowlist` (empty = nothing allowed) → budget per lineage (`rerun_budget`, default 1; `lineage_root` links a failing build to the execution that queued it via `new_build_number`) → per-job rate limit (`rerun_job_window_hours`, holds even if the queue item never resolved) → live checks: still the job's latest build, result still FAILURE, no masked (password) parameters.
- **Never a blind second POST:** the intent (`triggering`) is committed *before* the POST. `JenkinsUncertainError` (transport failure after the request left) or a crash → `uncertain`, never retried; `JenkinsError` (Jenkins refused) → `failed`; a stale `claimed` (nothing sent) → `failed`; a stale `triggering` → `uncertain`. Stale = `execution_lease_seconds`.
- **Client:** `trigger_rebuild` uses `build` for parameterless jobs, `buildWithParameters` otherwise, sends a crumb only if `crumbIssuer` offers one, and requires a `Location` queue URL under the Jenkins base; `resolve_queue_item` polls it for the new build number (`rerun_queue_timeout_seconds`). Only tested against `FakeJenkins`; no real rebuild has been triggered.
- **Rebuild result (read-only tracking):** `executor.track_outcomes(reader)` runs in every worker loop (any `MODE_INFRA`, GETs only) and records how each `triggered` execution's build ended in `RebuildOutcome` (`queued → running → finished` with Jenkins' own `build_result`, or `cancelled` / `unknown`). Each build is re-checked at most every `rebuild_poll_seconds` (20) and abandoned as `unknown` after `rebuild_track_hours` (24). If the queue item had not started when we triggered it, the tracker resolves it later (`queue_item_state`, then `find_build_by_queue_id` because Jenkins forgets queue items after a few minutes) and writes `new_build_number`, which is also what links a later failure of that build into the re-run chain for the budget. A transient Jenkins error keeps the last state. It is a separate table, so existing databases need no ALTER.
- **API/UI:** event detail includes `execution` (with `execution.rebuild`) and a truthful `action.executed`; `rebuild` is also on every `/api/events` row and every live failing-jobs row. The UI shows a badge (`rebuild #567 running / SUCCESS / FAILURE / waiting in Jenkins queue / cancelled`) in the live table, a **Rebuild** column in the events table and the drawer's Execution block, and keeps polling an event until its rebuild has ended (`rebuildPending`) so the result appears without a refresh; it also shows the mode and a kill-switch control (operators, auto mode only).
- **Not done (needs owner decisions):** promotion criteria, the real allowlist, and any live `auto` run; notify (P3) after a repeated infra failure is not implemented, the budget just stops further re-runs.
- **Labels attach to `Analysis`** (verdict + corrected category) and feed `/api/metrics/accuracy`.
- **Stored data:** only the redacted parser excerpt and `redact_mapping`-redacted build parameters (secret-looking parameter *names* such as `*PASS*`, `*TOKEN*`, `*KEY*`, `*CRED*` hide the value); never raw console logs. Worker `last_error` keeps messages only for `JenkinsError`/`LLMError`.
- **DB:** SQLAlchemy, `create_all` on first engine use (no migrations until Postgres). `store.db.utcnow()` (naive UTC) for every timestamp. Tests get a fresh in-memory DB per test via conftest (`DATABASE_URL=sqlite://`).
- **Test doubles:** `tests.fakes.FakeJenkins` (`httpx.MockTransport` routed on `"/path"` or `("POST", "/path")`; a route may return `(status, body, headers)` or be a callable, e.g. to raise `httpx.ReadTimeout`; `.posts()` lists POSTs), `make_jenkins()`/`jenkins`/`queued_event`/`users`/`viewer`/`labeler`/`operator` fixtures in conftest, `responses_for(log, signature, category)` for grounded fake LLM replies.

### Cross-file invariants

- **Categories** are `models.ErrorCategory`: `infra`, `devops`, `compilation`, `needs_triage`. Their definitions live in the `root_cause_node` prompt in `graph.py`; change both together.
- **Validated rule**: `build_analysis_response` returns the LLM category only when `validation_result["valid"] is True`; otherwise `error_category = "needs_triage"` and the LLM's label goes to `llm_category`. Downstream remediation must rely on this.
- **Failure mapping**: unusable LLM output → HTTP 200 `needs_triage` (`analyze_log` catches `LLMOutputError`; Groq's JSON-mode 400 `json_validate_failed` is retried as a parse failure, not treated as an outage); `groq`/`httpx`/`httpcore` loggers are pinned to ≥ INFO so `LOG_LEVEL=DEBUG` never logs prompts; `LLMProviderError`/`LLMUnavailableError` → HTTP 503 in `main.py`; upload > `max_upload_bytes` → 413.
- **Evidence grounding**: evidence must be an exact substring of `parsed_log`, including the `[Line N] ` prefix. `validate_evidence()` drops non-matching items; `root_cause_node` keeps them in `rejected_evidence`, and `validate_analysis()` reports any rejected item, forcing validator rejection — partly invented evidence never validates.
- **Parser truncation**: if the latest error block alone exceeds `parser_max_characters`, its tail is kept on line boundaries; a large failure is never reported as "No error context found".
- **Redaction**: patterns never span a newline (line count is a tested invariant). Don't add `:`-separated `token` matching — it would redact compiler text like `Unexpected token: '}'`.
- **Context separator** `"\n\n--- ERROR CONTEXT ---\n\n"` is produced in `log_processor.py` and split on in `graph.py` `parser_node`.
- **Tests**: `tests/conftest.py` autouse fixture removes `GROQ_API_KEY`, disables `.env` loading, and clears settings/client caches. Use the `fake_llm(responses)` fixture with `tests.fakes` builders (`detected_json`, `root_cause_json`, `verdict_json`); responses are consumed in call order detector → root_cause → validator (→ root_cause → validator on retry); with `LLM_MERGE_DETECTOR` it is analyze → validator. Parametrized tests with huge values need short `ids=` (Windows caps an env var at 32,767 chars and pytest stores the test id in one). Test files that read repo files pass `encoding="utf-8"`.
- **Fixtures**: `backend/tests/fixtures/logs/*.log` are force-included in git via a `.gitignore` negation (root `.gitignore` ignores `*.log`).
- **Frontend ↔ backend**: `frontend/app.js` uses `window.API_BASE` or `http://127.0.0.1:8000`; CORS origins come from `settings.cors_origins`.
- `/upload-log` is a leftover diagnostic endpoint not used by the frontend.
