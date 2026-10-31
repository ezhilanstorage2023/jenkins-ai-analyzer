"""Download console logs of recently failed Jenkins builds, redacted, for
offline classification checks.

READ-ONLY: only HTTP GET, never follows redirects, only URLs under
--jenkins-url. Secrets are redacted (backend/utils/redaction.py) and long
lines capped *before* anything is written to disk.

Standard library only; run from the repository root (any OS):

    python scripts/fetch_failed_consoles.py \
        --jenkins-url https://<your-jenkins>/ \
        --user <your-jenkins-user> \
        --jobs-json jenkins_json_response.txt \
        --limit 15 --out failed-logs

The API token is read from the JENKINS_API_TOKEN environment variable, or
prompted for (hidden). It is never accepted on the command line.
Use your personal Jenkins account, not the team service account.
"""

import argparse
import base64
import csv
import getpass
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "backend"))

from utils.log_processor import cap_line_length  # noqa: E402
from utils.redaction import redact  # noqa: E402

PAUSE_SECONDS = 0.5


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        # A redirect usually means an SSO/login page; never follow it with
        # credentials attached.
        return None


def _under_base(url: str, base: str) -> bool:
    target, root = urlsplit(url), urlsplit(base)
    return (
        (target.scheme, target.netloc.lower()) == (root.scheme, root.netloc.lower())
        and (target.path.rstrip("/") + "/").startswith(root.path.rstrip("/") + "/")
        and ".." not in target.path.split("/")
    )


class ReadOnlyJenkins:
    def __init__(self, base: str, user: str, token: str, max_bytes: int):
        self.base = base.rstrip("/") + "/"
        self.max_bytes = max_bytes
        self._auth = "Basic " + base64.b64encode(f"{user}:{token}".encode()).decode()
        self._opener = urllib.request.build_opener(_NoRedirect)

    def _open(self, url: str):
        if not _under_base(url, self.base):
            raise ValueError(f"refusing URL outside Jenkins base: {urlsplit(url).path}")

        request = urllib.request.Request(
            url, method="GET", headers={"Authorization": self._auth}
        )
        response = self._opener.open(request, timeout=60)

        if not 200 <= response.status < 300:
            raise urllib.error.HTTPError(url, response.status, "non-2xx", None, None)

        return response

    def json(self, url: str):
        with self._open(url) as response:
            return json.load(response)

    def console_tail(self, url: str) -> tuple[str, int, bool]:
        buffer, total = bytearray(), 0

        with self._open(url) as response:
            while chunk := response.read(64 * 1024):
                total += len(chunk)
                buffer.extend(chunk)

                if len(buffer) > self.max_bytes:
                    del buffer[: len(buffer) - self.max_bytes]

        truncated = total > self.max_bytes
        text = buffer.decode("utf-8", errors="replace")

        if truncated and "\n" in text:
            text = text.split("\n", 1)[1]

        return text, total, truncated


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)[:120]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--jenkins-url", required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--jobs-json", required=True,
                        help="Jenkins root /api/json response (with a 'jobs' list)")
    parser.add_argument("--limit", type=int, default=15)
    parser.add_argument("--out", default="failed-logs")
    parser.add_argument("--max-bytes", type=int, default=5_000_000)
    parser.add_argument("--seed", type=int, default=7,
                        help="sampling seed (same seed = same job sample)")
    args = parser.parse_args()

    token = os.environ.get("JENKINS_API_TOKEN") or getpass.getpass("Jenkins API token: ")
    jenkins = ReadOnlyJenkins(args.jenkins_url, args.user, token, args.max_bytes)

    jobs = json.load(open(args.jobs_json, encoding="utf-8"))["jobs"]
    failing = [
        job for job in jobs
        if (job.get("color") or "").startswith("red")
        and _under_base(job["url"], jenkins.base)
    ]
    random.Random(args.seed).shuffle(failing)
    sample = failing[: args.limit]

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    manifest = out / "manifest.csv"
    print(f"{len(failing)} failing jobs; fetching {len(sample)} into {out}/")

    with open(manifest, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["job", "build_number", "build_url", "job_class",
                         "file", "bytes", "truncated", "error"])

        for job in sample:
            name, cls = job["name"], job["_class"].rsplit(".", 1)[-1]

            try:
                build = jenkins.json(
                    job["url"] + "lastFailedBuild/api/json?tree=number,url,result"
                )
                build_url = jenkins.base + urlsplit(build["url"]).path.lstrip("/")
                text, total, truncated = jenkins.console_tail(build_url + "consoleText")
                safe = redact(cap_line_length(text))

                filename = f"{_safe_name(name)}__{build['number']}.log"
                (out / filename).write_text(safe, encoding="utf-8")
                writer.writerow([name, build["number"], build_url, cls,
                                 filename, total, truncated, ""])
                print(f"  ok   {name} #{build['number']} ({total:,} bytes)")
            except Exception as err:  # keep going; record the reason
                reason = type(err).__name__ + (
                    f" {err.code}" if isinstance(err, urllib.error.HTTPError) else ""
                )
                writer.writerow([name, "", "", cls, "", "", "", reason])
                print(f"  skip {name}: {reason}")

            time.sleep(PAUSE_SECONDS)

    print(f"Done. Share the folder '{out}' (logs are redacted; review before sharing).")


if __name__ == "__main__":
    main()
