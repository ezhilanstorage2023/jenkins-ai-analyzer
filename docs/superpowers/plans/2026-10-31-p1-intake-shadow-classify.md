# P1 Intake + Shadow Classify Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Jenkins failures reach the service automatically and are processed into a reviewable decision.
- Jenkins sends failures to a token-protected webhook.
- A background worker enriches each failure from the Jenkins API and classifies it (rules, then the hardened LangGraph pipeline, then a reconcile step).
- The worker records the remediation it *would* take, in shadow mode.
- DevOps reviewers see every event in an authenticated review UI and label it, which builds the accuracy data used later to promote each category to auto.

**Architecture:**
- The FastAPI app gains three routers: `api/webhooks.py`, `api/review.py`, and `/ui` static pages.
- The SQLAlchemy store (`store/`) holds the events and a DB-backed queue.
- A separate worker process (`worker.py`) claims events with a lease and runs `pipeline.process_event`, which chains `clients/jenkins.py`, `enrichment.py`, the existing `graph.analyze_log`, `classification/rules.py` + `classification/reconcile.py`, and `policy.py`.
- **Nothing in P1 writes to Jenkins, Gerrit or email.** Proposed actions are only recorded.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2 (SQLite now; the URL is configurable for Postgres), httpx (sync client; `MockTransport` in tests), LangGraph/Groq (existing), stdlib `hashlib.scrypt` for passwords, vanilla JS UI.

**Spec:** `docs/superpowers/specs/2026-10-30-jenkins-auto-remediation-design.md` (§5, §5.1, §5.2, §6.1, §6.2, §6.4 path 1, §6.5, §7 P1 row). Also `docs/architecture/jenkins-auto-remediation-architecture.md` §7, §9, §10, §11.

## Decisions taken for this plan

| Decision | Source |
|---|---|
| Webhook auth is a shared token in the `X-Webhook-Token` header, compared with `secrets.compare_digest` against `JENKINS_WEBHOOK_TOKEN`. This replaces HMAC (spec §6.1). | User, 2026-10-31 |
| Proposed DevOps notifications target `owner_email_override` (env `EMAIL_ID`) while testing; config.xml owner lookup stays P3. | User, 2026-10-31 |
| `.env` is loaded from `backend/.env` (anchored to the `settings.py` directory, not the working directory). | User moved the file; closes deferred P0 minor M7 |
| Jenkins auth is `JENKINS_USERNAME` + `JENKINS_API_TOKEN` (Basic). Password auth is not supported. | Claude's call (P0 handoff advice) |
| Labels attach to the **analysis**: a verdict on the category plus a corrected category. P1 has no executors, so classification accuracy is the measurable thing. | Claude's call |
| No schema migrations tool in P1: `Base.metadata.create_all` runs on first engine use. Alembic is introduced at the first Postgres deployment. | Claude's call; schema still changes through P4 |
| The review UI is served by FastAPI at `/ui/` behind HTTP Basic (same origin as the API, so the browser login prompt covers the data calls). The existing upload page at :5500 is unchanged. | Claude's call |
| The failing stage comes from `wfapi/describe` when available, with a fallback to the console's `[Pipeline] { (Stage)` markers (Stage View plugin presence on 2.479.2 is unknown). | Claude's call |
| `controller_id` defaults to `"default"`; one Jenkins controller is configured in P1. | Spec §3 (multi-controller schema), single URL for now |

## Global Constraints

- Run all backend commands from `backend/`. Python 3.12 venv at `backend/.venv` (built with uv, as in P0).
- Work on branch `p1-intake-shadow` from `main`. Commit per task. Do not push.
- **Tests never call real Jenkins, Groq, Gerrit or SMTP.** Use `httpx.MockTransport` for Jenkins and the existing `fake_llm` fixture for the LLM.
- **No writes to external systems in P1.** No POST to Jenkins, no Gerrit calls, no email. `policy.py` only returns proposals.
- **Every outbound Jenkins URL must start with `settings.jenkins_url`** (normalized with a trailing slash). Anything else raises `JenkinsError` before any request is made.
- **Secrets:**
  - `jenkins_api_token` and `jenkins_webhook_token` are `SecretStr` and are never logged.
  - Stored build parameters pass through `redact_mapping`. Stored log text is only the redacted `parsed_log` excerpt; raw logs are never stored.
- The test owner email is never hard-coded in code, tests or docs. Tests use `owner@example.com`.
- Category strings come only from `models.ErrorCategory`.
- The default DB is `sqlite:///<backend dir>/data/remediation.db` (absolute path, so it doesn't depend on the working directory). `backend/data/` is git-ignored.
- Times are naive UTC from a single helper, `store.db.utcnow()`.

## Review Focus

1. **Webhook `build_url` outside the configured Jenkins** (SSRF via the worker): expect 422 at intake, and the client refuses it anyway. Test owners: Task 3, `test_build_url_outside_jenkins_rejected`; Task 4, `test_client_refuses_foreign_url`.
2. **Missing, wrong or unconfigured webhook token:** wrong or missing → 401; token not configured on the server → 503. The webhook must never accept unauthenticated requests. Test owner: Task 3, `test_webhook_token_required`, `test_webhook_disabled_without_configured_token`.
3. **The same build posted twice** (Jenkins retry, or a webhook plus a backfill): one event only; the second call returns 200 `duplicate: true`. Test owners: Task 3, `test_duplicate_webhook_returns_existing_event`; Task 12, `test_backfill_skips_existing_events`.
4. **Jenkins down, 5xx, or a huge console log:** the event is retried with backoff and then marked `failed`, with no crash loop. The console log is tail-limited to `jenkins_max_log_bytes` without loading it all. Test owners: Task 4, `test_console_tail_is_bounded`; Task 8, `test_jenkins_error_retries_then_fails`.
5. **Worker crash mid-event:** after the lease expires the event is claimed again, and two workers never hold the same live claim. Test owner: Task 8, `test_expired_lease_is_reclaimed`, `test_live_claim_not_double_claimed`.

---

### Task 1: Settings for P1

**Files:** Modify `backend/settings.py`, `backend/tests/test_settings.py`, `backend/requirements.txt` (add `sqlalchemy`), `.gitignore` (add `backend/data/`)

**Interfaces:**
- Produces new `Settings` fields (env names in brackets):
  - `database_url: str` (default `f"sqlite:///{Path(__file__).parent / 'data' / 'remediation.db'}"`)
  - `jenkins_url: str | None = None` [JENKINS_URL]
  - `jenkins_username: str | None = None`
  - `jenkins_api_token: SecretStr | None = None`
  - `jenkins_webhook_token: SecretStr | None = None`
  - `jenkins_max_log_bytes: int = 5_000_000`
  - `jenkins_timeout_seconds: float = 30.0`
  - `owner_email_override: str | None`, read from `OWNER_EMAIL_OVERRIDE` or `EMAIL_ID` via `Field(validation_alias=AliasChoices(...))`
  - `rerun_job_allowlist: list[str] = []` (fnmatch patterns)
  - `worker_poll_seconds: float = 5.0`
  - `worker_max_attempts: int = 3`
  - `worker_lease_seconds: int = 600`
  - `worker_backoff_seconds: int = 60`
- Also produces:
  - `model_config.env_file = Path(__file__).parent / ".env"`
  - `Settings.jenkins_base_url` property: `jenkins_url` with a guaranteed trailing slash, or `None`.

- [x] **Step 1: Failing tests** (append to `tests/test_settings.py`)

```python
from pathlib import Path
import settings as settings_module

def test_env_file_anchored_to_backend_dir():
    assert Path(settings_module.Settings.model_config["env_file"]) == Path(settings_module.__file__).parent / ".env"

def test_email_id_alias_sets_owner_override(monkeypatch):
    monkeypatch.setenv("EMAIL_ID", "owner@example.com")
    get_settings.cache_clear()
    assert get_settings().owner_email_override == "owner@example.com"

def test_jenkins_base_url_gets_trailing_slash(monkeypatch):
    monkeypatch.setenv("JENKINS_URL", "https://ci.example.com")
    get_settings.cache_clear()
    assert get_settings().jenkins_base_url == "https://ci.example.com/"

def test_default_database_is_absolute_sqlite_path():
    url = get_settings().database_url
    assert url.startswith("sqlite:////") and url.endswith("/data/remediation.db")
```

Note: the conftest autouse fixture sets `env_file` to `None` per test. `test_env_file_anchored_to_backend_dir` must therefore read the class default before the fixture patches it: capture it at module import as `ENV_FILE_DEFAULT = Settings.model_config["env_file"]` and assert on that.

- [x] **Step 2: Run, expect FAIL** — `pytest tests/test_settings.py -q` → assertion and attribute errors.
- [x] **Step 3: Implement** the fields per Interfaces. Pin `sqlalchemy` to the version `uv pip install sqlalchemy` resolves. Add `backend/data/` to `.gitignore`.
- [x] **Step 4: Run, expect PASS** — `pytest tests -q` → all pass.
- [x] **Step 5: Commit** — `git add backend/settings.py backend/tests/test_settings.py backend/requirements.txt .gitignore && git commit -m "feat: P1 settings (database, jenkins, worker, owner override)"`

---

### Task 2: Store — models, engine, sessions

**Files:** Create `backend/store/__init__.py`, `backend/store/db.py`, `backend/store/models.py`, `backend/tests/test_store.py`. Modify `backend/tests/conftest.py`.

**Interfaces:**
- `store/db.py`:
  - `utcnow() -> datetime` (naive UTC)
  - `get_engine() -> Engine`: cached per `database_url`; runs `Base.metadata.create_all`; uses `StaticPool` + `check_same_thread=False` when the URL is `"sqlite://"`; creates the parent dir for file SQLite.
  - `reset_engine_cache() -> None`
  - `SessionLocal() -> Session`, bound to `get_engine()`
  - `session_scope()`: context manager that commits, or rolls back on error
  - `get_session()`: FastAPI dependency yielding a `Session`
- `store/models.py` (SQLAlchemy 2 `DeclarativeBase` as `Base`, `Mapped`/`mapped_column`):
  - `Event`: `id` int pk; `controller_id` str; `job` str; `build_number` int; `build_url` str; `result` str | None; `source` str (`"webhook"` | `"backfill"`); `status` str (`"queued"` | `"processing"` | `"done"` | `"failed"`), default `"queued"`; `attempts` int = 0; `next_attempt_at` datetime; `claimed_at` datetime | None; `last_error` str | None; `received_at` datetime. Unique constraint `uq_event_build` on `(controller_id, job, build_number)`.
  - `Enrichment`: `event_id` FK unique; `failing_stage` str | None; `parameters` JSON; `causes` JSON; `artifactory_links` JSON; `gerrit_scope` JSON; `log_excerpt` Text; `log_bytes` int; `log_truncated` bool.
  - `Analysis`: `id` pk; `event_id` FK unique; `response` JSON (the `build_analysis_response` dict); `rule_matches` JSON; `final_category` str; `reason_codes` JSON; `model` str; `created_at`.
  - `ProposedAction`: `id` pk; `event_id` FK unique; `action_type` str; `target` JSON; `allowed` bool; `reason_codes` JSON; `created_at`.
  - `Label`: `id` pk; `analysis_id` FK unique; `reviewer` str; `verdict` str; `correct_category` str | None; `note` str | None; `created_at`.
  - `User`: `id` pk; `username` unique; `password_hash` str; `role` str.
  - One-to-one relationships from `Event` to `enrichment`, `analysis` and `action`, and from `Analysis` to `label`.
- `tests/conftest.py`: the autouse fixture also sets `DATABASE_URL=sqlite://` (env) and calls `reset_engine_cache()` before and after each test, so every test gets a fresh in-memory DB.

- [x] **Step 1: Failing tests**

```python
import pytest
from sqlalchemy.exc import IntegrityError
from store.db import session_scope, utcnow
from store.models import Event, Analysis

def _event(**kw):
    base = dict(controller_id="default", job="acme/build", build_number=7,
                build_url="https://ci.example.com/job/acme/job/build/7/",
                source="webhook", next_attempt_at=utcnow(), received_at=utcnow())
    return Event(**base | kw)

def test_event_roundtrip_defaults():
    with session_scope() as s:
        s.add(_event())
    with session_scope() as s:
        e = s.query(Event).one()
        assert e.status == "queued" and e.attempts == 0

def test_duplicate_build_rejected():
    with pytest.raises(IntegrityError):
        with session_scope() as s:
            s.add(_event()); s.add(_event())

def test_json_columns_roundtrip():
    with session_scope() as s:
        e = _event(); s.add(e); s.flush()
        s.add(Analysis(event_id=e.id, response={"validated": True}, rule_matches=[],
                       final_category="infra", reason_codes=[], model="m", created_at=utcnow()))
    with session_scope() as s:
        assert s.query(Analysis).one().response == {"validated": True}
```

- [x] **Step 2: Run, expect FAIL** — `pytest tests/test_store.py -q` → `ModuleNotFoundError: store`.
- [x] **Step 3: Implement** per Interfaces.
- [x] **Step 4: Run, expect PASS** — `pytest tests -q`.
- [x] **Step 5: Commit** — `git add backend/store backend/tests/test_store.py backend/tests/conftest.py && git commit -m "feat: add SQLAlchemy store for events, analyses, actions, labels"`

---

### Task 3: Webhook intake

**Files:** Create `backend/api/__init__.py`, `backend/api/webhooks.py`, `backend/tests/test_webhooks.py`. Modify `backend/main.py` (`app.include_router(webhooks.router)`).

**Interfaces:**
- Consumes: `get_settings` (Task 1); `get_session`, `utcnow`, `Event` (Task 2)
- Produces:
  - `POST /webhooks/jenkins`, with payload model `JenkinsFailureHook`:
    - `controller_id: str = "default"`
    - `job: str` (min length 1)
    - `build_number: int` (≥ 1)
    - `build_url: str`
    - `result: str | None = None`
  - Order of checks:
    1. Token not configured → 503 `"Webhook intake is not configured."`
    2. Header `X-Webhook-Token` missing or mismatched (`secrets.compare_digest`) → 401 `"Invalid webhook token."`
    3. `jenkins_url` unset, or `build_url` not starting with `settings.jenkins_base_url` → 422 `"build_url is not under the configured Jenkins URL."`
    4. `result` present and `.upper() != "FAILURE"` → 200 `{"status": "ignored"}`
    5. Existing `(controller_id, job, build_number)` → 200 `{"event_id": id, "duplicate": true}`
    6. Otherwise insert an `Event(source="webhook", status="queued", next_attempt_at=utcnow())` → 202 `{"event_id": id, "duplicate": false}`
  - The token value is never logged.

- [x] **Step 1: Failing tests** (use `TestClient(main.app)`; a fixture sets `JENKINS_WEBHOOK_TOKEN=t0k` and `JENKINS_URL=https://ci.example.com` and clears the settings cache)

```python
HOOK = {"job": "acme/build", "build_number": 7,
        "build_url": "https://ci.example.com/job/acme/job/build/7/", "result": "FAILURE"}

def post(body, token="t0k"):
    headers = {"X-Webhook-Token": token} if token else {}
    return client.post("/webhooks/jenkins", json=body, headers=headers)

def test_new_failure_queued(webhook_env):
    r = post(HOOK)
    assert r.status_code == 202 and r.json()["duplicate"] is False

def test_duplicate_webhook_returns_existing_event(webhook_env):
    first = post(HOOK).json()["event_id"]
    r = post(HOOK)
    assert r.status_code == 200 and r.json() == {"event_id": first, "duplicate": True}

def test_webhook_token_required(webhook_env):
    assert post(HOOK, token=None).status_code == 401
    assert post(HOOK, token="wrong").status_code == 401

def test_webhook_disabled_without_configured_token(monkeypatch):
    monkeypatch.setenv("JENKINS_URL", "https://ci.example.com")
    get_settings.cache_clear()
    assert post(HOOK).status_code == 503

def test_build_url_outside_jenkins_rejected(webhook_env):
    r = post(HOOK | {"build_url": "http://169.254.169.254/latest/"})
    assert r.status_code == 422

def test_non_failure_result_ignored(webhook_env):
    r = post(HOOK | {"result": "SUCCESS"})
    assert r.status_code == 200 and r.json() == {"status": "ignored"}
```

- [x] **Step 2: Run, expect FAIL** — `pytest tests/test_webhooks.py -q` → 404s.
- [x] **Step 3: Implement** the router per Interfaces.
- [x] **Step 4: Run, expect PASS** — `pytest tests -q`.
- [x] **Step 5: Commit** — `git add backend/api backend/main.py backend/tests/test_webhooks.py && git commit -m "feat: token-authenticated Jenkins failure webhook with dedupe"`

---

### Task 4: Jenkins client (read-only)

**Files:** Create `backend/clients/__init__.py`, `backend/clients/jenkins.py`, `backend/tests/test_jenkins_client.py`

**Interfaces:**
- Consumes: `get_settings` (Task 1)
- Produces in `clients/jenkins.py`:
  - `class JenkinsError(Exception)`
  - `@dataclass BuildInfo`: `result: str | None`, `parameters: dict[str, str]`, `causes: list[str]`
  - `@dataclass ConsoleLog`: `text: str`, `total_bytes: int`, `truncated: bool`
  - `@dataclass Stage`: `name: str`, `status: str`
  - `@dataclass BuildRef`: `number: int`, `url: str`, `result: str | None`
  - `class JenkinsClient(base_url: str, username: str, api_token: str, *, timeout: float, max_log_bytes: int, transport: httpx.BaseTransport | None = None)` with:
    - `get_build_info(build_url) -> BuildInfo`: GET `{build_url}api/json?tree=result,actions[parameters[name,value],causes[shortDescription]]`. Skips parameters whose value is not a string or number (e.g. password params with no value).
    - `get_console(build_url) -> ConsoleLog`: streams `{build_url}consoleText` and keeps only the last `max_log_bytes`. When truncated, drops the partial first line. Decodes UTF-8 with replacement.
    - `get_stages(build_url) -> list[Stage] | None`: GET `{build_url}wfapi/describe`; 404 → `None`.
    - `list_builds(job: str, limit: int) -> list[BuildRef]`: `job` `"a/b"` maps to `{base}job/a/job/b/api/json?tree=builds[number,url,result]{0,limit}`.
  - Every method first checks that the URL starts with `base_url`, otherwise raises `JenkinsError("refusing URL outside Jenkins base")`. HTTP errors, timeouts and non-2xx statuses (except the wfapi 404) raise `JenkinsError(f"{status or type} for {path}")`, where `path` excludes the base URL and query string.
  - Auth: `httpx.BasicAuth(username, api_token)`.
  - `jenkins_client_from_settings() -> JenkinsClient` raises `JenkinsError("Jenkins is not configured")` when the URL, username or token is missing.

- [x] **Step 1: Failing tests** (build a `MockTransport` handler that routes on `request.url.path`; `client = JenkinsClient("https://ci.example.com/", "u", "tok", timeout=5, max_log_bytes=64, transport=...)`)

```python
BUILD = "https://ci.example.com/job/acme/job/build/7/"

def test_build_info_parses_parameters_and_causes(): ...
    # api/json returns {"result":"FAILURE","actions":[{"parameters":[{"name":"GERRIT_CHANGE_NUMBER","value":"48213"},{"name":"PW"}]},{"causes":[{"shortDescription":"Triggered by Gerrit"}]},{}]}
    # assert info.parameters == {"GERRIT_CHANGE_NUMBER": "48213"} and info.causes == ["Triggered by Gerrit"]

def test_console_tail_is_bounded():
    # consoleText returns "\n".join(f"line {i}" for i in range(100)).encode()
    log = client.get_console(BUILD)
    assert log.truncated is True and log.total_bytes > 64
    assert len(log.text.encode()) <= 64 and log.text.endswith("line 99")
    assert not log.text.startswith("ine")          # partial first line dropped

def test_stages_absent_returns_none():            # wfapi/describe -> 404
    assert client.get_stages(BUILD) is None

def test_client_refuses_foreign_url():
    with pytest.raises(JenkinsError):
        client.get_console("http://169.254.169.254/latest/")
    assert requests_seen == []                       # handler never called

def test_server_error_raises_jenkins_error():      # consoleText -> 503
    with pytest.raises(JenkinsError):
        client.get_console(BUILD)

def test_list_builds_uses_job_path():
    refs = client.list_builds("acme/build", 2)
    assert requests_seen[-1].url.path == "/job/acme/job/build/api/json"
    assert [r.number for r in refs] == [7, 6]

def test_basic_auth_header_sent():
    client.get_stages(BUILD)
    assert requests_seen[-1].headers["authorization"].startswith("Basic ")
```

- [x] **Step 2: Run, expect FAIL** — `pytest tests/test_jenkins_client.py -q` → import error.
- [x] **Step 3: Implement** per Interfaces. For the streaming tail, use `client.stream("GET", ...)` with `iter_bytes()` and a `bytearray` trimmed to `max_log_bytes` after each chunk.
- [x] **Step 4: Run, expect PASS** — `pytest tests -q`.
- [x] **Step 5: Commit** — `git add backend/clients backend/tests/test_jenkins_client.py && git commit -m "feat: read-only Jenkins REST client with URL scoping and bounded log fetch"`

---

### Task 5: Enrichment

**Files:** Create `backend/enrichment.py`, `backend/tests/test_enrichment.py`. Modify `backend/utils/redaction.py` (add `redact_mapping`), `backend/tests/test_redaction.py`.

**Interfaces:**
- Consumes: `JenkinsClient`, `BuildInfo`, `ConsoleLog`, `Stage` (Task 4); `redact` (P0)
- Produces:
  - `redact_mapping(values: dict[str, str]) -> dict[str, str]` in `utils/redaction.py`. Values whose key matches the P0 `_SECRET_IDENTIFIER` pattern (full-match, case-insensitive) become `[REDACTED]`; all other values pass through `redact()`.
  - `failing_stage(stages: list[Stage] | None, console: str) -> str | None`. Returns the first stage with status in `{"FAILED", "ABORTED"}`. Otherwise it returns the last name matched by `^\[Pipeline\] \{ \((.+)\)$` (multiline) that does not start with `"Declarative: "`. Otherwise `None`.
  - `artifactory_links(console: str) -> list[str]`: regex `https?://[^\s"'<>]*artifactory[^\s"'<>]*` (case-insensitive), deduplicated in order, at most 20.
  - `gerrit_scope(parameters: dict[str, str]) -> dict[str, str]`: maps the `GERRIT_` params `CHANGE_NUMBER`, `PATCHSET_NUMBER`, `CHANGE_ID`, `PROJECT`, `BRANCH`, `TOPIC`, `CHANGE_OWNER_EMAIL` and `PATCHSET_UPLOADER_EMAIL` to lower-case keys without the prefix (e.g. `change_number`). Only keys that are present are included.
  - `@dataclass EnrichmentData`: `build: BuildInfo` (parameters already redacted), `console: ConsoleLog` (text already redacted), `failing_stage: str | None`, `artifactory_links: list[str]`, `gerrit_scope: dict[str, str]`
  - `enrich(client: JenkinsClient, build_url: str) -> EnrichmentData`: calls build info, console and stages; redacts the console text and parameters; computes the rest. `gerrit_scope` is computed from the **unredacted** parameters, because none of its keys match secret names.

- [x] **Step 1: Failing tests**

```python
def test_failing_stage_prefers_wfapi():
    assert failing_stage([Stage("Checkout", "SUCCESS"), Stage("Compile", "FAILED")], "") == "Compile"

def test_failing_stage_falls_back_to_console_markers():
    log = (FIX / "compilation_java_symbol.log").read_text()
    assert failing_stage(None, log) == "Compile"

def test_failing_stage_skips_declarative_post_stage():
    log = (FIX / "devops_missing_credentials.log").read_text()
    assert failing_stage(None, log) == "Publish"

def test_artifactory_links_deduped():
    log = "get https://artifactory.example.com/a.jar\nretry https://artifactory.example.com/a.jar\n"
    assert artifactory_links(log) == ["https://artifactory.example.com/a.jar"]

def test_gerrit_scope_from_trigger_params():
    p = {"GERRIT_CHANGE_NUMBER": "48213", "GERRIT_TOPIC": "t1", "BRANCH": "main"}
    assert gerrit_scope(p) == {"change_number": "48213", "topic": "t1"}

def test_secret_parameters_redacted():
    out = redact_mapping({"DEPLOY_PASSWORD": "hunter2", "TARGET": "url https://u:p@h/x"})
    assert out["DEPLOY_PASSWORD"] == "[REDACTED]" and "u:p@" not in out["TARGET"]

def test_enrich_redacts_console_and_params(): ...
    # MockTransport serves the compilation fixture + "Authorization: Bearer abc.def.123" line
    # and params {"API_TOKEN": "x1", "GERRIT_CHANGE_NUMBER": "48213"}
    # assert "abc.def.123" not in data.console.text
    # assert data.build.parameters["API_TOKEN"] == "[REDACTED]"
    # assert data.gerrit_scope["change_number"] == "48213"
    # assert data.failing_stage == "Compile"
```

- [x] **Step 2: Run, expect FAIL** — `pytest tests/test_enrichment.py tests/test_redaction.py -q`.
- [x] **Step 3: Implement** per Interfaces.
- [x] **Step 4: Run, expect PASS** — `pytest tests -q`.
- [x] **Step 5: Commit** — `git add backend/enrichment.py backend/utils/redaction.py backend/tests/test_enrichment.py backend/tests/test_redaction.py && git commit -m "feat: enrichment (failing stage, artifactory links, gerrit scope, redaction)"`

---

### Task 6: Rules engine and reconcile

**Files:** Create `backend/classification/__init__.py`, `backend/classification/rules.py`, `backend/classification/reconcile.py`, `backend/tests/test_rules.py`

**Interfaces:**
- Produces in `rules.py`:
  - `@dataclass(frozen=True) RuleMatch`: `rule_id: str`, `category: str`, `line_number: int` (1-based), `line: str`
  - `evaluate_rules(log: str, failing_stage: str | None) -> list[RuleMatch]`: at most one match per rule (its first line). Compilation rules are skipped when `failing_stage` is set and does not match `(?i)compil|build|make|assembl`.
  - `RULES_VERSION = "2026-10-31.1"`
- Produces in `reconcile.py`:
  - `@dataclass Reconciliation`: `final_category: str`, `reason_codes: list[str]`
  - `reconcile(analysis: dict, matches: list[RuleMatch]) -> Reconciliation`. `analysis` is the `build_analysis_response` dict. Reason codes, in this order:
    - `unvalidated` if `analysis["validated"]` is not true
    - `low_confidence` if `confidence != "high"`
    - `rule_ambiguous` if the matches span more than one category
    - `rule_llm_conflict` if they span exactly one category and it differs from `analysis["llm_category"]`
  - `final_category = analysis["llm_category"]` when there are no reason codes and `llm_category` is set; otherwise `"needs_triage"`.
- Rule table (exact; all regexes case-insensitive):

| rule_id | category | pattern |
|---|---|---|
| `infra.channel_closed` | infra | `ChannelClosedException\|RemovedNodeListener` |
| `infra.agent_offline` | infra | `agent .* (was marked offline\|went offline\|is offline)\|Cannot contact [\w.-]+:` |
| `infra.disk_full` | infra | `No space left on device` |
| `infra.oom` | infra | `OOMKilled\|Out of memory: Killed process\|java\.lang\.OutOfMemoryError` |
| `infra.network` | infra | `Could not resolve host\|Temporary failure in name resolution\|Connection refused\|Connection timed out\|Read timed out` |
| `infra.artifactory_5xx` | infra | `artifactory\S*.*\b(500\|502\|503\|504)\b` |
| `devops.missing_credentials` | devops | `Could not find credentials entry` |
| `devops.permission_denied` | devops | `Permission denied` |
| `devops.command_not_found` | devops | `command not found` |
| `devops.no_tool` | devops | `No tool named` |
| `devops.pipeline_script` | devops | `WorkflowScript: \d+:\|No such DSL method\|groovy\.lang\.MissingPropertyException` |
| `compilation.compiler_error` | compilation | `\S+\.(java\|kt\|scala\|c\|cc\|cpp\|cxx\|h\|hpp\|go\|cs\|ts\|rs):\d+(:\d+)?:?\s+(fatal )?error\|error: cannot find symbol\|undefined reference to\|COMPILATION ERROR\|Compilation failed` |
| `tests.failures` | needs_triage | `Tests run: \d+, Failures: [1-9]\|There (were\|are) test failures\|FAILED: \S*Test` |

- [x] **Step 1: Failing tests**

```python
@pytest.mark.parametrize("name, category", [
    ("infra_agent_offline.log", "infra"), ("devops_missing_credentials.log", "devops"),
    ("compilation_java_symbol.log", "compilation"), ("test_failure.log", "needs_triage"),
])
def test_fixture_rule_category(name, category):
    log = (FIX / name).read_text()
    cats = {m.category for m in evaluate_rules(log, None)}
    assert category in cats

def test_compilation_rules_skipped_outside_build_stage():
    assert evaluate_rules("Foo.java:3: error: x", "Deploy") == []

def test_rule_match_reports_line_number():
    m = evaluate_rules("ok\nNo space left on device\n", None)[0]
    assert (m.rule_id, m.line_number) == ("infra.disk_full", 2)

def A(cat, validated=True, conf="high"):
    return {"llm_category": cat, "validated": validated, "confidence": conf}

def M(cat): return RuleMatch(f"{cat}.x", cat, 1, "l")

def test_agreement_keeps_category():
    assert reconcile(A("infra"), [M("infra")]) == Reconciliation("infra", [])

def test_rules_silent_keeps_validated_high_category():
    assert reconcile(A("devops"), []).final_category == "devops"

def test_conflict_routes_to_triage():
    assert reconcile(A("compilation"), [M("infra")]) == Reconciliation("needs_triage", ["rule_llm_conflict"])

def test_ambiguous_rules_route_to_triage():
    assert reconcile(A("infra"), [M("infra"), M("devops")]).reason_codes == ["rule_ambiguous"]

def test_low_confidence_and_unvalidated_reasons():
    r = reconcile(A("infra", validated=False, conf="medium"), [])
    assert r == Reconciliation("needs_triage", ["unvalidated", "low_confidence"])
```

- [x] **Step 2: Run, expect FAIL** — `pytest tests/test_rules.py -q`.
- [x] **Step 3: Implement** per Interfaces and the rule table.
- [x] **Step 4: Run, expect PASS** — `pytest tests -q`. If a fixture also triggers a rule from a second category (e.g. `Connection refused` in the infra fixture is fine; anything that makes the set ambiguous is not), fix the pattern, not the fixture.
- [x] **Step 5: Commit** — `git add backend/classification backend/tests/test_rules.py && git commit -m "feat: deterministic rules engine and LLM/rules reconcile"`

---

### Task 7: Shadow policy — proposed actions

**Files:** Create `backend/policy.py`, `backend/tests/test_policy.py`

**Interfaces:**
- Consumes: `EnrichmentData` (Task 5); `get_settings` (Task 1)
- Produces:
  - `@dataclass ActionProposal`: `action_type: str`, `target: dict`, `allowed: bool`, `reason_codes: list[str]`
  - `propose_action(final_category: str, job: str, enrichment: EnrichmentData) -> ActionProposal | None`:
    - `infra` → `("rerun", {"job": job, "parameters": enrichment.build.parameters})`. `allowed` if `fnmatch` matches any pattern in `rerun_job_allowlist`; otherwise reason `job_not_allowed`.
    - `devops` → `("notify_owner", {"recipient": owner_email_override, "source": "override"})`. `allowed` if a recipient is set; otherwise reason `target_unresolved`.
    - `compilation` → `("exclude_culprit", {"change_number", "patchset_number", "topic"})`, taken from `gerrit_scope` (each `None` if absent). `allowed` if `change_number` is present; otherwise reason `target_unresolved`.
    - `needs_triage` → `None`.
  - Pure function; no I/O.

- [x] **Step 1: Failing tests** (build `EnrichmentData` with a small helper in the test file)

```python
def test_infra_rerun_denied_by_default(): 
    p = propose_action("infra", "acme/build", enr(params={"X": "1"}))
    assert (p.action_type, p.allowed, p.reason_codes) == ("rerun", False, ["job_not_allowed"])
    assert p.target == {"job": "acme/build", "parameters": {"X": "1"}}

def test_infra_rerun_allowed_by_pattern(monkeypatch):
    monkeypatch.setenv("RERUN_JOB_ALLOWLIST", '["acme/*"]'); get_settings.cache_clear()
    assert propose_action("infra", "acme/build", enr()).allowed is True

def test_devops_notify_uses_owner_override(monkeypatch):
    monkeypatch.setenv("EMAIL_ID", "owner@example.com"); get_settings.cache_clear()
    p = propose_action("devops", "acme/build", enr())
    assert p.target == {"recipient": "owner@example.com", "source": "override"} and p.allowed

def test_devops_without_override_unresolved():
    assert propose_action("devops", "j", enr()).reason_codes == ["target_unresolved"]

def test_compilation_targets_gerrit_change():
    p = propose_action("compilation", "j", enr(gerrit={"change_number": "48213", "patchset_number": "2"}))
    assert p.target == {"change_number": "48213", "patchset_number": "2", "topic": None} and p.allowed

def test_needs_triage_has_no_action():
    assert propose_action("needs_triage", "j", enr()) is None
```

- [x] **Step 2: Run, expect FAIL**. **Step 3: Implement.** **Step 4: Run, expect PASS** (`pytest tests -q`).
- [x] **Step 5: Commit** — `git add backend/policy.py backend/tests/test_policy.py && git commit -m "feat: shadow policy producing proposed remediation actions"`

---

### Task 8: Worker — claim, process, retry

**Files:** Create `backend/pipeline.py`, `backend/worker.py`, `backend/tests/test_worker.py`

**Interfaces:**
- Consumes: Tasks 2, 4, 5, 6, 7; `graph.analyze_log` and `graph.build_analysis_response` (P0); `llm.LLMProviderError` (P0); `rules.RULES_VERSION`
- Produces in `pipeline.py`:
  - `process_event(session: Session, event: Event, jenkins: JenkinsClient) -> None`, which in order:
    1. calls `enrich`
    2. calls `analyze_log(enrichment.console.text)` and then `build_analysis_response(result, f"{event.job}#{event.build_number}")`
    3. runs `evaluate_rules(console text, failing_stage)` and then `reconcile`
    4. calls `propose_action`
    5. writes `Enrichment` (`log_excerpt = result.get("parsed_log", "")`; the degraded result from P0's `LLMOutputError` path has no `parsed_log`), `Analysis` (`model` = `settings.llm_model`, `rule_matches` = dicts plus `"rules_version"`) and `ProposedAction` (only when a proposal exists)
    6. sets `event.status = "done"`

    It raises `JenkinsError` / `LLMProviderError` unchanged.
- Produces in `worker.py`:
  - `claim_next(session: Session, now: datetime) -> Event | None`. Picks the lowest id where either `status="queued" and next_attempt_at <= now`, or `status="processing" and claimed_at < now - worker_lease_seconds`. It claims with a conditional `UPDATE ... WHERE id=:id AND status=:seen_status AND claimed_at IS :seen_claimed`, setting `status="processing"` and `claimed_at=now`. It returns the event only if `rowcount == 1`.
  - `run_once(jenkins: JenkinsClient, now: datetime | None = None) -> bool`. Returns `False` if nothing was claimed. On `JenkinsError` / `LLMProviderError` it increments `attempts` and sets `last_error = str(err)[:500]`. Then:
    - if `attempts >= worker_max_attempts` → `status="failed"`;
    - otherwise → `status="queued"`, `claimed_at=None`, `next_attempt_at = now + worker_backoff_seconds * 2 ** (attempts - 1)`.

    Any other exception is handled the same way (logged with `logger.exception` metadata only).
  - `main() -> None`: loop of `run_once` + `time.sleep(worker_poll_seconds)` when idle, using `jenkins_client_from_settings()` and `configure_logging`. Runs as `python -m worker`.

- [x] **Step 1: Failing tests** (`MockTransport` serves `compilation_java_symbol.log`, api/json with `GERRIT_CHANGE_NUMBER=48213`, and wfapi 404; `fake_llm` returns detector/root_cause/verdict JSON for the `[Line 20]` evidence line, computed the same way as `test_fixtures.py`)

```python
def test_run_once_processes_event_end_to_end(fake_llm, queued_event, jenkins):
    fake_llm(compilation_responses())
    assert run_once(jenkins) is True
    with session_scope() as s:
        e = s.get(Event, queued_event)
        assert e.status == "done"
        assert e.analysis.final_category == "compilation"
        assert e.enrichment.failing_stage == "Compile"
        assert e.action.action_type == "exclude_culprit" and e.action.target["change_number"] == "48213"

def test_idle_returns_false(jenkins):
    assert run_once(jenkins) is False

def test_jenkins_error_retries_then_fails(queued_event, broken_jenkins):   # consoleText -> 503
    t0 = utcnow()
    run_once(broken_jenkins, now=t0)
    with session_scope() as s:
        e = s.get(Event, queued_event)
        assert (e.status, e.attempts) == ("queued", 1)
        assert e.next_attempt_at == t0 + timedelta(seconds=60)
    run_once(broken_jenkins, now=t0 + timedelta(seconds=61))
    run_once(broken_jenkins, now=t0 + timedelta(seconds=10_000))
    with session_scope() as s:
        assert s.get(Event, queued_event).status == "failed"

def test_expired_lease_is_reclaimed(queued_event):
    t0 = utcnow()
    with session_scope() as s: assert claim_next(s, t0).id == queued_event
    with session_scope() as s: assert claim_next(s, t0 + timedelta(seconds=601)).id == queued_event

def test_live_claim_not_double_claimed(queued_event):
    t0 = utcnow()
    with session_scope() as s: claim_next(s, t0)
    with session_scope() as s: assert claim_next(s, t0 + timedelta(seconds=5)) is None

def test_stored_excerpt_is_redacted(fake_llm, queued_event, jenkins_with_secret_line): ...
    # console includes "Authorization: Bearer abc.def.123"; assert it's absent from enrichment.log_excerpt and analysis.response
```

- [x] **Step 2: Run, expect FAIL** — `pytest tests/test_worker.py -q`.
- [x] **Step 3: Implement** per Interfaces.
- [x] **Step 4: Run, expect PASS** — `pytest tests -q`.
- [x] **Step 5: Commit** — `git add backend/pipeline.py backend/worker.py backend/tests/test_worker.py && git commit -m "feat: worker with leased claims, retry/backoff, and shadow pipeline"`

---

### Task 9: Local accounts and roles

**Files:** Create `backend/auth.py`, `backend/manage.py`, `backend/tests/test_auth.py`

**Interfaces:**
- Consumes: `User`, `get_session` (Task 2)
- Produces:
  - `hash_password(password: str) -> str`: format `scrypt$16384$8$1$<salt hex>$<hash hex>`, with a 16-byte random salt and 32-byte `dklen`.
  - `verify_password(password: str, stored: str) -> bool`: constant-time comparison.
  - `ROLES = ("viewer", "labeler", "operator")`
  - `current_user()`: FastAPI dependency using `HTTPBasic(realm="remediation")`. On failure → 401 with `WWW-Authenticate: Basic realm="remediation"`.
  - `require_role(role: str)` → dependency returning the `User`; insufficient role → 403.
  - `manage.py`: `python -m manage create-user <username> --role <role>` (reads the password via `getpass` twice and must match; an existing username is an error) and `python -m manage set-password <username>`.

- [x] **Step 1: Failing tests** (a mini FastAPI app in the test file with routes `GET /v` → `require_role("viewer")` and `GET /l` → `require_role("labeler")`)

```python
def test_hash_roundtrip_and_salted():
    h1, h2 = hash_password("pw"), hash_password("pw")
    assert h1 != h2 and verify_password("pw", h1) and not verify_password("nope", h1)

def test_unauthenticated_gets_basic_challenge():
    r = client.get("/v")
    assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Basic")

def test_wrong_password_401(users):
    assert client.get("/v", auth=("viewer1", "bad")).status_code == 401

def test_role_hierarchy(users):
    assert client.get("/v", auth=("labeler1", "pw")).status_code == 200
    assert client.get("/l", auth=("viewer1", "pw")).status_code == 403

def test_manage_create_user(monkeypatch):
    monkeypatch.setattr("getpass.getpass", lambda *_: "pw")
    manage.main(["create-user", "alice", "--role", "labeler"])
    with session_scope() as s:
        assert s.query(User).filter_by(username="alice").one().role == "labeler"
```

- [x] **Step 2: Run, expect FAIL**. **Step 3: Implement.** **Step 4: Run, expect PASS** (`pytest tests -q`).
- [x] **Step 5: Commit** — `git add backend/auth.py backend/manage.py backend/tests/test_auth.py && git commit -m "feat: local accounts with scrypt hashing, Basic auth and roles"`

---

### Task 10: Review API

**Files:** Create `backend/api/review.py`, `backend/tests/test_review_api.py`. Modify `backend/main.py` (include router).

**Interfaces:**
- Consumes: Tasks 2 and 9; `ErrorCategory`
- Produces (every route requires at least `viewer`; label requires `labeler`):
  - `GET /api/events?status=&category=&unlabeled=&limit=50` returns a list of `{id, job, build_number, build_url, source, status, received_at, final_category, llm_category, validated, reason_codes, label_verdict}`, newest first. `category` filters on `final_category`; `unlabeled=true` keeps events with an analysis and no label; `limit` is capped at 200.
  - `GET /api/events/{id}` returns `{event, enrichment, analysis, action, label}`. Sections are `null` when absent; `enrichment.parameters` is the stored redacted dict. 404 if missing.
  - `POST /api/events/{id}/label` takes `{verdict: "correct"|"incorrect", correct_category: ErrorCategory | None, note: str | None}` and upserts the `Label` for that event's analysis, with `reviewer = user.username`. It returns 422 when `verdict == "incorrect"` without `correct_category`, or when the event has no analysis.
  - `GET /api/metrics/accuracy` returns, for each `final_category` with labels, `{labeled, correct, accuracy}`, where `accuracy = round(correct / labeled, 3)`.

- [x] **Step 1: Failing tests** (seed helper creates events with analyses; users from Task 9 fixtures)

```python
def test_events_require_auth():
    assert client.get("/api/events").status_code == 401

def test_list_filters_unlabeled_triage(seeded, viewer):
    r = client.get("/api/events?category=needs_triage&unlabeled=true", auth=viewer)
    assert [e["final_category"] for e in r.json()] == ["needs_triage"]

def test_viewer_cannot_label(seeded, viewer):
    assert client.post(f"/api/events/{seeded.infra}/label", json={"verdict": "correct"}, auth=viewer).status_code == 403

def test_incorrect_requires_category(seeded, labeler):
    r = client.post(f"/api/events/{seeded.infra}/label", json={"verdict": "incorrect"}, auth=labeler)
    assert r.status_code == 422

def test_label_upsert_and_accuracy(seeded, labeler):
    client.post(f"/api/events/{seeded.infra}/label", json={"verdict": "incorrect", "correct_category": "devops"}, auth=labeler)
    client.post(f"/api/events/{seeded.infra}/label", json={"verdict": "correct"}, auth=labeler)
    m = client.get("/api/metrics/accuracy", auth=labeler).json()
    assert m["infra"] == {"labeled": 1, "correct": 1, "accuracy": 1.0}

def test_detail_never_contains_raw_log(seeded, viewer):
    d = client.get(f"/api/events/{seeded.infra}", auth=viewer).json()
    assert "log_excerpt" in d["enrichment"] and "console" not in json.dumps(d)
```

- [x] **Step 2: Run, expect FAIL**. **Step 3: Implement.** **Step 4: Run, expect PASS** (`pytest tests -q`).
- [x] **Step 5: Commit** — `git add backend/api/review.py backend/main.py backend/tests/test_review_api.py && git commit -m "feat: review API (events, detail, labels, accuracy)"`

---

### Task 11: Review UI

**Files:** Create `backend/static/review/index.html`, `backend/static/review/review.js`, `backend/static/review/review.css`. Modify `backend/main.py` and `backend/tests/test_review_api.py`.

**Interfaces:**
- Consumes: the Task 10 endpoints; `current_user` (Task 9)
- Produces:
  - `GET /ui/` and `GET /ui/{name}`, both requiring `viewer`. `name` must be one of `{"index.html", "review.js", "review.css"}`, otherwise 404; `/ui/` serves `index.html`.
  - The page contains:
    - An accuracy strip from `/api/metrics/accuracy`.
    - A filter bar: "Needs triage (unlabeled)" (default), "All", and one entry per category.
    - An event table: job, build number (linked to `build_url`), received time, final category, LLM category, validated, reason codes, label.
    - A detail panel: failing stage, Artifactory links, Gerrit scope, the analysis root cause / evidence / fix, the proposed action (type, target, allowed, reason codes) shown with the heading **"Proposed (shadow — not executed)"**, and the redacted excerpt in a `<pre>`.
    - A label form: Correct / Incorrect, a category select (required for Incorrect), a note, and Save.
  - All DOM writes use `textContent` or `createElement`; never `innerHTML` with data. `fetch` uses same-origin relative URLs, so the browser's Basic credentials apply.

- [x] **Step 1: Failing tests** (append to `test_review_api.py`)

```python
def test_ui_requires_auth():
    assert client.get("/ui/").status_code == 401

def test_ui_serves_whitelisted_files_only(viewer):
    assert client.get("/ui/", auth=viewer).status_code == 200
    assert client.get("/ui/review.js", auth=viewer).status_code == 200
    assert client.get("/ui/..%2Fsettings.py", auth=viewer).status_code == 404

def test_ui_never_uses_innerhtml():
    js = (Path(main.__file__).parent / "static/review/review.js").read_text()
    assert "innerHTML" not in js
```

- [x] **Step 2: Run, expect FAIL**. **Step 3: Implement** the route and the three files. **Step 4: Run, expect PASS** (`pytest tests -q`; `node --check backend/static/review/review.js`).
- [x] **Step 5: Commit** — `git add backend/static backend/main.py backend/tests/test_review_api.py && git commit -m "feat: authenticated shadow-mode review UI"`

---

### Task 12: Backfill, deployment wiring, docs, end-to-end check

**Files:**
- Create `backend/backfill.py`, `backend/tests/test_backfill.py`, `backend/tests/test_e2e_shadow.py`, `docs/integration/jenkins-failure-webhook.md`
- Modify `docker-compose.yml`, `backend/.dockerignore` (add `.venv/`, `data/`), `README.md`, `CLAUDE.md`

**Interfaces:**
- Consumes: `JenkinsClient.list_builds`, `jenkins_client_from_settings` (Task 4); `Event`, `session_scope` (Task 2); the webhook, worker and review API (Tasks 3, 8, 10)
- Produces:
  - `backfill(jenkins: JenkinsClient, job: str, count: int, controller_id: str = "default") -> tuple[int, int]` returns `(queued, skipped)`. Only builds with `result == "FAILURE"` are considered; existing `(controller_id, job, number)` rows are skipped; new rows get `source="backfill"`.
  - CLI: `python -m backfill --job <folder/name> --count 50`.
  - `docker-compose.yml`:
    - `backend` and a new `worker` service (same image, `command: python -m worker`) share a `./backend/data:/app/data` volume and `env_file: ./backend/.env`.
    - `DATABASE_URL=sqlite:////app/data/remediation.db` is set for both.
  - `docs/integration/jenkins-failure-webhook.md`:
    - The Jenkinsfile `post { failure { ... } }` snippet. It uses `withCredentials([string(credentialsId: 'remediation-webhook-token', variable: 'HOOK_TOKEN')])`, builds the body with `groovy.json.JsonOutput.toJson([job: env.JOB_NAME, build_number: env.BUILD_NUMBER as int, build_url: env.BUILD_URL, result: 'FAILURE'])`, and sends it with `sh` + `curl -fsS --max-time 10 -H "X-Webhook-Token: $HOOK_TOKEN" -H 'Content-Type: application/json' --data @payload.json <service>/webhooks/jenkins || true`. A webhook failure must never fail the build.
    - The shared-library variant.
    - The backfill command.
    - How to create the first labeler: `python -m manage create-user`.
  - README: P1 run instructions (API + worker + `/ui/`). CLAUDE.md: the new modules and invariants (no external writes in P1; URL scoping; queue states; labels on analyses).

- [x] **Step 1: Failing tests**

```python
# test_backfill.py
def test_backfill_queues_failures_only(jenkins_builds):     # builds 7 FAILURE, 6 SUCCESS, 5 FAILURE
    assert backfill(jenkins_builds, "acme/build", 3) == (2, 0)

def test_backfill_skips_existing_events(jenkins_builds):
    backfill(jenkins_builds, "acme/build", 3)
    assert backfill(jenkins_builds, "acme/build", 3) == (0, 2)

# test_e2e_shadow.py — webhook -> worker -> review API, all in-process
def test_webhook_to_review_end_to_end(webhook_env, fake_llm, jenkins, labeler):
    event_id = client.post("/webhooks/jenkins", json=HOOK, headers={"X-Webhook-Token": "t0k"}).json()["event_id"]
    fake_llm(compilation_responses())
    assert run_once(jenkins) is True
    d = client.get(f"/api/events/{event_id}", auth=labeler).json()
    assert d["analysis"]["final_category"] == "compilation"
    assert d["action"]["action_type"] == "exclude_culprit" and d["action"]["allowed"] is True
    assert client.post(f"/api/events/{event_id}/label", json={"verdict": "correct"}, auth=labeler).status_code == 200
```

Move the shared fixtures (`webhook_env`, `jenkins`, `queued_event`, `compilation_responses`, user fixtures) into `tests/conftest.py` or `tests/fakes.py` when a second test file needs them.

- [x] **Step 2: Run, expect FAIL**. **Step 3: Implement** `backfill.py`, the compose changes, `.dockerignore` and the docs.
- [x] **Step 4: Verify**:
  - `pytest tests -q` → all pass.
  - `docker build -t jenkins-ai-backend ./backend` succeeds.
  - `docker compose config` is valid.
  - Manual: start the API and the worker locally with `JENKINS_URL` pointing at a stub. For the stub, `python -m http.server` serving a directory laid out as `job/demo/job/build/7/consoleText` and `api/json` from the fixtures. POST the webhook with curl, then confirm the event appears in `/ui/` after login.
- [x] **Step 5: Commit** — `git add backend/backfill.py backend/tests docker-compose.yml backend/.dockerignore docs/integration README.md CLAUDE.md && git commit -m "feat: backfill CLI, worker service, webhook integration docs"`

---

## Exit criteria (spec §7, P1)

- Failures flow end to end, webhook → worker → DB → UI, with nothing written to Jenkins, Gerrit or email (`test_webhook_to_review_end_to_end`).
- Classification accuracy is measurable per category from DevOps labels (`/api/metrics/accuracy`).
- Before the webhook is pointed at a production controller:
  - The **LLM data handling** open item (approval to send redacted build logs to Groq) is resolved.
  - The **hosting target and identity provider** open item is decided.
  (Named rather than numbered: the design doc and architecture doc number their open items differently.)
