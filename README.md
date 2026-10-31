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
| Jenkins webhook, background worker, review UI, labeling (**shadow mode**) | ✅ P1 — see §6 |
| Automatic re-run of infra failures | ⏳ P2 |
| Owner email for DevOps failures | ⏳ P3 |
| Gerrit culprit exclusion + re-trigger for compilation failures | ⏳ P4 |

The app **never triggers builds, votes in Gerrit, or sends email**. The upload page only reads what you upload. The P1 worker reads from Jenkins with GET requests only and records the action it *would* take (shadow mode).

---

## 2. Prerequisites

Commands are shown for **Linux/macOS (bash)** and **Windows (PowerShell)**. On Windows:
- Use **PowerShell**, not `cmd.exe`.
- Type `curl.exe`, not `curl`. In Windows PowerShell 5.1, `curl` is an alias for `Invoke-WebRequest`, which takes different arguments.
- Install Python 3.12 from python.org, which gives you the `py` launcher, or install `uv`.

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

Windows (PowerShell):

```powershell
git clone https://github.com/vasan-rj/jenkins-ai-analyzer.git
cd jenkins-ai-analyzer
git checkout main; git pull
```

Create **`backend/.env`** (it is git-ignored; never commit it). On Windows you can open it with `notepad backend\.env`. Save it as plain text, with no `.txt` extension added:

```bash
GROQ_API_KEY=<your-groq-key>
```

That is all the **upload page** needs. The **P1 shadow pipeline** (§6) also needs the following; `.env` is always read from `backend/`, whatever directory you start from:

```bash
JENKINS_URL=https://<your-jenkins>/
JENKINS_USERNAME=<service account>
JENKINS_API_TOKEN=<API token — not the password>
JENKINS_WEBHOOK_TOKEN=<random secret shared with Jenkins>
EMAIL_ID=<test recipient shown on proposed DevOps notifications>
```

`GERRIT_KEY` is for a later phase and is ignored for now. Never paste real values into chat, tickets or commits.

---

## 4. Start the application

Pick **one** option.

### Option A — Docker Compose (recommended)

```bash
docker compose up --build
```

The command is the same on Windows (PowerShell, with Docker Desktop running).

- Backend API: http://127.0.0.1:8000
- Upload page: http://127.0.0.1:5500

Stop with `Ctrl+C`, then `docker compose down`.

### Option B — Local Python

Terminal 1, the backend. Run it from inside `backend/`, where the Python modules live. `backend/.env` is found automatically:

```bash
cd backend
python3.12 -m venv .venv          # if this fails with "ensurepip": uv venv --python 3.12 .venv
. .venv/bin/activate
pip install -r requirements.txt   # with uv: uv pip install -r requirements.txt
uvicorn main:app --port 8000
```

Windows (PowerShell):

```powershell
cd backend
py -3.12 -m venv .venv            # or: uv venv --python 3.12 .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt   # with uv: uv pip install -r requirements.txt
uvicorn main:app --port 8000
```

If activation fails with "running scripts is disabled on this system", allow scripts for the current window only, then activate again:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

Terminal 2, the frontend, from the repo root:

```bash
python3 -m http.server 5500 --directory frontend
```

Windows (PowerShell):

```powershell
py -3.12 -m http.server 5500 --directory frontend
```

Open http://127.0.0.1:5500. The page must be served on port **5500**; the API only accepts browser requests from that origin.

---

## 5. Test in this order

### Step 1 — Health check

```bash
curl http://127.0.0.1:8000/
# {"message":"Jenkins AI Analyzer API is running"}
```

Windows (PowerShell):

```powershell
curl.exe http://127.0.0.1:8000/
# or: Invoke-RestMethod http://127.0.0.1:8000/
```

### Step 2 — Automated tests (no API key or VPN needed)

```bash
cd backend && . .venv/bin/activate
pytest tests -q
```

Windows (PowerShell):

```powershell
cd backend
.\.venv\Scripts\Activate.ps1
pytest tests -q
```

Expected: all tests pass. The tests never call the real LLM. (With Docker only: `docker compose run --rm backend pytest tests -q`.)

### Step 3 — Sample logs (checks the LLM end to end)

Upload each file from `backend/tests/fixtures/logs/` on the upload page, or use curl:

```bash
curl -s -F "file=@backend/tests/fixtures/logs/infra_agent_offline.log" http://127.0.0.1:8000/analyze-log
```

Windows (PowerShell, from the repo root):

```powershell
curl.exe -s -F "file=@backend/tests/fixtures/logs/infra_agent_offline.log" http://127.0.0.1:8000/analyze-log
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

   Windows (PowerShell):

   ```powershell
   curl.exe -fsS -u "<your-user>:<your-api-token>" "<build URL>consoleText" -o myjob-1234.log
   ```

   Use your personal account for manual downloads, not the team service account.
4. Upload the file on the upload page (or `curl -F "file=@myjob-1234.log" ...` as in Step 3; `curl.exe` on Windows).
5. Record the result in the table below and share it with the team. These results become the evaluation set for later phases.

Aim for a spread: a few agent/network failures, a few credential or pipeline-script failures, a few compiler errors, and a few test failures.

| Job | Build # | What actually failed (your judgment) | `error_category` | `validated` | `confidence` | Correct? | Notes |
|---|---|---|---|---|---|---|---|
| | | | | | | | |

Before upload, the app redacts tokens, passwords, credentialed URLs, API keys and private keys. The redacted log excerpt is then sent to Groq's hosted API. Only upload logs your team has agreed can be processed this way.

---

## 6. P1 shadow pipeline: webhook → worker → review UI

**What it does:**
- Failed builds arrive by webhook (or by backfill).
- The worker reads each build from Jenkins (console tail, parameters, failing stage) **using GET requests only**.
- It classifies the failure with deterministic rules plus the LLM. Any disagreement, unvalidated result or low confidence becomes `needs_triage`.
- It records the remediation it *would* take.
- Reviewers label each classification at `/ui/`, which builds per-category accuracy.

> **Data handling:** the worker sends redacted console excerpts to Groq's hosted API. Only point it at a production Jenkins (backfill or webhook) once your team has approved sending these logs to Groq (open item: *LLM data handling* in the design doc).

**Start it** (VPN required for real Jenkins):

```bash
cd backend && . .venv/bin/activate
python -m manage create-user <you> --role labeler   # once; prompts for a password
uvicorn main:app --port 8000                         # terminal 1
python -m worker                                     # terminal 2
```

Windows (PowerShell). Run `cd backend` and activate the venv in **each** terminal:

```powershell
cd backend
.\.venv\Scripts\Activate.ps1
python -m manage create-user <you> --role labeler   # once; prompts for a password
uvicorn main:app --port 8000                         # terminal 1
python -m worker                                     # terminal 2
```

With Docker, `docker compose up --build` also starts the `worker` service. It exits cleanly if the Jenkins settings are missing. Create the user with `docker compose run --rm backend python -m manage create-user <you> --role labeler`. The database lives in `backend/data/`.

**Feed it failures**, either way:

- **Backfill (no Jenkins changes needed):** `python -m backfill --job "folder/name" --count 20`. The command is the same in PowerShell, with the venv active.
- **Live webhook:** add the `post { failure { ... } }` snippet from `docs/integration/jenkins-failure-webhook.md` to the job.

**Review:** open http://127.0.0.1:8000/ui/ and log in.
- The default view is "Needs triage (unlabeled)".
- Click an event to see the evidence, the redacted log excerpt, and the proposed action under **"Proposed (shadow — not executed)"**.
- Mark each one **Correct** or **Incorrect** (with the correct category). Accuracy per category is shown at the top.

**Roles:** `viewer` (read), `labeler` (read + label), `operator` (reserved for enabling auto mode in later phases).

**Proposed actions** (recorded only):

| Final category | Proposed action | Would be allowed when |
|---|---|---|
| `infra` | `rerun` (stores job, build URL and parameter *names*; values are re-fetched from Jenkins at execution time in P2) | job matches `RERUN_JOB_ALLOWLIST` |
| `devops` | `notify_owner` | `EMAIL_ID` is set |
| `compilation` | `exclude_culprit` (Gerrit change from `GERRIT_*` build params) | the build has a Gerrit change number |
| `needs_triage` | none | — |

---

## 7. Reading the result

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

## 8. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `503 LLM is not configured (GROQ_API_KEY missing)` | `backend/.env` is missing or has no key. On Windows, check that Notepad didn't save it as `.env.txt` (`dir backend` shows the real name). |
| `503 LLM provider request failed` | Groq is unreachable or rejected the key. Check the key and your internet access. |
| `413 Log file exceeds ... bytes` | The log is over 20 MB. Trim it to the failing part, or raise `MAX_UPLOAD_BYTES`. |
| Upload page shows `Failed to fetch` | Backend not running on :8000, or the page isn't served on :5500. |
| `needs_triage` with `No error context found in log.` | The log has no error/failure lines; check you downloaded the failed build's console. |
| Slow responses | Groq rate limiting; the client retries automatically. |
| Jenkins hostname doesn't resolve | VPN not connected. |
| Windows: `curl` errors such as "A parameter cannot be found" | Use `curl.exe`; plain `curl` is PowerShell's `Invoke-WebRequest`. |
| Windows: `Activate.ps1 cannot be loaded` | Run `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` in that window first. |
| Windows: `py -3.12` not found | Install Python 3.12 from python.org (it includes the `py` launcher), or use `uv venv --python 3.12 .venv`. |
| Webhook returns `503` | `JENKINS_WEBHOOK_TOKEN` not set in `backend/.env`. |
| Webhook returns `422` | `build_url` is not under `JENKINS_URL`. |
| Event stuck in `queued` / ends `failed` | Check `last_error` on the event in `/ui/`; Jenkins unreachable or Groq unavailable. Retries back off 60 s, 120 s, then the event is marked failed. After the outage: `python -m manage requeue --status failed`. |
| `/ui/` keeps asking for a password | Wrong credentials; reset with `python -m manage set-password <user>`. |

---

## 9. Configuration reference

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
| `DATABASE_URL` | `sqlite:///<backend>/data/remediation.db` | Event store (SQLAlchemy URL) |
| `JENKINS_URL` | — | Jenkins base URL; all outbound Jenkins calls must stay under it |
| `JENKINS_USERNAME` / `JENKINS_API_TOKEN` | — | Read-only REST access for the worker and backfill |
| `JENKINS_WEBHOOK_TOKEN` | — | Shared webhook secret (`X-Webhook-Token`) |
| `JENKINS_MAX_LOG_BYTES` | `5000000` | Console tail fetched per build |
| `EMAIL_ID` | — | Test recipient on proposed DevOps notifications |
| `RERUN_JOB_ALLOWLIST` | `[]` | JSON list of job patterns eligible for a re-run |
| `WORKER_MAX_ATTEMPTS` / `WORKER_BACKOFF_SECONDS` / `WORKER_LEASE_SECONDS` | `3` / `60` / `600` | Retry and crash-recovery tuning |

---

## 10. How it works

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
