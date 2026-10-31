from clients.jenkins import BuildInfo, ConsoleLog
from enrichment import EnrichmentData
from policy import propose_action
from settings import get_settings

BUILD = "https://ci.example.com/job/acme/job/build/7/"


def enr(params=None, gerrit=None):
    return EnrichmentData(
        build=BuildInfo(result="FAILURE", parameters=params or {}, causes=[]),
        console=ConsoleLog(text="", total_bytes=0, truncated=False),
        failing_stage=None,
        artifactory_links=[],
        gerrit_scope=gerrit or {},
    )


def test_infra_rerun_denied_by_default():
    proposal = propose_action("infra", "acme/build", BUILD, enr(params={"X": "1"}))

    assert (proposal.action_type, proposal.allowed, proposal.reason_codes) == (
        "rerun", False, ["job_not_allowed"]
    )
    # Parameters are re-fetched at execution time (P2); only names are kept.
    assert proposal.target == {
        "job": "acme/build", "build_url": BUILD, "parameter_names": ["X"]
    }


def test_infra_rerun_allowed_by_pattern(monkeypatch):
    monkeypatch.setenv("RERUN_JOB_ALLOWLIST", '["acme/*"]')
    get_settings.cache_clear()

    proposal = propose_action("infra", "acme/build", BUILD, enr())

    assert proposal.allowed is True
    assert proposal.reason_codes == []


def test_devops_notify_uses_owner_override(monkeypatch):
    monkeypatch.setenv("EMAIL_ID", "owner@example.com")
    get_settings.cache_clear()

    proposal = propose_action("devops", "acme/build", BUILD, enr())

    assert proposal.action_type == "notify_owner"
    assert proposal.target == {"recipient": "owner@example.com", "source": "override"}
    assert proposal.allowed is True


def test_devops_without_override_unresolved():
    proposal = propose_action("devops", "j", BUILD, enr())

    assert proposal.allowed is False
    assert proposal.reason_codes == ["target_unresolved"]


def test_compilation_targets_gerrit_change():
    proposal = propose_action(
        "compilation", "j", BUILD,
        enr(gerrit={"change_number": "48213", "patchset_number": "2"}),
    )

    assert proposal.action_type == "exclude_culprit"
    assert proposal.target == {
        "change_number": "48213", "patchset_number": "2", "topic": None
    }
    assert proposal.allowed is True


def test_compilation_without_gerrit_change_unresolved():
    proposal = propose_action("compilation", "j", BUILD, enr())

    assert proposal.reason_codes == ["target_unresolved"]


def test_needs_triage_has_no_action():
    assert propose_action("needs_triage", "j", BUILD, enr()) is None
