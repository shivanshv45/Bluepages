"""Tests for the Layer 2 revision pairs and answer key.

The answer key is the only reason Layer 3 has a correctness measure rather than
an opinion, which makes it load-bearing. Two things are tested here:

1. The drafts really contain the changes the key claims. `validate` does this,
   and these tests confirm `validate` actually catches drift rather than passing
   everything.
2. The pairs encode the specific judgment calls the PRD names as the product,
   not just any eight differences.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from ripple.parse import parse_fdx
from ripple.testdata import (
    AnswerKey,
    ChangeKind,
    Department,
    load_answer_key,
    load_pair,
    summarise,
    validate,
)

FIXTURES = Path(__file__).parent.parent / "fixtures"


@pytest.fixture(scope="module")
def small_key():
    return load_answer_key(FIXTURES / "small-answer-key.json")


@pytest.fixture(scope="module")
def feature_key():
    return load_answer_key(FIXTURES / "feature-answer-key.json")


@pytest.fixture(scope="module")
def small_pair(small_key):
    return load_pair(small_key, FIXTURES)


class TestKeysValidate:
    """Both keys must describe their drafts exactly."""

    def test_small_key_validates(self, small_key):
        assert validate(small_key, FIXTURES) == []

    def test_feature_key_validates(self, feature_key):
        assert validate(feature_key, FIXTURES) == []

    def test_both_pairs_exist(self, small_key, feature_key):
        """PLAN.md asks for two pairs: one small for iteration, one at scale."""
        assert small_key.pair == "small"
        assert feature_key.pair == "feature"

    def test_change_ids_are_unique(self, small_key):
        assert len(small_key.change_ids) == len(small_key.changes)


class TestValidatorCatchesDrift:
    """A key that has rotted is worse than no key: it reports success against
    scenes that no longer exist. Each case here mutates a valid key and asserts
    the mutation is caught.
    """

    @pytest.fixture
    def raw(self):
        return json.loads((FIXTURES / "small-answer-key.json").read_text(encoding="utf-8"))

    def _problems(self, raw: dict) -> list[str]:
        return validate(AnswerKey.model_validate(raw), FIXTURES)

    def test_nonexistent_scene_number(self, raw):
        raw["changes"][0]["from_scene"] = "99"
        assert any("not in" in p for p in self._problems(raw))

    def test_rename_with_wrong_original_name(self, raw):
        change = next(c for c in raw["changes"] if c["kind"] == "character_renamed")
        change["from_name"] = "PLUMBER"
        assert any("does not speak" in p for p in self._problems(raw))

    def test_rename_where_old_name_survives(self, raw):
        """If the old name still speaks, it was not a rename."""
        change = next(c for c in raw["changes"] if c["kind"] == "character_renamed")
        change["from_name"] = "HARLAN"
        assert any("still speaks" in p for p in self._problems(raw))

    def test_time_of_day_with_wrong_from_value(self, raw):
        change = next(c for c in raw["changes"] if c["kind"] == "time_of_day_changed")
        change["from_value"] = "NIGHT"
        assert any("expected NIGHT" in p for p in self._problems(raw))

    def test_relocated_prop_in_the_wrong_scene(self, raw):
        change = next(c for c in raw["changes"] if c["kind"] == "element_relocated")
        change["to_scene"] = "6"
        assert any("is not in scene" in p for p in self._problems(raw))

    def test_insert_that_already_existed(self, raw):
        change = next(c for c in raw["changes"] if c["kind"] == "scene_inserted")
        change["to_scene"] = "6"
        assert any("already exists" in p for p in self._problems(raw))

    def test_unchanged_scene_that_actually_changed(self, raw):
        raw["unchanged_scenes"].append("2")
        assert any("differs between drafts" in p for p in self._problems(raw))

    def test_department_count_mismatch(self, raw):
        """This caught a real miscount when the key was first written."""
        raw["expected_department_counts"]["props"] = 9
        assert any("expected_department_counts" in p for p in self._problems(raw))

    def test_added_element_that_was_already_there(self, raw):
        change = next(c for c in raw["changes"] if c["kind"] == "element_added")
        change["element"] = "pickup truck"
        assert any("already appears" in p for p in self._problems(raw))

    def test_valid_key_produces_no_problems(self, raw):
        """The control: unmutated, it must pass."""
        assert self._problems(copy.deepcopy(raw)) == []


class TestJudgmentCases:
    """The pairs encode the specific calls the PRD names as the product."""

    def test_prop_relocated_not_added(self, small_key, small_pair):
        """The letter opener leaves scene 3 and appears in scene 7.

        A diff sees a deletion and an addition. The product has to see one
        object that moved, because props needs the difference between a
        continuity note and a purchase order.
        """
        before, after = small_pair
        change = next(c for c in small_key.changes if c.id == "prop-relocated")
        assert change.kind is ChangeKind.ELEMENT_RELOCATED
        assert "letter opener" in before.scene(change.from_scene).full_text.lower()
        assert "letter opener" not in after.scene(change.from_scene).full_text.lower()
        assert "letter opener" in after.scene(change.to_scene).full_text.lower()

    def test_added_prop_is_the_contrast_case(self, small_key, small_pair):
        """The glasses look like the letter opener to a diff, but are a new buy."""
        before, after = small_pair
        change = next(c for c in small_key.changes if c.id == "prop-added")
        assert change.kind is ChangeKind.ELEMENT_ADDED
        everything_before = " ".join(s.full_text for s in before.scenes).lower()
        assert "reading glasses" not in everything_before
        assert "reading glasses" in after.scene(change.to_scene).full_text.lower()

    def test_character_renamed_keeps_the_role(self, small_key, small_pair):
        """Same scene, same function, same dialogue. A rename, not a new part."""
        before, after = small_pair
        assert "JANITOR" in before.all_characters
        assert "JANITOR" not in after.all_characters
        assert "CUSTODIAN" in after.all_characters
        # The dialogue is identical, which is what makes it a rename.
        old_scene = before.scene("4")
        new_scene = after.scene("4")
        assert old_scene.dialogue_text == new_scene.dialogue_text

    def test_action_rewrite_has_no_prop_consequence(self, small_key, small_pair):
        """The category a structural diff cannot resolve.

        The text changed and the kit did not: still one envelope, but a
        different physical action and therefore a different camera setup.
        """
        before, after = small_pair
        change = next(
            c for c in small_key.changes if c.id == "action-rewritten-no-prop-change"
        )
        assert change.departments == [Department.AD]
        assert Department.PROPS not in change.departments
        assert "hands her the envelope" in before.scene("7").full_text
        assert "slides the envelope across" in after.scene("7").full_text
        # The envelope itself is unchanged in both.
        assert "envelope" in before.scene("7").full_text.lower()
        assert "envelope" in after.scene("7").full_text.lower()

    def test_day_to_night_is_not_a_props_change(self, small_key, small_pair):
        """A scheduling change and a possible location re-quote. Not props."""
        from ripple.model import TimeOfDay

        before, after = small_pair
        change = next(c for c in small_key.changes if c.id == "day-to-night")
        assert before.scene("2").time_of_day is TimeOfDay.DAY
        assert after.scene("2").time_of_day is TimeOfDay.NIGHT
        assert Department.SCHEDULE in change.departments
        assert Department.PROPS not in change.departments

    def test_scene_omitted_keeps_its_number(self, small_pair):
        """The convention that makes cross-draft alignment possible at all."""
        before, after = small_pair
        assert before.scene("5") is not None
        omitted = after.scene("5")
        assert omitted is not None
        assert omitted.omitted
        assert omitted.elements == []

    def test_inserted_scene_sits_between_neighbours(self, small_pair):
        """34A-style: it must sort between 5 and 6, not after 59."""
        from ripple.model import SceneNumber

        _, after = small_pair
        numbers = [s.number for s in after.scenes if s.number]
        assert numbers == sorted(numbers)
        insert = SceneNumber.parse("5A")
        assert insert.is_insert
        assert SceneNumber.parse("5") < insert < SceneNumber.parse("6")

    def test_clearance_risk_is_a_named_brand(self, small_key, small_pair):
        """A rights liability introduced by the revision, in a new scene."""
        _, after = small_pair
        change = next(c for c in small_key.changes if c.kind is ChangeKind.CLEARANCE_RISK)
        assert change.element == "Ford Bronco"
        assert "Bronco" in after.scene(change.to_scene).full_text
        assert Department.CLEARANCE in change.departments


class TestFeatureScale:
    """The scale pair: the same changes, buried in unchanged material."""

    @pytest.fixture(scope="class")
    @classmethod
    def pair(cls):
        return (
            parse_fdx(FIXTURES / "feature-pair-1.fdx"),
            parse_fdx(FIXTURES / "feature-pair-2.fdx"),
        )

    def test_is_feature_length(self, pair):
        before, after = pair
        assert before.scene_count >= 100
        assert after.scene_count >= 100

    def test_scene_numbers_are_stable_across_drafts(self, pair):
        """The premise the whole project rests on.

        An earlier version of the generator numbered each draft independently,
        so the insert shifted every later scene and the same content sat at 76
        in one draft and 78 in the other. `validate` caught it. Scene numbers
        are stable by convention; that is why scenes are marked OMITTED rather
        than deleted, and why alignment works.
        """
        before, after = pair
        before_numbers = {str(s.number) for s in before.scenes}
        after_numbers = {str(s.number) for s in after.scenes}
        # Draft 2 adds only the insert; it removes nothing.
        assert before_numbers <= after_numbers
        assert after_numbers - before_numbers == {"61A"}

    def test_most_scenes_are_untouched(self, pair):
        """The false-positive baseline.

        Anything the pipeline reports outside the labelled set is a false
        positive, and there has to be enough unchanged material for that to
        mean something.
        """
        before, after = pair
        shared = {str(s.number) for s in before.scenes} & {
            str(s.number) for s in after.scenes
        }
        identical = sum(
            1 for n in shared if before.scene(n).full_text == after.scene(n).full_text
        )
        assert identical / len(shared) > 0.9

    def test_all_eight_changes_survive_scaling(self, feature_key):
        assert len(feature_key.changes) == 8
        kinds = {c.kind for c in feature_key.changes}
        assert ChangeKind.ELEMENT_RELOCATED in kinds
        assert ChangeKind.CHARACTER_RENAMED in kinds
        assert ChangeKind.SCENE_OMITTED in kinds
        assert ChangeKind.SCENE_INSERTED in kinds

    def test_relocation_spans_a_real_distance(self, feature_key):
        """At scale the prop moves across the film, not to the next scene."""
        change = next(
            c for c in feature_key.changes if c.kind is ChangeKind.ELEMENT_RELOCATED
        )
        from_number = int("".join(ch for ch in change.from_scene if ch.isdigit()))
        to_number = int("".join(ch for ch in change.to_scene if ch.isdigit()))
        assert abs(to_number - from_number) > 10


class TestSummary:
    def test_summarise_reports_the_shape(self, small_key):
        summary = summarise(small_key)
        assert summary["changes"] == 8
        assert summary["by_department"]["props"] == 2
        assert "5A" in summary["scenes_touched"]

    def test_lookup_by_department(self, small_key):
        props = small_key.by_department(Department.PROPS)
        assert {c.id for c in props} == {"prop-relocated", "prop-added"}

    def test_lookup_by_kind(self, small_key):
        assert len(small_key.by_kind(ChangeKind.SCENE_OMITTED)) == 1


class TestLoading:
    def test_missing_key_names_the_generator(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="make_revision_pair"):
            load_answer_key(tmp_path / "nope.json")

    def test_must_not_say_phrases_are_recorded(self, small_key):
        """The expensive errors: calling a relocated prop a new buy."""
        change = next(c for c in small_key.changes if c.id == "prop-relocated")
        assert change.must_not_say
        assert any("new" in phrase for phrase in change.must_not_say)
