"""Layers 3.3 and 3.4 against the real models. Costs money.

Deselected by default. Run with:

    pytest -m live

Everything else in the suite tests the machinery around the model call. This is
the only test that measures the thing the project is actually judged on: whether
Claude reaches the right conclusion about a relocated prop, a renamed character,
an action rewrite with no physical consequence, and a DAY to NIGHT flip.

The bar is the answer key, and it is deliberately all-or-nothing. Eight labelled
changes is small enough that a partial pass is a failure with a nice number
attached to it.

Kept on the small pair on purpose (CLAUDE.md): the feature pair is a scale test,
not an iteration loop, and running it on every prompt tweak wastes both time and
credit. Responses are cached on disk, so a re-run after an unrelated change is
free.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ripple.diff import align, diff_drafts
from ripple.events import CollectingStream, EventKind
from ripple.llm import ModelClient, RunBudget
from ripple.parse import parse_fdx
from ripple.semantic import extract_draft, reason_about_diff, score
from ripple.testdata import ChangeKind, load_answer_key

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def credentials():
    """Skip rather than fail when AWS is not configured yet."""
    import boto3

    try:
        boto3.client("sts").get_caller_identity()
    except Exception as exc:
        pytest.skip(f"no AWS credentials: {type(exc).__name__}")


@pytest.fixture(scope="module")
def drafts():
    return (
        parse_fdx(FIXTURES / "small-draft-1.fdx"),
        parse_fdx(FIXTURES / "small-draft-2.fdx"),
    )


@pytest.fixture(scope="module")
def key():
    return load_answer_key(FIXTURES / "small-answer-key.json")


@pytest.fixture(scope="module")
def live_run(credentials, drafts, key):
    """One real run of the whole semantic pipeline, shared by every assertion.

    Module-scoped so the eight scenes are reasoned about once, not once per
    test. The budget is a hard ceiling: the small pair is six changed scenes
    plus extraction, so 40 calls is generous and still bounded.
    """
    before, after = drafts
    stream = CollectingStream()
    client = ModelClient(stream=stream, budget=RunBudget(max_calls=40))

    mechanical = diff_drafts(align(before, after, stream=stream), stream=stream)
    elements = extract_draft(
        after,
        client,
        stream=stream,
        scenes=["2", "3", "4", "5A", "7"],
    )
    result = reason_about_diff(mechanical, client, elements=elements, stream=stream)
    return result, elements, stream, client


def test_the_answer_key_is_met(live_run, key):
    """The headline. Every labelled change found, judged right, nothing forbidden.

    When this fails, read the scorecard it prints rather than the assertion: the
    interesting information is which judgment went wrong, not that one did.
    """
    result, _, _, _ = live_run
    card = score(key, result)

    print("\n" + "=" * 70)
    for entry in card.scores:
        status = "OK  " if entry.correct else "WRONG"
        print(f"{status} {entry.change_id}")
        if entry.found:
            print(f"      got:  {entry.found_kind.value if entry.found_kind else '?'}")
            print(f"      want: {entry.expected_kind.value}")
            print(f"      said: {entry.finding_summary}")
        if entry.departments_missed:
            print(f"      missed departments: {[d.value for d in entry.departments_missed]}")
        if entry.said_forbidden:
            print(f"      FORBIDDEN: {entry.said_forbidden}")
    print(card.summary())
    print("=" * 70)

    assert card.passed, f"scorecard: {card.summary()}"


def test_the_relocated_prop_is_not_called_a_new_buy(live_run, key):
    """The single most expensive wrong answer in the key.

    Props being told to purchase a letter opener that is already in the
    inventory is the error the whole semantic layer exists to prevent.
    """
    result, _, _, _ = live_run
    card = score(key, result)
    entry = next(s for s in card.scores if s.change_id == "prop-relocated")

    assert entry.found, "the letter opener relocation was not found at all"
    assert entry.kind_correct, f"called it {entry.found_kind}, not a relocation"
    assert not entry.said_forbidden, entry.said_forbidden


def test_the_rename_is_not_called_a_new_role(live_run, key):
    """Casting's answer is worth thousands of dollars."""
    result, _, _, _ = live_run
    card = score(key, result)
    entry = next(s for s in card.scores if s.change_id == "character-renamed")

    assert entry.found and entry.kind_correct
    assert not entry.said_forbidden, entry.said_forbidden


def test_the_action_rewrite_does_not_reach_props(live_run, key):
    """The category a structural diff cannot resolve.

    The text changed and the kit did not. Telling props about it is the noise
    that makes a department stop reading.
    """
    result, _, _, _ = live_run
    card = score(key, result)
    entry = next(s for s in card.scores if s.change_id == "action-rewritten-no-prop-change")

    assert entry.found and entry.kind_correct
    assert not entry.said_forbidden, entry.said_forbidden


def test_the_day_night_flip_is_scheduling_not_props(live_run, key):
    result, _, _, _ = live_run
    card = score(key, result)
    entry = next(s for s in card.scores if s.change_id == "day-to-night")

    assert entry.found and entry.kind_correct
    assert not entry.said_forbidden, entry.said_forbidden


def test_the_branded_vehicle_is_flagged_for_clearance(live_run, key):
    """Caught at script stage it is a phone call, caught after the shoot a reshoot."""
    result, _, _, _ = live_run
    clearance = result.by_kind(ChangeKind.CLEARANCE_RISK)
    assert clearance, "no clearance risk raised for the Ford Bronco"
    assert any("bronco" in (f.element or f.summary).lower() for f in clearance)


def test_nothing_is_invented_in_unchanged_scenes(live_run, key):
    """Scenes 1, 6 and 8 are identical. A confident finding there is a fabrication."""
    result, _, _, _ = live_run
    card = score(key, result)
    assert not card.findings_in_unchanged_scenes, card.findings_in_unchanged_scenes


def test_extraction_finds_the_branded_vehicle(live_run):
    """Layer 3.3 has to see the Bronco for the clearance consumer to read it."""
    _, elements, _, _ = live_run
    branded = {name.lower() for _, e in elements.branded for name in [e.name]}
    assert any("bronco" in n for n in branded), f"branded elements found: {branded}"


def test_extraction_keeps_the_qualifying_adjective(live_run):
    """"brass letter opener", not "letter opener".

    The adjective is what lets the next draft's extraction match the same
    object. Generalising it away breaks element identity across drafts.
    """
    _, elements, _, _ = live_run
    names = {e.name.lower() for e in elements.all_elements}
    opener = [n for n in names if "opener" in n]
    assert opener, f"no letter opener extracted from scene 7: {sorted(names)}"
    assert any("brass" in n for n in opener), f"lost the adjective: {opener}"


def test_which_model_answered_is_recorded(live_run):
    """Fallback output is weaker and that must be visible, not silent."""
    result, _, stream, _ = live_run
    assert result.models_used, "no model recorded as having answered"

    finished = stream.of_kind(EventKind.MODEL_CALL_FINISHED)
    assert finished
    assert all("model" in e.data for e in finished)


def test_the_run_stayed_inside_its_budget(live_run):
    """The cost guard is the real protection. Billing lags hours."""
    _, _, _, client = live_run
    spend = client.budget.summary()
    print(f"\nspend: {spend}")
    assert spend["calls"] <= spend["max_calls"]
