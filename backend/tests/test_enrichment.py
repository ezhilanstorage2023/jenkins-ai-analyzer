from pathlib import Path

from clients.jenkins import JenkinsClient, Stage
from enrichment import artifactory_links, enrich, failing_stage, gerrit_scope
from tests.fakes import FakeJenkins

FIX = Path(__file__).parent / "fixtures" / "logs"
BUILD = "https://ci.example.com/job/acme/job/build/7/"
BUILD_PATH = "/job/acme/job/build/7/"


def test_failing_stage_prefers_wfapi():
    stages = [Stage("Checkout", "SUCCESS"), Stage("Compile", "FAILED")]

    assert failing_stage(stages, "") == "Compile"


def test_failing_stage_falls_back_to_console_markers():
    log = (FIX / "compilation_java_symbol.log").read_text()

    assert failing_stage(None, log) == "Compile"


def test_failing_stage_skips_declarative_post_stage():
    log = (FIX / "devops_missing_credentials.log").read_text()

    assert failing_stage(None, log) == "Publish"


def test_failing_stage_unknown():
    assert failing_stage([], "plain freestyle log") is None


def test_artifactory_links_deduped():
    log = (
        "get https://artifactory.example.com/a.jar\n"
        "retry https://artifactory.example.com/a.jar\n"
        "also http://repo.example.com/x\n"
    )

    assert artifactory_links(log) == ["https://artifactory.example.com/a.jar"]


def test_artifactory_links_capped():
    log = "\n".join(f"https://artifactory.example.com/{i}.jar" for i in range(30))

    assert len(artifactory_links(log)) == 20


def test_gerrit_scope_from_trigger_params():
    params = {"GERRIT_CHANGE_NUMBER": "48213", "GERRIT_TOPIC": "t1", "BRANCH": "main"}

    assert gerrit_scope(params) == {"change_number": "48213", "topic": "t1"}


def test_enrich_redacts_console_and_params():
    console = (FIX / "compilation_java_symbol.log").read_text()
    console = "Authorization: Bearer abc.def.123\n" + console
    fake = FakeJenkins({
        BUILD_PATH + "api/json": (200, {"result": "FAILURE", "actions": [
            {"parameters": [
                {"name": "API_TOKEN", "value": "x1"},
                {"name": "GERRIT_CHANGE_NUMBER", "value": "48213"},
            ]},
        ]}),
        BUILD_PATH + "consoleText": (200, console),
    })
    client = JenkinsClient(
        "https://ci.example.com/", "u", "tok",
        timeout=5, max_log_bytes=5_000_000, transport=fake.transport,
    )

    data = enrich(client, BUILD)

    assert "abc.def.123" not in data.console.text
    assert data.build.parameters["API_TOKEN"] == "[REDACTED]"
    assert data.gerrit_scope["change_number"] == "48213"
    assert data.failing_stage == "Compile"
    assert data.console.text.count("\n") == console.count("\n")
