"""The mechanical diff (Layer 3.2).

`difflib` inside each aligned scene, producing raw change spans. This is the
substrate the semantic layer reasons over, not an answer in itself. The
distinction is the whole product: a diff can say the words "letter opener"
disappeared from scene 3 and appeared in scene 91, but only the semantic layer
can say whether that is one object that moved or a cut and a new buy.

So this module is deliberately conservative. It reports what changed, typed by
element, with enough structure for Layer 3.4 to reason over. It draws no
conclusions about meaning, and in particular it never claims an element was
"moved": cross-scene identity is a judgment, and asserting it here would bake
the wrong answer into the substrate.

Two things it does do, because both are mechanical facts rather than judgments:

- classifies a changed element by its type, so a dialogue-only rewrite is
  distinguishable from an action change without a model call
- surfaces candidate relocations, phrased as candidates, by noting text that
  left one scene and appeared in another. Layer 3.4 decides whether they are
  the same object.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from enum import Enum

from bluepages.diff.align import Alignment, ScenePair
from bluepages.events import EventKind, EventStream, NullStream
from bluepages.model import Element, ElementType


class SpanKind(str, Enum):
    """What happened to one element within a scene."""

    ADDED = "added"
    REMOVED = "removed"
    REPLACED = "replaced"


@dataclass
class ChangeSpan:
    """One element-level change inside an aligned scene.

    `before` and `after` hold the elements themselves rather than bare strings,
    so the semantic layer can see the element type and speaker without
    re-parsing.
    """

    kind: SpanKind
    scene_number: str
    element_type: ElementType
    before: Element | None = None
    after: Element | None = None
    # Word-level detail for a replacement, so a one-word change in a long
    # paragraph does not have to be re-derived downstream.
    words_removed: list[str] = field(default_factory=list)
    words_added: list[str] = field(default_factory=list)

    @property
    def before_text(self) -> str:
        return self.before.text if self.before else ""

    @property
    def after_text(self) -> str:
        return self.after.text if self.after else ""

    @property
    def is_dialogue_only(self) -> bool:
        """A dialogue or parenthetical change with no action involved.

        Useful as a cheap filter: a dialogue rewrite usually has no physical
        consequence, so these can be batched to the cheap model rather than
        each earning a judgment call.
        """
        return self.element_type in (ElementType.DIALOGUE, ElementType.PARENTHETICAL)

    @property
    def speaker(self) -> str | None:
        for element in (self.after, self.before):
            if element is not None and element.speaker:
                return element.speaker
        return None

    def __repr__(self) -> str:
        return f"<ChangeSpan {self.scene_number} {self.kind.value} {self.element_type.value}>"


@dataclass
class RelocationCandidate:
    """Text that left one scene and appeared in another.

    Deliberately named a *candidate*. Whether the letter opener in scene 91 is
    the one that left scene 31 is the judgment Layer 3.4 exists to make; this
    only supplies the evidence that the question is worth asking.
    """

    phrase: str
    from_scene: str
    to_scene: str
    # How much of the surrounding wording survived, 0..1. High similarity is
    # evidence for the same object, not proof of it.
    similarity: float = 0.0

    def __repr__(self) -> str:
        return f"<Relocation? {self.phrase!r} {self.from_scene}->{self.to_scene}>"


@dataclass
class SceneDiff:
    """Every change inside one aligned scene."""

    pair: ScenePair
    spans: list[ChangeSpan] = field(default_factory=list)

    @property
    def number(self) -> str:
        return self.pair.number

    @property
    def heading_changed(self) -> bool:
        """Whether the slugline changed.

        A DAY -> NIGHT flip changes no element text at all, so it produces zero
        spans. It is still one of the most consequential changes a revision can
        carry, so the scene must not be dropped for having an empty span list.
        """
        return self.pair.heading_changed

    @property
    def has_changes(self) -> bool:
        """Whether this scene changed at all, in its body or its slugline."""
        return bool(self.spans) or self.heading_changed

    @property
    def has_action_change(self) -> bool:
        """Whether anything physical changed. Action lines carry the kit."""
        return any(s.element_type is ElementType.ACTION for s in self.spans)

    @property
    def dialogue_only(self) -> bool:
        """True when every span is dialogue. Usually no physical consequence."""
        return bool(self.spans) and all(s.is_dialogue_only for s in self.spans)

    def of_kind(self, kind: SpanKind) -> list[ChangeSpan]:
        return [s for s in self.spans if s.kind is kind]


@dataclass
class DraftDiff:
    """The mechanical diff of two aligned drafts."""

    alignment: Alignment
    scenes: list[SceneDiff] = field(default_factory=list)
    relocation_candidates: list[RelocationCandidate] = field(default_factory=list)

    @property
    def spans(self) -> list[ChangeSpan]:
        return [span for scene in self.scenes for span in scene.spans]

    def summary(self) -> dict[str, object]:
        by_type: dict[str, int] = {}
        for span in self.spans:
            by_type[span.element_type.value] = by_type.get(span.element_type.value, 0) + 1
        return {
            "scenes_with_changes": len(self.scenes),
            "spans": len(self.spans),
            "added": len([s for s in self.spans if s.kind is SpanKind.ADDED]),
            "removed": len([s for s in self.spans if s.kind is SpanKind.REMOVED]),
            "replaced": len([s for s in self.spans if s.kind is SpanKind.REPLACED]),
            "by_element_type": dict(sorted(by_type.items())),
            "heading_changes": sum(1 for s in self.scenes if s.heading_changed),
            "dialogue_only_scenes": sum(1 for s in self.scenes if s.dialogue_only),
            "relocation_candidates": len(self.relocation_candidates),
        }


def diff_drafts(
    alignment: Alignment,
    stream: EventStream | None = None,
) -> DraftDiff:
    """Run the mechanical diff over an alignment.

    Only matched pairs are diffed. Inserted, omitted and removed scenes are
    already fully described by the alignment: an inserted scene is entirely new
    and an omitted one is entirely gone, so running difflib over them would
    produce a span per line and bury the real changes.
    """
    stream = stream or NullStream()
    result = DraftDiff(alignment=alignment)

    for pair in alignment.changed:
        scene_diff = _diff_scene(pair)
        # A heading-only change has no spans but is still a finding, so the
        # test is `has_changes` rather than a non-empty span list.
        if scene_diff.has_changes:
            result.scenes.append(scene_diff)
            # `heading_changed` is only ever true for a matched pair, which has
            # both sides; the explicit test is for the type checker's benefit.
            if scene_diff.heading_changed and pair.before and pair.after:
                stream.emit(
                    EventKind.CHANGE_DETECTED,
                    f"scene {pair.number}: heading "
                    f"{pair.before.heading!r} -> {pair.after.heading!r}",
                    scene_number=pair.number,
                    span_kind="heading",
                    before=pair.before.heading,
                    after=pair.after.heading,
                )
            for span in scene_diff.spans:
                stream.emit(
                    EventKind.CHANGE_DETECTED,
                    f"scene {span.scene_number}: {span.kind.value} "
                    f"{span.element_type.value}",
                    scene_number=span.scene_number,
                    span_kind=span.kind.value,
                    element_type=span.element_type.value,
                )

    result.relocation_candidates = _find_relocation_candidates(result)
    for candidate in result.relocation_candidates:
        stream.emit(
            EventKind.CHANGE_DETECTED,
            f"possible relocation: {candidate.phrase!r} "
            f"{candidate.from_scene} -> {candidate.to_scene}",
            phrase=candidate.phrase,
            from_scene=candidate.from_scene,
            to_scene=candidate.to_scene,
            candidate=True,
        )

    summary = result.summary()
    stream.emit(
        EventKind.INFO,
        f"diffed: {summary['spans']} spans across "
        f"{summary['scenes_with_changes']} scenes",
        diff=summary,
    )
    return result


def _diff_scene(pair: ScenePair) -> SceneDiff:
    """Diff one aligned pair, element by element.

    Elements are matched on their text so that an inserted paragraph shifts
    nothing: without this, adding one action line at the top of a scene reports
    every element below it as replaced.
    """
    scene_diff = SceneDiff(pair=pair)
    if pair.before is None or pair.after is None:
        return scene_diff

    before_elements = pair.before.elements
    after_elements = pair.after.elements

    matcher = difflib.SequenceMatcher(
        None,
        [_element_key(e) for e in before_elements],
        [_element_key(e) for e in after_elements],
        autojunk=False,
    )

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue

        if tag == "replace":
            # Pair them up positionally as far as they go: a rewritten line is
            # one replacement, not a delete plus an unrelated insert.
            paired = min(i2 - i1, j2 - j1)
            for offset in range(paired):
                old = before_elements[i1 + offset]
                new = after_elements[j1 + offset]
                removed, added = _word_diff(old.text, new.text)
                scene_diff.spans.append(
                    ChangeSpan(
                        kind=SpanKind.REPLACED,
                        scene_number=pair.number,
                        element_type=new.type,
                        before=old,
                        after=new,
                        words_removed=removed,
                        words_added=added,
                    )
                )
            for offset in range(paired, i2 - i1):
                old = before_elements[i1 + offset]
                scene_diff.spans.append(
                    ChangeSpan(
                        kind=SpanKind.REMOVED,
                        scene_number=pair.number,
                        element_type=old.type,
                        before=old,
                    )
                )
            for offset in range(paired, j2 - j1):
                new = after_elements[j1 + offset]
                scene_diff.spans.append(
                    ChangeSpan(
                        kind=SpanKind.ADDED,
                        scene_number=pair.number,
                        element_type=new.type,
                        after=new,
                    )
                )

        elif tag == "delete":
            for index in range(i1, i2):
                old = before_elements[index]
                scene_diff.spans.append(
                    ChangeSpan(
                        kind=SpanKind.REMOVED,
                        scene_number=pair.number,
                        element_type=old.type,
                        before=old,
                    )
                )

        elif tag == "insert":
            for index in range(j1, j2):
                new = after_elements[index]
                scene_diff.spans.append(
                    ChangeSpan(
                        kind=SpanKind.ADDED,
                        scene_number=pair.number,
                        element_type=new.type,
                        after=new,
                    )
                )

    return scene_diff


def _element_key(element: Element) -> str:
    """Identity for matching elements across drafts.

    Includes the type so a line does not match a differently-typed one with the
    same words, e.g. a character cue and a one-word action line.
    """
    return f"{element.type.value}|{element.text}"


_WORD_RE = re.compile(r"\w+|[^\w\s]")


def _word_diff(before: str, after: str) -> tuple[list[str], list[str]]:
    """Which words left and which arrived.

    Lets a one-word change in a long paragraph be seen as one word, which is
    what tells a typo fix apart from a rewritten action beat.
    """
    before_words = _WORD_RE.findall(before)
    after_words = _WORD_RE.findall(after)
    matcher = difflib.SequenceMatcher(None, before_words, after_words, autojunk=False)

    removed: list[str] = []
    added: list[str] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("replace", "delete"):
            removed.extend(before_words[i1:i2])
        if tag in ("replace", "insert"):
            added.extend(after_words[j1:j2])
    return removed, added


# Noun phrases worth tracking across scenes. Deliberately narrow: the point is
# to raise a question for the semantic layer, not to extract every element,
# which is Layer 3.3's job with a model behind it.
_STOPWORDS = frozenset(
    """the a an and or but of to in on at by for with from into onto over under
    is are was were be been being he she it they him her them his hers its their
    this that these those there here as if then than so not no yes up down out
    off again once very just also too""".split()  # noqa: SIM905 - prose reads better than a 60-item literal
)


def _find_relocation_candidates(diff: DraftDiff) -> list[RelocationCandidate]:
    """Note phrases that left one scene and appeared in another.

    This is evidence, not a conclusion. The letter opener leaving scene 31 and
    appearing in scene 91 is exactly the question the product exists to answer,
    and answering it needs judgment about whether it is the same object. All
    this does is make sure the question gets asked.
    """
    removed_phrases: dict[str, str] = {}
    added_phrases: dict[str, str] = {}

    for span in diff.spans:
        # Only action lines carry physical elements. A prop named in dialogue is
        # a mention, not a thing on the truck.
        if span.element_type is not ElementType.ACTION:
            continue
        if span.kind in (SpanKind.REMOVED, SpanKind.REPLACED):
            for phrase in _noun_phrases(span.before_text):
                if phrase not in span.after_text.lower():
                    removed_phrases.setdefault(phrase, span.scene_number)
        if span.kind in (SpanKind.ADDED, SpanKind.REPLACED):
            for phrase in _noun_phrases(span.after_text):
                if phrase not in span.before_text.lower():
                    added_phrases.setdefault(phrase, span.scene_number)

    matches: list[RelocationCandidate] = []
    for phrase, from_scene in removed_phrases.items():
        to_scene = added_phrases.get(phrase)
        if to_scene is not None and to_scene != from_scene:
            matches.append(
                RelocationCandidate(
                    phrase=phrase,
                    from_scene=from_scene,
                    to_scene=to_scene,
                    similarity=1.0,
                )
            )

    # "brass letter", "letter opener" and "brass letter opener" all describe one
    # object. Keep the longest phrase for a given journey and drop the fragments
    # it contains, so the semantic layer is asked one question rather than three.
    matches.sort(key=lambda c: len(c.phrase), reverse=True)
    kept: list[RelocationCandidate] = []
    for candidate in matches:
        journey = (candidate.from_scene, candidate.to_scene)
        if any(
            candidate.phrase in other.phrase
            and journey == (other.from_scene, other.to_scene)
            for other in kept
        ):
            continue
        kept.append(candidate)
    return sorted(kept, key=lambda c: (c.from_scene, c.phrase))


def _noun_phrases(text: str) -> set[str]:
    """Candidate object names from an action line.

    Two- and three-word lower-cased runs with no stopwords. Crude on purpose:
    it only has to be good enough to notice that "letter opener" appears in two
    places, and precision comes from the model pass, not from here.
    """
    words = [w.lower() for w in _WORD_RE.findall(text) if w.isalpha()]
    phrases: set[str] = set()
    for size in (2, 3):
        for index in range(len(words) - size + 1):
            window = words[index : index + size]
            if any(w in _STOPWORDS or len(w) < 3 for w in window):
                continue
            phrases.add(" ".join(window))
    return phrases


__all__ = [
    "ChangeSpan",
    "DraftDiff",
    "RelocationCandidate",
    "SceneDiff",
    "SpanKind",
    "diff_drafts",
]
