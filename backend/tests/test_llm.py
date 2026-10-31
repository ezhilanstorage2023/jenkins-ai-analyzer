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
