"""Scene alignment across drafts (Layer 3.1).

Alignment comes before text comparison. Get it wrong and everything downstream
is noise: a misaligned pair reports every scene as rewritten, and the semantic
layer then reasons carefully about changes that never happened.

The anchor is `@Number`, which is stable across drafts by industry convention.
That convention is exactly why scenes are marked `OMITTED` rather than deleted:
deleting scene 34 would renumber everything after it and destroy the anchor.

Four cases have to be handled, and each is a different production fact:

    matched    the same scene in both drafts, changed or not
    omitted    present in both, but marked OMITTED in the newer draft
    inserted   new in the newer draft, usually `34A` between 34 and 35
    removed    gone from the newer draft without being marked OMITTED

The last is worth distinguishing rather than folding into `omitted`. A scene
that vanished without the marker means either a non-conforming draft or a
renumbering, and the AD needs to know which, because the second invalidates
every scene number downstream.

When a draft carries no scene numbers at all, alignment falls back to matching
on heading and content. That is a weaker result and is reported as such: a
pre-production draft is a different quality of input, not an error.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from enum import Enum

from bluepages.events import EventKind, EventStream, NullStream
from bluepages.model import Scene, Screenplay


class AlignmentKind(str, Enum):
    """What happened to one scene between two drafts."""

    MATCHED = "matched"
    OMITTED = "omitted"
    INSERTED = "inserted"
    REMOVED = "removed"


class AlignmentMethod(str, Enum):
    """How the pairing was established. Confidence descends down this list."""

    SCENE_NUMBER = "scene_number"   # the anchor: stable by convention
    HEADING = "heading"             # unnumbered draft: slugline match
    CONTENT = "content"             # last resort: text similarity


@dataclass
class ScenePair:
    """One aligned pair: a scene in the old draft, the new one, or both."""

    kind: AlignmentKind
    method: AlignmentMethod
    before: Scene | None = None
    after: Scene | None = None
    # 0..1, how confident the pairing is. Exact number matches are 1.0.
    confidence: float = 1.0

    @property
    def number(self) -> str:
        """The scene number this pair is about, as written."""
        scene = self.after or self.before
        if scene is None or scene.number is None:
            return "?"
        return str(scene.number)

    @property
    def moved(self) -> bool:
        """Whether the scene sits at a different position in the new draft.

        A scene can keep its number and still move, which is a schedule fact:
        the shooting order changes even though nothing in the scene did.
        """
        if self.before is None or self.after is None:
            return False
        return self.before.index != self.after.index

    @property
    def text_changed(self) -> bool:
        """Whether the scene's text differs at all. The cheap first question."""
        if self.before is None or self.after is None:
            return True
        return self.before.full_text != self.after.full_text

    @property
    def heading_changed(self) -> bool:
        if self.before is None or self.after is None:
            return False
        return self.before.heading != self.after.heading

    @property
    def time_of_day_changed(self) -> bool:
        """DAY -> NIGHT: a scheduling change, and possibly a location re-quote.

        An omitted scene is excluded: a bare OMITTED marker has no slugline, so
        its parsed time-of-day is UNKNOWN and would otherwise read as a flip.
        The scene being cut is the finding; a phantom DAY -> UNKNOWN is noise.
        """
        if self.before is None or self.after is None or self.kind is AlignmentKind.OMITTED:
            return False
        return self.before.time_of_day is not self.after.time_of_day

    @property
    def location_changed(self) -> bool:
        """Same exclusion as `time_of_day_changed`: an OMITTED scene has none."""
        if self.before is None or self.after is None or self.kind is AlignmentKind.OMITTED:
            return False
        return self.before.location != self.after.location

    @property
    def heading_fields_changed(self) -> bool:
        """Any slugline field differing. What locations and scheduling read."""
        return self.time_of_day_changed or self.location_changed

    def __repr__(self) -> str:
        return f"<ScenePair {self.number} {self.kind.value}>"


@dataclass
class Alignment:
    """The full alignment of two drafts."""

    pairs: list[ScenePair] = field(default_factory=list)
    method: AlignmentMethod = AlignmentMethod.SCENE_NUMBER
    # Set when the drafts could not be aligned on scene numbers.
    degraded: bool = False
    notes: list[str] = field(default_factory=list)

    def of_kind(self, kind: AlignmentKind) -> list[ScenePair]:
        return [p for p in self.pairs if p.kind is kind]

    @property
    def matched(self) -> list[ScenePair]:
        return self.of_kind(AlignmentKind.MATCHED)

    @property
    def changed(self) -> list[ScenePair]:
        """Matched pairs whose text actually differs. What the diff works on."""
        return [p for p in self.matched if p.text_changed]

    @property
    def unchanged(self) -> list[ScenePair]:
        return [p for p in self.matched if not p.text_changed]

    def summary(self) -> dict[str, int | str | bool]:
        return {
            "method": self.method.value,
            "degraded": self.degraded,
            "total": len(self.pairs),
            "matched": len(self.matched),
            "changed": len(self.changed),
            "unchanged": len(self.unchanged),
            "omitted": len(self.of_kind(AlignmentKind.OMITTED)),
            "inserted": len(self.of_kind(AlignmentKind.INSERTED)),
            "removed": len(self.of_kind(AlignmentKind.REMOVED)),
            "moved": sum(1 for p in self.matched if p.moved),
        }


def align(
    before: Screenplay,
    after: Screenplay,
    stream: EventStream | None = None,
) -> Alignment:
    """Align two drafts scene by scene.

    Uses scene numbers when both drafts carry them, which is the normal case for
    anything in production. Falls back to heading matching otherwise, and says
    so rather than pretending the result is as good.
    """
    stream = stream or NullStream()
    stream.emit(
        EventKind.ALIGN_STARTED,
        f"aligning {before.scene_count} scenes against {after.scene_count}",
        before_scenes=before.scene_count,
        after_scenes=after.scene_count,
    )

    if before.has_scene_numbers and after.has_scene_numbers:
        alignment = _align_by_number(before, after, stream)
    else:
        alignment = _align_by_heading(before, after, stream)
        alignment.degraded = True
        note = (
            "one or both drafts have no scene numbers; aligned on headings "
            "instead, which cannot distinguish a rewritten scene from a "
            "replaced one"
        )
        alignment.notes.append(note)
        stream.emit(EventKind.PARSE_WARNING, note)

    for pair in alignment.pairs:
        stream.emit(
            EventKind.SCENE_ALIGNED,
            f"scene {pair.number}: {pair.kind.value}",
            scene_number=pair.number,
            kind=pair.kind.value,
            method=pair.method.value,
            text_changed=pair.text_changed,
            moved=pair.moved,
        )

    summary = alignment.summary()
    # Nested rather than splatted: the summary has its own `method` and `kind`
    # vocabulary, and splatting it would collide with `emit`'s parameters.
    stream.emit(
        EventKind.INFO,
        f"aligned: {summary['matched']} matched "
        f"({summary['changed']} changed), {summary['inserted']} inserted, "
        f"{summary['omitted']} omitted, {summary['removed']} removed",
        alignment=summary,
    )
    return alignment


def _align_by_number(
    before: Screenplay,
    after: Screenplay,
    stream: EventStream,
) -> Alignment:
    """The primary path: match on `@Number`, the stable anchor."""
    alignment = Alignment(method=AlignmentMethod.SCENE_NUMBER)

    before_by_number = {str(s.number): s for s in before.scenes if s.number}
    after_by_number = {str(s.number): s for s in after.scenes if s.number}

    for number, old_scene in before_by_number.items():
        new_scene = after_by_number.get(number)

        if new_scene is None:
            # Gone without an OMITTED marker. Either a non-conforming draft or a
            # renumbering, and the difference matters: a renumbering invalidates
            # every scene number after it.
            alignment.pairs.append(
                ScenePair(
                    kind=AlignmentKind.REMOVED,
                    method=AlignmentMethod.SCENE_NUMBER,
                    before=old_scene,
                )
            )
            continue

        # Marked OMITTED in the newer draft: the scene was cut, and its number
        # deliberately survives so nothing after it renumbers.
        if new_scene.omitted and not old_scene.omitted:
            alignment.pairs.append(
                ScenePair(
                    kind=AlignmentKind.OMITTED,
                    method=AlignmentMethod.SCENE_NUMBER,
                    before=old_scene,
                    after=new_scene,
                )
            )
            continue

        alignment.pairs.append(
            ScenePair(
                kind=AlignmentKind.MATCHED,
                method=AlignmentMethod.SCENE_NUMBER,
                before=old_scene,
                after=new_scene,
            )
        )

    for number, new_scene in after_by_number.items():
        if number not in before_by_number:
            alignment.pairs.append(
                ScenePair(
                    kind=AlignmentKind.INSERTED,
                    method=AlignmentMethod.SCENE_NUMBER,
                    after=new_scene,
                )
            )

    alignment.pairs.sort(key=_pair_sort_key)
    return alignment


def _align_by_heading(
    before: Screenplay,
    after: Screenplay,
    stream: EventStream,
) -> Alignment:
    """Fallback for unnumbered drafts.

    Sluglines are matched in sequence with `difflib`, so the scene order carries
    the alignment where numbers cannot. This cannot tell a heavily rewritten
    scene from a replaced one, which is why the result is marked degraded.
    """
    alignment = Alignment(method=AlignmentMethod.HEADING)

    before_keys = [_heading_key(s) for s in before.scenes]
    after_keys = [_heading_key(s) for s in after.scenes]

    matcher = difflib.SequenceMatcher(None, before_keys, after_keys, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for offset in range(i2 - i1):
                old_scene = before.scenes[i1 + offset]
                new_scene = after.scenes[j1 + offset]
                kind = (
                    AlignmentKind.OMITTED
                    if new_scene.omitted and not old_scene.omitted
                    else AlignmentKind.MATCHED
                )
                alignment.pairs.append(
                    ScenePair(
                        kind=kind,
                        method=AlignmentMethod.HEADING,
                        before=old_scene,
                        after=new_scene,
                        confidence=0.8,
                    )
                )
        elif tag == "replace":
            # Same slot, different heading: pair them up positionally as far as
            # they go, then treat the remainder as inserts or removals.
            paired = min(i2 - i1, j2 - j1)
            for offset in range(paired):
                old_scene = before.scenes[i1 + offset]
                new_scene = after.scenes[j1 + offset]
                alignment.pairs.append(
                    ScenePair(
                        kind=AlignmentKind.MATCHED,
                        method=AlignmentMethod.CONTENT,
                        before=old_scene,
                        after=new_scene,
                        confidence=_similarity(old_scene, new_scene),
                    )
                )
            for offset in range(paired, i2 - i1):
                alignment.pairs.append(
                    ScenePair(
                        kind=AlignmentKind.REMOVED,
                        method=AlignmentMethod.HEADING,
                        before=before.scenes[i1 + offset],
                        confidence=0.6,
                    )
                )
            for offset in range(paired, j2 - j1):
                alignment.pairs.append(
                    ScenePair(
                        kind=AlignmentKind.INSERTED,
                        method=AlignmentMethod.HEADING,
                        after=after.scenes[j1 + offset],
                        confidence=0.6,
                    )
                )
        elif tag == "delete":
            for index in range(i1, i2):
                alignment.pairs.append(
                    ScenePair(
                        kind=AlignmentKind.REMOVED,
                        method=AlignmentMethod.HEADING,
                        before=before.scenes[index],
                        confidence=0.7,
                    )
                )
        elif tag == "insert":
            for index in range(j1, j2):
                alignment.pairs.append(
                    ScenePair(
                        kind=AlignmentKind.INSERTED,
                        method=AlignmentMethod.HEADING,
                        after=after.scenes[index],
                        confidence=0.7,
                    )
                )

    return alignment


def _heading_key(scene: Scene) -> str:
    """A comparable identity for a scene without a number.

    Built from the parsed heading fields rather than the raw slugline, so
    reformatting alone does not look like a new scene.
    """
    return f"{scene.int_ext.value}|{scene.location}|{scene.time_of_day.value}"


def _similarity(before: Scene, after: Scene) -> float:
    """How alike two scenes are textually, 0..1."""
    return difflib.SequenceMatcher(
        None, before.full_text, after.full_text, autojunk=False
    ).ratio()


def _pair_sort_key(pair: ScenePair) -> tuple[int, str, int]:
    """Order pairs the way a script supervisor reads them: 5, 5A, 6.

    Falls back to draft position when a pair has no number, so nothing is
    dropped from the report.
    """
    scene = pair.after or pair.before
    if scene is None:
        return (10**9, "", 0)
    if scene.number is None:
        return (10**9, "", scene.index)
    number, suffix, _prefix = scene.number.sort_key
    return (number, suffix, scene.index)


__all__ = [
    "Alignment",
    "AlignmentKind",
    "AlignmentMethod",
    "ScenePair",
    "align",
]
