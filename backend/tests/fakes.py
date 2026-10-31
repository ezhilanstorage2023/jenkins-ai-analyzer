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


class FakeJenkins:
    """httpx MockTransport routing on URL path; records every request."""

    def __init__(self, routes=None):
        # path -> (status, body); body is bytes/str (raw) or dict/list (JSON)
        self.routes = dict(routes or {})
        self.seen = []

    def _handle(self, request):
        import httpx

        self.seen.append(request)
        status, body = self.routes.get(request.url.path, (404, b"not found"))

        if isinstance(body, (dict, list)):
            return httpx.Response(status, json=body)

        if isinstance(body, str):
            body = body.encode()

        return httpx.Response(status, content=body)

    @property
    def transport(self):
        import httpx

        return httpx.MockTransport(self._handle)


def responses_for(log, signature, category):
    """Canned detector/root-cause/verdict JSON grounded in `log`."""

    from utils.log_processor import extract_error_context

    parsed = extract_error_context(log)
    evidence = next(line for line in parsed.splitlines() if signature in line)

    return [
        detected_json([evidence]),
        root_cause_json(category, [evidence]),
        verdict_json(True),
    ]
