"""Strands as the invocation substrate (Layer 3.5).

The project's agents run on `strands.Agent`. What this module does is put that
agent *underneath* the guards `ModelClient` already enforces, rather than
alongside them.

The earlier reasoning against Strands was that adopting it would cost the
per-run call ceiling, the disk cache and the whitelist fallback classifier. That
was true of `Swarm` and `GraphBuilder`, which own the whole orchestration and
give the caller no place to stand. It is not true of `Agent`, which takes
`hooks=` and a configured `BedrockModel`:

- `max_tokens` is `BedrockModel` config, so no call can omit it.
- `BeforeInvocationEvent` fires on every entry point including
  `structured_output`, so the call ceiling is a hook that cancels the
  invocation. `BeforeModelCallEvent` explicitly does *not* fire for
  `structured_output`, which is why the ceiling hangs off the invocation event
  and not the model-call one. Getting that backwards would leave structured
  calls unbounded, which is the exact failure mode the ceiling exists for.
- The disk cache and the fallback chain sit above the agent and are untouched:
  a cache hit never constructs one, and a throttled rung is classified by the
  same whitelist as before.

So Strands drives the calls and the cost guards still hold structurally.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bluepages.llm.client import ModelRole, RunBudget


class AgentBudgetExceeded(RuntimeError):
    """The ceiling was hit inside an agent invocation.

    Raised by the hook rather than returned, so a runaway loop stops at the call
    that would have exceeded the budget instead of one call later.
    """


class BudgetHook:
    """Enforces the per-run call ceiling on every Strands invocation.

    Registered against `BeforeInvocationEvent` because that is the one event
    fired by `__call__`, `stream_async` *and* `structured_output`. The
    model-call events skip structured output entirely.
    """

    def __init__(self, budget: RunBudget) -> None:
        self.budget = budget

    def register_hooks(self, registry: Any, **kwargs: Any) -> None:
        from strands.hooks.events import BeforeInvocationEvent

        registry.add_callback(BeforeInvocationEvent, self._check)

    def _check(self, event: Any) -> None:
        from bluepages.llm.client import BudgetExceededError

        try:
            self.budget.check()
        except BudgetExceededError as exc:
            # Cancel the invocation and raise: a cancelled Strands invocation
            # returns normally, and a silent empty answer here would look like
            # a scene with nothing to report.
            event.cancel = str(exc)
            raise AgentBudgetExceeded(str(exc)) from exc


def build_agent(
    role: ModelRole,
    system: str | None,
    max_tokens: int,
    temperature: float,
    budget: RunBudget,
    region: str,
    read_timeout: int = 120,
    connect_timeout: int = 10,
) -> Any:
    """One configured Strands agent for one rung of the chain.

    Constructed per call rather than cached: these are stateless single
    completions, and a reused agent would accumulate conversation history that
    the next scene has no business seeing.
    """
    from botocore.config import Config
    from strands import Agent
    from strands.models.bedrock import BedrockModel

    model = BedrockModel(
        model_id=role.model_id,
        max_tokens=max_tokens,
        temperature=temperature,
        # Our own chain handles fallback. Botocore retrying underneath would
        # multiply latency before we ever saw the throttle.
        boto_client_config=Config(
            retries={"max_attempts": 2, "mode": "standard"},
            read_timeout=read_timeout,
            connect_timeout=connect_timeout,
        ),
        region_name=region,
    )
    return Agent(
        model=model,
        system_prompt=system,
        # No tools and no history: each call is one scene, judged on its own
        # evidence. Tools arrive in the ingest agent, which genuinely has work
        # to choose between.
        tools=[],
        hooks=[BudgetHook(budget)],
        callback_handler=None,
    )


def usage_from(result: Any) -> dict[str, int]:
    """Token usage out of an `AgentResult`, in the shape `RunBudget` charges.

    Strands accumulates usage across the event loop's cycles, so this is the
    whole invocation and not just the final turn.
    """
    usage = getattr(getattr(result, "metrics", None), "accumulated_usage", None) or {}
    return {
        "inputTokens": int(usage.get("inputTokens", 0) or 0),
        "outputTokens": int(usage.get("outputTokens", 0) or 0),
    }


def text_of(result: Any) -> str:
    """The assistant's text out of an `AgentResult`.

    Strands returns a message whose content is a list of blocks. Joining the
    text blocks matches what the boto3 path returned, so `parse_as` downstream
    cannot tell the two apart.
    """
    message = getattr(result, "message", None) or {}
    content = message.get("content", []) if isinstance(message, dict) else []
    return "".join(
        block.get("text", "")
        for block in content
        if isinstance(block, dict) and "text" in block
    )


__all__ = ["AgentBudgetExceeded", "BudgetHook", "build_agent", "text_of", "usage_from"]
