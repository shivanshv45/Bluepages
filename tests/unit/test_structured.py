"""Structured output parsing.

The packaging is forgiving and the content is strict, and both halves matter:
a model wrapping JSON in prose must not fail a run, and a model returning the
wrong shape must fail it fatally rather than burning the fallback chain.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from ripple.llm.fallback import is_retryable
from ripple.llm.structured import SchemaError, extract_json, parse_as, schema_hint


class Thing(BaseModel):
    name: str
    count: int = 0


def test_bare_object():
    assert extract_json('{"name": "x"}') == {"name": "x"}


def test_fenced_block():
    text = 'Here is the answer:\n```json\n{"name": "letter opener"}\n```\nHope that helps.'
    assert extract_json(text) == {"name": "letter opener"}


def test_unlabelled_fence():
    assert extract_json('```\n{"name": "x"}\n```') == {"name": "x"}


def test_prose_around_bare_json():
    text = 'I think the answer is {"name": "x", "count": 2} based on the scene.'
    assert extract_json(text) == {"name": "x", "count": 2}


def test_top_level_list():
    assert extract_json("[1, 2, 3]") == [1, 2, 3]


def test_empty_response_is_a_schema_error():
    with pytest.raises(SchemaError):
        extract_json("   ")


def test_no_json_at_all():
    with pytest.raises(SchemaError):
        extract_json("I am sorry, I cannot help with that.")


def test_parse_as_validates():
    thing = parse_as('{"name": "brass letter opener", "count": 1}', Thing)
    assert thing.name == "brass letter opener"
    assert thing.count == 1


def test_wrong_shape_raises_schema_error():
    with pytest.raises(SchemaError) as exc:
        parse_as('{"count": "not a number"}', Thing)
    # The message has to name the schema and show what came back, or a failing
    # run tells you nothing about which prompt is wrong.
    assert "Thing" in str(exc.value)


def test_schema_error_is_fatal_not_retryable():
    """The rule from DECISIONS.md: a schema error fails identically everywhere.

    Retrying it on Haiku and then on Groq burns three quotas on one bad prompt.
    """
    assert not is_retryable(SchemaError("bad shape"))


def test_schema_error_mentioning_timeout_stays_fatal():
    """The classifier must not read the message text of a data error.

    A schema error about a field literally named "timeout" would otherwise look
    retryable. There is an equivalent test in test_fallback.py; this one guards
    the same rule at the layer that produces the error.
    """
    assert not is_retryable(SchemaError("field 'timeout' is required, request timed out"))


def test_schema_hint_is_json():
    hint = schema_hint(Thing)
    assert '"name"' in hint
    assert '"properties"' in hint
