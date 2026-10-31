# Jenkins AI Analyzer

Classifies a failed Jenkins build from its console log. It extracts the exact error lines as evidence, determines the root cause, recommends a fix, and assigns one of four categories: **infra**, **devops**, **compilation** or **needs_triage**.

It is being extended into an automated remediation system (re-run infra failures, notify owners of DevOps failures, exclude the culprit Gerrit change on compilation failures). Design and plans: `docs/superpowers/specs/`, `docs/architecture/`, `docs/superpowers/plans/`.

**Stack:** FastAPI · LangGraph · Groq LLM · static HTML/JS frontend · Docker Compose.

---

## 1. What works today

| Capability | Status |
|---|---|
| Upload a console log in the browser or via API and get a classification | ✅ Available |
| 4-class taxonomy with a `validated` flag (unvalidated → `needs_triage`) | ✅ Available |
| Secret redaction before any LLM call | ✅ Available |
| Jenkins webhook, background worker, review UI, labeling | 🚧 P1, in progress |
| Automatic re-run of infra failures | ⏳ P2 |
| Owner email for DevOps failures | ⏳ P3 |
| Gerrit culprit exclusion + re-trigger for compilation failures | ⏳ P4 |

The current app **only reads what you upload**. It does not connect to Jenkins, Gerrit or email, and it never triggers builds or votes.

---

## 2. Prerequisites

- Git, and either **Docker + Docker Compose** (easiest) or **Python 3.12**.
- A **Groq API key** (https://console.groq.com).
- For testing with real builds: **corporate VPN** access to Jenkins, and your own Jenkins login or personal API token.

---

## 3. Setup (once)

```bash
git clone https://github.com/vasan-rj/jenkins-ai-analyzer.git
cd jenkins-ai-analyzer
git checkout main && git pull
```

Create **`backend/.env`** (it is git-ignored; never commit it):

```bash
GROQ_API_KEY=<your-groq-key>
```

That is the only variable the current app needs. Other variables in the team's `.env` (`JENKINS_*`, `GERRIT_KEY`, `EMAIL_ID`) are for upcoming phases and are ignored for now. Never paste real values into chat, tickets or commits.

---

## 4. Start the application

Pick **one** option.

### Option A — Docker Compose (recommended)

```bash
docker compose up --build
```

- Backend API: http://127.0.0.1:8000
- Upload page: http://127.0.0.1:5500

Stop with `Ctrl+C`, then `docker compose down`.

### Option B — Local Python

Terminal 1, the backend. **Start it from inside `backend/`** so it finds `backend/.env`:

```bash
cd backend
python3.12 -m venv .venv          # if this fails with "ensurepip": uv venv --python 3.12 .venv
. .venv/bin/activate
pip install -r requirements.txt   # with uv: uv pip install -r requirements.txt
uvicorn main:app --port 8000
```

Terminal 2, the frontend, from the repo root:

```bash
python3 -m http.server 5500 --directory frontend
```

Open http://127.0.0.1:5500. The page must be served on port **5500**; the API only accepts browser requests from that origin.

---

## 5. Test in this order

### Step 1 — Health check

```bash
curl http://127.0.0.1:8000/
# {"message":"Jenkins AI Analyzer API is running"}
```

### Step 2 — Automated tests (no API key or VPN needed)

```bash
cd backend && . .venv/bin/activate
pytest tests -q
```

Expected: all tests pass. The tests never call the real LLM. (With Docker only: `docker compose run --rm backend pytest tests -q`.)

### Step 3 — Sample logs (checks the LLM end to end)

Upload each file from `backend/tests/fixtures/logs/` on the upload page, or use curl:

```bash
curl -s -F "file=@backend/tests/fixtures/logs/infra_agent_offline.log" http://127.0.0.1:8000/analyze-log
```

| Sample file | Expected `error_category` | Expected `validated` |
|---|---|---|
| `infra_agent_offline.log` | `infra` | `true` |
| `devops_missing_credentials.log` | `devops` | `true` |
| `compilation_java_symbol.log` | `compilation` | `true` |
| `test_failure.log` | `needs_triage` | `true` |

Each analysis makes about 3 LLM calls and usually takes 5–30 s. Groq rate limits can add delays; the client retries automatically.

### Step 4 — Real Jenkins failures (VPN required)

1. Connect to the corporate VPN and open Jenkins in your browser.
2. Pick a **failed** build: open it → **Console Output** → **View as plain text**. That page is `<build URL>consoleText`.
3. Save it as a file (e.g. `myjob-1234.log`). Alternatively, with **your own** Jenkins user and personal API token:

   ```bash
   curl -fsS -u "<your-user>:<your-api-token>" "<build URL>consoleText" -o myjob-1234.log
   ```

   Use your personal account for manual downloads, not the team service account.
4. Upload the file on the upload page (or `curl -F "file=@myjob-1234.log" ...` as in Step 3).
5. Record the result in the table below and share it with the team. These results become the evaluation set for later phases.

Aim for a spread: a few agent/network failures, a few credential or pipeline-script failures, a few compiler errors, and a few test failures.

| Job | Build # | What actually failed (your judgment) | `error_category` | `validated` | `confidence` | Correct? | Notes |
|---|---|---|---|---|---|---|---|
| | | | | | | | |

Before upload, the app redacts tokens, passwords, credentialed URLs, API keys and private keys. The redacted log excerpt is then sent to Groq's hosted API. Only upload logs your team has agreed can be processed this way.

---

## 6. Reading the result

| Field | Meaning |
|---|---|
| `error_category` | Final category. Only a validated analysis keeps the LLM's category; everything else is `needs_triage`. |
| `llm_category` | What the LLM proposed, even when it was not validated. |
| `validated` | `true` when the evidence and fix passed both the deterministic and the LLM validator checks. |
| `validation_reason` | Why it was or wasn't validated, e.g. `No error context found in log.` |
| `evidence` | Exact log lines (`[Line N] ...`) that support the root cause. |
| `confidence` | `high` / `medium` / `low` from the LLM. |
| `attempts` | Root-cause attempts used (max 3). |

| Category | Meaning |
|---|---|
| `infra` | Agent/node offline, executor or channel failure, network/DNS, disk, OOM, external service (e.g. Artifactory) unavailable |
| `devops` | Jenkinsfile or pipeline script, credentials, permissions, missing tool, environment configuration |
| `compilation` | Application source fails to compile or link |
| `needs_triage` | Failing tests, unclear causes, insufficient evidence, or anything not validated |

---

## 7. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `503 LLM is not configured (GROQ_API_KEY missing)` | `backend/.env` is missing or has no key, or the backend was started outside `backend/` (Option B). |
| `503 LLM provider request failed` | Groq is unreachable or rejected the key. Check the key and your internet access. |
| `413 Log file exceeds ... bytes` | The log is over 20 MB. Trim it to the failing part, or raise `MAX_UPLOAD_BYTES`. |
| Upload page shows `Failed to fetch` | Backend not running on :8000, or the page isn't served on :5500. |
| `needs_triage` with `No error context found in log.` | The log has no error/failure lines; check you downloaded the failed build's console. |
| Slow responses | Groq rate limiting; the client retries automatically. |
| Jenkins hostname doesn't resolve | VPN not connected. |

---

## 8. Configuration reference

Set in `backend/.env` or as environment variables.

| Variable | Default | Purpose |
|---|---|---|
| `GROQ_API_KEY` | — | Required for analysis |
| `LLM_MODEL` | `openai/gpt-oss-120b` | Groq model |
| `LLM_PARSE_RETRIES` | `2` | Extra attempts when LLM output fails schema validation |
| `MAX_VALIDATION_ATTEMPTS` | `3` | Root-cause / validator loop limit |
| `PARSER_MAX_CHARACTERS` | `10000` | Max extracted log context sent to the LLM |
| `MAX_UPLOAD_BYTES` | `20000000` | Larger uploads return 413 |
| `CORS_ORIGINS` | `["http://127.0.0.1:5500","http://localhost:5500"]` | JSON list |
| `LOG_LEVEL` | `INFO` | JSON logs to stdout |

---

## 9. How it works

```text
log → redact secrets → parser (extract [Line N] error context)
        ├─ no error context → needs_triage (no LLM call)
        └─ error detector → root cause → validator ─┬─ valid → result
                               ^                    └─ invalid → retry (max 3) → needs_triage
```

- **Parser:** finds error lines and their surrounding context, prioritizing specific root-cause patterns, and keeps the latest context within `PARSER_MAX_CHARACTERS`.
- **Evidence:** must be copied exactly from the log. Invented evidence fails validation.
- **API:** `POST /analyze-log` (multipart `file`). `/upload-log` is a legacy diagnostic endpoint.

CI (`.github/workflows/ci.yml`) runs `pytest tests` and builds the backend Docker image.
