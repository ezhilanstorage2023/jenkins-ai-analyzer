import logging
import re
from functools import lru_cache
from typing import TypeVar

import groq
from pydantic import BaseModel, ValidationError

from settings import get_settings

logger = logging.getLogger("llm")

T = TypeVar("T", bound=BaseModel)

_CODE_FENCE = re.compile(
    r"^\s*```[A-Za-z]*\s*\n?(.*?)\n?\s*```\s*$",
    re.DOTALL,
)


class LLMError(Exception):
    pass


class LLMOutputError(LLMError):
    """LLM replied, but never with output matching the expected schema."""


class LLMProviderError(LLMError):
    """The LLM provider could not be reached or rejected the request."""


class LLMUnavailableError(LLMProviderError):
    """No LLM is configured."""


@lru_cache
def _client_for_key(api_key: str) -> groq.Groq:
    return groq.Groq(api_key=api_key)


def reset_client_cache() -> None:
    _client_for_key.cache_clear()


def get_llm_client() -> groq.Groq:
    api_key = get_settings().groq_api_key

    if api_key is None:
        raise LLMUnavailableError("GROQ_API_KEY is not configured")

    return _client_for_key(api_key.get_secret_value())


def _parse(text: str | None, schema: type[T]) -> T:
    candidate = text or ""

    fenced = _CODE_FENCE.match(candidate)
    if fenced:
        candidate = fenced.group(1)

    try:
        return schema.model_validate_json(candidate)
    except ValidationError:
        # Fall back to the outermost JSON object when the model
        # surrounds it with prose.
        start = candidate.find("{")
        end = candidate.rfind("}")

        if start == -1 or end <= start:
            raise

        return schema.model_validate_json(candidate[start:end + 1])


def _is_json_generation_failure(err: groq.APIError) -> bool:
    # In JSON mode Groq rejects invalid JSON with a 400 instead of
    # returning it; treat that like unparseable output, not an outage.
    if not isinstance(err, groq.BadRequestError):
        return False

    body = err.body

    if isinstance(body, dict):
        body = body.get("error", body)

    return isinstance(body, dict) and body.get("code") == "json_validate_failed"


def complete_json(prompt: str, schema: type[T]) -> T:
    settings = get_settings()
    client = get_llm_client()
    attempts = 1 + settings.llm_parse_retries
    last_error_type = "unknown"

    for attempt in range(1, attempts + 1):
        try:
            response = client.chat.completions.create(
                model=settings.llm_model,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
            )
        except groq.APIError as err:
            if not _is_json_generation_failure(err):
                raise LLMProviderError(
                    f"LLM provider request failed: {type(err).__name__}"
                ) from err

            last_error_type = "json_validate_failed"
            logger.warning(
                "LLM failed to generate JSON",
                extra={"schema": schema.__name__, "attempt": attempt},
            )
            continue

        try:
            return _parse(response.choices[0].message.content, schema)
        except ValidationError as err:
            # Report only the error type: raw output may echo log content.
            last_error_type = err.errors()[0]["type"]
            logger.warning(
                "LLM output failed schema validation",
                extra={"schema": schema.__name__, "attempt": attempt},
            )

    raise LLMOutputError(
        f"{schema.__name__} invalid after {attempts} attempts ({last_error_type})"
    )
