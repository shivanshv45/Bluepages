"""Getting structured data back from a text completion.

Bedrock's Converse API can return tool-shaped output, but the fallback rungs
(Groq, Gemini) shape theirs differently, and a schema that only works on the
primary model defeats the point of having a chain. So every provider is asked
for JSON in text and parsed here, identically.

The parsing is deliberately forgiving about packaging and strict about content.
Models wrap JSON in prose or fences, and rejecting that would fail a run over
formatting. But a response that parses and then fails validation is a prompt
bug, and it is raised as one: `SchemaError` is not retryable, so the fallback
classifier lets it terminate the run rather than burning the chain on it.
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, ValidationError


class SchemaError(ValueError):
    """The model's answer did not match the schema we asked for.

    Deliberately a ValueError: the fallback classifier treats Python data errors
    as fatal, so a bad prompt stops the run instead of being retried on three
    providers that would all fail the same way.
    """


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def extract_json(text: str) -> Any:
    """Pull the JSON value out of a model response.

    Handles a bare object, a fenced block, and prose wrapped around either.
    """
    if not text or not text.strip():
        raise SchemaError("model returned an empty response")

    candidates: list[str] = []
    fenced = _FENCE_RE.search(text)
    if fenced:
        candidates.append(fenced.group(1).strip())
    candidates.append(text.strip())
    # Last resort: the outermost braces or brackets in the whole response.
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = text.find(opener), text.rfind(closer)
        if start != -1 and end > start:
            candidates.append(text[start : end + 1])

    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue

    raise SchemaError(f"no JSON found in model response: {text[:200]!r}")


def parse_as[T: BaseModel](text: str, schema: type[T]) -> T:
    """Parse a model response into a pydantic model, or raise `SchemaError`."""
    data = extract_json(text)
    try:
        return schema.model_validate(data)
    except ValidationError as exc:
        raise SchemaError(
            f"response did not match {schema.__name__}: {exc.error_count()} "
            f"validation error(s). First: {exc.errors()[0].get('msg', '')}. "
            f"Got: {json.dumps(data)[:300]}"
        ) from exc


def schema_hint(schema: type[BaseModel]) -> str:
    """A compact JSON-schema string to put in a prompt.

    Sent to the model verbatim so the shape it is asked for and the shape it is
    validated against cannot drift apart.
    """
    return json.dumps(schema.model_json_schema(), indent=2)


__all__ = ["SchemaError", "extract_json", "parse_as", "schema_hint"]
