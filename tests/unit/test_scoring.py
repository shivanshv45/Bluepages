"""Scoring semantic output against the answer key.

The scorer is what turns "the model said something" into "the model is right",
so it is tested the way the answer key itself was: against deliberate wrong
answers. A scorer that passes a bad run is worse than no scorer, because it
converts an unverified claim into a verified-looking one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bluepages.semantic.reasoning import Finding, SemanticResult
from bluepages.semantic.scoring import score
from bluepages.testdata import ChangeKind, Department, load_answer_key

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


@pytest.fixture
def key():
    return load_answer_key(FIXTURES / "small-answer-key.json")


def _perfect(key) -> SemanticResult:
    """What a correct run looks like: every labelled change, judged right."""
    return SemanticResult(
        findings=[
            Finding(
                kind=ChangeKind.TIME_OF_DAY_CHANGED,
                summary="Scene 2 flips DAY to NIGHT.",
                scene="2",
                reasoning="A scheduling change and a possible location re-quote.",
                departments=[Department.SCHEDULE, Department.LOCATIONS],
                confidence=0.97,
            ),
            Finding(
                kind=ChangeKind.ELEMENT_RELOCATED,
                summary="The brass letter opener moves from scene 3 to scene 7.",
                scene="7",
                from_scene="3",
                element="brass letter opener",
                reasoning="Scene 7 says it sits where he left it, so it is the same object.",
                departments=[Department.PROPS],
                confidence=0.92,
            ),
            Finding(
                kind=ChangeKind.CHARACTER_RENAMED,
                summary="JANITOR is renamed CUSTODIAN.",
                scene="4",
                reasoning="Same scene, same function, identical dialogue.",
                departments=[Department.CAST],
                confidence=0.95,
            ),
            Finding(
                kind=ChangeKind.ACTION_REWRITTEN,
                summary="Handing the envelope becomes sliding it across the table.",
                scene="7",
                reasoning="Same single envelope. A different camera setup.",
                departments=[Department.AD],
                confidence=0.88,
            ),
            Finding(
                kind=ChangeKind.SCENE_OMITTED,
                summary="Scene 5 is marked OMITTED.",
                scene="5",
                reasoning="The number survives. The sheriff's office interior is released.",
                departments=[Department.SCHEDULE, Department.LOCATIONS, Department.CAST],
                confidence=0.99,
            ),
            Finding(
                kind=ChangeKind.SCENE_INSERTED,
                summary="Scene 5A is inserted between 5 and 6.",
                scene="5A",
                reasoning="A new exterior and a picture vehicle.",
                departments=[
                    Department.LOCATIONS,
                    Department.TRANSPORT,
                    Department.SCHEDULE,
                    Department.CAST,
                ],
                confidence=0.94,
            ),
            Finding(
                kind=ChangeKind.ELEMENT_ADDED,
                summary="Wire-rimmed reading glasses are added in scene 7.",
                scene="7",
                element="wire-rimmed reading glasses",
                reasoning="They appear nowhere in the earlier draft.",
                departments=[Department.PROPS],
                confidence=0.9,
            ),
            Finding(
                kind=ChangeKind.CLEARANCE_RISK,
                summary="A Ford Bronco is named in the new scene 5A.",
                scene="5A",
                element="Ford Bronco",
                reasoning="A named brand introduced by the revision.",
                risk="medium",
                departments=[Department.CLEARANCE, Department.TRANSPORT],
                confidence=0.93,
            ),
        ]
    )


def test_a_correct_run_passes(key):
    card = score(key, _perfect(key))
    assert card.total == 8
    assert card.found == 8
    assert card.fully_correct == 8
    assert card.forbidden_said == 0
    assert card.department_recall == 1.0
    assert card.passed


def test_an_empty_run_fails(key):
    card = score(key, SemanticResult())
    assert card.found == 0
    assert card.recall == 0.0
    assert not card.passed


def test_a_missed_change_fails(key):
    """A miss is a department not told. It cannot pass."""
    result = _perfect(key)
    result.findings = [f for f in result.findings if f.kind is not ChangeKind.CHARACTER_RENAMED]
    card = score(key, result)

    rename = next(s for s in card.scores if s.change_id == "character-renamed")
    assert not rename.found
    assert card.found == 7
    assert not card.passed


def test_relocation_called_an_addition_is_wrong(key):
    """The expensive error the project exists to avoid.

    Found in the right scene, but the judgment is inverted: props would be told
    to buy an opener that is already in the inventory.
    """
    result = _perfect(key)
    for finding in result.findings:
        if finding.kind is ChangeKind.ELEMENT_RELOCATED:
            finding.kind = ChangeKind.ELEMENT_ADDED
            finding.summary = "A new letter opener is required in scene 7."
            finding.reasoning = "Purchase a letter opener for the table."
    card = score(key, result)

    entry = next(s for s in card.scores if s.change_id == "prop-relocated")
    assert entry.found
    assert not entry.kind_correct
    assert "purchase a letter opener" in entry.said_forbidden
    assert not entry.correct
    assert not card.passed


def test_rename_called_a_new_role_is_caught(key):
    """Casting's answer is worth thousands of dollars."""
    result = _perfect(key)
    for finding in result.findings:
        if finding.kind is ChangeKind.CHARACTER_RENAMED:
            finding.summary = "A new role, CUSTODIAN, must be cast."
            finding.reasoning = "Cast a custodian for scene 4."
    card = score(key, result)

    entry = next(s for s in card.scores if s.change_id == "character-renamed")
    assert sorted(entry.said_forbidden) == ["cast a custodian", "new role"]
    assert not card.passed


def test_day_to_night_routed_to_props_is_caught(key):
    """Routing a scheduling change to props is noise, and the key forbids it."""
    result = _perfect(key)
    for finding in result.findings:
        if finding.kind is ChangeKind.TIME_OF_DAY_CHANGED:
            finding.departments = [Department.PROPS]
            finding.reasoning = "The prop dressing will need to read at night."
    card = score(key, result)

    entry = next(s for s in card.scores if s.change_id == "day-to-night")
    assert "prop" in entry.said_forbidden
    assert Department.SCHEDULE in entry.departments_missed
    assert not card.passed


def test_forbidden_phrase_matching_respects_word_boundaries(key):
    """"prop" must not fire on "properly" or "proposed".

    A scorer that flags a correct answer is as bad as one that passes a wrong
    one: it makes the number meaningless in the other direction.
    """
    result = _perfect(key)
    for finding in result.findings:
        if finding.kind is ChangeKind.TIME_OF_DAY_CHANGED:
            finding.reasoning = (
                "The location must be lit properly for a night shoot, as proposed."
            )
    card = score(key, result)

    entry = next(s for s in card.scores if s.change_id == "day-to-night")
    assert entry.said_forbidden == []
    assert card.passed


def test_forbidden_phrase_matches_simple_plurals(key):
    """"new prop" should catch "new props". Models pluralise freely."""
    result = _perfect(key)
    for finding in result.findings:
        if finding.kind is ChangeKind.ACTION_REWRITTEN:
            finding.reasoning = "This introduces new props for the table."
    card = score(key, result)

    entry = next(s for s in card.scores if s.change_id == "action-rewritten-no-prop-change")
    assert "new prop" in entry.said_forbidden


def test_findings_in_unchanged_scenes_fail_the_run(key):
    """The key asserts 1, 6 and 8 are identical. A finding there is invented."""
    result = _perfect(key)
    result.findings.append(
        Finding(
            kind=ChangeKind.ELEMENT_ADDED,
            summary="A radio is added to the truck.",
            scene="6",
            departments=[Department.PROPS],
            confidence=0.9,
        )
    )
    card = score(key, result)

    assert card.fully_correct == 8
    assert len(card.findings_in_unchanged_scenes) == 1
    assert not card.passed


def test_extra_findings_are_reported_not_penalised(key):
    """The key labels the judgment calls it tests, not every consequence.

    Scene 3 genuinely changed and a finding there is defensible, so it is shown
    to a human rather than counted as an error.
    """
    result = _perfect(key)
    result.findings.append(
        Finding(
            kind=ChangeKind.ACTION_REWRITTEN,
            summary="The study action is rewritten around the ledger.",
            scene="3",
            departments=[Department.ART],
            confidence=0.7,
        )
    )
    card = score(key, result)

    assert len(card.unmatched_findings) == 1
    assert card.passed


def test_partial_department_routing_is_measured(key):
    """Recall on routing is its own number: a right call sent to half the crew."""
    result = _perfect(key)
    for finding in result.findings:
        if finding.kind is ChangeKind.SCENE_INSERTED:
            finding.departments = [Department.LOCATIONS]
    card = score(key, result)

    entry = next(s for s in card.scores if s.change_id == "scene-inserted")
    assert entry.departments_hit == [Department.LOCATIONS]
    assert len(entry.departments_missed) == 3
    assert entry.department_recall == pytest.approx(0.25)
    # Routing is measured, but the judgment itself was still correct.
    assert entry.correct
    assert card.department_recall < 1.0


def test_the_right_finding_is_picked_when_a_scene_has_several(key):
    """Scene 7 carries a relocation, an addition and an action rewrite at once.

    Matching on scene alone would let one finding answer all three.
    """
    card = score(key, _perfect(key))

    relocation = next(s for s in card.scores if s.change_id == "prop-relocated")
    addition = next(s for s in card.scores if s.change_id == "prop-added")
    rewrite = next(s for s in card.scores if s.change_id == "action-rewritten-no-prop-change")

    assert "letter opener" in relocation.finding_summary
    assert "glasses" in addition.finding_summary
    assert "envelope" in rewrite.finding_summary


def test_one_finding_cannot_answer_two_changes(key):
    """A single vague finding in scene 7 must not score three changes correct."""
    result = SemanticResult(
        findings=[
            Finding(
                kind=ChangeKind.ELEMENT_ADDED,
                summary="Something changed in scene 7.",
                scene="7",
                departments=[Department.PROPS],
                confidence=0.5,
            )
        ]
    )
    card = score(key, result)
    assert card.fully_correct <= 1
    assert not card.passed


def test_accuracy_and_recall_are_different_numbers(key):
    """Found-but-misjudged is the case that makes the distinction matter."""
    result = _perfect(key)
    for finding in result.findings:
        if finding.kind is ChangeKind.ELEMENT_RELOCATED:
            finding.kind = ChangeKind.ELEMENT_ADDED
    card = score(key, result)

    assert card.recall == 1.0
    assert card.accuracy < 1.0
