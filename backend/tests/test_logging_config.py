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
