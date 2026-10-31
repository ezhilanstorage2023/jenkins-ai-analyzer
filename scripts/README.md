# Classification check with real Jenkins logs

Two steps. Step 1 runs on a machine with VPN access to Jenkins; step 2 runs wherever the Groq key is.

## Step 1: fetch failed-build consoles (teammate, on VPN)

What the script does:
- **Read-only:** only GET requests, never follows redirects, only URLs under `--jenkins-url`.
- **Samples failures:** picks `--limit` currently failing jobs from the Jenkins root API response (`jenkins_json_response.txt`).
- **Saves each latest failed console:** keeps the last 5 MB, caps each line at 4,096 characters and **redacts secrets before writing**.
- **Needs nothing extra:** Python 3.10+ standard library only, no venv. Run it from the repository root.

Use **your own** Jenkins account and personal API token (Jenkins → your name → Configure → API Token), not the team service account.

Linux/macOS:

```bash
export JENKINS_API_TOKEN='<your personal API token>'
python3 scripts/fetch_failed_consoles.py \
  --jenkins-url https://apac-ci.harman.com/ \
  --user <your-jenkins-user> \
  --jobs-json jenkins_json_response.txt \
  --limit 15 --out failed-logs
```

Windows (PowerShell):

```powershell
$env:JENKINS_API_TOKEN = '<your personal API token>'
py -3 scripts\fetch_failed_consoles.py `
  --jenkins-url https://apac-ci.harman.com/ `
  --user <your-jenkins-user> `
  --jobs-json jenkins_json_response.txt `
  --limit 15 --out failed-logs
Remove-Item Env:JENKINS_API_TOKEN
```

If `JENKINS_API_TOKEN` is not set, the script prompts for the token (hidden input). Output:
- `failed-logs/<job>__<build>.log`, one file per build
- `failed-logs/manifest.csv`, with job, build, URL, job type, size and any skip reason

`--seed` changes which failing jobs are sampled.

**Before sharing:** skim the logs. Redaction catches common secret formats, but these are internal build logs, so share them only through approved channels. **Do not commit `failed-logs/`.**

## Step 2: classify the saved logs

```bash
backend/.venv/bin/python scripts/classify_saved_logs.py failed-logs
```

Windows (PowerShell):

```powershell
backend\.venv\Scripts\python.exe scripts\classify_saved_logs.py failed-logs
```

This writes `failed-logs/results.csv` with these columns:
- the failing stage
- the rule categories
- the LLM category, validated flag and confidence
- the **final category** and reason codes
- the root cause and the first evidence lines

Then add a column with what actually failed (your judgment) to measure accuracy. Each log costs about 3 Groq calls, and the redacted excerpts are sent to Groq.
