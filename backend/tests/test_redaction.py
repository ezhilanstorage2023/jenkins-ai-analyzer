import pytest

from utils.redaction import redact


@pytest.mark.parametrize("raw, secret", [
    ("Authorization: Bearer abc.def.ghi", "abc.def.ghi"),
    ("git clone https://bot:hunter2@gerrit.example/repo", "hunter2"),
    ("export PASSWORD=s3cr3t", "s3cr3t"),
    ('{"token": "tok-123"}', "tok-123"),
    ("key gsk_" + "a" * 30, "gsk_" + "a" * 30),
    ("ghp_" + "b" * 36, "ghp_" + "b" * 36),
    ("AKIAABCDEFGHIJKLMNOP", "AKIAABCDEFGHIJKLMNOP"),
])
def test_redacts_secret(raw, secret):
    out = redact(raw)

    assert secret not in out
    assert "[REDACTED" in out


def test_private_key_block_preserves_line_count():
    raw = (
        "a\n-----BEGIN RSA PRIVATE KEY-----\nMIIE\nxyz\n"
        "-----END RSA PRIVATE KEY-----\nb"
    )
    out = redact(raw)

    assert "MIIE" not in out
    assert len(out.splitlines()) == len(raw.splitlines())
    assert out.splitlines()[-1] == "b"


def test_compiler_text_untouched():
    raw = (
        "src/app.ts(3,1): error TS1128: Unexpected token: '}'\n"
        "error: cannot find symbol"
    )

    assert redact(raw) == raw


@pytest.mark.parametrize("raw", [
    "+ ./deploy.sh --password=\n[ERROR] deploy failed: 401",
    "secret='x\nERROR: don't do that",
    'token="\na\nb\nc',
    "Authorization: Bearer\nERROR next line",
    '{"token": "\n"}',
])
def test_redaction_never_changes_line_count(raw):
    out = redact(raw)

    assert out.count("\n") == raw.count("\n")

    if "ERROR" in raw:
        assert "ERROR" in out


@pytest.mark.parametrize("raw, secret", [
    ("GROQ_API_KEY=abc123secret", "abc123secret"),
    ("DB_PASSWORD=hunter2", "hunter2"),
    ("GITHUB_TOKEN=tok999", "tok999"),
    ("aws_secret_access_key = wJalrXUtnFEMI", "wJalrXUtnFEMI"),
    ("curl -H 'Bearer eyJhbGciOi.payload.sig9'", "eyJhbGciOi.payload.sig9"),
    ("  password: hunter2", "hunter2"),
    ("mysql --password hunter2 -u root", "hunter2"),
    ('{"access_token": "at-1"}', "at-1"),
    ('{"client_secret": "cs-2"}', "cs-2"),
    ('{"refresh_token": "rt-3"}', "rt-3"),
    ("github_pat_" + "A" * 40, "github_pat_" + "A" * 40),
    ("//registry.npmjs.org/:_authToken=npm_abc123", "npm_abc123"),
])
def test_redacts_common_secret_forms(raw, secret):
    assert secret not in redact(raw)


def test_unterminated_private_key_redacted_to_end():
    raw = "start\n-----BEGIN OPENSSH PRIVATE KEY-----\nb3Blbn\nmore"
    out = redact(raw)

    assert "b3Blbn" not in out and "more" not in out
    assert out.count("\n") == raw.count("\n")
    assert out.startswith("start\n")


@pytest.mark.parametrize("raw", [
    "error: expected token: ';' before '}'",
    "Bearer token required for this endpoint",
    "WARNING: token: deprecated syntax",
])
def test_non_secret_text_untouched(raw):
    assert redact(raw) == raw


from utils.redaction import redact_mapping  # noqa: E402


def test_secret_parameters_redacted():
    out = redact_mapping({
        "DEPLOY_PASSWORD": "hunter2",
        "API_TOKEN": "x1",
        "TARGET": "url https://u:p@h/x",
        "BRANCH": "main",
    })

    assert out["DEPLOY_PASSWORD"] == "[REDACTED]"
    assert out["API_TOKEN"] == "[REDACTED]"
    assert "u:p@" not in out["TARGET"]
    assert out["BRANCH"] == "main"


@pytest.mark.parametrize("name", [
    "DB_PASS", "SSH_KEY", "PRIVATE_KEY", "CREDENTIALS", "KEYSTORE_PASS", "AUTH",
])
def test_more_secret_parameter_names_redacted(name):
    assert redact_mapping({name: "s3cr3t"})[name] == "[REDACTED]"


def test_ordinary_parameter_names_kept():
    params = {"BRANCH": "main", "GERRIT_CHANGE_ID": "I1", "TARGET_ENV": "qa"}

    assert redact_mapping(params) == params


@pytest.mark.parametrize("raw", [
    "-" * 5000,
    "a-" * 20000,
    "x" * 40000,
])
def test_redaction_is_fast_on_long_lines(raw):
    import time

    start = time.monotonic()
    redact(raw)

    assert time.monotonic() - start < 1.0
