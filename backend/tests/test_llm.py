import groq
import httpx
import pytest

import llm
from models import ValidationVerdict


def test_complete_json_parses_plain_json(fake_llm):
    fake = fake_llm(['{"valid": true, "reason": "ok"}'])

    assert llm.complete_json("p", ValidationVerdict).valid is True
    assert fake.calls[0]["response_format"] == {"type": "json_object"}
    assert fake.calls[0]["model"] == "openai/gpt-oss-120b"


def test_complete_json_strips_code_fences(fake_llm):
    fake_llm(['```json\n{"valid": false}\n```'])

    assert llm.complete_json("p", ValidationVerdict).valid is False


def test_complete_json_extracts_object_from_prose(fake_llm):
    fake_llm(['Here you go: {"valid": true} hope that helps'])

    assert llm.complete_json("p", ValidationVerdict).valid is True


def test_complete_json_retries_then_succeeds(fake_llm):
    fake = fake_llm(["not json", '{"valid": true}'])

    assert llm.complete_json("p", ValidationVerdict).valid is True
    assert len(fake.calls) == 2


def test_complete_json_raises_after_retries_exhausted(fake_llm):
    fake = fake_llm(["bad", "bad", "bad"])

    with pytest.raises(llm.LLMOutputError):
        llm.complete_json("p", ValidationVerdict)

    assert len(fake.calls) == 3


def test_provider_error_wrapped_without_retry(fake_llm):
    err = groq.APIConnectionError(
        request=httpx.Request("POST", "https://api.groq.com")
    )
    fake = fake_llm([err])

    with pytest.raises(llm.LLMProviderError):
        llm.complete_json("p", ValidationVerdict)

    assert len(fake.calls) == 1


def test_missing_api_key_raises_unavailable():
    with pytest.raises(llm.LLMUnavailableError):
        llm.get_llm_client()


def _json_validate_failed():
    return groq.BadRequestError(
        "Failed to generate JSON",
        response=httpx.Response(
            400,
            request=httpx.Request("POST", "https://api.groq.com")
        ),
        body={"error": {"code": "json_validate_failed", "message": "x"}},
    )


def test_json_validate_failed_is_retried_as_parse_failure(fake_llm):
    fake = fake_llm([_json_validate_failed(), '{"valid": true}'])

    assert llm.complete_json("p", ValidationVerdict).valid is True
    assert len(fake.calls) == 2


def test_json_validate_failed_exhausted_raises_output_error(fake_llm):
    fake_llm([_json_validate_failed()] * 3)

    with pytest.raises(llm.LLMOutputError):
        llm.complete_json("p", ValidationVerdict)


def test_other_bad_request_is_provider_error(fake_llm):
    err = groq.BadRequestError(
        "model not found",
        response=httpx.Response(
            400,
            request=httpx.Request("POST", "https://api.groq.com")
        ),
        body={"error": {"code": "model_not_found", "message": "x"}},
    )
    fake_llm([err])

    with pytest.raises(llm.LLMProviderError) as exc:
        llm.complete_json("p", ValidationVerdict)

    assert not isinstance(exc.value, llm.LLMOutputError)


# ---- rate limits, per-call model, reasoning effort -------------------------

def _rate_limited(retry_after=None):
    headers = {"retry-after": retry_after} if retry_after is not None else {}

    return groq.RateLimitError(
        "slow down",
        response=httpx.Response(
            429, headers=headers,
            request=httpx.Request("POST", "https://api.groq.com"),
        ),
        body=None,
    )


@pytest.fixture
def sleeps(monkeypatch):
    waited = []
    monkeypatch.setattr(llm, "_sleep", waited.append)
    return waited


def test_rate_limit_waits_retry_after_then_succeeds(fake_llm, sleeps):
    fake = fake_llm([_rate_limited("2"), '{"valid": true}'])

    assert llm.complete_json("p", ValidationVerdict).valid is True
    assert sleeps == [2.0]
    assert len(fake.calls) == 2


def test_rate_limit_wait_is_capped(fake_llm, sleeps, monkeypatch):
    monkeypatch.setenv("LLM_RATE_LIMIT_MAX_WAIT_SECONDS", "10")
    from settings import get_settings
    get_settings.cache_clear()
    fake_llm([_rate_limited("600"), '{"valid": true}'])

    llm.complete_json("p", ValidationVerdict)

    assert sleeps == [10.0]


def test_rate_limit_without_header_uses_default_wait(fake_llm, sleeps):
    fake_llm([_rate_limited(), '{"valid": true}'])

    llm.complete_json("p", ValidationVerdict)

    assert sleeps == [5.0]


def test_rate_limit_exhausted_raises_rate_limit_error(fake_llm, sleeps):
    fake = fake_llm([_rate_limited("1")] * 3)

    with pytest.raises(llm.LLMRateLimitError) as raised:
        llm.complete_json("p", ValidationVerdict)

    assert isinstance(raised.value, llm.LLMProviderError)  # still maps to 503
    assert len(fake.calls) == 3          # first try + llm_rate_limit_retries
    assert sleeps == [1.0, 1.0]


def test_rate_limit_wait_does_not_use_a_parse_attempt(fake_llm, sleeps):
    # 2 rate limits, then 3 bad outputs (the full parse budget), then success
    # would not be reachable if waits consumed parse attempts.
    fake = fake_llm([_rate_limited("0"), _rate_limited("0"), "bad", "bad", '{"valid": true}'])

    assert llm.complete_json("p", ValidationVerdict).valid is True
    assert len(fake.calls) == 5


def test_default_model_and_no_reasoning_effort(fake_llm):
    fake = fake_llm(['{"valid": true}'])

    llm.complete_json("p", ValidationVerdict)

    assert fake.calls[0]["model"] == "openai/gpt-oss-120b"
    assert "reasoning_effort" not in fake.calls[0]


def test_model_and_reasoning_effort_overrides(fake_llm):
    fake = fake_llm(['{"valid": true}'])

    llm.complete_json(
        "p", ValidationVerdict, model="openai/gpt-oss-20b", reasoning_effort="low"
    )

    assert fake.calls[0]["model"] == "openai/gpt-oss-20b"
    assert fake.calls[0]["reasoning_effort"] == "low"


def test_reasoning_effort_from_settings(fake_llm, monkeypatch):
    monkeypatch.setenv("LLM_REASONING_EFFORT", "low")
    from settings import get_settings
    get_settings.cache_clear()
    fake = fake_llm(['{"valid": true}'])

    llm.complete_json("p", ValidationVerdict)

    assert fake.calls[0]["reasoning_effort"] == "low"


def test_model_for_falls_back_to_default(monkeypatch):
    from settings import get_settings
    monkeypatch.setenv("LLM_MODEL_VALIDATOR", "openai/gpt-oss-20b")
    get_settings.cache_clear()
    settings = get_settings()

    assert settings.model_for("validator") == "openai/gpt-oss-20b"
    assert settings.model_for("detector") == settings.llm_model
    assert settings.model_for("root_cause") == settings.llm_model
