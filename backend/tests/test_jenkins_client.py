import pytest

from clients.jenkins import JenkinsClient, JenkinsError, jenkins_client_from_settings
from tests.fakes import FakeJenkins

BASE = "https://ci.example.com/"
BUILD = "https://ci.example.com/job/acme/job/build/7/"
BUILD_PATH = "/job/acme/job/build/7/"


def make(routes, max_log_bytes=64):
    fake = FakeJenkins(routes)
    client = JenkinsClient(
        BASE, "u", "tok",
        timeout=5,
        max_log_bytes=max_log_bytes,
        transport=fake.transport,
    )
    return client, fake


def test_build_info_parses_parameters_and_causes():
    client, _ = make({BUILD_PATH + "api/json": (200, {
        "result": "FAILURE",
        "actions": [
            {"parameters": [
                {"name": "GERRIT_CHANGE_NUMBER", "value": "48213"},
                {"name": "PW"},
                {"name": "RETRIES", "value": 3},
            ]},
            {"causes": [{"shortDescription": "Triggered by Gerrit"}]},
            {},
        ],
    })})

    info = client.get_build_info(BUILD)

    assert info.result == "FAILURE"
    assert info.parameters == {"GERRIT_CHANGE_NUMBER": "48213", "RETRIES": "3"}
    assert info.causes == ["Triggered by Gerrit"]


def test_console_tail_is_bounded():
    text = "\n".join(f"line {i}" for i in range(100))
    client, _ = make({BUILD_PATH + "consoleText": (200, text)})

    log = client.get_console(BUILD)

    assert log.truncated is True
    assert log.total_bytes == len(text.encode())
    assert len(log.text.encode()) <= 64
    assert log.text.endswith("line 99")
    assert log.text.startswith("line ")


def test_console_small_log_not_truncated():
    client, _ = make({BUILD_PATH + "consoleText": (200, "a\nb")})

    log = client.get_console(BUILD)

    assert (log.text, log.truncated, log.total_bytes) == ("a\nb", False, 3)


def test_stages_parsed():
    client, _ = make({BUILD_PATH + "wfapi/describe": (200, {
        "stages": [
            {"name": "Checkout", "status": "SUCCESS"},
            {"name": "Compile", "status": "FAILED"},
        ],
    })})

    stages = client.get_stages(BUILD)

    assert [(s.name, s.status) for s in stages] == [
        ("Checkout", "SUCCESS"), ("Compile", "FAILED")
    ]


def test_stages_absent_returns_none():
    client, _ = make({})

    assert client.get_stages(BUILD) is None


def test_client_refuses_foreign_url():
    client, fake = make({})

    with pytest.raises(JenkinsError):
        client.get_console("http://169.254.169.254/latest/")

    assert fake.seen == []


def test_server_error_raises_jenkins_error():
    client, _ = make({BUILD_PATH + "consoleText": (503, "busy")})

    with pytest.raises(JenkinsError) as exc:
        client.get_console(BUILD)

    assert "tok" not in str(exc.value)


def test_list_builds_uses_job_path():
    client, fake = make({"/job/acme/job/build/api/json": (200, {"builds": [
        {"number": 7, "url": BUILD, "result": "FAILURE"},
        {"number": 6, "url": BASE + "job/acme/job/build/6/", "result": "SUCCESS"},
    ]})})

    refs = client.list_builds("acme/build", 2)

    assert fake.seen[-1].url.path == "/job/acme/job/build/api/json"
    assert "{0,2}" in fake.seen[-1].url.params["tree"]
    assert [(r.number, r.result) for r in refs] == [(7, "FAILURE"), (6, "SUCCESS")]


def test_basic_auth_header_sent():
    client, fake = make({})

    client.get_stages(BUILD)

    assert fake.seen[-1].headers["authorization"].startswith("Basic ")


def test_only_get_requests_are_made():
    client, fake = make({BUILD_PATH + "consoleText": (200, "x")})

    client.get_console(BUILD)
    client.get_stages(BUILD)

    assert {r.method for r in fake.seen} == {"GET"}


def test_from_settings_requires_configuration():
    with pytest.raises(JenkinsError):
        jenkins_client_from_settings()
