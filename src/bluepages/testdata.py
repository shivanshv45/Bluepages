"""Loading and validating the Layer 2 answer key.

The answer key is the only reason Layer 3 has a correctness measure rather than
an opinion. That makes it load-bearing, and a key that has drifted away from the
fixtures it describes is worse than no key at all: it reports success against
scenes that no longer exist.

So the key is typed, and `validate` checks every claim in it against the actual
parsed drafts before any scoring uses it.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from bluepages.model import Screenplay
from bluepages.parse import parse_fdx


class ChangeKind(str, Enum):
    """The kinds of change the answer key can assert.

    These name what happened in production terms, not in diff terms. The
    difference between `element_relocated` and `element_added` is the whole
    judgment the product exists to make.
    """

    ELEMENT_RELOCATED = "element_relocated"
    ELEMENT_ADDED = "element_added"
    ELEMENT_REMOVED = "element_removed"
    CHARACTER_RENAMED = "character_renamed"
    ACTION_REWRITTEN = "action_rewritten"
    TIME_OF_DAY_CHANGED = "time_of_day_changed"
    SCENE_OMITTED = "scene_omitted"
    SCENE_INSERTED = "scene_inserted"
    SCENE_MOVED = "scene_moved"
    CLEARANCE_RISK = "clearance_risk"


class Department(str, Enum):
    """Who receives a finding.

    Four departments first, per DECISIONS.md, plus the three non-department
    consumers of the same diff: clearance, schedule, and the AD. Social is a
    fifth routed department: it needs judgment (is this newsworthy, write the
    copy), unlike Finance, which only restates numbers already computed
    elsewhere and never receives findings directly.
    """

    PROPS = "props"
    WARDROBE = "wardrobe"
    TRANSPORT = "transport"
    LOCATIONS = "locations"
    CAST = "cast"
    ART = "art"
    STUNTS = "stunts"
    SFX = "sfx"
    CLEARANCE = "clearance"
    SCHEDULE = "schedule"
    AD = "ad"
    SOCIAL = "social"
    FINANCE = "finance"


class LabelledChange(BaseModel):
    """One change, with the judgment it is testing and who should hear about it."""

    id: str
    kind: ChangeKind
    summary: str
    from_scene: str | None = None
    to_scene: str | None = None
    departments: list[Department] = Field(default_factory=list)
    # Why the correct answer is correct. Not scored directly, but it is what
    # makes a failure diagnosable rather than just a red mark.
    judgment: str = ""
    # Phrases that would indicate the model reached the wrong conclusion, e.g.
    # calling a relocated prop a new buy. Scored: these are the expensive errors.
    must_not_say: list[str] = Field(default_factory=list)

    element: str | None = None
    from_name: str | None = None
    to_name: str | None = None
    from_value: str | None = None
    to_value: str | None = None
    risk: str | None = None


class AnswerKey(BaseModel):
    """The labelled ground truth for one revision pair."""

    pair: str
    draft_from: str
    draft_to: str
    note: str = ""
    changes: list[LabelledChange]
    unchanged_scenes: list[str] = Field(default_factory=list)
    expected_department_counts: dict[Department, int] = Field(default_factory=dict)

    @property
    def change_ids(self) -> set[str]:
        return {c.id for c in self.changes}

    def by_department(self, department: Department) -> list[LabelledChange]:
        return [c for c in self.changes if department in c.departments]

    def by_kind(self, kind: ChangeKind) -> list[LabelledChange]:
        return [c for c in self.changes if c.kind is kind]

    def scenes_touched(self) -> set[str]:
        touched: set[str] = set()
        for change in self.changes:
            touched.update(s for s in (change.from_scene, change.to_scene) if s)
        return touched


def load_answer_key(path: str | Path) -> AnswerKey:
    """Read and type-check an answer key."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"{path}: answer key not found. Run "
            "`python scripts/make_revision_pair.py --out tests/fixtures`"
        )
    return AnswerKey.model_validate_json(path.read_text(encoding="utf-8"))


def load_pair(key: AnswerKey, fixtures: str | Path) -> tuple[Screenplay, Screenplay]:
    """Parse the two drafts the key describes."""
    fixtures = Path(fixtures)
    return (
        parse_fdx(fixtures / key.draft_from),
        parse_fdx(fixtures / key.draft_to),
    )


def validate(key: AnswerKey, fixtures: str | Path) -> list[str]:
    """Check every claim in the key against the actual drafts.

    Returns a list of problems; empty means the key describes reality. This is
    what stops a key from silently rotting as the fixtures change.
    """
    problems: list[str] = []
    before, after = load_pair(key, fixtures)

    before_numbers = {str(s.number) for s in before.scenes if s.number}
    after_numbers = {str(s.number) for s in after.scenes if s.number}

    for change in key.changes:
        # Every referenced scene must exist in the draft it is claimed to be in.
        if change.from_scene and change.from_scene not in before_numbers:
            problems.append(
                f"{change.id}: from_scene {change.from_scene!r} is not in {key.draft_from}"
            )
        if change.to_scene and change.to_scene not in after_numbers:
            problems.append(
                f"{change.id}: to_scene {change.to_scene!r} is not in {key.draft_to}"
            )

        # Each kind carries a claim that can be checked directly.
        if change.kind is ChangeKind.SCENE_OMITTED and change.to_scene:
            scene = after.scene(change.to_scene)
            if scene is not None and not scene.omitted:
                problems.append(
                    f"{change.id}: scene {change.to_scene} is not marked OMITTED in "
                    f"{key.draft_to}"
                )

        if (
            change.kind is ChangeKind.SCENE_INSERTED
            and change.to_scene
            and change.to_scene in before_numbers
        ):
            problems.append(
                f"{change.id}: scene {change.to_scene} already exists in "
                f"{key.draft_from}, so it is not an insert"
            )

        if change.kind is ChangeKind.TIME_OF_DAY_CHANGED and change.from_scene:
            old = before.scene(change.from_scene)
            new = after.scene(change.to_scene or change.from_scene)
            if old is not None and new is not None:
                if old.time_of_day is new.time_of_day:
                    problems.append(
                        f"{change.id}: scene {change.from_scene} has the same "
                        f"time of day in both drafts ({old.time_of_day.value})"
                    )
                if change.from_value and old.time_of_day.value != change.from_value:
                    problems.append(
                        f"{change.id}: expected {change.from_value} in "
                        f"{key.draft_from}, found {old.time_of_day.value}"
                    )
                if change.to_value and new.time_of_day.value != change.to_value:
                    problems.append(
                        f"{change.id}: expected {change.to_value} in "
                        f"{key.draft_to}, found {new.time_of_day.value}"
                    )

        if change.kind is ChangeKind.CHARACTER_RENAMED:
            if change.from_name and change.from_name not in before.all_characters:
                problems.append(
                    f"{change.id}: {change.from_name!r} does not speak in {key.draft_from}"
                )
            if change.to_name and change.to_name not in after.all_characters:
                problems.append(
                    f"{change.id}: {change.to_name!r} does not speak in {key.draft_to}"
                )
            if change.from_name and change.from_name in after.all_characters:
                problems.append(
                    f"{change.id}: {change.from_name!r} still speaks in "
                    f"{key.draft_to}, so it was not renamed"
                )

        if change.kind is ChangeKind.ELEMENT_RELOCATED and change.element:
            term = change.element.lower()
            if change.from_scene:
                old = before.scene(change.from_scene)
                if old is not None and term not in old.full_text.lower():
                    problems.append(
                        f"{change.id}: {change.element!r} is not in scene "
                        f"{change.from_scene} of {key.draft_from}"
                    )
            if change.to_scene:
                new = after.scene(change.to_scene)
                if new is not None and term not in new.full_text.lower():
                    problems.append(
                        f"{change.id}: {change.element!r} is not in scene "
                        f"{change.to_scene} of {key.draft_to}"
                    )

        if change.kind is ChangeKind.ELEMENT_ADDED and change.element and change.to_scene:
            term = change.element.lower()
            new = after.scene(change.to_scene)
            if new is not None and term not in new.full_text.lower():
                problems.append(
                    f"{change.id}: {change.element!r} is not in scene "
                    f"{change.to_scene} of {key.draft_to}"
                )
            # A genuinely added element must be absent from the earlier draft.
            if term in "\n".join(s.full_text for s in before.scenes).lower():
                problems.append(
                    f"{change.id}: {change.element!r} already appears in "
                    f"{key.draft_from}, so it is not an addition"
                )

        if change.kind is ChangeKind.CLEARANCE_RISK and change.element and change.to_scene:
            new = after.scene(change.to_scene)
            if new is not None and change.element.lower() not in new.full_text.lower():
                problems.append(
                    f"{change.id}: {change.element!r} is not in scene "
                    f"{change.to_scene} of {key.draft_to}"
                )

    # Scenes claimed unchanged must actually be identical.
    for number in key.unchanged_scenes:
        old = before.scene(number)
        new = after.scene(number)
        if old is None or new is None:
            problems.append(f"unchanged_scenes: scene {number} missing from one draft")
        elif old.full_text != new.full_text:
            problems.append(f"unchanged_scenes: scene {number} differs between drafts")

    # The declared per-department totals must match the changes listed.
    for department, expected in key.expected_department_counts.items():
        actual = len(key.by_department(department))
        if actual != expected:
            problems.append(
                f"expected_department_counts: {department.value} says {expected}, "
                f"changes list {actual}"
            )

    # Duplicate ids would make scoring ambiguous.
    if len(key.change_ids) != len(key.changes):
        problems.append("duplicate change ids")

    return problems


def summarise(key: AnswerKey) -> dict[str, Any]:
    """A printable summary of what the key asserts."""
    by_kind: dict[str, int] = {}
    by_dept: dict[str, int] = {}
    for change in key.changes:
        by_kind[change.kind.value] = by_kind.get(change.kind.value, 0) + 1
        for department in change.departments:
            by_dept[department.value] = by_dept.get(department.value, 0) + 1
    return {
        "pair": key.pair,
        "drafts": f"{key.draft_from} -> {key.draft_to}",
        "changes": len(key.changes),
        "by_kind": dict(sorted(by_kind.items())),
        "by_department": dict(sorted(by_dept.items())),
        "unchanged_scenes": key.unchanged_scenes,
        "scenes_touched": sorted(key.scenes_touched()),
    }


__all__ = [
    "AnswerKey",
    "ChangeKind",
    "Department",
    "LabelledChange",
    "load_answer_key",
    "load_pair",
    "summarise",
    "validate",
]
