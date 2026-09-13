"""Semantic reasoning (Layer 3.4).

Claude Sonnet over the mechanical diff. This is the layer the project exists
for: everything below it can say the words "letter opener" left scene 3 and
appeared in scene 7, and none of it can say whether that is one object that
moved or a cut and a new buy. Props needs the difference, because one is a
continuity note and the other is a purchase order.

Four questions, all from the PRD:

    Is the object in the new scene the one that left the old one?
    Is CUSTODIAN a renamed JANITOR or a new role to cast?
    Does this rewritten action line have a physical consequence, or is it prose?
    Does this change affect the schedule rather than any department's kit?

The design decision that matters here is what the model is asked. It is given
the mechanical evidence for one scene, and it answers about that scene. It is
not handed the whole draft and asked what changed: that is a summarisation task
and it hallucinates changes that are not in the diff. Every finding it returns
must name a scene the diff actually flagged, and `_reject_unfounded` drops the
ones that do not.

Findings use the answer key's own vocabulary (`ChangeKind`, `Department`) so
scoring is a comparison, not a translation.
"""

from __future__ import annotations

import concurrent.futures
import re
from dataclasses import dataclass, field as dc_field

from pydantic import BaseModel, ConfigDict, Field, field_validator

from bluepages.diff import Alignment, AlignmentKind, DraftDiff, SceneDiff
from bluepages.events import EventKind, EventStream, NullStream
from bluepages.llm import ModelClient
from bluepages.llm.structured import SchemaError, parse_as
from bluepages.semantic.elements import DraftElements
from bluepages.testdata import ChangeKind, Department

# Confidence below this is reported but marked for the AD to check rather than
# sent to a department as fact.
UNCERTAIN_BELOW = 0.6

# A ripple check is a real bulk model call per candidate. Capped per run for
# the same reason MAX_SEARCHES caps sourcing: CLAUDE.md forbids an unbounded
# per-run cost, and a feature-length revision can reject many findings.
MAX_RIPPLES = 2

# The scene number at the front of a value like "SCENE 4: ..." or "2/INT. ...".
# Anchored so a number later in a sentence is not mistaken for a scene.
_SCENE_NUMBER_RE = re.compile(r"^\s*(?:scenes?\s*)?(\d+[A-Za-z]?)\b", re.IGNORECASE)


class Finding(BaseModel):
    """One thing that changed, what it means, and who needs to know.

    This is the product's output unit. It is deliberately the same shape as a
    `LabelledChange` in the answer key, because the key is the correctness
    measure and a finding that cannot be compared to it cannot be scored.
    """

    kind: ChangeKind
    summary: str = Field(description="One sentence, in production terms")
    scene: str = Field(description="The scene number this is about")
    from_scene: str | None = None
    departments: list[Department] = Field(default_factory=list)
    # Why. The AD reads this to decide whether to trust the finding, so it has
    # to state the evidence, not restate the summary.
    reasoning: str = ""
    element: str | None = None
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    # Set by the clearance question, not by the department routing.
    risk: str | None = None

    @field_validator("scene", "from_scene", "element", mode="before")
    @classmethod
    def _as_text(cls, v: object) -> object:
        """Accept a scene number the model wrote as a number.

        Scene numbers are strings because of inserts like 34A, but a model
        looking at scene 7 writes `7`, not `"7"`. Rejecting that threw away
        correct judgments over JSON typing: every finding in a scene was lost
        because one field was unquoted.
        """
        if isinstance(v, bool):
            return v
        if isinstance(v, int | float):
            # 7.0 from a JSON float must not become "7.0".
            return str(int(v)) if float(v).is_integer() else str(v)
        return v

    @field_validator("scene", "from_scene", mode="after")
    @classmethod
    def _bare_number(cls, v: str | None) -> str | None:
        """Reduce "SCENE 4: the janitor" or "2/INT. KITCHEN" to just the number.

        The scene guard compares against the numbers the diff flagged, so a
        finding that labels its scene instead of naming it gets dropped even
        though the judgment is right. Only the leading number is taken, and a
        value that holds no number is left alone for the guard to reject.
        """
        if v is None:
            return None
        match = _SCENE_NUMBER_RE.search(v)
        return match.group(1).upper() if match else v

    @field_validator("departments", mode="before")
    @classmethod
    def _drop_unknown(cls, v: object) -> object:
        """Ignore a department the model invented rather than failing the scene.

        A finding routed to a department that does not exist is a bad route, not
        a bad finding, and losing the whole judgment over it is the worse trade.
        """
        if not isinstance(v, list):
            return v
        known = {d.value for d in Department}
        return [d for d in v if not isinstance(d, str) or d.lower() in known]

    @property
    def uncertain(self) -> bool:
        return self.confidence < UNCERTAIN_BELOW


class SceneFindings(BaseModel):
    """What the model returns for one scene.

    Strict for the same reason as `SceneElements`: a wrong-shaped response must
    not validate into an empty list, because "this scene changed nothing
    meaningful" is a real and common answer here and has to stay
    distinguishable from a failure.
    """

    model_config = ConfigDict(extra="forbid")

    findings: list[Finding]


@dataclass
class RippleQuestion:
    """A finding the model made about a scene the diff never flagged.

    This is the one legitimate path to a finding in an unflagged scene: the
    model noticed something while reasoning about a neighbouring change, and
    `semantic/ripples.py` checks it against the actual scene text rather than
    trusting or discarding it outright. Capped at MAX_RIPPLES per run.
    """

    asked_about: str
    finding: Finding
    note: str = dc_field(default="")


class SemanticResult(BaseModel):
    """Every finding across the revision, plus how it was produced."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    findings: list[Finding] = Field(default_factory=list)
    models_used: dict[str, int] = Field(default_factory=dict)
    fallbacks: int = 0
    scenes_reasoned: int = 0
    rejected: list[str] = Field(default_factory=list)
    # The subset of `rejected` worth checking as a ripple. Populated by
    # reason_about_diff, consumed by semantic/ripples.py.
    ripple_questions: list[RippleQuestion] = Field(default_factory=list)

    def by_department(self, department: Department) -> list[Finding]:
        return [f for f in self.findings if department in f.departments]

    def by_kind(self, kind: ChangeKind) -> list[Finding]:
        return [f for f in self.findings if f.kind is kind]

    @property
    def department_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for finding in self.findings:
            for department in finding.departments:
                counts[department.value] = counts.get(department.value, 0) + 1
        return dict(sorted(counts.items()))

    def summary(self) -> dict[str, object]:
        by_kind: dict[str, int] = {}
        for finding in self.findings:
            by_kind[finding.kind.value] = by_kind.get(finding.kind.value, 0) + 1
        return {
            "findings": len(self.findings),
            "scenes_reasoned": self.scenes_reasoned,
            "by_kind": dict(sorted(by_kind.items())),
            "by_department": self.department_counts,
            "uncertain": sum(1 for f in self.findings if f.uncertain),
            "models_used": dict(self.models_used),
            "fallbacks": self.fallbacks,
            "rejected": len(self.rejected),
        }


SYSTEM = """You are the 1st Assistant Director reading a script revision.

You are given the mechanical diff of one scene: the exact lines that changed. Your job is to say what those changes MEAN for the people who have to shoot them, and to route each consequence to the department that owns it.

The judgment calls that matter, in order of how expensive it is to get them wrong:

1. RELOCATED vs ADDED. If an object leaves one scene and a matching object appears in another, decide whether it is the same object moved or a cut plus a new buy. Same object = a continuity and set-dressing note. New object = a purchase order. Look at whether the wording and the object are the same, and whether the new scene treats it as already existing. Getting this wrong costs the production money in one direction and a missing prop in the other.

2. RENAMED vs NEW ROLE. If a character cue changes, decide whether the part was renamed or a new part was written. Same scene, same function, unchanged dialogue means a rename: casting must NOT be told to fill a new role. A genuinely new character with their own lines is a booking.

3. CONSEQUENCE vs PROSE. A rewritten action line may change nothing physical. "hands her the envelope" becoming "slides the envelope across the table" is the same one envelope: props are unaffected. It is a different physical action and therefore a different camera setup, which is the AD's concern, not the props department's. Say so, and do not route it to props.

4. SCHEDULE, NOT KIT. A DAY to NIGHT flip changes no object at all. It is a scheduling change and possibly a location re-quote, because a night shoot at a practical location is priced differently. Routing it to props is noise.

5. CLEARANCE. Any real brand, song title, book title, artwork or recognisable building introduced by this revision is a rights liability. Flag it with a risk level. Caught at script stage it is a phone call; caught after the shoot it is a reshoot.

6. SOCIAL. Route to social only when a change is genuinely newsworthy to an audience following the production: a new location, a striking new prop or vehicle, a notable cast change. An ordinary rewrite, a scheduling flip, or a continuity fix is not social's business, and routing routine changes there is the same noise problem as over-routing to any other department.

Rules:
- Report only what the given diff shows. Never infer a change you were not shown.
- One finding per real change. Do not split a rename into a cue finding and a dialogue finding.
- A dialogue rewrite with no physical consequence is usually not worth a finding at all. Return an empty list rather than manufacturing one.
- Route to the departments that must act. Routing broadly is not caution, it is noise, and a department that gets noise stops reading.
- confidence is your own: 0.9+ when the text is unambiguous, below 0.6 when you are genuinely unsure and the AD should check.

Return only JSON matching the schema. No prose outside it."""


_KIND_VALUES = ", ".join(k.value for k in ChangeKind)
# Finance is a consumer, not a routing target: it restates figures already
# computed from decisions and budget rows, and never reasons about a finding
# directly. Excluding it from what the model can route to is what keeps that
# true, per DECISIONS.md.
_DEPT_VALUES = ", ".join(d.value for d in Department if d is not Department.FINANCE)

_RESPONSE_HINT = (
    'Return JSON: {"findings": [{"kind": ..., "summary": ..., "scene": ..., '
    '"from_scene": ..., "departments": [...], "reasoning": ..., "element": ..., '
    '"confidence": 0.0-1.0, "risk": "low"|"medium"|"high"|null}]}\n'
    f"kind is one of: {_KIND_VALUES}\n"
    f"departments are from: {_DEPT_VALUES}"
)


def _changed_scene_prompt(
    scene: SceneDiff,
    diff: DraftDiff,
    elements: DraftElements | None,
) -> str:
    """The evidence for one changed scene: heading, spans, and relocation hints."""
    pair = scene.pair
    lines = [f"SCENE {scene.number}", ""]

    if scene.heading_changed and pair.before and pair.after:
        lines.append("HEADING CHANGED:")
        lines.append(f"  was: {pair.before.heading}")
        lines.append(f"  now: {pair.after.heading}")
        lines.append("")

    if scene.spans:
        lines.append("LINES CHANGED:")
        for span in scene.spans:
            lines.append(f"  [{span.kind.value} {span.element_type.value}]")
            if span.before:
                lines.append(f"    was: {span.before.text}")
            if span.after:
                lines.append(f"    now: {span.after.text}")
        lines.append("")

    # Relocation candidates touching this scene, phrased as the open question
    # they are. The evidence is mechanical; the identity call is the model's.
    related = [
        c
        for c in diff.relocation_candidates
        if c.from_scene == scene.number or c.to_scene == scene.number
    ]
    if related:
        lines.append("POSSIBLE RELOCATIONS (decide: same object moved, or cut plus new buy):")
        for candidate in related:
            lines.append(
                f"  {candidate.phrase!r} left scene {candidate.from_scene} "
                f"and appears in scene {candidate.to_scene}"
            )
        lines.append("")

    if elements is not None:
        scene_elements = elements.by_scene().get(scene.number)
        if scene_elements and scene_elements.elements:
            lines.append("ELEMENTS EXTRACTED FROM THIS SCENE IN THE NEW DRAFT:")
            for element in scene_elements.elements:
                mark = " [branded]" if element.branded else ""
                lines.append(f"  {element.category.value}: {element.name}{mark}")
            lines.append("")

    lines.append(_RESPONSE_HINT)
    return "\n".join(lines)


def _structural_scene_prompt(
    pair_kind: AlignmentKind,
    number: str,
    heading: str,
    body: str,
    elements: DraftElements | None,
) -> str:
    """Evidence for a scene that was inserted or omitted whole.

    These carry no spans: the alignment already describes them fully. They still
    need judgment, because an inserted scene brings a location, a vehicle and a
    cast day with it, and an omitted one releases them.
    """
    if pair_kind is AlignmentKind.OMITTED:
        lines = [
            f"SCENE {number} IS MARKED OMITTED IN THE NEW DRAFT.",
            "",
            f"It was: {heading}",
            "",
            body,
            "",
            "The scene number survives, which is the industry convention. Say what "
            "this releases and who needs to know.",
            "",
        ]
    else:
        lines = [
            f"SCENE {number} IS NEW IN THIS DRAFT.",
            "",
            f"Heading: {heading}",
            "",
            body,
            "",
            "Say what this scene newly requires and who needs to know. Flag any "
            "brand, song, book, artwork or recognisable building it introduces.",
            "",
        ]

    if elements is not None:
        scene_elements = elements.by_scene().get(number)
        if scene_elements and scene_elements.elements:
            lines.append("ELEMENTS EXTRACTED FROM THIS SCENE:")
            for element in scene_elements.elements:
                mark = " [branded]" if element.branded else ""
                lines.append(f"  {element.category.value}: {element.name}{mark}")
            lines.append("")

    lines.append(_RESPONSE_HINT)
    return "\n".join(lines)


def reason_about_diff(
    diff: DraftDiff,
    client: ModelClient,
    elements: DraftElements | None = None,
    stream: EventStream | None = None,
    max_workers: int = 3,
) -> SemanticResult:
    """Reason over every changed, inserted and omitted scene.

    One call per scene rather than one call for the whole revision. A single
    call over a 120-scene diff would not fit, and more importantly a model asked
    to summarise a whole draft invents changes; a model shown one scene's diff
    and asked what it means does not.
    """
    stream = stream or NullStream()
    alignment = diff.alignment

    jobs: list[tuple[str, str]] = []  # (scene number, prompt)

    for scene in diff.scenes:
        jobs.append((scene.number, _changed_scene_prompt(scene, diff, elements)))

    for pair in alignment.of_kind(AlignmentKind.OMITTED):
        before = pair.before
        jobs.append((
            pair.number,
            _structural_scene_prompt(
                AlignmentKind.OMITTED,
                pair.number,
                before.heading if before else "",
                before.full_text if before else "",
                elements,
            ),
        ))

    for pair in alignment.of_kind(AlignmentKind.INSERTED):
        after = pair.after
        jobs.append((
            pair.number,
            _structural_scene_prompt(
                AlignmentKind.INSERTED,
                pair.number,
                after.heading if after else "",
                after.full_text if after else "",
                elements,
            ),
        ))

    stream.emit(
        EventKind.INFO,
        f"reasoning over {len(jobs)} changed scenes",
        scenes=len(jobs),
    )

    result = SemanticResult(scenes_reasoned=len(jobs))
    changed_numbers = _scenes_the_diff_flagged(diff, alignment)

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(_reason_one, number, prompt, client, stream): number
            for number, prompt in jobs
        }
        for future in concurrent.futures.as_completed(futures):
            number = futures[future]
            try:
                findings, model_name, via_fallback = future.result()
            except SchemaError as exc:
                stream.emit(
                    EventKind.PARSE_WARNING,
                    f"scene {number}: reasoning returned unusable JSON, skipped",
                    scene_number=number,
                    error=str(exc)[:200],
                )
                continue

            result.models_used[model_name] = result.models_used.get(model_name, 0) + 1
            if via_fallback:
                result.fallbacks += 1

            kept, rejected, ripples = _reject_unfounded(findings, number, changed_numbers)
            result.rejected.extend(rejected)
            if len(result.ripple_questions) < MAX_RIPPLES:
                result.ripple_questions.extend(
                    ripples[: MAX_RIPPLES - len(result.ripple_questions)]
                )
            for note in rejected:
                stream.emit(
                    EventKind.PARSE_WARNING,
                    f"dropped a finding about a scene the diff did not flag: {note}",
                    scene_number=number,
                )
            for finding in kept:
                result.findings.append(finding)
                stream.emit(
                    EventKind.CHANGE_DETECTED,
                    f"scene {finding.scene}: {finding.kind.value} -> "
                    f"{', '.join(d.value for d in finding.departments) or 'unrouted'}"
                    + ("  [uncertain]" if finding.uncertain else ""),
                    scene_number=finding.scene,
                    kind=finding.kind.value,
                    departments=[d.value for d in finding.departments],
                    confidence=finding.confidence,
                    semantic=True,
                )

    result.findings.sort(key=lambda f: (_scene_sort_key(f.scene), f.kind.value))
    stream.emit(
        EventKind.INFO,
        f"{len(result.findings)} findings across "
        f"{len(result.department_counts)} departments",
        semantic=result.summary(),
    )
    return result


def _reason_one(
    number: str,
    prompt: str,
    client: ModelClient,
    stream: EventStream,
) -> tuple[list[Finding], str, bool]:
    """One scene's judgment call. Sonnet-class: this is the product."""
    # A scene with several findings needs room, and a reasoning model spends
    # part of the budget thinking before it writes any JSON: the old hardcoded
    # 2000 truncated mid-object and lost the whole scene's judgment. Explicit
    # rather than defaulted, because CLAUDE.md requires every call to set it.
    settings = getattr(client, "settings", None)
    max_tokens = getattr(settings, "bluepages_max_tokens", 4096)
    completion = client.complete(
        prompt=prompt,
        system=SYSTEM,
        judgment=True,
        max_tokens=max_tokens,
        cache_key_extra="reason-v1",
        label=f"scene {number}",
    )
    parsed = parse_as(completion.text, SceneFindings)
    return parsed.findings, completion.model_name, completion.via_fallback


def _scenes_the_diff_flagged(diff: DraftDiff, alignment: Alignment) -> set[str]:
    """Scene numbers the mechanical layer actually found a change in."""
    return (
        {s.number for s in diff.scenes}
        | {p.number for p in alignment.of_kind(AlignmentKind.OMITTED)}
        | {p.number for p in alignment.of_kind(AlignmentKind.INSERTED)}
        | {p.number for p in alignment.of_kind(AlignmentKind.REMOVED)}
    )


def _reject_unfounded(
    findings: list[Finding],
    asked_about: str,
    flagged: set[str],
) -> tuple[list[Finding], list[str], list[RippleQuestion]]:
    """Drop findings about scenes the diff never flagged.

    The guard against the failure mode that matters most here: a model given one
    scene's evidence and asked what it means will occasionally answer about the
    draft in general. A confident finding about a scene that did not change is
    worse than no finding, because the AD has no way to tell it apart from a
    real one.

    Each rejection is also a ripple candidate: the model noticed a consequence
    in a scene nobody asked about. `semantic/ripples.py` decides which of
    these are worth checking against the real scene text.
    """
    kept: list[Finding] = []
    rejected: list[str] = []
    ripples: list[RippleQuestion] = []
    for finding in findings:
        # A relocation legitimately names two scenes, so both are checked.
        scenes = [s for s in (finding.scene, finding.from_scene) if s]
        if all(s in flagged for s in scenes):
            kept.append(finding)
        else:
            note = (
                f"asked about scene {asked_about}, answered about "
                f"{'/'.join(scenes) or '(no scene)'}: {finding.summary[:80]}"
            )
            rejected.append(note)
            if scenes:
                ripples.append(RippleQuestion(asked_about=asked_about, finding=finding, note=note))
    return kept, rejected, ripples


def _scene_sort_key(number: str) -> tuple[int, str]:
    digits = "".join(c for c in number if c.isdigit())
    return (int(digits) if digits else 10**9, number)


__all__ = [
    "MAX_RIPPLES",
    "UNCERTAIN_BELOW",
    "Finding",
    "RippleQuestion",
    "SceneFindings",
    "SemanticResult",
    "reason_about_diff",
]
