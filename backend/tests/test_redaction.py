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
