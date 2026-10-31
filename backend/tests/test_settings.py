from settings import get_settings


def test_defaults():
    s = get_settings()
    assert s.llm_model == "openai/gpt-oss-120b"
    assert s.max_validation_attempts == 3
    assert s.parser_max_characters == 10000
    assert s.groq_api_key is None


def test_env_override(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    monkeypatch.setenv("MAX_UPLOAD_BYTES", "100")
    get_settings.cache_clear()
    s = get_settings()
    assert s.groq_api_key.get_secret_value() == "gsk_test"
    assert "gsk_test" not in repr(s)
    assert s.max_upload_bytes == 100
