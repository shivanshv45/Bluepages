"""A model client that answers from a script instead of from Bedrock.

Layers 3.3 and 3.4 are mostly not model calls: they are prompt construction,
concurrency, schema validation, the guard against findings about unchanged
scenes, and scoring. All of that is testable without spending anything, and it
is the part most likely to break.

What this cannot test is whether the model reaches the right judgment. That
needs the real thing, and it is covered by the `live` marker, which is
deselected by default.
"""

from __future__ import annotations

import json
from typing import Any

from bluepages.llm import Completion, RunBudget


class ScriptedClient:
    """Replays canned answers, matched by a substring of the prompt.

    Rules are checked in order, so a specific rule can precede a general one.
    A prompt matching nothing raises, because a silently empty answer would let
    a broken prompt-construction bug pass as "the model found nothing".
    """

    def __init__(
        self,
        rules: list[tuple[str, Any]] | None = None,
        default: Any = None,
        model_name: str = "haiku-4.5",
    ) -> None:
        self.rules = rules or []
        self.default = default
        self.model_name = model_name
        self.prompts: list[str] = []
        self.systems: list[str | None] = []
        self.judgment_calls = 0
        self.budget = RunBudget(max_calls=1000)

    def complete(
        self,
        prompt: str,
        system: str | None = None,
        judgment: bool = False,
        max_tokens: int | None = None,
        temperature: float = 0.0,
        cache_key_extra: str = "",
    ) -> Completion:
        # Every call must be bounded. If this ever fires, a caller has found a
        # path around the guard that exists to stop a runaway AWS bill.
        assert max_tokens is not None, "max_tokens must always be set"
        self.prompts.append(prompt)
        self.systems.append(system)
        if judgment:
            self.judgment_calls += 1
        self.budget.calls_made += 1

        for needle, answer in self.rules:
            if needle in prompt:
                return self._completion(answer)
        if self.default is not None:
            return self._completion(self.default)
        raise AssertionError(f"no scripted answer for prompt:\n{prompt[:400]}")

    def _completion(self, answer: Any) -> Completion:
        text = answer if isinstance(answer, str) else json.dumps(answer)
        return Completion(
            text=text,
            model_name=self.model_name,
            model_id="fake",
            provider="fake",
            input_tokens=10,
            output_tokens=20,
        )


def elements_answer(*elements: dict[str, Any]) -> dict[str, Any]:
    """A Layer 3.3 response body."""
    return {"elements": list(elements)}


def findings_answer(*findings: dict[str, Any]) -> dict[str, Any]:
    """A Layer 3.4 response body."""
    return {"findings": list(findings)}


EMPTY_ELEMENTS = {"elements": []}
EMPTY_FINDINGS = {"findings": []}
