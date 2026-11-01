# Jenkins AI Analyzer — User & Tester Guide

Version 1.0 · 2026-11-01 · Applies to the application as built through P3 (see `docs/architecture/jenkins-auto-remediation-architecture.md`, section 1.1)

**Audience**
- **Users:** DevOps and build engineers who review failed builds. Part 2.
- **Testers:** anyone verifying the application end to end. Part 3.
- **Administrators:** whoever installs and configures it. Part 1.

**What the application does, in one paragraph.** It watches a Jenkins controller for failed builds, reads each failure's console log, and decides whether it is an infrastructure problem (`infra`), a pipeline/automation/credentials problem (`devops`), a source-code compile problem (`compilation`) or unclear (`needs_triage`). It shows its reasoning and verbatim evidence, and can act: **re-run** the build for infra failures and **email the job owner** for devops failures. Every action starts in **shadow mode** (recorded, never executed) and is switched to **automatic** one category at a time, once reviewers have confirmed it is accurate.

---

## Part 1 — Setup (administrators)

### 1.1 Prerequisites
- Python 3.12 (3.13 also works), network access to the Jenkins controller (VPN if the controller is internal) and to the LLM provider.
- A Jenkins user with an API token. **Read** access is enough for shadow mode; **Build** permission on the allowlisted jobs is needed only for automatic re-runs.
- A Groq API key.

### 1.2 Install and run (Windows PowerShell; run from `backend`)

```powershell
cd backend
python -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt

# Terminal 1: API + review UI
.\venv\Scripts\python.exe -m uvicorn main:app --port 8000

# Terminal 2: the worker (classifies queued failures, runs automatic actions)
.\venv\Scripts\python.exe -m worker

# Create review accounts (prompts for a password)
.\venv\Scripts\python.exe -m manage create-user alice --role operator
```

Open **http://127.0.0.1:8000/ui/** and sign in (the browser shows a username/password prompt).

**Both processes must be running.** The API only *queues* a failure; the worker does the work. If a classified build stays `queued`, the worker is not running.

Docker alternative: `docker compose up --build` starts the API, the worker and the legacy upload page.

### 1.3 Configuration (`backend/.env`)

Never commit this file and never paste secrets into chat or tickets. The application reads it at start-up: **restart the API and the worker after changing it.**

| Variable | Needed for | Meaning |
|---|---|---|
| `GROQ_API_KEY` | Classification | LLM provider key |
| `LLM_MODEL` | Optional | Default `openai/gpt-oss-120b` |
| `JENKINS_URL` | Everything | Base URL of the controller |
| `JENKINS_USERNAME`, `JENKINS_API_TOKEN` | Everything | Service account (use its API token, not a password) |
| `JENKINS_WEBHOOK_TOKEN` | Webhook intake | Shared secret Jenkins sends in `X-Webhook-Token` |
| `JENKINS_JOBS` | Optional | If set, only these jobs can be browsed and classified (an allowlist). Unset = every job |
| `EMAIL_ID` | Email | Test recipient for devops notifications |
| `MODE_INFRA` | Re-run | `off` / `shadow` (default) / `auto` |
| `RERUN_JOB_ALLOWLIST` | Re-run | JSON list of job patterns eligible for a re-run, e.g. `["sandbox/*"]`. Empty = none |
| `RERUN_BUDGET` | Re-run | Automatic re-runs per failure chain (default 1) |
| `MODE_DEVOPS` | Email | `off` / `shadow` (default) / `auto` |
| `FROM_EMAIL_ID` | Email (`auto`) | Sender mailbox |
| `SMTP_PASSWORD` | Email (`auto`) | Sender's SMTP password. **For Gmail this must be an App Password** (Google Account → Security → 2-Step Verification → App passwords). Aliases: `FROM_EMAIL_PASSWORD`, `GMAIL_APP_PASSWORD` |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_STARTTLS`, `SMTP_USE_SSL`, `SMTP_USERNAME` | Email | Default `smtp.gmail.com`, `587`, STARTTLS. Login name defaults to `FROM_EMAIL_ID` |
| `OWNER_MAP` | Email | JSON: job pattern → recipients, first match wins, e.g. `{"team-a/*": ["a@example.com"]}` |
| `OWNER_PARAMETER_NAME` | Email | Optional job parameter whose default holds the owner address |
| `ALLOWED_EMAIL_DOMAINS` | Email | Domains accepted for `OWNER_MAP` / parameter recipients (not applied to `EMAIL_ID`) |
| `NOTIFY_RATE_WINDOW_HOURS` | Email | One mail per job + failure signature per window (default 6) |
| `REVIEW_UI_URL` | Email | Adds a review link to the message |
| `KILL_SWITCH` | Safety | `true` stops every automatic action; cannot be cleared from the UI |
| `DATABASE_URL` | Optional | Default SQLite file `backend/data/remediation.db` |

`MODE_DEVOPS=auto` is refused at start-up unless `FROM_EMAIL_ID` and `SMTP_PASSWORD` are set, so a typo cannot silently disable mailing.

**Check the mail settings** without touching anything else:

```powershell
.\venv\Scripts\python.exe -m mailer test
```

It sends one test message from `FROM_EMAIL_ID` to `EMAIL_ID` and prints `Sent. Message-ID …` or the reason it failed (for example `535` = wrong password / needs an App Password). Check the spam folder the first time.

### 1.4 Roles

| Role | Can do |
|---|---|
| `viewer` | Open the UI, browse failing jobs, read events, metrics and email previews |
| `labeler` | Everything a viewer can, plus label classifications and rate email recipients |
| `operator` | Everything a labeler can, plus **Classify** builds and use the **kill switch** |

---

## Part 2 — User guide

### 2.1 The three modes: what the system is allowed to do

The banner at the top of the page always tells you which mode is live.

| Mode | Infra failures | DevOps failures |
|---|---|---|
| `off` | Nothing recorded | Nothing recorded |
| `shadow` (default) | "Would re-run" is recorded; **nothing is re-run** | An email preview is recorded; **nothing is sent** |
| `auto` | The failed build is re-run in Jenkins (allowlisted jobs only) | The preview is emailed to the owner |

Compilation failures never trigger an action today (that part is designed but not built); they are classified and shown.

### 2.2 The page at a glance

- **Header:** the mode banner; who you are and your role; mode chips (`infra re-run: shadow`, `devops email: shadow`); the **kill switch** button (operators, only when something is in `auto`).
- **Accuracy bar:** how reviewers rated past classifications per category, and a summary of email previews (would send / suppressed / blocked, recipients right / wrong, and, in `auto`, sent / failed / uncertain).
- **Two tabs:** **Live failing jobs** and **Classified events**. Clicking a row opens the **detail drawer** on the right (Esc or **Close** hides it).

### 2.3 Live failing jobs

Shows jobs on the controller whose most recent completed build failed, straight from Jenkins (one request, a second or two).

| Control | What it does |
|---|---|
| Search job name | Server-side search, applied before paging |
| Failed within | 7 / 30 (default) / 90 days / 1 year / any time. Old failures are usually not worth acting on |
| Sort | Newest failure (default) or job name |
| Rows | Page size 25 / 50 / 100; use **Previous / Next** |
| include disabled jobs | Disabled jobs are hidden by default |
| Refresh from Jenkins | Re-reads the list |

Columns: tick box, job, build (link to Jenkins), when it failed (hover for the UTC time), **State**, action.

**State** shows how far each build has got:

| Badge | Meaning |
|---|---|
| not classified | Nothing has been done for this build yet |
| queued | Waiting for the worker |
| processing | The worker is classifying it |
| done · infra / devops / compilation / needs_triage | Classified; the word is the final category |
| failed | Processing failed (Jenkins unreachable, LLM provider down, …); the drawer shows the error. It retries automatically up to 3 times |
| rebuild #N waiting in Jenkins queue / running / SUCCESS / FAILURE / cancelled / status unknown | The result of an **automatic re-run** of this build (see 2.8) |

**To classify (operators):**
1. Click **Classify** on a row, **or** tick several rows (the box in the header selects the page) and click **Classify selected** (up to 50 at once).
2. The row turns `queued`, then `processing`, then `done · <category>`. This normally takes under a minute; longer if the model has to retry or the provider is rate limiting. You do not need to refresh.
3. Click **Details** on the row.

If Jenkins cannot be reached (for example the VPN is down), the table shows **"Could not load failing jobs from Jenkins …"** instead of an empty list. Reconnect and press **Refresh from Jenkins**.

Classifying the same build twice does nothing new: the existing event is returned. A build that has not failed, or that does not exist, is refused with a message.

### 2.4 Classified events

Everything that has been queued, with a filter. **The default filter is "Needs triage (unlabeled)", so it hides events you have already labeled and anything classified as infra or devops. Choose "All" to see everything.**

Columns: job, build, received time, final category, the LLM's own category, validated yes/no, reason codes, your label, and the **Rebuild** result.

### 2.5 The detail drawer: how to read a classification

| Section | What it tells you |
|---|---|
| Status / Attempts / Error | Where the event is in the queue; the error if processing failed |
| **Final category** | What the system decided (the only one actions are based on) |
| LLM category, Validated, Confidence, Model | What the model concluded and how sure it was |
| Failing stage | The pipeline stage that failed, when Jenkins reports it |
| Reason codes | Why a result was held back (table in 2.6) |
| Root cause | The model's explanation |
| Recommended fix (advisory) | A suggestion. It is never executed and is not verified |
| **Evidence** | Lines copied **verbatim** from the build log. Anything the model could not quote exactly is rejected, so evidence is trustworthy even when the explanation is not |
| Rule matches | Deterministic patterns found in the log (rule id → category, line) |
| Redacted log excerpt | The part of the log used. Secrets are masked; raw logs are never stored |
| **Proposed (shadow — not executed)** | What the system would do. The heading changes to **Executed** once an action really ran |
| Execution | For infra in `auto`: the re-run state, the new build and its result (2.8) |
| **Email preview** | For devops: recipients, decision, subject, body, delivery status (2.9) |
| Label this classification | Your feedback (2.7) |

### 2.6 Categories and reason codes

| Category | Typical causes |
|---|---|
| `infra` | Agent offline or disconnected, no free node for a label, disk full, out of memory, network/DNS, external service down |
| `devops` | Pipeline script errors, credentials or permission problems, missing tools, bad environment configuration, automation scripts failing |
| `compilation` | The project's own source fails to compile or link |
| `needs_triage` | Test failures, mixed or unclear causes, or not enough evidence |

A classification is only trusted (made the **final** category) when the validator accepted it, confidence is `high`, and no rule contradicts it. Otherwise the final category is `needs_triage` and the reason codes say why:

| Reason code | Meaning |
|---|---|
| `unvalidated` | The validator rejected the analysis or its evidence could not be matched to the log |
| `low_confidence` | Confidence was not `high` |
| `rule_ambiguous` | Deterministic rules pointed at more than one category |
| `rule_llm_conflict` | A rule and the model disagree |

`needs_triage` is the safe default: it never triggers an action. A flood of `needs_triage` is not a malfunction, it is the system declining to guess.

### 2.7 Reviewing and labeling (labelers and operators)

Labels are how the system earns trust to act automatically, so they matter more than anything else you do in the UI.

1. Open an event's drawer and read the root cause and the **evidence** (judge the log lines, not the prose).
2. Under **Label this classification** choose **Correct**, or **Incorrect** and pick the correct category, add an optional note, and **Save label**.
3. For devops emails marked **would send**, also use **Recipient is right / Wrong recipient** in the Email preview.

The accuracy chips update immediately. Promotion of a category to `auto` is proposed to require at least 50 labeled events, at least 95% rated correct, and zero wrong targets.

### 2.8 Infra failures: automatic re-run

Only when `MODE_INFRA=auto`, and only for jobs in `RERUN_JOB_ALLOWLIST`.

1. A build is classified `infra` (validated, `high` confidence, no rule conflict).
2. The worker checks, **in this order**, and skips with a reason if any fails: kill switch off, mode `auto`, job allowlisted, budget left for this failure chain, job not re-run too recently, **the failed build is still the job's latest build**, it is still failed, and it has **no secret (password) parameters**.
3. It re-runs the build with the **same parameters** and records the new build.
4. The drawer's **Execution** section and the **Rebuild** badges show: waiting in queue → running → **SUCCESS / FAILURE / UNSTABLE / ABORTED**, how long it took, and a link. The page updates by itself.

Execution states you may see: `triggered` (a build was queued), `skipped` (a guard said no; the reason is shown), `failed` (Jenkins refused the request; nothing was queued) and **`uncertain`**. `uncertain` means the request may or may not have been applied (for example the connection dropped). It is **never retried automatically**, because a retry could start two builds. Look in Jenkins, then decide manually.

If the re-run itself fails, the budget prevents another automatic re-run: a person has to look at it.

### 2.9 DevOps failures: owner email

1. A build is classified `devops`. The system works out **who** to email: `OWNER_MAP` first, then the job's owner parameter if configured, then `EMAIL_ID` (the test recipient).
2. It records an **Email preview**: recipients, the subject, the full body, and one of:
   - **would send:** valid recipients, within the rate limit.
   - **suppressed:** the same failure (same job and failure signature) was already mailed within the window.
   - **blocked:** no valid recipient, a rejected address, a forbidden domain, more than three recipients, or the kill switch is on.
3. In `shadow` that is all: the heading reads **"Email preview (shadow — not sent)"**.
4. In `auto` the worker sends a **would send** preview by SMTP. The heading becomes **"Email (sent)"**, with the send time and message id. Delivery states: `sent`, `failed` (the server refused or the app could not connect: nothing was delivered), **`uncertain`** (the connection broke after the message may have been accepted: never resent), and `skipped` (the kill switch or mode changed before sending).

The message contains the job, the build link, the failing stage, the root cause, up to five verbatim log lines (secrets masked) and the advisory fix. It never contains passwords or raw logs.

### 2.10 The kill switch

Operators see **Stop all executions** whenever any category is in `auto`. It stops automatic re-runs and email immediately and persists until someone presses **Resume executions** (which asks for confirmation). Anything already queued is skipped, not lost. If the switch was set in `.env` (`KILL_SWITCH=true`) the button is disabled: change it there and restart.

---

## Part 3 — Tester guide

### 3.1 Test levels

| Level | What | How | Calls real systems? |
|---|---|---|---|
| Automated | About 500 unit, integration and end-to-end tests | `.\venv\Scripts\python.exe -m pytest tests -q` from `backend` | **No.** A fake LLM, a fake Jenkins and a real local SMTP server |
| Live, read-only | Jenkins listing, console fetch, rules on real logs | UI Live tab; `python -m backfill` | Jenkins (GET only) |
| Live, classifying | Real LLM on real logs | UI **Classify** | Jenkins (GET) + LLM provider (sends **redacted** excerpts) |
| Live, acting | Real re-run; real email | Only after 3.3, in `auto` | Jenkins write; SMTP |
| Benchmark | Compare LLM configurations on a labeled set | `python -m eval.benchmark …` | LLM provider |

Run the automated suite first and after any change. It must be fully green. (Windows note: very long parametrized test ids can break pytest teardown because of the 32,767-character environment-variable limit; new tests use short `ids=`.)

### 3.2 What the automated tests prove (and do not)

They prove: every guard and state machine, the rules and reconcile logic, the API and role checks, redaction and size caps, header-injection rejection, that email is never sent in cleartext when TLS is requested, that an unknown send/re-run outcome is never retried, that only `mailer.py` imports an email library, and the full path failed build → classification → delivery into a real (local) SMTP inbox.

They do **not** prove: the real Gmail/relay login and delivery, the real Jenkins permission model for a re-run beyond the one manual validation, LLM quality on your logs, or browser rendering. Those are covered by the live steps below.

### 3.3 Safe test practices

- **Use a throwaway database** for destructive or experimental runs: set `DATABASE_URL=sqlite:///C:/path/to/test.db` for that process. Do not experiment in the shared database.
- **Never** put a production job in `RERUN_JOB_ALLOWLIST` for a test. Use one sandbox job whose re-run is harmless and cheap. A re-run is a real build on a real controller and cannot be undone (you can cancel it in Jenkins).
- Keep `MODE_INFRA=shadow` and `MODE_DEVOPS=shadow` for everything except the specific live tests below.
- For email, the only recipient is `EMAIL_ID` (your own mailbox) until `OWNER_MAP` is deliberately introduced.
- Do not paste secrets into chat or screenshots. The UI and logs never show them; if you ever see one in a preview, that is a **defect**: report it.
- Inspect data without changing it (read-only SQL):

```powershell
.\venv\Scripts\python.exe -c "import sqlite3; c=sqlite3.connect('file:data/remediation.db?mode=ro',uri=True); print(c.execute('select id,job,build_number,status,attempts from events order by id desc limit 5').fetchall())"
```

Useful tables: `events`, `analyses`, `proposed_actions`, `executions`, `rebuild_outcomes`, `notifications`, `email_deliveries`, `labels`.

### 3.4 Smoke test (15 minutes, shadow mode)

1. Start the API and worker; open `/ui/`; sign in as an operator. **Expected:** the banner says Shadow mode; chips read `shadow`.
2. Live tab → **Refresh from Jenkins**. **Expected:** a list with a count ("1–50 of N failing jobs").
3. Pick a failing job and click **Classify**. **Expected:** `queued` → `processing` → `done · <category>`, under a few minutes.
4. Click **Details**. **Expected:** root cause, verbatim evidence, reason codes (if held back), "Proposed (shadow — not executed)".
5. Label it. **Expected:** the accuracy chip updates.
6. Classified events tab, filter **All**. **Expected:** the event is listed.

### 3.5 Acceptance test cases

Mark each Pass / Fail with notes. IDs are stable so defects can reference them.

**Access and roles**

| ID | Steps | Expected |
|---|---|---|
| T-01 | Open `/ui/` without signing in | Browser asks for credentials; no data shown |
| T-02 | Sign in as `viewer`; look at the Live tab | No **Classify** buttons, select-all disabled |
| T-03 | As `viewer`, open a drawer | Label form hidden; text says the role is read-only |
| T-04 | As `labeler`, open a drawer | Can label; still no **Classify** |
| T-05 | As `operator` | **Classify**, batch and label all available |

**Live failing jobs**

| ID | Steps | Expected |
|---|---|---|
| T-10 | Change **Failed within** 7 → any time | Total grows or stays equal; never shrinks |
| T-11 | Type part of a job name | List narrows to matching jobs; total reflects the match, not the page |
| T-12 | Sort by job name; page through | Alphabetical; no duplicates between pages |
| T-13 | Toggle **include disabled jobs** | Total can grow |
| T-14 | Disconnect the VPN, press **Refresh from Jenkins** | A red message explaining Jenkins cannot be reached; **not** an empty "no jobs" table |

**Classify**

| ID | Steps | Expected |
|---|---|---|
| T-20 | Classify one build | queued → processing → done, no refresh needed |
| T-21 | Tick 3 builds, **Classify selected** | All three queued; a toast reports the count |
| T-22 | Classify the same build again | Same event; no duplicate row in Classified events |
| T-23 | Stop the worker, classify, wait | Stays `queued`; starting the worker processes it |
| T-24 | Classify a build that has since succeeded or does not exist (via the API) | Refused with 422 and a message |

**Classification quality** (use known logs; sample logs ship in `backend/tests/fixtures/logs`)

| ID | Log | Expected final category |
|---|---|---|
| T-30 | Credentials entry missing | `devops` |
| T-31 | Agent offline / channel closed | `infra` |
| T-32 | Compiler error in project source | `compilation` |
| T-33 | Failing unit tests | `needs_triage` |
| T-34 | Any log where the model and a rule disagree | `needs_triage` with `rule_llm_conflict` |
| T-35 | A log with a secret-looking value | The excerpt and evidence show it masked |

**Labeling**

| ID | Steps | Expected |
|---|---|---|
| T-40 | Label Correct | Saved; chip updates |
| T-41 | Label Incorrect without choosing a category | Refused with a message |
| T-42 | Re-label the same event | Replaces the previous label |

**Email, shadow mode**

| ID | Steps | Expected |
|---|---|---|
| T-50 | Classify a devops failure | Preview **would send** to `EMAIL_ID`, source `override`, heading "(shadow — not sent)" |
| T-51 | Read the body | Job, build link, stage, root cause, verbatim evidence, advisory fix; **no secrets** |
| T-52 | Classify the same failure again | **suppressed** (`rate_limited`) |
| T-53 | Unset `EMAIL_ID`, restart, classify | **blocked** (`target_unresolved`) |
| T-54 | Rate the recipient right / wrong | Saved; chip counts update; only **would send** previews can be rated |

**Email, automatic** (needs credentials; see 1.3)

| ID | Steps | Expected |
|---|---|---|
| T-60 | `python -m mailer test` | `Sent. Message-ID …`; the message arrives (check spam) |
| T-61 | `MODE_DEVOPS=auto`, restart both, classify a *new* devops failure | Heading **"Email (sent)"**, message id, send time; mail arrives from `FROM_EMAIL_ID` to `EMAIL_ID` |
| T-62 | The message in the inbox | Identical to the preview body |
| T-63 | Classify the same failure again | **suppressed**; no second mail |
| T-64 | Set a wrong `SMTP_PASSWORD`, restart, classify a new failure | Delivery **failed** with `535`; **no password in the error text**; no mail |
| T-65 | Fix the password, classify the same failure again | Mail is sent (a failed delivery does not use up the rate-limit window) |
| T-66 | Press **Stop all executions** with a queued delivery | Delivery **skipped** (`kill_switch`); no mail |

**Infra re-run** (sandbox job only; see 3.3)

| ID | Steps | Expected |
|---|---|---|
| T-70 | `MODE_INFRA=auto`, allowlist = sandbox job, classify its failed infra build | Execution **triggered**; a new build appears in Jenkins with the **same parameters**; drawer shows **Rebuild** waiting → running → result |
| T-71 | A job *not* in the allowlist | `skipped` (`job_not_allowed`); no build |
| T-72 | Start a newer build of the job first, then classify the older failed build | `skipped` (`stale_build`) |
| T-73 | A build with a password parameter | `skipped` (`secret_parameters`) |
| T-74 | Let the re-run fail, then classify its failure | `skipped` (`budget_exhausted`); no second re-run |
| T-75 | **Stop all executions**, classify an infra failure | `skipped` (`kill_switch`) |
| T-76 | While a re-run is running, watch the Live and Classified tabs | The **Rebuild** badge changes by itself and ends as SUCCESS / FAILURE / … |

**Resilience**

| ID | Steps | Expected |
|---|---|---|
| T-80 | Restart the worker during processing | The event is picked up again after the lease expires; no duplicates |
| T-81 | Make the LLM unavailable (bad key) and classify | Event retries then `failed` with an error; **no** category is invented |
| T-82 | After Jenkins/VPN recovers, `python -m manage requeue --status failed` | Failed events are queued again |
| T-83 | Hard-refresh after an upgrade (Ctrl+F5) | The new UI loads (files are revalidated, but an old tab can linger) |

**Security checks**

| ID | Steps | Expected |
|---|---|---|
| T-90 | POST the webhook without or with a wrong `X-Webhook-Token` | 401 |
| T-91 | POST a webhook whose `build_url` is on another host or another job | 422 |
| T-92 | Search all stored previews and excerpts for known test secrets | None present |
| T-93 | `grep` production code for outbound writes | Only `clients/jenkins.py` (`trigger_rebuild`) and `mailer.py` |

### 3.6 Exit criteria for sign-off

- The automated suite is green.
- T-01 … T-54 pass in shadow mode.
- T-60 … T-66 pass with a real mailbox (before enabling `MODE_DEVOPS=auto` anywhere shared).
- T-70 … T-76 pass on a sandbox job (before enabling `MODE_INFRA=auto` anywhere shared).
- No secrets found (T-35, T-92); no duplicate sends or re-runs observed.
- For promotion of a category to `auto`: at least 50 labeled events in it, at least 95% correct, zero wrong targets.

### 3.7 Known limitations and not yet validated live

- **Real SMTP relay:** the sender is tested against a local SMTP server only; the Gmail/relay login and delivery have not been run.
- **Automatic re-run in production:** one real re-run was validated manually in a throwaway database; there has been no production `auto` run.
- **Compilation remediation (Gerrit):** not built; compilation failures are classified only.
- **Classification quality:** measured on a small, provisionally labeled sample. On real logs the model sometimes calls an infra failure (for example "all nodes offline") `devops`; the safeguard routes such conflicts to `needs_triage` instead of acting, which is safe but means some re-runs do not happen.
- **Model comparison:** an attempt to compare cheaper models was inconclusive (rate limits). The default model is unchanged. The benchmark harness is available.
- **No alerting or dashboards** beyond the UI chips and metrics endpoints.
- **Data sent to the LLM provider:** redacted log excerpts. Approval for this by security/data governance is still an open decision.

### 3.8 Troubleshooting

| Symptom | Likely cause | What to do |
|---|---|---|
| Live tab shows "Could not load failing jobs from Jenkins" | VPN/network, wrong `JENKINS_URL` or token | Reconnect; test `JENKINS_URL` in a browser; check the service account |
| Classified events tab is empty | The default filter hides labeled and non-triage events | Set the filter to **All** |
| A classified build stays `queued` | Worker not running (or crashed) | Start `python -m worker`; check its terminal |
| `failed` with `LLMRateLimitError` | Provider quota | Wait and use `python -m manage requeue --status failed`; consider another model or tier |
| `failed` with `JenkinsError … ConnectError` | Jenkins unreachable from the worker machine | Fix connectivity; requeue |
| Always `needs_triage` | Low confidence or a rule/LLM conflict (see reason codes) | Read the reason codes and evidence; this is expected for unclear logs |
| Page looks old or buttons missing | Cached script after an upgrade | Hard refresh (Ctrl+F5); restart the API |
| API returns 404 for a new feature | The API process was started before the upgrade | Restart the API (it is not auto-reloading) |
| Email stays "not sent (shadow mode)" | `MODE_DEVOPS` is not `auto`, or the worker was not restarted | Set it, restart **both** processes |
| API/worker refuses to start with "MODE_DEVOPS=auto needs FROM_EMAIL_ID and SMTP_PASSWORD" | Credentials missing | Add them to `backend/.env` |
| `python -m mailer test` shows 535 | Wrong password, or Gmail needs an App Password | Create an App Password (2-Step Verification must be on) |
| Re-run `skipped: stale_build` | A newer build of that job exists | Expected; classify the newest failed build |
| Re-run `uncertain` | The request outcome was unknown | Check Jenkins for a new build; never retry blindly |
| No Rebuild badge on an infra event | Shadow mode, a guard skipped it, or no rebuild was queued | Open the drawer's Execution section for the reason |

---

## Appendix A — Glossary

| Term | Meaning |
|---|---|
| Event | One failed build known to the system (also the work-queue entry) |
| Shadow mode | Record what would be done; do nothing |
| Final category | The decision actions are based on (validated, `high` confidence, no rule conflict) |
| Evidence | Verbatim log lines supporting the root cause |
| Signature | A fingerprint of "the same failure again" (job + category + first evidence line, digits removed) |
| Lineage | A chain of re-runs: the original failed build and the builds the system started for it |
| Budget | How many automatic re-runs a lineage (and a job, within a time window) may have |
| Lease | A time-limited claim the worker holds on an event, so a crashed worker cannot block it |
| Uncertain | A write whose outcome is unknown; parked, never retried automatically |
| Kill switch | Stops all automatic actions immediately |

## Appendix B — State summary

| Thing | States |
|---|---|
| Event | `queued` → `processing` → `done` / `failed` |
| Execution (re-run) | `pending` → `claimed` → `skipped` / `triggering` → `triggered` / `failed` / `uncertain` |
| Rebuild result | `queued` → `running` → `finished` (SUCCESS / FAILURE / UNSTABLE / ABORTED …) / `cancelled` / `unknown` |
| Email decision | `would_send` / `suppressed` / `blocked` |
| Email delivery | `pending` → `claimed` → `skipped` / `sending` → `sent` / `failed` / `uncertain` |

## Appendix C — Where to look in the code

| Need | Location |
|---|---|
| Architecture, workflows, data model | `docs/architecture/jenkins-auto-remediation-architecture.md` |
| Design decisions and phases | `docs/superpowers/specs/2026-10-30-jenkins-auto-remediation-design.md` |
| Jenkins webhook snippet | `docs/integration/jenkins-failure-webhook.md` |
| Developer notes and invariants | `CLAUDE.md` |
| Sample logs for testing | `backend/tests/fixtures/logs/` |
