"""Tests for the model fallback classifier (Layer 3.5).

The rule being defended, from DECISIONS.md:

    Trigger on throttling, timeouts and 5xx only. Never on schema or prompt
    errors, which fail identically on every provider and would burn three
    quotas on one bug.

These tests exist because that rule is easy to write and easy to violate: any
"retry on Exception" makes a single bad prompt cost three providers' quota.
"""

from __future__ import annotations

import pytest
from botocore.exceptions import ClientError

from ripple.llm import Retryability, RunBudget, classify, is_retryable
from ripple.llm.client import BudgetExceededError


def aws_error(code: str, status: int) -> ClientError:
    return ClientError(
        {
            "Error": {"Code": code, "Message": "test"},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        "Converse",
    )


class TestFallsBack:
    """Infrastructure failures: the request was fine, the service was not."""

    @pytest.mark.parametrize(
        "code,status",
        [
            ("ThrottlingException", 429),
            ("TooManyRequestsException", 429),
            ("ProvisionedThroughputExceededException", 429),
            ("ServiceUnavailableException", 503),
            ("InternalServerException", 500),
            ("ModelTimeoutException", 408),
        ],
    )
    def test_aws_infrastructure_errors(self, code, status):
        assert classify(aws_error(code, status)) is Retryability.FALLBACK

    @pytest.mark.parametrize("exc", [TimeoutError("timed out"), ConnectionError("reset")])
    def test_transport_failures(self, exc):
        assert is_retryable(exc)

    def test_unknown_5xx(self):
        assert classify(aws_error("SomethingNew", 502)) is Retryability.FALLBACK


class TestNeverFallsBack:
    """Our own bugs. They fail identically on every provider."""

    @pytest.mark.parametrize(
        "code,status",
        [
            ("ValidationException", 400),
            ("AccessDeniedException", 403),
            ("ResourceNotFoundException", 404),
            ("UnrecognizedClientException", 403),
            ("SerializationException", 400),
            ("ExpiredTokenException", 403),
        ],
    )
    def test_request_errors_are_fatal(self, code, status):
        assert classify(aws_error(code, status)) is Retryability.FATAL

    @pytest.mark.parametrize(
        "exc",
        [
            ValueError("bad schema"),
            KeyError("props"),
            TypeError("str expected"),
            IndexError("out of range"),
            AttributeError("no attribute"),
        ],
    )
    def test_python_data_errors_are_fatal(self, exc):
        assert classify(exc) is Retryability.FATAL

    def test_schema_error_mentioning_timeout_is_still_fatal(self):
        """The trap this classifier exists to avoid.

        A prompt or schema bug whose message happens to contain a retryable word
        must not consume the whole chain. Text is the weakest signal and is only
        consulted for exception types that are not our own data errors.
        """
        exc = ValueError("schema validation failed: field 'timeout' is required")
        assert classify(exc) is Retryability.FATAL

    def test_json_decode_error_is_fatal(self):
        """A malformed model response is a prompt problem, not a service outage."""
        import json

        with pytest.raises(json.JSONDecodeError) as info:
            json.loads("{not json")
        assert classify(info.value) is Retryability.FATAL

    def test_unknown_exception_is_fatal_by_default(self):
        """Whitelist, not blacklist. A new error type surfaces as a bug."""

        class SomethingNobodyAnticipated(Exception):
            pass

        assert classify(SomethingNobodyAnticipated("?")) is Retryability.FATAL


class TestRunBudget:
    """CLAUDE.md: never an LLM call in an unbounded loop."""

    def test_budget_stops_a_runaway_loop(self):
        budget = RunBudget(max_calls=3)
        budget.calls_made = 3
        with pytest.raises(BudgetExceededError):
            budget.check()

    def test_budget_allows_calls_under_the_ceiling(self):
        budget = RunBudget(max_calls=3)
        budget.calls_made = 2
        budget.check()  # must not raise

    def test_budget_tracks_per_model_counts(self):
        """Which model answered has to be visible, not silent."""
        from ripple.llm import Completion

        budget = RunBudget(max_calls=10)
        budget.charge(
            Completion(text="x", model_name="sonnet-5", model_id="m", provider="bedrock",
                       input_tokens=100, output_tokens=50)
        )
        budget.charge(
            Completion(text="y", model_name="haiku-4.5", model_id="m", provider="bedrock",
                       via_fallback=True, input_tokens=10, output_tokens=5)
        )
        summary = budget.summary()
        assert summary["calls"] == 2
        assert summary["input_tokens"] == 110
        assert summary["by_model"] == {"sonnet-5": 1, "haiku-4.5": 1}


class TestChainOrder:
    """Fall back within Bedrock first; leave AWS only on provider failure."""

    def test_judgment_starts_at_sonnet(self):
        from ripple.config import Settings
        from ripple.llm import ModelClient

        client = ModelClient(settings=Settings(groq_api_key=None, gemini_api_key=None))
        assert [r.name for r in client.chain(judgment=True)] == ["sonnet-5", "haiku-4.5"]

    def test_bulk_starts_at_haiku(self):
        from ripple.config import Settings
        from ripple.llm import ModelClient

        client = ModelClient(settings=Settings(groq_api_key=None, gemini_api_key=None))
        assert [r.name for r in client.chain(judgment=False)] == ["haiku-4.5", "sonnet-5"]

    def test_external_providers_come_last(self):
        from ripple.config import Settings
        from ripple.llm import ModelClient

        client = ModelClient(settings=Settings(groq_api_key="k", gemini_api_key="k"))
        chain = [r.provider for r in client.chain(judgment=True)]
        assert chain[:2] == ["bedrock", "bedrock"]
        assert set(chain[2:]) == {"groq", "gemini"}


class TestChainBehaviour:
    """The chain end to end, with faults injected at the provider boundary.

    These subclass `ModelClient` and override `_invoke` rather than patching the
    classifier, so the logic under test is the real one.
    """

    @staticmethod
    def _settings():
        from ripple.config import Settings

        return Settings(groq_api_key=None, gemini_api_key=None, ripple_cache_llm=False)

    def test_throttling_falls_back_and_reports_which_model_answered(self):
        from ripple.events import CollectingStream, EventKind
        from ripple.llm import ModelClient, RunBudget

        class ThrottleFirst(ModelClient):
            def _invoke(self, role, prompt, system, max_tokens, temperature):
                if role.name == "sonnet-5":
                    raise aws_error("ThrottlingException", 429)
                return "ready", {"inputTokens": 10, "outputTokens": 2}

        events = CollectingStream()
        client = ThrottleFirst(
            settings=self._settings(), stream=events, budget=RunBudget(max_calls=5)
        )
        result = client.complete("hi", judgment=True)

        assert result.model_name == "haiku-4.5"
        assert result.via_fallback
        assert events.count(EventKind.MODEL_FALLBACK) == 1

    def test_schema_error_does_not_consume_the_chain(self):
        """One bad prompt must cost one provider, not three."""
        from ripple.events import CollectingStream, EventKind
        from ripple.llm import ModelClient, RunBudget

        attempts = []

        class SchemaBug(ModelClient):
            def _invoke(self, role, prompt, system, max_tokens, temperature):
                attempts.append(role.name)
                raise ValueError("schema validation failed: field 'timeout' required")

        events = CollectingStream()
        client = SchemaBug(
            settings=self._settings(), stream=events, budget=RunBudget(max_calls=5)
        )
        with pytest.raises(ValueError, match="schema validation"):
            client.complete("hi", judgment=True)

        assert len(attempts) == 1
        assert events.count(EventKind.MODEL_FALLBACK) == 0

    def test_chain_exhaustion_reports_every_failure(self):
        from ripple.llm import AllModelsFailedError, ModelClient, RunBudget

        class AllDown(ModelClient):
            def _invoke(self, role, prompt, system, max_tokens, temperature):
                raise aws_error("ServiceUnavailableException", 503)

        client = AllDown(settings=self._settings(), budget=RunBudget(max_calls=5))
        with pytest.raises(AllModelsFailedError) as info:
            client.complete("hi")
        assert len(info.value.failures) == 2

    def test_max_tokens_is_never_unset(self):
        """CLAUDE.md: always set max_tokens. No code path may omit it."""
        from ripple.llm import ModelClient, RunBudget

        seen: dict[str, int] = {}

        class Capture(ModelClient):
            def _invoke(self, role, prompt, system, max_tokens, temperature):
                seen["max_tokens"] = max_tokens
                return "x", {}

        Capture(settings=self._settings(), budget=RunBudget(max_calls=3)).complete("hi")
        assert seen["max_tokens"] > 0
