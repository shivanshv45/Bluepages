"""Clearance and schedule impact (Layers 5.2 and 5.3).

Neither uses a model, which is the point: the judgment was already made
upstream, so these assemble what is known rather than re-deciding it. That also
makes them exactly testable.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bluepages.agents import clearance_report, finance_report, schedule_report
from bluepages.agents.decisions import Decision
from bluepages.diff import align, diff_drafts
from bluepages.parse import parse_fdx
from bluepages.semantic.elements import (
    DraftElements,
    ElementCategory,
    ExtractedElement,
    SceneExtraction,
)
from bluepages.semantic.reasoning import Finding, SemanticResult
from bluepages.testdata import ChangeKind, Department

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


@pytest.fixture
def drafts():
    return (
        parse_fdx(FIXTURES / "small-draft-1.fdx"),
        parse_fdx(FIXTURES / "small-draft-2.fdx"),
    )


@pytest.fixture
def diff(drafts):
    before, after = drafts
    return diff_drafts(align(before, after))


def _branded(scene: str, name: str, quote: str = "") -> DraftElements:
    return DraftElements(
        scenes=[
            SceneExtraction(
                scene_number=scene,
                elements=[
                    ExtractedElement(
                        name=name,
                        category=ElementCategory.VEHICLE,
                        branded=True,
                        quote=quote,
                    )
                ],
            )
        ]
    )


# --- clearance -------------------------------------------------------------


def test_a_clearance_finding_becomes_a_flag():
    result = SemanticResult(
        findings=[
            Finding(
                kind=ChangeKind.CLEARANCE_RISK,
                summary="A Ford Bronco is named in the new scene 5A.",
                scene="5A",
                element="Ford Bronco",
                risk="medium",
                reasoning="A named brand introduced by the revision.",
                departments=[Department.CLEARANCE],
            )
        ]
    )
    report = clearance_report(result)

    assert len(report.flags) == 1
    assert report.flags[0].element == "Ford Bronco"
    assert report.flags[0].risk == "medium"


def test_a_branded_element_becomes_a_flag_without_a_finding():
    """The extractor's branded flag is a second, independent source.

    A brand the reasoning layer did not raise as a finding is still a liability.
    """
    report = clearance_report(
        SemanticResult(), elements=_branded("5A", "Ford Bronco", "a county Ford Bronco")
    )

    assert len(report.flags) == 1
    assert report.flags[0].quote == "a county Ford Bronco"


def test_the_same_object_from_both_sources_is_one_flag():
    """One Bronco is one phone call, not two."""
    result = SemanticResult(
        findings=[
            Finding(
                kind=ChangeKind.CLEARANCE_RISK,
                summary="A Ford Bronco is named.",
                scene="5A",
                element="Ford Bronco",
                risk="medium",
                departments=[Department.CLEARANCE],
            )
        ]
    )
    report = clearance_report(result, elements=_branded("5A", "Ford Bronco", "the line"))

    assert len(report.flags) == 1
    # The finding's risk survives, and the element's quote is carried across so
    # legal can read the actual wording.
    assert report.flags[0].risk == "medium"
    assert report.flags[0].quote == "the line"


def test_an_object_already_in_the_previous_draft_is_not_newly_introduced():
    """Re-flagging the same brand every draft is how a legal report gets ignored."""
    previous = _branded("31", "Ford Bronco")
    report = clearance_report(
        SemanticResult(), elements=_branded("5A", "Ford Bronco"), previous=previous
    )

    assert len(report.flags) == 1
    assert not report.flags[0].newly_introduced
    assert report.new_flags == []


def test_a_genuinely_new_brand_is_newly_introduced():
    previous = _branded("31", "Chevrolet Impala")
    report = clearance_report(
        SemanticResult(), elements=_branded("5A", "Ford Bronco"), previous=previous
    )

    assert report.flags[0].newly_introduced
    assert len(report.new_flags) == 1


def test_flags_are_ordered_worst_first():
    """A legal report is read from the top, so the expensive one goes there."""
    result = SemanticResult(
        findings=[
            Finding(kind=ChangeKind.CLEARANCE_RISK, summary="a", scene="3",
                    element="low thing", risk="low"),
            Finding(kind=ChangeKind.CLEARANCE_RISK, summary="b", scene="4",
                    element="high thing", risk="high"),
            Finding(kind=ChangeKind.CLEARANCE_RISK, summary="c", scene="5",
                    element="medium thing", risk="medium"),
        ]
    )
    report = clearance_report(result)

    assert [f.risk for f in report.flags] == ["high", "medium", "low"]
    assert len(report.high_risk) == 1


def test_an_unbranded_element_is_not_flagged():
    elements = DraftElements(
        scenes=[
            SceneExtraction(
                scene_number="7",
                elements=[
                    ExtractedElement(name="manila envelope", category=ElementCategory.PROP)
                ],
            )
        ]
    )
    assert clearance_report(SemanticResult(), elements=elements).flags == []


# --- schedule impact -------------------------------------------------------


def test_day_to_night_is_a_schedule_impact(diff, drafts):
    before, after = drafts
    report = schedule_report(diff, before, after)

    flips = report.of_kind("time_of_day")
    assert len(flips) == 1
    assert flips[0].scene == "2"
    assert flips[0].detail == "DAY -> NIGHT"
    assert any("night shoot" in t for t in flips[0].touches)


def test_an_omitted_scene_releases_its_location_and_cast(diff, drafts):
    """What a cut gives back is the useful half of the news."""
    before, after = drafts
    report = schedule_report(diff, before, after)

    omitted = report.of_kind("omitted")
    assert len(omitted) == 1
    assert omitted[0].scene == "5"
    touches = " ".join(omitted[0].touches)
    assert "COUNTY SHERIFF" in touches
    assert "DEPUTY COLE" in touches


def test_an_inserted_scene_names_what_it_needs(diff, drafts):
    before, after = drafts
    report = schedule_report(diff, before, after)

    inserted = report.of_kind("inserted")
    assert len(inserted) == 1
    assert inserted[0].scene == "5A"
    touches = " ".join(inserted[0].touches)
    assert "PARKING LOT" in touches


def test_a_scene_that_moved_without_changing_is_surfaced(diff, drafts):
    """Nothing in the scene changed, which is exactly why it is easy to miss."""
    before, after = drafts
    report = schedule_report(diff, before, after)

    moved = report.of_kind("moved")
    assert moved
    assert all(m.touches == ["shooting order"] for m in moved)


def test_impacts_are_ordered_by_scene(diff, drafts):
    """5A must read between 5 and 6, not after 59."""
    before, after = drafts
    report = schedule_report(diff, before, after)

    scenes = [i.scene for i in report.impacts]
    assert scenes.index("5") < scenes.index("5A") < scenes.index("6")


def test_schedule_impact_costs_no_model_call(diff, drafts):
    """Structural facts. Paying a model to restate arithmetic would be waste."""
    before, after = drafts
    # No client is passed at all: if one were needed this would not compile.
    report = schedule_report(diff, before, after)
    assert report.impacts


def test_an_unchanged_revision_has_no_impacts(drafts):
    before, _ = drafts
    same = diff_drafts(align(before, before))
    assert schedule_report(same, before, before).impacts == []


def test_finance_costs_no_model_call_and_splits_completed_from_queued():
    """Restates decisions already made. No client is passed: if one were
    needed this would not compile."""
    decisions = [
        Decision(kind="procure", summary="a", department="props", status="approved",
                  payload={"total": 500.0}),
        Decision(kind="procure", summary="b", department="transport", status="proposed",
                  payload={"total": 1200.0}),
        Decision(kind="route", summary="c", department="props", status="done"),
    ]
    report = finance_report(decisions)

    assert report.completed_total == 500.0
    assert report.queued_total == 1200.0
    assert len(report.lines) == 2


def test_finance_ignores_non_procurement_decisions():
    decisions = [Decision(kind="urgency", summary="x", department="props", status="done")]
    assert finance_report(decisions).lines == []
