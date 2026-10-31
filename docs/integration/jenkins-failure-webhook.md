# Jenkins → Remediation Service integration (P1, shadow mode)

This sends every failed build to the remediation service. The service then:
- reads the build from the Jenkins REST API (read-only)
- classifies the failure
- records the action it **would** take.

In P1 it never re-runs builds, votes in Gerrit or sends email.

## 1. Service-side settings (`backend/.env`)

| Variable | Purpose |
|---|---|
| `JENKINS_URL` | Base URL of the Jenkins controller. Webhooks with a `build_url` outside it are rejected. |
| `JENKINS_USERNAME` / `JENKINS_API_TOKEN` | Account and API token the worker uses for **read-only** REST calls. |
| `JENKINS_WEBHOOK_TOKEN` | Shared secret Jenkins sends in `X-Webhook-Token`. The webhook returns 503 until it is set. |
| `GROQ_API_KEY` | LLM classification. |
| `EMAIL_ID` | Test-period recipient shown on proposed DevOps notifications (nothing is sent in P1). |
| `RERUN_JOB_ALLOWLIST` | JSON list of `fnmatch` patterns for jobs whose infra re-run would be allowed, e.g. `["team/build-*"]`. Default: none. |

Generate a webhook token with `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`. Store it in `backend/.env` and as a Jenkins **Secret text** credential with ID `remediation-webhook-token`.

## 2. Jenkinsfile snippet

Add this to the declarative pipeline:
- Set `REMEDIATION_URL` once, in `environment {}` or as a global Jenkins property.
- The webhook call can never fail the build (`|| true`, 10 s timeout).
- The shell script is single-quoted, so the token is expanded by the shell rather than interpolated into the Groovy string.

```groovy
pipeline {
    agent any
    environment {
        REMEDIATION_URL = 'https://remediation.example.internal'
    }
    stages {
        // ... existing stages ...
    }
    post {
        failure {
            script {
                // Never let the webhook change the build's outcome.
                try {
                    def payload = groovy.json.JsonOutput.toJson([
                        job         : env.JOB_NAME,
                        build_number: env.BUILD_NUMBER as int,
                        build_url   : env.BUILD_URL,
                        result      : 'FAILURE',
                    ])
                    writeFile file: 'remediation-payload.json', text: payload
                    withCredentials([string(credentialsId: 'remediation-webhook-token', variable: 'HOOK_TOKEN')]) {
                        // The header goes in via curl's stdin config (printf is a shell
                        // builtin), so the token never appears in the agent's process list.
                        sh '''
                            printf 'header = "X-Webhook-Token: %s"\n' "$HOOK_TOKEN" | \
                            curl -fsS --max-time 10 --config - \
                              -H 'Content-Type: application/json' \
                              --data @remediation-payload.json \
                              "$REMEDIATION_URL/webhooks/jenkins" || true
                        '''
                    }
                } catch (err) {
                    echo "Remediation webhook skipped: ${err}"
                }
            }
        }
    }
}
```

Notes:
- `build_url` must be exactly `<JENKINS_URL>/job/<name>/<build number>/` for the given `job`. The service rejects any other URL, query string or controller id with 422, so `env.BUILD_URL` must use the same root URL as `JENKINS_URL` (it does for apac-ci today).
- `post` needs an agent for `writeFile` and `sh`. With `agent none`, wrap the block in `node { ... }`.
- On Windows agents, use `bat` with `curl.exe` and `%HOOK_TOKEN%`.
- If the Groovy sandbox blocks `JsonOutput`, use the shared-library variant below; libraries run trusted.

## 3. Shared-library variant (recommended for many jobs)

`vars/notifyRemediation.groovy` in your shared library contains the same `script { ... }` body as above, wrapped in `def call() { ... }`. Each Jenkinsfile then only needs:

```groovy
post {
    failure {
        notifyRemediation()
    }
}
```

## 4. Webhook contract

```
POST /webhooks/jenkins
X-Webhook-Token: <shared token>
Content-Type: application/json

{"job": "folder/name", "build_number": 1234, "build_url": "<JENKINS_URL>/job/folder/job/name/1234/", "result": "FAILURE", "controller_id": "default"}
```

| Response | Meaning |
|---|---|
| `202 {"event_id": N, "duplicate": false}` | Queued for the worker |
| `200 {"event_id": N, "duplicate": true}` | Already known (Jenkins retry or backfill) |
| `200 {"status": "ignored"}` | `result` was not `FAILURE` |
| `401` | Missing or wrong token |
| `422` | `build_url` not exactly this job's build under `JENKINS_URL`, unknown `controller_id`, or invalid payload |
| `503` | `JENKINS_WEBHOOK_TOKEN` not configured on the service |

## 5. Try it without touching Jenkins pipelines

> **Data handling:** this sends redacted console excerpts to Groq's hosted API. Run it against a production Jenkins only once your team has approved that (open item *LLM data handling* in the design doc).

Backfill recent failures of one job. This is read-only against Jenkins:

```bash
cd backend && . .venv/bin/activate
python -m backfill --job "folder/name" --count 20
python -m worker        # Ctrl+C once the queue is drained
```

Windows (PowerShell):

```powershell
cd backend
.\.venv\Scripts\Activate.ps1
python -m backfill --job "folder/name" --count 20
python -m worker        # Ctrl+C once the queue is drained
```

Then review the results at `/ui/`.
