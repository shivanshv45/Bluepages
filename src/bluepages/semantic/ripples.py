"""Ripples: checking the model's own unfounded hunches (Layer 6).

`_reject_unfounded` in reasoning.py drops findings that name a scene the diff
never flagged, because a model reasoning about one scene occasionally answers
about the draft in general and a confident wrong finding is worse than none.
But some of those rejections are real: the model noticed a consequence in a
neighbouring scene while reasoning about the change in front of it. That is
the one legitimate path to a finding in an unflagged scene, and it is worth
one cheap check rather than silent loss.

A ripple check reads the actual scene text and asks: does this line really
confirm the hunch? It is a bulk call, not a judgment call, because the
question is narrow (yes/no plus a quote), and it is capped at MAX_RIPPLES per
run for the same cost reason agents/sourcing.py caps searches.
"""

from __future__ import annotations

from dataclasses import dataclass

from bluepages.events import EventKind, EventStream, NullStream
from bluepages.llm import ModelClient
from bluepages.llm.structured import SchemaError, parse_as
from bluepages.model import Screenplay
from bluepages.semantic.reasoning import Finding, RippleQuestion
from pydantic import BaseModel, ConfigDict, Field

PROMPT = """You are checking a hunch against a script scene.

Another pass of reasoning, working on a different scene, produced this finding
about THIS scene, which it was not actually asked about:

  {summary}
  (reasoning given: {reasoning})

Here is the actual text of the scene it named:

---
{scene_text}
---

Does the scene text really confirm this finding? Answer only from what the
text shows. If the finding is not supported, say so.

Return JSON: {{"confirmed": true|false, "quote": "the exact line that confirms
it, or empty string", "note": "one short sentence"}}"""


class RippleCheck(BaseModel):
    """The model's verdict on one ripple candidate."""

    model_config = ConfigDict(extra="forbid")

    confirmed: bool
    quote: str = ""
    note: str = ""


@dataclass
class Ripple:
    """One ripple, checked and resolved."""

    question: RippleQuestion
    confirmed: bool
    quote: str
    note: str

    @property
    def finding(self) -> Finding | None:
        return self.question.finding if self.confirmed else None


def check_ripples(
    questions: list[RippleQuestion],
    after: Screenplay,
    client: ModelClient,
    stream: EventStream | None = None,
) -> list[Ripple]:
    """Check each ripple candidate against the real scene text.

    One bulk call per candidate, max_tokens bounded per CLAUDE.md's cost rule.
    A scene that no longer exists (a bad scene number) or a call that fails
    resolves to unconfirmed rather than raising: a ripple is a bonus finding,
    not something a run should fail over.
    """
    stream = stream or NullStream()
    ripples: list[Ripple] = []

    for question in questions:
        finding = question.finding
        scene_number = finding.scene
        scene = after.scene(scene_number) if scene_number else None

        stream.emit(
            EventKind.RIPPLE_OPENED,
            finding.summary,
            scene_number=scene_number,
            heading=scene.heading if scene else "",
            hypothesis=finding.summary,
        )

        if scene is None:
            stream.emit(
                EventKind.RIPPLE_RESOLVED,
                "that scene does not exist in this draft",
                scene_number=scene_number,
                outcome="quiet",
                result="that scene does not exist in this draft",
            )
            ripples.append(Ripple(question=question, confirmed=False, quote="", note=""))
            continue

        stream.emit(
            EventKind.RIPPLE_STEP,
            f"reading scene {scene_number} against the hunch",
            scene_number=scene_number,
            step="reading the scene",
        )

        try:
            completion = client.complete(
                prompt=PROMPT.format(
                    summary=finding.summary,
                    reasoning=finding.reasoning or "(none given)",
                    scene_text=scene.full_text,
                ),
                system="Answer only from the scene text given. Return only JSON.",
                judgment=False,
                max_tokens=800,
                cache_key_extra="ripple-v1",
                label=f"ripple scene {scene_number}",
            )
            check = parse_as(completion.text, RippleCheck)
        except SchemaError:
            note = "ripple check returned unusable JSON, dropped"
            stream.emit(
                EventKind.RIPPLE_RESOLVED,
                note,
                scene_number=scene_number,
                outcome="quiet",
                result=note,
            )
            ripples.append(Ripple(question=question, confirmed=False, quote="", note=""))
            continue

        confirmed = check.confirmed and bool(check.quote.strip())
        result = check.note or (check.quote if confirmed else "not supported by the scene text")
        stream.emit(
            EventKind.RIPPLE_RESOLVED,
            result,
            scene_number=scene_number,
            outcome="finding" if confirmed else "quiet",
            result=result,
            quote=check.quote,
        )
        ripples.append(
            Ripple(question=question, confirmed=confirmed, quote=check.quote, note=check.note)
        )

    return ripples


def confirmed_findings(ripples: list[Ripple]) -> list[Finding]:
    """Findings a ripple actually confirmed, ready to fold into the result."""
    return [r.finding for r in ripples if r.finding is not None]


__all__ = ["Ripple", "check_ripples", "confirmed_findings"]
