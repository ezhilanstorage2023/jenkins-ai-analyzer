# Jenkins AI Analyzer

An AI-powered Jenkins build log analyzer that automatically identifies build failures, extracts the relevant evidence, determines the likely root cause, and recommends an actionable fix.

The application uses **FastAPI**, **LangGraph**, and **Groq LLMs**, with a lightweight web frontend and Docker Compose for containerized execution.

---

## Features

- Upload Jenkins build logs through a web interface
- Extract relevant error and failure contexts from large logs
- Detect the primary build error
- Identify the affected component
- Perform AI-based root cause analysis
- Extract supporting evidence from the original log
- Deterministically validate evidence
- Validate the generated analysis
- Retry root-cause analysis when validation fails
- Provide recommended fixes
- Provide confidence level
- FastAPI REST API
- LangGraph multi-agent workflow
- Dockerized backend
- Docker Compose development environment
- Automated Python tests
- GitHub Actions CI

---

## Architecture

```text
                    Jenkins Build Log
                           |
                           v
                    Web Frontend
                           |
                           v
                    FastAPI Backend
                           |
                           v
                      LangGraph
                           |
             +-------------+-------------+
             |             |             |
             v             v             v
          Parser      Error Detector  Root Cause
             |             |             |
             +-------------+-------------+
                           |
                           v
                       Validator
                           |
                    +------+------+
                    |             |
                  Valid         Invalid
                    |             |
                    v             v
                   END       Retry Root Cause
```

The graph ends early, without calling the LLM, when the log contains no error context. Validation is retried up to `MAX_VALIDATION_ATTEMPTS` times.

---

## Error categories

| Category | Meaning |
|---|---|
| `infra` | Agent/node offline, executor or channel failure, network/DNS, disk, OOM, external service (e.g. Artifactory) unavailable |
| `devops` | Jenkinsfile or pipeline script, credentials, permissions, missing tool, environment configuration |
| `compilation` | Application source fails to compile or link |
| `needs_triage` | Failing tests, unclear causes, insufficient evidence |

Only a **validated** analysis keeps the LLM's category. Anything the validator rejects, LLM output that cannot be parsed, or a log with no error context is returned as `needs_triage` with `validated: false` and a `validation_reason`. The LLM's own label is still reported in `llm_category`.

Logs are redacted (tokens, passwords, credentialed URLs, API keys, private keys) before any LLM call. Line numbers are preserved.

---

## Configuration

Set in `backend/.env` (loaded relative to the working directory) or as environment variables.

| Variable | Default | Purpose |
|---|---|---|
| `GROQ_API_KEY` | — | Required for analysis; without it `/analyze-log` returns 503 |
| `LLM_MODEL` | `openai/gpt-oss-120b` | Groq model |
| `LLM_PARSE_RETRIES` | `2` | Extra attempts when LLM output fails schema validation |
| `MAX_VALIDATION_ATTEMPTS` | `3` | Root-cause / validator loop limit |
| `PARSER_MAX_CHARACTERS` | `10000` | Max extracted log context sent to the LLM |
| `MAX_UPLOAD_BYTES` | `20000000` | Larger uploads return 413 |
| `CORS_ORIGINS` | `["http://127.0.0.1:5500","http://localhost:5500"]` | JSON list |
| `LOG_LEVEL` | `INFO` | JSON logs to stdout |

---

## Development

```bash
cd backend
python3.12 -m venv .venv && . .venv/bin/activate   # or: uv venv --python 3.12 .venv
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
pytest tests -q        # tests never call the real LLM
```

Frontend: `python -m http.server 5500 --directory frontend` from the repo root. Full stack: `docker compose up --build`.
