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
        {"number": 7, "url": BUILD, "result": "FAILURE", "timestamp": 1700000000000},
        {"number": 6, "url": BASE + "job/acme/job/build/6/", "result": "SUCCESS"},
    ]})})

    refs = client.list_builds("acme/build", 2)

    assert fake.seen[-1].url.path == "/job/acme/job/build/api/json"
    assert "{0,2}" in fake.seen[-1].url.params["tree"]
    assert "timestamp" in fake.seen[-1].url.params["tree"]
    assert [(r.number, r.result) for r in refs] == [(7, "FAILURE"), (6, "SUCCESS")]
    assert refs[0].timestamp == 1700000000000
    assert refs[1].timestamp is None


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


def test_console_redirect_is_jenkins_error():
    client, _ = make({BUILD_PATH + "consoleText": (302, "moved")})

    with pytest.raises(JenkinsError):
        client.get_console(BUILD)


def test_json_redirect_is_jenkins_error():
    client, _ = make({BUILD_PATH + "api/json": (301, "moved")})

    with pytest.raises(JenkinsError):
        client.get_build_info(BUILD)


def test_html_instead_of_json_is_jenkins_error():
    client, _ = make({BUILD_PATH + "api/json": (200, "<html>login</html>")})

    with pytest.raises(JenkinsError):
        client.get_build_info(BUILD)


def test_build_url_for_encodes_folders():
    from clients.jenkins import build_url_for

    assert build_url_for(BASE, "team a/build", 3) == (
        "https://ci.example.com/job/team%20a/job/build/3/"
    )
    # Multibranch names arrive pre-encoded in JOB_NAME; Jenkins encodes again.
    assert build_url_for(BASE, "repo/feature%2Fx", 1) == (
        "https://ci.example.com/job/repo/job/feature%252Fx/1/"
    )


# ---- list_jobs ------------------------------------------------------------

def _jobs_client(body, status=200):
    fake = FakeJenkins({"/api/json": (status, body)})

    return JenkinsClient(
        "https://ci.example.com", "svc", "tok",
        timeout=5, max_log_bytes=1000, transport=fake.transport,
    ), fake


def _job(name, result="FAILURE", number=3, color="red", ts=1000):
    last = None if result is None else {"number": number, "result": result, "timestamp": ts}
    return {"fullName": name, "color": color, "lastCompletedBuild": last}


def test_list_jobs_flattens_folders_and_reads_last_build():
    body = {"jobs": [
        _job("top"),
        {"fullName": "team", "jobs": [
            _job("team/a"),
            {"fullName": "team/sub", "jobs": [_job("team/sub/b", result="SUCCESS", color="blue")]},
        ]},
    ]}
    client, _ = _jobs_client(body)

    jobs = {j.full_name: j for j in client.list_jobs()}

    assert set(jobs) == {"top", "team/a", "team/sub/b"}  # folders are not jobs
    assert (jobs["top"].last_number, jobs["top"].last_result, jobs["top"].last_timestamp) == (
        3, "FAILURE", 1000)
    assert jobs["team/sub/b"].last_result == "SUCCESS"


def test_list_jobs_tolerates_missing_fields():
    body = {"jobs": [
        {"fullName": "never-built", "color": "notbuilt"},
        {"fullName": "no-color", "lastCompletedBuild": {"number": 1, "result": "FAILURE"}},
        {"color": "red"},                     # no name: skipped
        {"fullName": "off", "color": "disabled", "lastCompletedBuild": None},
    ]}
    client, _ = _jobs_client(body)

    jobs = {j.full_name: j for j in client.list_jobs()}

    assert set(jobs) == {"never-built", "no-color", "off"}
    assert jobs["never-built"].last_result is None
    assert jobs["no-color"].last_timestamp is None and jobs["no-color"].disabled is False
    assert jobs["off"].disabled is True


def test_list_jobs_empty_and_null_jobs():
    assert _jobs_client({"jobs": []})[0].list_jobs() == []
    assert _jobs_client({})[0].list_jobs() == []


def test_list_jobs_sends_one_get_with_a_nested_tree():
    client, fake = _jobs_client({"jobs": []})

    client.list_jobs()

    assert len(fake.seen) == 1 and fake.seen[0].method == "GET"
    tree = fake.seen[0].url.params["tree"]
    assert "lastCompletedBuild[number,result,timestamp]" in tree
    assert tree.count("jobs[") == 4  # root + three folder levels


@pytest.mark.parametrize("status, body", [(503, b"down"), (302, b""), (200, b"<html>login</html>")])
def test_list_jobs_errors_are_jenkins_errors(status, body):
    client, _ = _jobs_client(body, status=status)

    with pytest.raises(JenkinsError):
        client.list_jobs()
