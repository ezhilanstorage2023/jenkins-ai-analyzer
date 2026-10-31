import json
from types import SimpleNamespace


class FakeLLMClient:
    """Stands in for groq.Groq; replays queued responses in order."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create)
        )

    def _create(self, **kwargs):
        self.calls.append(kwargs)

        if not self.responses:
            raise AssertionError("FakeLLMClient exhausted")

        response = self.responses.pop(0)

        if isinstance(response, Exception):
            raise response

        message = SimpleNamespace(content=response)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def detected_json(evidence, main_error="Build failed"):
    return json.dumps({
        "main_error": main_error,
        "error_messages": [main_error],
        "affected_component": "build",
        "evidence": evidence,
    })


def root_cause_json(category, evidence, confidence="high"):
    return json.dumps({
        "root_cause": "Root cause derived from evidence",
        "error_category": category,
        "evidence": evidence,
        "affected_component": "build",
        "recommended_fix": "Fix the reported error",
        "confidence": confidence,
    })


def verdict_json(valid, reason="ok"):
    return json.dumps({
        "valid": valid,
        "reason": reason,
        "unsupported_claims": [],
    })
