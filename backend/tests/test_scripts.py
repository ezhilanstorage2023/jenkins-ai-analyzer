"""The standalone scripts under scripts/: fetch (read-only, redacting) and classify."""

import csv
import importlib.util
import urllib.error
from pathlib import Path

import pytest

from tests.fakes import responses_for

REPO = Path(__file__).resolve().parents[2]
FIX = Path(__file__).parent / "fixtures" / "logs"
BASE = "https://ci.example.com/"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def fetch():
    return _load("fetch_failed_consoles")


@pytest.fixture
def classify():
    return _load("classify_saved_logs")


# ---- fetch_failed_consoles ----------------------------------------------

@pytest.mark.parametrize("url, ok", [
    (BASE + "job/a/1/", True),
    (BASE, True),
    ("https://CI.example.com/job/a/", True),
    ("https://ci.example.com.evil.net/job/a/", False),
    ("http://ci.example.com/job/a/", False),
    ("https://ci.example.com:8443/job/a/", False),
    (BASE + "job/../../etc/", False),
    ("https://other.example.com/job/a/", False),
])
def test_under_base(fetch, url, ok):
    assert fetch._under_base(url, BASE) is ok


def test_safe_name_strips_path_characters(fetch):
    assert "/" not in fetch._safe_name("folder/name with spaces")
    assert len(fetch._safe_name("x" * 500)) == 120


class _Response:
    def __init__(self, body=b"", status=200):
        self._body, self.status = body, status

    def read(self, size=-1):
        if size is None or size < 0:
            size = len(self._body)

        chunk, self._body = self._body[:size], self._body[size:]
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Opener:
    def __init__(self, response):
        self.response = response
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        return self.response


def _jenkins(fetch, response, max_bytes=5_000_000):
    jenkins = fetch.ReadOnlyJenkins(BASE, "me", "tok", max_bytes)
    jenkins._opener = _Opener(response)
    return jenkins


def test_fetch_client_refuses_foreign_url_without_a_request(fetch):
    jenkins = _jenkins(fetch, _Response())

    with pytest.raises(ValueError):
        jenkins.json("https://evil.example.net/api/json")

    assert jenkins._opener.requests == []


def test_fetch_client_sends_get_with_basic_auth(fetch):
    jenkins = _jenkins(fetch, _Response(b'{"number": 3}'))

    assert jenkins.json(BASE + "job/a/api/json") == {"number": 3}

    request = jenkins._opener.requests[0]
    assert request.get_method() == "GET"
    assert request.get_header("Authorization").startswith("Basic ")


def test_fetch_non_2xx_is_an_error(fetch):
    jenkins = _jenkins(fetch, _Response(b"", status=302))

    with pytest.raises(urllib.error.HTTPError):
        jenkins.json(BASE + "job/a/api/json")


def test_console_tail_is_bounded_and_drops_partial_first_line(fetch):
    body = b"first line is long\nsecond\nthird\n"
    jenkins = _jenkins(fetch, _Response(body), max_bytes=16)

    text, total, truncated = jenkins.console_tail(BASE + "job/a/1/consoleText")

    assert truncated is True
    assert total == len(body)
    # The 16-byte tail starts mid-line ("ng\n..."); that fragment is dropped.
    assert text == "second\nthird\n"


def test_fetch_main_redacts_before_writing_and_records_skips(
    fetch, tmp_path, monkeypatch
):
    jobs = tmp_path / "jobs.json"
    jobs.write_text(
        '{"jobs": ['
        '{"name": "good", "url": "%sjob/good/", "color": "red", '
        '"_class": "x.WorkflowJob"},'
        '{"name": "bad", "url": "%sjob/bad/", "color": "red", '
        '"_class": "x.WorkflowJob"},'
        '{"name": "fine", "url": "%sjob/fine/", "color": "blue", '
        '"_class": "x.WorkflowJob"}]}' % (BASE, BASE, BASE),
        encoding="utf-8",
    )
    secret_line = "git clone https://bot:hunter2@gerrit.example/repo"

    def fake_json(self, url):
        if "/bad/" in url:
            raise urllib.error.HTTPError(url, 404, "nope", None, None)

        return {"number": 7, "url": BASE + "job/good/7/"}

    def fake_tail(self, url):
        assert url == BASE + "job/good/7/consoleText"
        return f"ok\n{secret_line}\n" + "z" * 9000 + "\n", 9100, False

    monkeypatch.setattr(fetch.ReadOnlyJenkins, "json", fake_json)
    monkeypatch.setattr(fetch.ReadOnlyJenkins, "console_tail", fake_tail)
    monkeypatch.setattr(fetch, "PAUSE_SECONDS", 0)
    monkeypatch.setenv("JENKINS_API_TOKEN", "tok")
    out = tmp_path / "out"
    monkeypatch.setattr("sys.argv", [
        "fetch", "--jenkins-url", BASE, "--user", "me",
        "--jobs-json", str(jobs), "--out", str(out),
    ])

    fetch.main()

    saved = (out / "good__7.log").read_text(encoding="utf-8")
    assert "hunter2" not in saved
    assert max(len(line) for line in saved.splitlines()) <= 4096 + 64

    rows = list(csv.DictReader((out / "manifest.csv").open(encoding="utf-8")))
    by_job = {row["job"]: row for row in rows}
    assert set(by_job) == {"good", "bad"}  # only failing (red) jobs are fetched
    assert by_job["good"]["file"] == "good__7.log"
    assert by_job["bad"]["error"] == "HTTPError 404"


# ---- classify_saved_logs ------------------------------------------------

CASES = [  # sorted filename order: the script globs sorted
    ("compilation_java_symbol.log", "cannot find symbol", "compilation"),
    ("devops_missing_credentials.log", "Could not find credentials entry", "devops"),
    ("infra_agent_offline.log", "ChannelClosedException", "infra"),
    ("test_failure.log", "FAILED: com.acme.FooTest.testParse", "needs_triage"),
]


def test_classify_writes_results_csv(classify, fake_llm, tmp_path, monkeypatch):
    folder = tmp_path / "logs"
    folder.mkdir()
    responses = []

    for name, signature, category in CASES:
        text = (FIX / name).read_text(encoding="utf-8")
        (folder / name).write_text(text, encoding="utf-8")
        responses += responses_for(text, signature, category)

    fake = fake_llm(responses)
    monkeypatch.setattr("sys.argv", ["classify", str(folder)])

    classify.main()

    rows = list(csv.DictReader((folder / "results.csv").open(encoding="utf-8")))

    assert [row["file"] for row in rows] == [name for name, _, _ in CASES]
    assert [row["final_category"] for row in rows] == [c for _, _, c in CASES]
    assert all(row["validated"] == "True" for row in rows)
    assert len(fake.calls) == 12 and fake.responses == []  # 3 per log, no extras
    assert set(rows[0]) >= {
        "failing_stage", "rule_categories", "llm_category", "confidence",
        "reason_codes", "root_cause", "evidence",
    }


def test_classify_honours_out_path(classify, fake_llm, tmp_path, monkeypatch):
    folder = tmp_path / "logs"
    folder.mkdir()
    name, signature, category = CASES[0]
    text = (FIX / name).read_text(encoding="utf-8")
    (folder / name).write_text(text, encoding="utf-8")
    fake_llm(responses_for(text, signature, category))
    out = tmp_path / "custom.csv"
    monkeypatch.setattr("sys.argv", ["classify", str(folder), "--out", str(out)])

    classify.main()

    assert out.exists() and not (folder / "results.csv").exists()
