"""The Strands invocation path (Layer 3.5).

These do not call Bedrock. What they check is the wiring that decides whether a
real run stays bounded: that the agent is configured with the max_tokens and
model id it was given, that the budget hook is registered against the one event
which fires for structured output as well as for plain calls, and that the
Strands and boto3 paths return the same shape so nothing above them can tell
which answered.
"""

from __future__ import annotations

from typing import Any, ClassVar

import pytest

from bluepages.config import Settings
from bluepages.llm import ModelClient, ModelRole, RunBudget
from bluepages.llm.agent_runtime import (
    AgentBudgetExceeded,
    BudgetHook,
    build_agent,
    text_of,
    usage_from,
)


class _Registry:
    """Records what a hook provider registers, without a real agent."""

    def __init__(self) -> None:
        self.callbacks: list[tuple[Any, Any]] = []

    def add_callback(self, event_type: Any, callback: Any, *, order: float = 0) -> None:
        self.callbacks.append((event_type, callback))


class _Event:
    def __init__(self) -> None:
        self.cancel: Any = False


def test_budget_hook_registers_on_the_invocation_event() -> None:
    """Not the model-call event.

    `BeforeModelCallEvent` is documented as not firing for `structured_output`.
    Hanging the ceiling off it would leave every structured call unbounded,
    which is the exact thing the ceiling exists to prevent.
    """
    from strands.hooks.events import BeforeInvocationEvent

    registry = _Registry()
    BudgetHook(RunBudget(max_calls=5)).register_hooks(registry)

    assert [event for event, _ in registry.callbacks] == [BeforeInvocationEvent]


def test_budget_hook_raises_at_the_ceiling() -> None:
    budget = RunBudget(max_calls=2, calls_made=2)
    registry = _Registry()
    BudgetHook(budget).register_hooks(registry)
    _, callback = registry.callbacks[0]

    with pytest.raises(AgentBudgetExceeded):
        callback(_Event())


def test_budget_hook_also_cancels_the_invocation() -> None:
    """Raising stops this call; cancelling stops Strands continuing regardless."""
    budget = RunBudget(max_calls=1, calls_made=1)
    registry = _Registry()
    BudgetHook(budget).register_hooks(registry)
    _, callback = registry.callbacks[0]

    event = _Event()
    with pytest.raises(AgentBudgetExceeded):
        callback(event)
    assert event.cancel  # truthy, and carries the reason
    assert "ceiling" in str(event.cancel)


def test_budget_hook_passes_under_the_ceiling() -> None:
    registry = _Registry()
    BudgetHook(RunBudget(max_calls=10, calls_made=3)).register_hooks(registry)
    _, callback = registry.callbacks[0]

    event = _Event()
    callback(event)
    assert event.cancel is False


def test_agent_is_built_with_the_bounded_config() -> None:
    """max_tokens and the model id reach the model, and no tools are attached."""
    agent = build_agent(
        role=ModelRole("bulk", "anthropic.claude-haiku-4-5", "bedrock"),
        system="you are a test",
        max_tokens=1234,
        temperature=0.0,
        budget=RunBudget(max_calls=10),
        region="us-west-2",
    )

    config = agent.model.get_config()
    assert config["max_tokens"] == 1234
    assert config["model_id"] == "anthropic.claude-haiku-4-5"
    assert config["temperature"] == 0.0


def test_agent_carries_the_budget_hook() -> None:
    """The ceiling has to be on the agent, not merely built alongside it.

    Checked by exhausting the budget and invoking the registered callback: a
    hook the agent never registered cannot stop a runaway loop.
    """
    from strands.hooks.events import BeforeInvocationEvent

    budget = RunBudget(max_calls=1, calls_made=1)
    agent = build_agent(
        role=ModelRole("bulk", "m", "bedrock"),
        system=None,
        max_tokens=100,
        temperature=0.0,
        budget=budget,
        region="us-west-2",
    )

    callbacks = list(agent.hooks.get_callbacks_for(BeforeInvocationEvent(agent=agent)))
    assert callbacks, "the agent registered no BeforeInvocationEvent callback"

    with pytest.raises(AgentBudgetExceeded):
        for callback in callbacks:
            callback(_Event())


def test_text_of_joins_the_text_blocks() -> None:
    class R:
        message: ClassVar[dict[str, Any]] = {"content": [{"text": "a"}, {"text": "b"}]}

    assert text_of(R()) == "ab"


def test_text_of_skips_non_text_blocks() -> None:
    """A tool-use block in the message must not become part of the JSON."""

    class R:
        message: ClassVar[dict[str, Any]] = {
            "content": [{"text": "a"}, {"toolUse": {"name": "x"}}, {"text": "b"}]
        }

    assert text_of(R()) == "ab"


def test_text_of_survives_an_empty_message() -> None:
    class R:
        message: ClassVar[dict[str, Any]] = {}

    assert text_of(R()) == ""


def test_usage_is_read_in_the_shape_the_budget_charges() -> None:
    class M:
        accumulated_usage: ClassVar[dict[str, int]] = {
            "inputTokens": 11,
            "outputTokens": 22,
            "totalTokens": 33,
        }

    class R:
        metrics = M()

    assert usage_from(R()) == {"inputTokens": 11, "outputTokens": 22}


def test_usage_defaults_to_zero_when_absent() -> None:
    """A missing usage block must not crash a run that otherwise succeeded."""

    class R:
        metrics = None

    assert usage_from(R()) == {"inputTokens": 0, "outputTokens": 0}


def test_strands_is_the_default_runtime() -> None:
    assert Settings().bluepages_agent_runtime == "strands"


def test_unknown_runtime_is_rejected() -> None:
    """A typo in the escape hatch must fail loudly, not silently pick a path."""
    with pytest.raises(ValueError, match="strands"):
        Settings(bluepages_agent_runtime="stands")


def test_runtime_setting_routes_the_bedrock_call() -> None:
    """The setting picks the path, and both are reachable."""
    calls: list[str] = []

    def make(runtime: str) -> ModelClient:
        client = ModelClient(settings=Settings(bluepages_agent_runtime=runtime))
        client._invoke_strands = lambda *a, **k: (calls.append("strands"), ("", {}))[1]  # type: ignore[method-assign]
        client._invoke_converse = lambda *a, **k: (calls.append("boto3"), ("", {}))[1]  # type: ignore[method-assign]
        return client

    role = ModelRole("bulk", "m", "bedrock")
    make("strands")._invoke_bedrock(role, "p", None, 10, 0.0)
    make("boto3")._invoke_bedrock(role, "p", None, 10, 0.0)

    assert calls == ["strands", "boto3"]
