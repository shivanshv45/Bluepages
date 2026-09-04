"""Tests for scene alignment (3.1) and the mechanical diff (3.2).

Alignment comes first and matters most: get it wrong and everything downstream
is noise, because a misaligned pair reports every scene as rewritten and the
semantic layer then reasons carefully about changes that never happened.

The scoring tests at the end are the real measure. They check the diff against
the Layer 2 answer key on both pairs, which is the only correctness measure this
layer has.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ripple.diff import (
    AlignmentKind,
    AlignmentMethod,
    SpanKind,
    align,
    diff_drafts,
)
from ripple.events import CollectingStream, EventKind
from ripple.model import ElementType
from ripple.parse import parse_fdx
from ripple.testdata import load_answer_key

FIXTURES = Path(__file__).parent.parent / "fixtures"


@pytest.fixture(scope="module")
def small():
    return (
        parse_fdx(FIXTURES / "small-draft-1.fdx"),
        parse_fdx(FIXTURES / "small-draft-2.fdx"),
    )


@pytest.fixture(scope="module")
def small_alignment(small):
    return align(*small)


@pytest.fixture(scope="module")
def small_diff(small_alignment):
    return diff_drafts(small_alignment)


class TestAlignment:
    def test_uses_scene_numbers_when_available(self, small_alignment):
        """The primary path: `@Number` is stable across drafts by convention."""
        assert small_alignment.method is AlignmentMethod.SCENE_NUMBER
        assert not small_alignment.degraded

    def test_every_scene_accounted_for(self, small, small_alignment):
        """Nothing may be silently dropped: a missed scene is a missed element."""
        before, after = small
        numbers = {p.number for p in small_alignment.pairs}
        assert {str(s.number) for s in before.scenes} <= numbers
        assert {str(s.number) for s in after.scenes} <= numbers

    def test_omitted_scene_is_its_own_kind(self, small_alignment):
        """A cut scene is not a removed one: the number deliberately survives."""
        omitted = small_alignment.of_kind(AlignmentKind.OMITTED)
        assert [p.number for p in omitted] == ["5"]
        assert omitted[0].before is not None
        assert omitted[0].after is not None

    def test_inserted_scene_detected(self, small_alignment):
        inserted = small_alignment.of_kind(AlignmentKind.INSERTED)
        assert [p.number for p in inserted] == ["5A"]
        assert inserted[0].before is None

    def test_no_false_removals(self, small_alignment):
        """A scene gone without an OMITTED marker means a renumbering.

        That would invalidate every scene number after it, so a false positive
        here is expensive.
        """
        assert small_alignment.of_kind(AlignmentKind.REMOVED) == []

    def test_pairs_are_in_script_order(self, small_alignment):
        """5A must sort between 5 and 6, not after 59."""
        numbers = [p.number for p in small_alignment.pairs]
        assert numbers == ["1", "2", "3", "4", "5", "5A", "6", "7", "8"]

    def test_unchanged_scenes_identified(self, small_alignment):
        """Scenes that did not change must not reach the semantic layer."""
        assert {p.number for p in small_alignment.unchanged} == {"1", "6", "8"}

    def test_changed_scenes_identified(self, small_alignment):
        assert {p.number for p in small_alignment.changed} == {"2", "3", "4", "7"}

    def test_time_of_day_flip_detected(self, small_alignment):
        """DAY -> NIGHT: a scheduling change, not a props change."""
        scene_2 = next(p for p in small_alignment.pairs if p.number == "2")
        assert scene_2.time_of_day_changed
        assert scene_2.heading_changed

    def test_omitted_scene_reports_no_phantom_heading_change(self, small_alignment):
        """A bare OMITTED has no slugline, so its parsed fields are UNKNOWN.

        Reporting that as DAY -> UNKNOWN would send noise to scheduling. The
        scene being cut is the finding.
        """
        omitted = small_alignment.of_kind(AlignmentKind.OMITTED)[0]
        assert not omitted.time_of_day_changed
        assert not omitted.location_changed

    def test_moved_scenes_flagged(self, small_alignment):
        """A scene can keep its number and still shift position.

        That is a schedule fact even when nothing inside the scene changed.
        """
        moved = [p.number for p in small_alignment.matched if p.moved]
        assert "6" in moved  # pushed down by the 5A insert


class TestUnnumberedFallback:
    """Pre-production drafts carry no scene numbers. That is a different quality
    of input, not an error, and the degradation must be visible.
    """

    def test_falls_back_to_headings(self):
        before = parse_fdx(FIXTURES / "sample-02.fdx")
        after = parse_fdx(FIXTURES / "sample-02.fdx")
        alignment = align(before, after)
        assert alignment.method is AlignmentMethod.HEADING
        assert alignment.degraded

    def test_degradation_is_reported(self):
        stream = CollectingStream()
        before = parse_fdx(FIXTURES / "sample-02.fdx")
        align(before, before, stream=stream)
        warnings = [w.message for w in stream.of_kind(EventKind.PARSE_WARNING)]
        assert any("no scene numbers" in w for w in warnings)

    def test_identical_unnumbered_drafts_show_no_changes(self):
        before = parse_fdx(FIXTURES / "sample-02.fdx")
        alignment = align(before, before)
        assert alignment.changed == []


class TestMechanicalDiff:
    def test_only_changed_scenes_appear(self, small_diff):
        assert {s.number for s in small_diff.scenes} == {"2", "3", "4", "7"}

    def test_heading_only_change_is_not_dropped(self, small_diff):
        """Scene 2's DAY -> NIGHT changes no element text at all.

        It produces zero spans, so a `if spans:` test would drop one of the most
        consequential changes a revision can carry.
        """
        scene_2 = next(s for s in small_diff.scenes if s.number == "2")
        assert scene_2.spans == []
        assert scene_2.heading_changed
        assert scene_2.has_changes

    def test_word_level_diff_isolates_the_rename(self, small_diff):
        """JANITOR -> CUSTODIAN in a long line must reduce to one word.

        This is the evidence the semantic layer needs to call it a rename rather
        than a new role.
        """
        scene_4 = next(s for s in small_diff.scenes if s.number == "4")
        action = next(s for s in scene_4.spans if s.element_type is ElementType.ACTION)
        assert action.words_removed == ["JANITOR"]
        assert action.words_added == ["CUSTODIAN"]

    def test_added_element_is_added_not_replaced(self, small_diff):
        """The reading glasses are new, and the diff must say so plainly."""
        scene_7 = next(s for s in small_diff.scenes if s.number == "7")
        added = [s.after_text for s in scene_7.of_kind(SpanKind.ADDED)]
        assert any("reading glasses" in t for t in added)

    def test_spans_carry_element_type(self, small_diff):
        """A dialogue rewrite is distinguishable from an action change without
        a model call, which is what keeps the cheap filter cheap.
        """
        types = {s.element_type for s in small_diff.spans}
        assert ElementType.ACTION in types
        assert ElementType.DIALOGUE in types

    def test_dialogue_spans_carry_their_speaker(self, small_diff):
        dialogue = [s for s in small_diff.spans if s.element_type is ElementType.DIALOGUE]
        assert dialogue
        assert all(s.speaker for s in dialogue)

    def test_inserted_and_omitted_scenes_are_not_diffed(self, small_diff):
        """They are already fully described by the alignment.

        Running difflib over an entirely new scene would emit a span per line
        and bury the real changes.
        """
        assert "5" not in {s.number for s in small_diff.scenes}
        assert "5A" not in {s.number for s in small_diff.scenes}


class TestRelocationCandidates:
    """The diff raises the question; the semantic layer answers it."""

    def test_letter_opener_surfaces_as_a_candidate(self, small_diff):
        candidates = small_diff.relocation_candidates
        assert any("letter opener" in c.phrase for c in candidates)
        candidate = next(c for c in candidates if "letter opener" in c.phrase)
        assert candidate.from_scene == "3"
        assert candidate.to_scene == "7"

    def test_overlapping_phrases_are_collapsed(self, small_diff):
        """'brass letter', 'letter opener' and 'brass letter opener' are one
        object. Asking the semantic layer three times about the same prop wastes
        three model calls and produces three findings for one fact.
        """
        journeys = [(c.from_scene, c.to_scene) for c in small_diff.relocation_candidates]
        assert len(journeys) == len(set(journeys))
        assert len(small_diff.relocation_candidates) == 1

    def test_added_element_is_not_a_relocation(self, small_diff):
        """The glasses appear only in draft 2, so nothing relocated."""
        assert not any(
            "glasses" in c.phrase for c in small_diff.relocation_candidates
        )

    def test_candidates_are_only_from_action_lines(self, small):
        """A prop named in dialogue is a mention, not a thing on the truck."""
        before, after = small
        diff = diff_drafts(align(before, after))
        # The dialogue change in scene 3 mentions "nineteen eighty-four"; it must
        # not become a relocation candidate.
        assert not any("nineteen" in c.phrase for c in diff.relocation_candidates)


class TestProgressEvents:
    """Layer 3.6: events emitted while working, not after."""

    def test_alignment_emits_per_scene(self, small):
        stream = CollectingStream()
        alignment = align(*small, stream=stream)
        assert stream.count(EventKind.SCENE_ALIGNED) == len(alignment.pairs)

    def test_diff_emits_change_events(self, small):
        stream = CollectingStream()
        diff_drafts(align(*small), stream=stream)
        assert stream.count(EventKind.CHANGE_DETECTED) > 0

    def test_events_carry_structured_data(self, small):
        stream = CollectingStream()
        align(*small, stream=stream)
        event = stream.of_kind(EventKind.SCENE_ALIGNED)[0]
        assert "scene_number" in event.data
        assert "kind" in event.data

    def test_summary_keys_do_not_collide_with_emit(self, small):
        """An alignment summary contains `method`, and a span carries `kind`.

        `emit(kind, message, /, **data)` takes those positionally so a payload
        may use the same names. This test exists because it did collide once.
        """
        stream = CollectingStream()
        alignment = align(*small, stream=stream)
        diff_drafts(alignment, stream=stream)  # must not raise
        assert stream.events


class TestScoredAgainstAnswerKey:
    """The only real correctness measure this layer has (PLAN.md 3.4).

    Scored on both pairs: the small one for the specific cases, the feature one
    because 115 unchanged scenes are what make a false-positive rate meaningful.
    """

    @staticmethod
    def _score(pair_name: str) -> tuple[set[str], set[str]]:
        key = load_answer_key(FIXTURES / f"{pair_name}-answer-key.json")
        before = parse_fdx(FIXTURES / key.draft_from)
        after = parse_fdx(FIXTURES / key.draft_to)
        alignment = align(before, after)
        result = diff_drafts(alignment)
        found = (
            {s.number for s in result.scenes}
            | {p.number for p in alignment.of_kind(AlignmentKind.OMITTED)}
            | {p.number for p in alignment.of_kind(AlignmentKind.INSERTED)}
        )
        return key.scenes_touched(), found

    def test_small_pair_finds_every_labelled_scene(self):
        expected, found = self._score("small")
        assert expected - found == set(), "missed a labelled scene"

    def test_small_pair_has_no_false_positives(self):
        expected, found = self._score("small")
        assert found - expected == set(), "reported an unchanged scene"

    def test_feature_pair_finds_every_labelled_scene(self):
        expected, found = self._score("feature")
        assert expected - found == set()

    def test_feature_pair_has_no_false_positives(self):
        """115 of 120 scenes are identical; none may be reported."""
        expected, found = self._score("feature")
        assert found - expected == set()

    def test_relocation_survives_the_distance(self):
        """At feature scale the prop moves across 60 scenes, not to its neighbour."""
        key = load_answer_key(FIXTURES / "feature-answer-key.json")
        before = parse_fdx(FIXTURES / key.draft_from)
        after = parse_fdx(FIXTURES / key.draft_to)
        result = diff_drafts(align(before, after))
        candidate = next(
            c for c in result.relocation_candidates if "letter opener" in c.phrase
        )
        assert candidate.from_scene == "31"
        assert candidate.to_scene == "91"
