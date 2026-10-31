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
# review UI: http://127.0.0.1:8000/ui/  (HTTP Basic)

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
- **llm.py**: the only Groq client. `complete_json(prompt, schema)` forces JSON mode, strips code fences / surrounding prose, validates against a Pydantic schema, retries `llm_parse_retries` times, then raises `LLMOutputError`. Provider failures raise `LLMProviderError`; no key raises `LLMUnavailableError`.
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

- **No external writes in P1.** `JenkinsClient` installs a GET-only request hook and never follows redirects; `policy.py` only returns proposals. P2+ must lift the GET guard deliberately for each write.
- **URL scoping:** every Jenkins URL (webhook `build_url`, client calls) must pass `utils.urls.url_under_base(url, settings.jenkins_base_url)` — parsed scheme/host/normalized path, not string prefix.
- **Queue states** on `Event.status`: `queued` → `processing` (lease `worker_lease_seconds`; expired leases are reclaimed) → `done` | `failed` (after `worker_max_attempts`, backoff `worker_backoff_seconds * 2**(n-1)`).
- **Final category** = `classification.reconcile` over the analysis and rule matches: validated + `high` confidence + rules agree-or-silent, else `needs_triage` with reason codes (`unvalidated`, `low_confidence`, `rule_ambiguous`, `rule_llm_conflict`). Fixture logs must each match exactly one rule category (`test_fixture_rule_category`).
- **Webhook binding:** `build_url` must equal `clients.jenkins.build_url_for(JENKINS_URL, job, build_number)` (no query/fragment) and `controller_id` must equal `settings.jenkins_controller_id`; the stored URL is the canonical one. Backfill also builds URLs this way.
- **Worker fencing:** `process_event(..., claimed_at)` and `_record_failure(..., claimed_at)` only finish/fail an event whose `status='processing' AND claimed_at` still matches (conditional UPDATE); otherwise `StaleClaimError` / discarded. Reclaiming an expired lease counts as an attempt; at `worker_max_attempts` the event fails. `python -m manage requeue --status failed` resets failed events.
- **Jenkins responses:** any non-2xx (including redirects — usually an SSO page) or non-JSON body raises `JenkinsError`.
- **Long lines:** `utils.log_processor.cap_line_length` (4096 chars, line count kept) runs before redaction/rules in `enrichment.enrich` and `graph.analyze_log`. Keep every per-line regex linear — no unanchored `\S+`/`.*` prefixes; perf tests guard this (`test_*_fast_on_long_lines`).
- **Rerun proposals** store `job`, `build_url`, `parameter_names` only; P2 must re-fetch real parameter values from Jenkins at execution time (stored parameters are redacted).
- **Labels attach to `Analysis`** (verdict + corrected category) and feed `/api/metrics/accuracy`.
- **Stored data:** only the redacted parser excerpt and `redact_mapping`-redacted build parameters (secret-looking parameter *names* such as `*PASS*`, `*TOKEN*`, `*KEY*`, `*CRED*` hide the value); never raw console logs. Worker `last_error` keeps messages only for `JenkinsError`/`LLMError`.
- **DB:** SQLAlchemy, `create_all` on first engine use (no migrations until Postgres). `store.db.utcnow()` (naive UTC) for every timestamp. Tests get a fresh in-memory DB per test via conftest (`DATABASE_URL=sqlite://`).
- **Test doubles:** `tests.fakes.FakeJenkins` (path-routed `httpx.MockTransport`), `make_jenkins()`/`jenkins`/`queued_event`/`users`/`viewer`/`labeler` fixtures in conftest, `responses_for(log, signature, category)` for grounded fake LLM replies.

### Cross-file invariants

- **Categories** are `models.ErrorCategory`: `infra`, `devops`, `compilation`, `needs_triage`. Their definitions live in the `root_cause_node` prompt in `graph.py`; change both together.
- **Validated rule**: `build_analysis_response` returns the LLM category only when `validation_result["valid"] is True`; otherwise `error_category = "needs_triage"` and the LLM's label goes to `llm_category`. Downstream remediation must rely on this.
- **Failure mapping**: unusable LLM output → HTTP 200 `needs_triage` (`analyze_log` catches `LLMOutputError`; Groq's JSON-mode 400 `json_validate_failed` is retried as a parse failure, not treated as an outage); `groq`/`httpx`/`httpcore` loggers are pinned to ≥ INFO so `LOG_LEVEL=DEBUG` never logs prompts; `LLMProviderError`/`LLMUnavailableError` → HTTP 503 in `main.py`; upload > `max_upload_bytes` → 413.
- **Evidence grounding**: evidence must be an exact substring of `parsed_log`, including the `[Line N] ` prefix. `validate_evidence()` drops non-matching items; `root_cause_node` keeps them in `rejected_evidence`, and `validate_analysis()` reports any rejected item, forcing validator rejection — partly invented evidence never validates.
- **Parser truncation**: if the latest error block alone exceeds `parser_max_characters`, its tail is kept on line boundaries; a large failure is never reported as "No error context found".
- **Redaction**: patterns never span a newline (line count is a tested invariant). Don't add `:`-separated `token` matching — it would redact compiler text like `Unexpected token: '}'`.
- **Context separator** `"\n\n--- ERROR CONTEXT ---\n\n"` is produced in `log_processor.py` and split on in `graph.py` `parser_node`.
- **Tests**: `tests/conftest.py` autouse fixture removes `GROQ_API_KEY`, disables `.env` loading, and clears settings/client caches. Use the `fake_llm(responses)` fixture with `tests.fakes` builders (`detected_json`, `root_cause_json`, `verdict_json`); responses are consumed in call order detector → root_cause → validator (→ root_cause → validator on retry).
- **Fixtures**: `backend/tests/fixtures/logs/*.log` are force-included in git via a `.gitignore` negation (root `.gitignore` ignores `*.log`).
- **Frontend ↔ backend**: `frontend/app.js` uses `window.API_BASE` or `http://127.0.0.1:8000`; CORS origins come from `settings.cors_origins`.
- `/upload-log` is a leftover diagnostic endpoint not used by the frontend.
