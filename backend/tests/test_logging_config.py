import json
import logging

from logging_config import JsonFormatter


def test_json_formatter_emits_one_json_object():
    record = logging.LogRecord(
        "graph", logging.INFO, __file__, 1, "parsed %d lines", (12,), None
    )
    record.event_id = "e1"

    out = json.loads(JsonFormatter().format(record))

    assert out["message"] == "parsed 12 lines"
    assert out["level"] == "INFO"
    assert out["logger"] == "graph"
    assert out["event_id"] == "e1"


def test_debug_level_does_not_enable_sdk_request_logging():
    from logging_config import configure_logging

    configure_logging("DEBUG")

    for name in ("groq", "httpx", "httpcore"):
        assert logging.getLogger(name).getEffectiveLevel() >= logging.INFO

    configure_logging("INFO")
