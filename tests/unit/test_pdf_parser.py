"""Tests for the PDF parser (Layer 1.3).

Fixtures used here:

- `real-screenplay.pdf` (The Social Network) and `real-screenplay-2.pdf`
  (Code 8) are genuine production screenplays, downloaded unmodified. They are
  the evidence that margins vary between real documents.
- `feature-draft-1.pdf` is the `.fdx` fixture rendered to PDF, so tier 1 and
  tier 2 can be compared on one script. That comparison is Layer 1's stated
  "done when", and it is the test that found both of this parser's real bugs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ripple.events import CollectingStream, EventKind
from ripple.model import ElementType, InteriorExterior, TimeOfDay
from ripple.parse import parse_script
from ripple.parse.pdf import Margins, PdfParseError, calibrate, parse_pdf

FIXTURES = Path(__file__).parent.parent / "fixtures"


# The two real screenplays are third-party scripts, fetched on demand by
# `scripts/fetch_fixtures.py` rather than committed. Tests needing them skip
# when they are absent, so a clean checkout still passes; the tier-equivalence
# test below runs regardless, because its fixtures are ours.
def _require(name: str):
    path = FIXTURES / name
    if not path.exists():
        pytest.skip(f"{name} not present; run `python scripts/fetch_fixtures.py`")
    return parse_pdf(path)


@pytest.fixture(scope="module")
def social():
    return _require("real-screenplay.pdf")


@pytest.fixture(scope="module")
def code8():
    return _require("real-screenplay-2.pdf")


@pytest.fixture(scope="module")
def rendered():
    """The .fdx fixture, rendered to PDF."""
    return parse_pdf(FIXTURES / "feature-draft-1.pdf")


class TestMarginCalibration:
    """Margins are measured per document, never hardcoded.

    Two real screenplays use columns 0.18" apart, and neither puts the character
    cue at the textbook 3.7". Any hardcoded value is wrong for one of them.
    """

    def test_two_real_scripts_use_different_margins(self, social, code8):
        a = social.meta["margins"]
        b = code8.meta["margins"]
        assert a["character"] != b["character"]
        assert abs(a["action"] - b["action"]) > 0.1

    def test_social_network_columns(self, social):
        m = social.meta["margins"]
        assert m["action"] == pytest.approx(1.3, abs=0.1)
        assert m["dialogue"] == pytest.approx(2.3, abs=0.1)
        assert m["character"] == pytest.approx(3.3, abs=0.1)

    def test_code8_columns(self, code8):
        m = code8.meta["margins"]
        assert m["action"] == pytest.approx(1.5, abs=0.1)
        assert m["dialogue"] == pytest.approx(2.5, abs=0.1)
        assert m["character"] == pytest.approx(3.5, abs=0.1)

    def test_columns_are_ordered(self, social, code8):
        """action < dialogue < parenthetical < character, always."""
        for parsed in (social, code8):
            m = parsed.meta["margins"]
            assert m["action"] < m["dialogue"] < m["parenthetical"] < m["character"]

    def test_confidence_is_high_on_real_screenplays(self, social, code8):
        assert social.meta["margins"]["confidence"] > 0.9
        assert code8.meta["margins"]["confidence"] > 0.9

    def test_scene_numbers_do_not_skew_calibration(self, rendered):
        """A numbered script prints numbers at 1.0", left of the action column.

        Counting those as a column dragged every role one position left and
        mis-assigned all four margins. Sluglines are excluded from the
        histogram because they are identifiable without coordinates.
        """
        m = rendered.meta["margins"]
        assert m["action"] == pytest.approx(1.5, abs=0.1)
        assert m["character"] == pytest.approx(3.7, abs=0.1)

    def test_calibration_falls_back_when_too_little_text(self):
        from ripple.parse.pdf import _Line

        margins = calibrate([_Line(text="x", x0=1.5, top=0, page=0)])
        assert margins.confidence == 0.0
        assert margins.action == 1.5  # the conventional default


class TestClassification:
    def test_classify_picks_nearest_column(self):
        m = Margins(action=1.5, dialogue=2.5, character=3.7, parenthetical=2.9)
        assert m.classify(1.52) is ElementType.ACTION
        assert m.classify(2.48) is ElementType.DIALOGUE
        assert m.classify(2.91) is ElementType.PARENTHETICAL
        assert m.classify(3.70) is ElementType.CHARACTER

    def test_far_from_any_column_becomes_action(self):
        """Action is the safe default: inventing a character is worse."""
        m = Margins(action=1.5, dialogue=2.5, character=3.7, parenthetical=2.9)
        assert m.classify(5.5) is ElementType.ACTION

    def test_timestamp_is_not_a_character(self, social):
        """A clock time sits near the cue column but names no one."""
        for name in social.all_characters:
            assert ":" not in name or not name[0].isdigit()

    def test_long_line_at_cue_column_is_dialogue(self):
        """In a tight layout, wrapped dialogue can land on the cue column."""
        from ripple.parse.pdf import _looks_like_cue

        assert _looks_like_cue("MARK")
        assert _looks_like_cue("ERICA (V.O.)")
        assert not _looks_like_cue(
            "THIS IS A VERY LONG LINE THAT IS CLEARLY NOT A CHARACTER CUE AT ALL"
        )

    def test_fade_in_is_a_transition(self, social):
        """FADE IN: sits at the action column, so position alone would miss it."""
        transitions = [
            e.text for s in social.scenes for e in s.elements
            if e.type is ElementType.TRANSITION
        ]
        assert any("FADE IN" in t for t in transitions)

    def test_transitions_detected(self, code8):
        transitions = [
            e.text for s in code8.scenes for e in s.elements
            if e.type is ElementType.TRANSITION
        ]
        assert any("CUT TO" in t for t in transitions)


class TestSceneExtraction:
    def test_scenes_found(self, social, code8):
        assert social.scene_count > 100
        assert code8.scene_count > 100

    def test_sluglines_parsed(self, code8):
        first = code8.scenes[0]
        assert first.int_ext is not InteriorExterior.UNKNOWN
        assert first.location
        assert first.time_of_day is not TimeOfDay.UNKNOWN

    def test_page_furniture_excluded(self, social):
        """Page numbers and (MORE) are not screenplay content."""
        texts = [e.text for s in social.scenes for e in s.elements]
        assert not any(t.strip().isdigit() for t in texts)
        assert not any(t.strip().upper() in {"(MORE)", "(CONTINUED)"} for t in texts)

    def test_dialogue_carries_speaker(self, code8):
        dialogue = [
            e for s in code8.scenes for e in s.elements
            if e.type is ElementType.DIALOGUE
        ]
        assert dialogue
        assert sum(1 for e in dialogue if e.speaker) / len(dialogue) > 0.9

    def test_source_tier_recorded(self, social):
        from ripple.model import SourceTier

        assert social.source_tier is SourceTier.PDF


class TestTierEquivalence:
    """Layer 1's stated done-when: the same script in .fdx and PDF must produce
    equivalent output.

    This comparison found both real bugs in this parser: omitted scenes being
    dropped entirely, and the scene-number column skewing calibration.
    """

    @pytest.fixture(scope="class")
    @classmethod
    def pair(cls):
        from ripple.parse import parse_fdx

        return (
            parse_fdx(FIXTURES / "feature-draft-1.fdx"),
            parse_pdf(FIXTURES / "feature-draft-1.pdf"),
        )

    def test_same_scene_count(self, pair):
        fdx, pdf = pair
        assert fdx.scene_count == pdf.scene_count

    def test_same_scene_numbers(self, pair):
        """Including the 34A-style inserts."""
        fdx, pdf = pair
        assert [str(s.number) for s in fdx.scenes] == [str(s.number) for s in pdf.scenes]

    def test_same_omitted_scenes(self, pair):
        """A bare OMITTED has no INT/EXT, so it needs its own detection."""
        fdx, pdf = pair
        assert len(fdx.omitted_scenes) == len(pdf.omitted_scenes)
        assert [str(s.number) for s in fdx.omitted_scenes] == [
            str(s.number) for s in pdf.omitted_scenes
        ]

    def test_same_locations(self, pair):
        fdx, pdf = pair
        assert [s.location for s in fdx.scenes] == [s.location for s in pdf.scenes]

    def test_same_time_of_day(self, pair):
        """DAY/NIGHT must survive the round trip: it drives scheduling."""
        fdx, pdf = pair
        assert [s.time_of_day for s in fdx.scenes] == [s.time_of_day for s in pdf.scenes]

    def test_same_int_ext(self, pair):
        fdx, pdf = pair
        assert [s.int_ext for s in fdx.scenes] == [s.int_ext for s in pdf.scenes]

    def test_same_characters(self, pair):
        fdx, pdf = pair
        assert fdx.all_characters == pdf.all_characters

    def test_dialogue_counts_match(self, pair):
        fdx, pdf = pair
        a = fdx.stats()["elements_by_type"]
        b = pdf.stats()["elements_by_type"]
        assert a["dialogue"] == b["dialogue"]
        assert a["character"] == b["character"]
        assert a["parenthetical"] == b["parenthetical"]


class TestProgressEvents:
    def test_lifecycle_and_scenes_emitted(self):
        stream = CollectingStream()
        parsed = parse_pdf(FIXTURES / "feature-draft-1.pdf", stream=stream)
        assert stream.count(EventKind.PARSE_STARTED) == 1
        assert stream.count(EventKind.PARSE_FINISHED) == 1
        assert stream.count(EventKind.SCENE_PARSED) == parsed.scene_count

    def test_calibration_is_reported(self):
        """The margins used must be visible: tier 2 is a reconstruction."""
        stream = CollectingStream()
        parse_pdf(FIXTURES / "feature-draft-1.pdf", stream=stream)
        info = stream.of_kind(EventKind.INFO)
        assert any("margins calibrated" in e.message for e in info)


class TestErrorHandling:
    def test_missing_file(self, tmp_path):
        with pytest.raises(PdfParseError, match="not found"):
            parse_pdf(tmp_path / "nope.pdf")

    def test_not_a_pdf(self, tmp_path):
        p = tmp_path / "fake.pdf"
        p.write_text("this is not a pdf", encoding="utf-8")
        with pytest.raises(PdfParseError):
            parse_pdf(p)

    def test_dispatcher_routes_pdf(self):
        """parse_script must now reach tier 2 rather than refusing."""
        parsed = parse_script(FIXTURES / "feature-draft-1.pdf")
        assert parsed.scene_count > 0
