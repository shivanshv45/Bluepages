"""Tests for the internal script model (Layer 1.2).

The scene-number tests carry the most weight. Alignment in Layer 3.1 anchors on
these, and an ordering bug there makes every downstream result noise.
"""

from __future__ import annotations

import pytest

from ripple.model import (
    InteriorExterior,
    SceneNumber,
    TimeOfDay,
    normalise_cue,
    parse_heading,
)


class TestSceneNumber:
    def test_plain_number(self):
        n = SceneNumber.parse("34")
        assert n is not None
        assert n.number == 34
        assert n.suffix == ""
        assert not n.is_insert

    def test_insert_suffix(self):
        n = SceneNumber.parse("34A")
        assert n is not None
        assert n.number == 34
        assert n.suffix == "A"
        assert n.is_insert

    def test_prefix_form(self):
        n = SceneNumber.parse("A34")
        assert n is not None
        assert n.number == 34
        assert n.prefix == "A"
        assert n.is_insert

    def test_inserts_sort_between_neighbours(self):
        """34 < 34A < 34B < 35. The ordering alignment depends on."""
        nums = [SceneNumber.parse(x) for x in ["35", "34B", "34", "34A"]]
        assert [str(n) for n in sorted(nums)] == ["34", "34A", "34B", "35"]

    def test_numeric_not_lexicographic(self):
        """String sorting would put 340 before 35. It must not."""
        nums = [SceneNumber.parse(x) for x in ["340", "35", "9"]]
        assert [str(n) for n in sorted(nums)] == ["9", "35", "340"]

    def test_whitespace_is_same_scene(self):
        assert SceneNumber.parse(" 34 ") == SceneNumber.parse("34")

    def test_hashable_for_alignment_dict(self):
        d = {SceneNumber.parse("34A"): "scene"}
        assert d[SceneNumber.parse("34A")] == "scene"

    @pytest.mark.parametrize("raw", [None, "", "   "])
    def test_absent_number_is_none(self, raw):
        assert SceneNumber.parse(raw) is None

    def test_unparseable_number_is_kept_not_dropped(self):
        n = SceneNumber.parse("PROLOGUE")
        assert n is not None
        assert n.raw == "PROLOGUE"
        assert n.number is None

    def test_unparseable_sorts_last(self):
        nums = [SceneNumber.parse(x) for x in ["PROLOGUE", "1"]]
        assert [str(n) for n in sorted(nums)] == ["1", "PROLOGUE"]


class TestParseHeading:
    @pytest.mark.parametrize(
        "heading,int_ext,location,tod",
        [
            ("INT. FARMHOUSE KITCHEN - NIGHT", InteriorExterior.INT, "FARMHOUSE KITCHEN", TimeOfDay.NIGHT),
            ("EXT. GRAVEL DRIVEWAY - DAY", InteriorExterior.EXT, "GRAVEL DRIVEWAY", TimeOfDay.DAY),
            ("INT./EXT. CAR - DAY", InteriorExterior.INT_EXT, "CAR", TimeOfDay.DAY),
            ("EXT./INT. VAN - NIGHT", InteriorExterior.INT_EXT, "VAN", TimeOfDay.NIGHT),
            ("INT. OFFICE - CONTINUOUS", InteriorExterior.INT, "OFFICE", TimeOfDay.CONTINUOUS),
            ("EXT. DINER - MOMENTS LATER", InteriorExterior.EXT, "DINER", TimeOfDay.MOMENTS_LATER),
            ("EXT. FIELD - MAGIC HOUR", InteriorExterior.EXT, "FIELD", TimeOfDay.DUSK),
            ("INT. KITCHEN NIGHT", InteriorExterior.INT, "KITCHEN", TimeOfDay.NIGHT),
            ("INT. HOUSE", InteriorExterior.INT, "HOUSE", TimeOfDay.UNKNOWN),
        ],
    )
    def test_slugline_parts(self, heading, int_ext, location, tod):
        got_ie, got_loc, got_tod = parse_heading(heading)
        assert got_ie is int_ext
        assert got_loc == location
        assert got_tod is tod

    def test_moments_later_not_matched_as_later(self):
        """Patterns are ordered longest-first; this is the case that proves it."""
        _, _, tod = parse_heading("EXT. DINER - MOMENTS LATER")
        assert tod is TimeOfDay.MOMENTS_LATER

    def test_location_keeps_internal_dashes(self):
        """A multi-part location must not be eaten by the time-of-day split."""
        _, loc, tod = parse_heading("EXT. ROOFTOP - LOS ANGELES - DUSK")
        assert loc == "ROOFTOP - LOS ANGELES"
        assert tod is TimeOfDay.DUSK

    def test_leading_scene_number_stripped(self):
        ie, loc, _ = parse_heading("34 INT. OFFICE - DAY")
        assert ie is InteriorExterior.INT
        assert loc == "OFFICE"

    def test_day_night_flip_is_representable(self):
        """The flip that is a scheduling change, not a prop change."""
        _, loc_a, tod_a = parse_heading("INT. KITCHEN - DAY")
        _, loc_b, tod_b = parse_heading("INT. KITCHEN - NIGHT")
        assert loc_a == loc_b
        assert tod_a is TimeOfDay.DAY
        assert tod_b is TimeOfDay.NIGHT


class TestNormaliseCue:
    @pytest.mark.parametrize(
        "cue",
        ["MARY", "MARY (CONT'D)", "MARY (V.O.)", "MARY (O.S.)", "MARY (CONT'D) (V.O.)", "  mary  "],
    )
    def test_all_forms_are_one_character(self, cue):
        """A rename must not be confused with a continuation marker.

        Casting's answer to "is CUSTODIAN a renamed JANITOR" is worth thousands
        of dollars; getting this wrong manufactures fake roles.
        """
        assert normalise_cue(cue) == "MARY"

    def test_distinct_characters_stay_distinct(self):
        assert normalise_cue("JANITOR") != normalise_cue("CUSTODIAN")
