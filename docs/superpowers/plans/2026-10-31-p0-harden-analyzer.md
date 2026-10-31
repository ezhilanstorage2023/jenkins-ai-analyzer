# P0 Harden Analyzer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the existing Jenkins AI Analyzer safe to drive automation. LLM output becomes schema-validated with retry, every result carries a `validated` flag, the 4-class taxonomy replaces the old labels, LLM calls stop blocking the event loop, configuration and logging are centralized, and secrets are redacted before any LLM call.

**Architecture:** Changes stay inside `backend/` and keep the current FastAPI + LangGraph layout. Three new modules:
- `settings.py`: pydantic-settings configuration.
- `llm.py`: the single Groq client, plus `complete_json()` for schema-validated calls with retry.
- `utils/redaction.py`: secret redaction.

`graph.py` keeps its node structure but calls `llm.complete_json()`. `main.py` runs the graph in a threadpool and maps LLM failures to HTTP 503. Unvalidated or unparseable results come back as HTTP 200 with category `needs_triage`, never as HTTP 500.

**Tech Stack:** Python 3.12, FastAPI, LangGraph, Groq SDK, Pydantic v2, pydantic-settings, pytest, httpx (for FastAPI `TestClient`).

**Spec:** `docs/superpowers/specs/2026-10-30-jenkins-auto-remediation-design.md` (§4 current-state findings, §7 P0 row, §6.2 taxonomy)

## Global Constraints

- Run all backend commands from `backend/`. Tests import top-level modules (`graph`, `llm`, `settings`, `models`, `utils.*`).
- Python 3.12 (`/home/vasan/.local/bin/python3.12` locally; CI and Docker already use 3.12).
- Category values are exactly `infra`, `devops`, `compilation`, `needs_triage`.
- The default LLM model is `openai/gpt-oss-120b`.
- Validation attempts max out at `3` (existing behavior, now set by `max_validation_attempts`).
- The parser LLM input cap is `10000` characters (existing behavior, now set by `parser_max_characters`).
- No test may call the real Groq API. The LLM is always faked through `tests/fakes.py`.
- The redaction token is exactly `[REDACTED]`. Redaction never changes the number of lines, so `[Line N]` evidence numbering stays aligned with the original log.
- Malformed or off-schema LLM output must never produce HTTP 500.
- Work on branch `p0-harden` (repo is on `main`, remote `origin`). Commit per task. Do not push.

## Review Focus

1. **Empty log or log with no error keywords:** expect HTTP 200, `error_category == "needs_triage"`, a reason saying no error context was found, and zero LLM calls. Test owner: Task 6, `test_no_error_context_short_circuits_without_llm`.
2. **LLM wraps JSON in ```` ```json ```` fences or adds prose around it:** the JSON should still parse. Test owner: Task 3, `test_complete_json_strips_code_fences` and `test_complete_json_extracts_object_from_prose`.
3. **LLM returns an old or unknown category label (e.g. `"Developer-Side Error"`):** expect a retry, then a degraded `needs_triage` response with HTTP 200, never 500. Test owners: Task 2, `test_root_cause_rejects_legacy_category`; Task 7, `test_garbage_llm_output_returns_200_needs_triage`.
4. **Secrets in the log (Bearer tokens, `password=`, credentialed URLs, Groq/GitHub/AWS keys, private-key blocks):** none should reach the LLM prompt or the logs, and line count must be unchanged. Compiler text such as `Unexpected token: '}'` must stay intact. Test owners: Task 4 (redaction unit tests); Task 6, `test_secrets_never_reach_llm_prompt`.
5. **Oversized upload:** expect HTTP 413 with a clear message, not a memory spike or 500. Test owner: Task 7, `test_upload_over_limit_returns_413`.

---

### Task 0: Environment baseline and branch

**Files:** none

- [ ] **Step 1: Create branch**

Run: `git switch -c p0-harden`
Expected: `Switched to a new branch 'p0-harden'`. `CLAUDE.md` and `docs/` stay untracked; they are committed in Task 9.

- [ ] **Step 2: Create venv and install**

Run: `cd backend && /home/vasan/.local/bin/python3.12 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt`
Expected: install completes with no resolver errors.

- [ ] **Step 3: Baseline tests**

Run: `pytest tests -q`
Expected: `5 passed`. If anything fails, stop and report; do not start Task 1 on a red baseline.

---

### Task 1: Settings module and test dependencies

**Files:**
- Create: `backend/settings.py`
- Create: `backend/tests/conftest.py`
- Create: `backend/tests/test_settings.py`
- Modify: `backend/requirements.txt` (add `pydantic-settings`, `httpx`)

**Interfaces:**
- Produces: `class Settings(BaseSettings)` and `get_settings() -> Settings` (`functools.lru_cache`). Fields and defaults:
  - `groq_api_key: SecretStr | None = None` (env `GROQ_API_KEY`)
  - `llm_model: str = "openai/gpt-oss-120b"`
  - `llm_parse_retries: int = 2` (total attempts = 1 + retries)
  - `max_validation_attempts: int = 3`
  - `parser_max_characters: int = 10000`
  - `max_upload_bytes: int = 20_000_000`
  - `cors_origins: list[str] = ["http://127.0.0.1:5500", "http://localhost:5500"]`
  - `log_level: str = "INFO"`
  - `model_config = SettingsConfigDict(env_file=".env", extra="ignore")`
- Produces: autouse fixture in `conftest.py`, run before and after each test. It:
  - deletes `GROQ_API_KEY` from the environment;
  - disables `.env` loading with `monkeypatch.setitem(Settings.model_config, "env_file", None)`, so a developer's real `backend/.env` key never leaks into tests;
  - calls `get_settings.cache_clear()`.

- [ ] **Step 1: Write failing tests**

```python
from settings import get_settings


def test_defaults():
    s = get_settings()
    assert s.llm_model == "openai/gpt-oss-120b"
    assert s.max_validation_attempts == 3
    assert s.parser_max_characters == 10000
    assert s.groq_api_key is None


def test_env_override(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    monkeypatch.setenv("MAX_UPLOAD_BYTES", "100")
    get_settings.cache_clear()
    s = get_settings()
    assert s.groq_api_key.get_secret_value() == "gsk_test"
    assert "gsk_test" not in repr(s)
    assert s.max_upload_bytes == 100
```

- [ ] **Step 2: Run, expect FAIL** — `pytest tests/test_settings.py -v` → `ModuleNotFoundError: No module named 'settings'`.
- [ ] **Step 3: Implement** `settings.py` per Interfaces and `conftest.py` fixture. Add `pydantic-settings` and `httpx` to `requirements.txt`, pinned to the versions `pip install` resolves (`pip freeze | grep -iE "pydantic-settings|httpx"`). Keep the existing `python-dotenv` pin; pydantic-settings uses it for `env_file`.
- [ ] **Step 4: Run, expect PASS** — `pytest tests -q` → all pass (7).
- [ ] **Step 5: Commit** — `git add backend/settings.py backend/tests/conftest.py backend/tests/test_settings.py backend/requirements.txt && git commit -m "feat: add pydantic settings module"`

---

### Task 2: Taxonomy and LLM output schemas

**Files:**
- Modify: `backend/models.py`
- Create: `backend/tests/test_models.py`

**Interfaces:**
- Produces in `models.py`:
  - `class ErrorCategory(str, Enum)`: `INFRA="infra"`, `DEVOPS="devops"`, `COMPILATION="compilation"`, `NEEDS_TRIAGE="needs_triage"`
  - `class DetectedErrors(BaseModel)`: `main_error: str`, `error_messages: list[str] = []`, `affected_component: str = "Unknown"`, `evidence: list[str] = []`
  - `class RootCauseResult(BaseModel)`: `root_cause: str`, `error_category: ErrorCategory`, `evidence: list[str] = []`, `affected_component: str = "Unknown"`, `recommended_fix: str`, `confidence: Literal["high", "medium", "low"]`. A `field_validator(mode="before")` on `error_category` and `confidence` applies `.strip().lower()` to strings.
  - `class ValidationVerdict(BaseModel)`: `valid: bool`, `reason: str = ""`, `unsupported_claims: list[str] = []`
  - `class AnalysisResponse(BaseModel)` (replaces the current one): `filename: str`, `failure_summary: str`, `root_cause: str`, `error_category: ErrorCategory`, `llm_category: ErrorCategory | None`, `validated: bool`, `validation_reason: str`, `attempts: int`, `affected_component: str`, `evidence: list[str]`, `recommended_fix: str`, `confidence: Literal["high", "medium", "low", "unknown"]`

- [ ] **Step 1: Write failing tests**

```python
import pytest
from pydantic import ValidationError
from models import ErrorCategory, RootCauseResult

BASE = {"root_cause": "x", "recommended_fix": "y", "confidence": "high"}


def test_category_values():
    assert {c.value for c in ErrorCategory} == {"infra", "devops", "compilation", "needs_triage"}


def test_root_cause_normalizes_case_and_whitespace():
    r = RootCauseResult(**BASE | {"error_category": " Compilation ", "confidence": "High"})
    assert r.error_category is ErrorCategory.COMPILATION
    assert r.confidence == "high"


def test_root_cause_rejects_legacy_category():
    with pytest.raises(ValidationError):
        RootCauseResult(**BASE | {"error_category": "Developer-Side Error"})
```

- [ ] **Step 2: Run, expect FAIL** — `pytest tests/test_models.py -v` → `ImportError: cannot import name 'ErrorCategory'`.
- [ ] **Step 3: Implement** the models per Interfaces.
- [ ] **Step 4: Run, expect PASS** — `pytest tests/test_models.py -v`. (`test_validation.py::test_build_analysis_response_includes_error_category` may still pass; Task 5 rewrites it.)
- [ ] **Step 5: Commit** — `git add backend/models.py backend/tests/test_models.py && git commit -m "feat: add 4-class taxonomy and LLM output schemas"`

---

### Task 3: Single LLM client with schema-validated JSON calls

**Files:**
- Create: `backend/llm.py`
- Create: `backend/tests/fakes.py`
- Create: `backend/tests/test_llm.py`
- Modify: `backend/tests/conftest.py` (add `fake_llm` fixture)

**Interfaces:**
- Consumes: `get_settings()` (Task 1); `DetectedErrors`, `RootCauseResult`, `ValidationVerdict` (Task 2)
- Produces in `llm.py`:
  - `class LLMError(Exception)`; `class LLMOutputError(LLMError)`; `class LLMProviderError(LLMError)`; `class LLMUnavailableError(LLMProviderError)`
  - `get_llm_client()`: returns a cached `groq.Groq` built from `settings.groq_api_key`. Raises `LLMUnavailableError("GROQ_API_KEY is not configured")` when the key is `None`.
  - `complete_json(prompt: str, schema: type[T]) -> T` where `T` is bound to `BaseModel`. Looks up `get_llm_client()` as a module global at call time so tests can monkeypatch it. Calls `client.chat.completions.create(model=settings.llm_model, messages=[{"role": "user", "content": prompt}], response_format={"type": "json_object"})`.
    - Parsing: strip a surrounding ```` ``` ````/```` ```json ```` fence. If the text still isn't valid JSON, take the substring from the first `{` to the last `}`. Then `schema.model_validate_json`.
    - Retries: on `json.JSONDecodeError` or `pydantic.ValidationError`, retry up to `settings.llm_parse_retries` more times, then raise `LLMOutputError` naming the schema and the last error.
    - Provider errors: `groq.APIError` is wrapped as `LLMProviderError` with no retry (the SDK already retries transport errors).
- Produces in `tests/fakes.py`:
  - `class FakeLLMClient`, built as `FakeLLMClient(responses: list[str | Exception])`. Exposes `.chat.completions.create(**kwargs)`, which pops the next response and either raises it (if it's an exception) or returns an object with `.choices[0].message.content`. Records every kwargs dict in `.calls` and raises `AssertionError("FakeLLMClient exhausted")` when empty.
  - JSON builders: `detected_json(evidence: list[str], main_error: str = "Build failed") -> str`; `root_cause_json(category: str, evidence: list[str], confidence: str = "high") -> str`; `verdict_json(valid: bool, reason: str = "ok") -> str`.
- Produces in `conftest.py`: fixture `fake_llm(monkeypatch)` returning a factory `make(responses) -> FakeLLMClient` that monkeypatches `llm.get_llm_client` to return the fake.

- [ ] **Step 1: Write failing tests** in `tests/test_llm.py`

```python
import groq, httpx, pytest
import llm
from models import ValidationVerdict


def test_complete_json_parses_plain_json(fake_llm):
    fake = fake_llm(['{"valid": true, "reason": "ok"}'])
    assert llm.complete_json("p", ValidationVerdict).valid is True
    assert fake.calls[0]["response_format"] == {"type": "json_object"}
    assert fake.calls[0]["model"] == "openai/gpt-oss-120b"


def test_complete_json_strips_code_fences(fake_llm):
    fake_llm(['```json\n{"valid": false}\n```'])
    assert llm.complete_json("p", ValidationVerdict).valid is False


def test_complete_json_extracts_object_from_prose(fake_llm):
    fake_llm(['Here you go: {"valid": true} hope that helps'])
    assert llm.complete_json("p", ValidationVerdict).valid is True


def test_complete_json_retries_then_succeeds(fake_llm):
    fake = fake_llm(["not json", '{"valid": true}'])
    assert llm.complete_json("p", ValidationVerdict).valid is True
    assert len(fake.calls) == 2


def test_complete_json_raises_after_retries_exhausted(fake_llm):
    fake = fake_llm(["bad", "bad", "bad"])
    with pytest.raises(llm.LLMOutputError):
        llm.complete_json("p", ValidationVerdict)
    assert len(fake.calls) == 3


def test_provider_error_wrapped_without_retry(fake_llm):
    err = groq.APIConnectionError(request=httpx.Request("POST", "https://api.groq.com"))
    fake = fake_llm([err])
    with pytest.raises(llm.LLMProviderError):
        llm.complete_json("p", ValidationVerdict)
    assert len(fake.calls) == 1


def test_missing_api_key_raises_unavailable():
    with pytest.raises(llm.LLMUnavailableError):
        llm.get_llm_client()
```

- [ ] **Step 2: Run, expect FAIL** — `pytest tests/test_llm.py -v` → `ModuleNotFoundError: No module named 'llm'`.
- [ ] **Step 3: Implement** `llm.py`, `tests/fakes.py`, and the `fake_llm` fixture per Interfaces. The cached client must reset when settings change: put the cache on a helper keyed by the API key string, and clear it in the conftest autouse fixture.
- [ ] **Step 4: Run, expect PASS** — `pytest tests/test_llm.py -v` → 7 passed.
- [ ] **Step 5: Commit** — `git add backend/llm.py backend/tests/fakes.py backend/tests/test_llm.py backend/tests/conftest.py && git commit -m "feat: add single LLM client with schema-validated JSON and retry"`

---

### Task 4: Secret redaction and structured logging

**Files:**
- Create: `backend/utils/redaction.py`
- Create: `backend/logging_config.py`
- Create: `backend/tests/test_redaction.py`
- Create: `backend/tests/test_logging_config.py`

**Interfaces:**
- Produces: `redact(text: str) -> str` in `utils/redaction.py`. Rules are applied in this order; each replacement is `[REDACTED]` unless noted:
  1. Private-key blocks, `-----BEGIN [A-Z ]*PRIVATE KEY-----` … `-----END [A-Z ]*PRIVATE KEY-----` (DOTALL). Replaced with `[REDACTED PRIVATE KEY]` followed by the same number of `\n` the block contained.
  2. `(?i)(authorization:\s*(?:bearer|basic|token)\s+)\S+` → keep group 1.
  3. `(?i)\b([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@` → `\1[REDACTED]@`.
  4. `(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|client[_-]?secret)\s*=\s*("[^"]*"|'[^']*'|\S+)` → `\1=[REDACTED]`.
  5. `(?i)("(?:password|secret|token|api[_-]?key)"\s*:\s*)"[^"]*"` → `\1"[REDACTED]"`.
  6. Token shapes `\bgsk_[A-Za-z0-9]{20,}`, `\bgh[pousr]_[A-Za-z0-9]{36,}`, `\bAKIA[0-9A-Z]{16}\b`, `\bxox[baprs]-[A-Za-z0-9-]{10,}`.
- Produces: `configure_logging(level: str) -> None` and `class JsonFormatter(logging.Formatter)` in `logging_config.py`. Each record is emitted as one JSON line with keys `ts` (ISO-8601 UTC), `level`, `logger`, `message`, plus any `extra=` fields. Configuration is idempotent (replaces root handlers, never stacks them).

- [ ] **Step 1: Write failing tests**

```python
import pytest
from utils.redaction import redact

@pytest.mark.parametrize("raw, secret", [
    ("Authorization: Bearer abc.def.ghi", "abc.def.ghi"),
    ("git clone https://bot:hunter2@gerrit.example/repo", "hunter2"),
    ("export PASSWORD=s3cr3t", "s3cr3t"),
    ('{"token": "tok-123"}', "tok-123"),
    ("key gsk_" + "a" * 30, "gsk_" + "a" * 30),
    ("ghp_" + "b" * 36, "ghp_" + "b" * 36),
    ("AKIAABCDEFGHIJKLMNOP", "AKIAABCDEFGHIJKLMNOP"),
])
def test_redacts_secret(raw, secret):
    out = redact(raw)
    assert secret not in out and "[REDACTED" in out

def test_private_key_block_preserves_line_count():
    raw = "a\n-----BEGIN RSA PRIVATE KEY-----\nMIIE\nxyz\n-----END RSA PRIVATE KEY-----\nb"
    out = redact(raw)
    assert "MIIE" not in out
    assert len(out.splitlines()) == len(raw.splitlines())
    assert out.splitlines()[-1] == "b"

def test_compiler_text_untouched():
    raw = "src/app.ts(3,1): error TS1128: Unexpected token: '}'\nerror: cannot find symbol"
    assert redact(raw) == raw
```

```python
import json, logging
from logging_config import JsonFormatter

def test_json_formatter_emits_one_json_object():
    rec = logging.LogRecord("graph", logging.INFO, __file__, 1, "parsed %d lines", (12,), None)
    rec.event_id = "e1"
    out = json.loads(JsonFormatter().format(rec))
    assert out["message"] == "parsed 12 lines"
    assert out["level"] == "INFO" and out["logger"] == "graph" and out["event_id"] == "e1"
```

- [ ] **Step 2: Run, expect FAIL** — `pytest tests/test_redaction.py tests/test_logging_config.py -v` → import errors.
- [ ] **Step 3: Implement** both modules per Interfaces.
- [ ] **Step 4: Run, expect PASS** — same command → all pass.
- [ ] **Step 5: Commit** — `git add backend/utils/redaction.py backend/logging_config.py backend/tests/test_redaction.py backend/tests/test_logging_config.py && git commit -m "feat: add secret redaction and JSON logging"`

---

### Task 5: Response builder with validated semantics

**Files:**
- Modify: `backend/graph.py` (`build_analysis_response` only)
- Modify: `backend/tests/test_validation.py` (replace `test_build_analysis_response_includes_error_category`)

**Interfaces:**
- Consumes: `AnalysisResponse`, `ErrorCategory` (Task 2)
- Produces: `build_analysis_response(result: dict, filename: str) -> dict`, whose keys match `AnalysisResponse` exactly. Rules:
  - `validated = result.get("validation_result", {}).get("valid") is True`
  - `llm_category = result.get("root_cause", {}).get("error_category")` (string or `None`)
  - `error_category = llm_category if validated and llm_category else "needs_triage"`
  - `validation_reason = validation_result.get("reason", "")`
  - `attempts = result.get("attempt_count", 0)`
  - `evidence`: the root-cause evidence if non-empty, else the detected evidence, else `[]`
  - `confidence = root_cause.get("confidence", "unknown")`
  - Text fields default to `"Unknown"`, and `recommended_fix` to `"Not yet determined"`.
  - Never raises `KeyError` on missing keys.

- [ ] **Step 1: Write failing tests** (replace the old category test in `tests/test_validation.py`)

```python
from models import AnalysisResponse

VALID_RC = {"root_cause": "src does not compile", "error_category": "compilation",
            "evidence": ["[Line 3] error: cannot find symbol"], "recommended_fix": "fix import",
            "confidence": "high", "affected_component": "javac"}


def test_validated_result_keeps_llm_category():
    r = build_analysis_response({"detected_errors": {"main_error": "javac failed"}, "root_cause": VALID_RC,
                                 "validation_result": {"valid": True, "reason": "ok"}, "attempt_count": 1}, "b.log")
    assert r["error_category"] == "compilation" and r["validated"] is True and r["attempts"] == 1
    AnalysisResponse(**r)


def test_unvalidated_result_forced_to_needs_triage():
    r = build_analysis_response({"detected_errors": {}, "root_cause": VALID_RC,
                                 "validation_result": {"valid": False, "reason": "unsupported"}, "attempt_count": 3}, "b.log")
    assert r["error_category"] == "needs_triage"
    assert r["llm_category"] == "compilation"
    assert r["validated"] is False and r["validation_reason"] == "unsupported"


def test_empty_result_does_not_raise():
    r = build_analysis_response({}, "b.log")
    assert r["error_category"] == "needs_triage" and r["failure_summary"] == "Unknown"
    assert r["confidence"] == "unknown"
    AnalysisResponse(**r)
```

- [ ] **Step 2: Run, expect FAIL** — `pytest tests/test_validation.py -v` → `KeyError: 'validated'` (or similar).
- [ ] **Step 3: Implement** `build_analysis_response` per the rules above.
- [ ] **Step 4: Run, expect PASS** — `pytest tests -q`.
- [ ] **Step 5: Commit** — `git commit -am "feat: surface validated flag and route unvalidated results to needs_triage"`

---

### Task 6: Graph pipeline on the hardened LLM layer

**Files:**
- Modify: `backend/graph.py`
- Create: `backend/tests/test_graph.py`

**Interfaces:**
- Consumes: `llm.complete_json`, `llm.LLMOutputError` (Task 3); `redact` (Task 4); `get_settings` (Task 1); schemas (Task 2); `FakeLLMClient`, builders, `fake_llm` (Task 3)
- Produces: `analyze_log(log_content: str) -> dict` (same name, same return shape as the input to `build_analysis_response`). Behavior changes:
  1. `log_content = redact(log_content)` runs first, before parsing.
  2. `parser_node` reads its cap from `settings.parser_max_characters`. If `parsed_log` is empty, it sets `validation_result = {"valid": False, "reason": "No error context found in log."}`, and a new conditional edge `parser_router` sends the graph to `END` before any LLM node.
  3. The three LLM nodes call `llm.complete_json(prompt, DetectedErrors | RootCauseResult | ValidationVerdict)` and store `.model_dump(mode="json")` in state. Remove the module-level `load_dotenv` and Groq client from `graph.py`.
  4. `validator_router` uses `settings.max_validation_attempts`.
  5. If `llm.LLMOutputError` escapes `app.invoke`, `analyze_log` returns `{"parser_metadata": {}, "detected_errors": {}, "root_cause": {}, "validation_result": {"valid": False, "reason": f"LLM output invalid: {err}"}, "attempt_count": 0}`. `LLMProviderError` propagates.
  6. Every `print` is replaced with `logging.getLogger("graph")` calls. Logs carry only metadata (counts, attempt numbers, validity), never log text, prompts or LLM output.
  7. The root-cause prompt replaces the three old category definitions with this exact text:
     - `infra`: Jenkins agent/node offline or disconnected, executor or channel failure, network/DNS, storage or disk space, out-of-memory kill, cloud resource, or an external service being unavailable (including Artifactory/registry 5xx or timeouts).
     - `devops`: Jenkinsfile or pipeline script error, build/deploy automation, credentials, permissions, missing tool or toolchain, or environment configuration.
     - `compilation`: application source code fails to compile or link (compiler or linker errors in project source).
     - `needs_triage`: failing tests, mixed or unclear causes, or evidence insufficient to choose one of the above.

     It also instructs: "Set error_category to exactly one of: infra, devops, compilation, needs_triage."

Fixture log used below (module constant in the test):

```python
LOG = "Started\nCompiling\n[javac] Foo.java:3: error: cannot find symbol\nBUILD FAILED\nFinished: FAILURE"
EVID = "[Line 3] [javac] Foo.java:3: error: cannot find symbol"
```

- [ ] **Step 1: Write failing tests** in `tests/test_graph.py`

```python
from graph import analyze_log, build_analysis_response
from fakes import detected_json, root_cause_json, verdict_json


def test_happy_path_three_llm_calls(fake_llm):
    fake = fake_llm([detected_json([EVID]), root_cause_json("compilation", [EVID]), verdict_json(True)])
    r = build_analysis_response(analyze_log(LOG), "b.log")
    assert r["error_category"] == "compilation" and r["validated"] is True
    assert len(fake.calls) == 3


def test_ungrounded_evidence_exhausts_retries_to_needs_triage(fake_llm):
    bad = root_cause_json("compilation", ["[Line 99] invented"])
    fake = fake_llm([detected_json([EVID])] + [bad, verdict_json(True)] * 3)
    r = build_analysis_response(analyze_log(LOG), "b.log")
    assert r["attempts"] == 3 and r["validated"] is False
    assert r["error_category"] == "needs_triage" and r["llm_category"] == "compilation"
    assert len(fake.calls) == 7


def test_no_error_context_short_circuits_without_llm(fake_llm):
    fake = fake_llm([])
    r = build_analysis_response(analyze_log("all good\nFinished: SUCCESS"), "b.log")
    assert r["error_category"] == "needs_triage"
    assert r["validation_reason"] == "No error context found in log."
    assert fake.calls == []


def test_unparseable_llm_output_degrades(fake_llm):
    fake_llm(["garbage"] * 3)
    r = build_analysis_response(analyze_log(LOG), "b.log")
    assert r["error_category"] == "needs_triage" and r["validation_reason"].startswith("LLM output invalid")


def test_secrets_never_reach_llm_prompt(fake_llm, caplog):
    log = "Authorization: Bearer SECRETTOKEN123\n" + LOG
    evid = "[Line 4] [javac] Foo.java:3: error: cannot find symbol"
    fake = fake_llm([detected_json([evid]), root_cause_json("compilation", [evid]), verdict_json(True)])
    caplog.set_level("DEBUG")
    analyze_log(log)
    assert all("SECRETTOKEN123" not in str(c["messages"]) for c in fake.calls)
    assert "SECRETTOKEN123" not in caplog.text
```

- [ ] **Step 2: Run, expect FAIL** — `pytest tests/test_graph.py -v` → failures (old graph calls the real client and has no short-circuit).
- [ ] **Step 3: Implement** the changes listed in Interfaces.
- [ ] **Step 4: Run, expect PASS** — `pytest tests -q` → all pass, including the existing `test_validation.py` and `test_log_processor.py`.
- [ ] **Step 5: Commit** — `git add backend/graph.py backend/tests/test_graph.py && git commit -m "feat: harden LangGraph pipeline with schema-validated LLM calls, redaction and 4-class taxonomy"`

---

### Task 7: API layer — threadpool, error mapping, upload limit

**Files:**
- Modify: `backend/main.py`
- Create: `backend/tests/test_api.py`

**Interfaces:**
- Consumes: `analyze_log`, `build_analysis_response` (Tasks 5–6); `LLMProviderError`, `LLMUnavailableError` (Task 3); `get_settings` (Task 1); `configure_logging` (Task 4)
- Produces, in `main.py`:
  - Remove `load_dotenv`, the module-level Groq client, the API-key `print`, and the `/test-llm` endpoint.
  - Call `configure_logging(settings.log_level)` at startup. CORS `allow_origins` comes from `settings.cors_origins`.
  - `POST /analyze-log`:
    - Reads at most `get_settings().max_upload_bytes + 1` bytes; above the limit it returns 413 with detail `f"Log file exceeds {max_upload_bytes} bytes."`.
    - Decodes UTF-8 with `errors="replace"`.
    - Runs `await run_in_threadpool(analyze_log, text)` (`starlette.concurrency`).
    - `LLMUnavailableError` → 503, detail `"LLM is not configured (GROQ_API_KEY missing)."`. Other `LLMProviderError` → 503, detail `"LLM provider request failed."`.
  - `GET /` and `POST /upload-log` are unchanged.

- [ ] **Step 1: Write failing tests** in `tests/test_api.py` (use `fastapi.testclient.TestClient(main.app)`; post with `files={"file": ("b.log", body, "text/plain")}`)

```python
import asyncio, pytest
from fastapi.testclient import TestClient
import graph, llm, main
from fakes import detected_json, root_cause_json, verdict_json

client = TestClient(main.app)
LOG = b"Compiling\n[javac] Foo.java:3: error: cannot find symbol\nBUILD FAILED"
EVID = "[Line 2] [javac] Foo.java:3: error: cannot find symbol"

def post(body):
    return client.post("/analyze-log", files={"file": ("b.log", body, "text/plain")})

def test_happy_path_200(fake_llm):
    fake_llm([detected_json([EVID]), root_cause_json("compilation", [EVID]), verdict_json(True)])
    r = post(LOG)
    assert r.status_code == 200 and r.json()["error_category"] == "compilation"

def test_garbage_llm_output_returns_200_needs_triage(fake_llm):
    fake_llm(["garbage"] * 3)
    r = post(LOG)
    assert r.status_code == 200 and r.json()["error_category"] == "needs_triage"

def test_missing_key_returns_503():
    r = post(LOG)
    assert r.status_code == 503 and "GROQ_API_KEY" in r.json()["detail"]

def test_upload_over_limit_returns_413(monkeypatch):
    monkeypatch.setenv("MAX_UPLOAD_BYTES", "10")
    from settings import get_settings; get_settings.cache_clear()
    assert post(b"x" * 11).status_code == 413

def test_non_utf8_upload_does_not_500(fake_llm):
    fake_llm([])
    r = post(b"\xff\xfe binary \x00 junk")
    assert r.status_code == 200 and r.json()["error_category"] == "needs_triage"

def test_analysis_runs_off_event_loop(monkeypatch):
    seen = {}
    def fake_analyze(text):
        try:
            asyncio.get_running_loop(); seen["in_loop"] = True
        except RuntimeError:
            seen["in_loop"] = False
        return {}
    monkeypatch.setattr(main, "analyze_log", fake_analyze)
    assert post(LOG).status_code == 200 and seen["in_loop"] is False

def test_test_llm_endpoint_removed():
    assert client.get("/test-llm").status_code == 404
```

- [ ] **Step 2: Run, expect FAIL** — `pytest tests/test_api.py -v`. Importing `main` may fail outright, since today it builds a Groq client with no key; that counts as the expected failure.
- [ ] **Step 3: Implement** the `main.py` changes per Interfaces. Import `analyze_log` into `main`'s namespace so the monkeypatch in the last-but-one test takes effect.
- [ ] **Step 4: Run, expect PASS** — `pytest tests -q` → all pass.
- [ ] **Step 5: Commit** — `git add backend/main.py backend/tests/test_api.py && git commit -m "feat: run analysis off event loop, map LLM failures to 503, cap upload size"`

---

### Task 8: Per-category log fixtures

**Files:**
- Create: `backend/tests/fixtures/logs/infra_agent_offline.log`, `devops_missing_credentials.log`, `compilation_java_symbol.log`, `test_failure.log`
- Create: `backend/tests/test_fixtures.py`

**Interfaces:**
- Consumes: `extract_error_context`; `analyze_log`, `build_analysis_response`; `fake_llm` and builders
- Fixture content: each is a realistic 15–40 line Jenkins console log. It contains the signature line below, generic surrounding noise (checkout, stage markers), and a final `Finished: FAILURE`.
  - infra: `hudson.remoting.ChannelClosedException: Channel "hudson.remoting.Channel@1a2b:agent-07": Remote call on agent-07 failed. The channel is closing down or has closed down`
  - devops: `ERROR: Could not find credentials entry with ID 'gerrit-ssh-key'`
  - compilation: `[javac] /src/com/acme/Foo.java:42: error: cannot find symbol`
  - test failure: `Tests run: 120, Failures: 2, Errors: 0, Skipped: 1` and `FAILED: com.acme.FooTest.testParse`

- [ ] **Step 1: Write tests** in `tests/test_fixtures.py`

```python
import pytest
from pathlib import Path
from utils.log_processor import extract_error_context
from graph import analyze_log, build_analysis_response
from fakes import detected_json, root_cause_json, verdict_json

FIX = Path(__file__).parent / "fixtures" / "logs"
CASES = [
    ("infra_agent_offline.log", "ChannelClosedException", "infra"),
    ("devops_missing_credentials.log", "Could not find credentials entry", "devops"),
    ("compilation_java_symbol.log", "cannot find symbol", "compilation"),
    ("test_failure.log", "FAILED: com.acme.FooTest.testParse", "needs_triage"),
]

@pytest.mark.parametrize("name, signature, _", CASES)
def test_parser_keeps_signature_line(name, signature, _):
    assert signature in extract_error_context((FIX / name).read_text())

@pytest.mark.parametrize("name, signature, category", CASES)
def test_pipeline_end_to_end_per_category(fake_llm, name, signature, category):
    parsed = extract_error_context((FIX / name).read_text())
    evid = next(l for l in parsed.splitlines() if signature in l)
    fake_llm([detected_json([evid]), root_cause_json(category, [evid]), verdict_json(True)])
    r = build_analysis_response(analyze_log((FIX / name).read_text()), name)
    assert r["error_category"] == category and r["evidence"] == [evid]
```

- [ ] **Step 2: Run, expect FAIL** — `pytest tests/test_fixtures.py -v` → `FileNotFoundError`.
- [ ] **Step 3: Write the four fixture logs** per Interfaces. If `test_parser_keeps_signature_line` fails for the infra fixture because `ChannelClosedException` matches no pattern, add `r"\bchannelclosedexception\b"` and `r"\bcould not find credentials\b"` to `root_cause_patterns` in `utils/log_processor.py`. Those are the only parser changes allowed in this task.
- [ ] **Step 4: Run, expect PASS** — `pytest tests -q`.
- [ ] **Step 5: Commit** — `git add backend/tests/fixtures backend/tests/test_fixtures.py backend/utils/log_processor.py && git commit -m "test: add per-category Jenkins log fixtures"`

---

### Task 9: Frontend taxonomy, docs, and end-to-end check

**Files:**
- Modify: `frontend/app.js`, `frontend/index.html`, `frontend/style.css`
- Modify: `README.md` (close the truncated architecture code fence at line 59; replace the category description with the 4-class taxonomy and the `validated` behavior; add the settings env vars table)
- Modify: `CLAUDE.md` (update the Architecture and Cross-file invariants sections: `settings.py`, `llm.py`, `utils/redaction.py`, the 4 categories in `models.ErrorCategory`, the `validated → needs_triage` rule, the `fake_llm` test fixture, and the removal of `/test-llm` and the duplicate Groq client)
- Add: `docs/` (design, architecture, this plan) and `CLAUDE.md` to git

**Interfaces:**
- Consumes: the `AnalysisResponse` JSON shape from Task 2
- Frontend:
  - `const API_BASE = window.API_BASE || "http://127.0.0.1:8000";`
  - Category labels: `infra` → "Infrastructure", `devops` → "DevOps", `compilation` → "Compilation", `needs_triage` → "Needs triage".
  - A new `#validated` badge shows "Validated" or "Not validated". When not validated, `validation_reason` appears beneath it.
  - Show `attempts` next to the confidence value.
  - All DOM writes keep using `textContent`.

- [ ] **Step 1: Implement** the frontend changes.
- [ ] **Step 2: Run backend tests** — `cd backend && pytest tests -q` → all pass.
- [ ] **Step 3: Manual check without a key**
  - Run `uvicorn main:app --port 8000` from `backend/` with `GROQ_API_KEY` unset, and `python -m http.server 5500 --directory frontend` from the repo root.
  - Upload `backend/tests/fixtures/logs/compilation_java_symbol.log`. Expect the status line `Error: LLM is not configured (GROQ_API_KEY missing).`
  - Upload a file containing only `Finished: SUCCESS`. Expect the "Needs triage" category, the "Not validated" badge, and the reason "No error context found in log."
- [ ] **Step 4: Manual check with key (if available)** — set `GROQ_API_KEY` in `backend/.env`, upload each of the 4 fixtures, and record the returned category and `validated` value in the commit message body. Mismatches are expected data for P1's eval set, not a failure of this task.
- [ ] **Step 5: Docker build** — `docker build -t jenkins-ai-backend ./backend` → succeeds.
- [ ] **Step 6: Commit**

```bash
git add frontend README.md CLAUDE.md docs
git commit -m "feat: show 4-class taxonomy and validation status in UI; update docs"
```

---

## Exit criteria (from spec §7, P0)

- `pytest tests -q` passes in `backend/` (CI runs the same command).
- No test or manual path where malformed LLM output returns HTTP 500 (Review Focus 3, Task 7 tests).
- The upload UI shows the new taxonomy and validation status (Task 9, Step 3).
