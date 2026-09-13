"""The feature-length pair, as a scale claim made executable. Costs money.

Deselected by default. Run with:

    pytest -m live tests/live/test_feature_scale.py

`test_live_semantic.py` is the correctness gate on the small pair and stays
there per CLAUDE.md: the feature pair is a scale test, not an iteration loop.
This file exists for a narrower, equally important claim: a 120-scene feature
script does not cost 120 scenes' worth of model calls. Extraction and
reasoning both scope to what the revision actually touched
(`pipeline/run.py::scenes_worth_extracting`), so the cost of a run tracks the
size of the revision, not the size of the script. This test asserts that
directly rather than leaving it as a claim nobody can check.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bluepages.diff import align, diff_drafts
from bluepages.events import CollectingStream
from bluepages.llm import ModelClient, RunBudget
from bluepages.parse import parse_fdx
from bluepages.pipeline.run import scenes_worth_extracting
from bluepages.semantic import extract_draft, reason_about_diff, score
from bluepages.testdata import load_answer_key

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

pytestmark = pytest.mark.live

# A feature-length revision that changes a handful of scenes should cost a
# handful of calls, not one per scene in the script. Generous over the actual
# count (4 changed scenes, 5 extracted) so the test fails on a real regression
# in scoping rather than on ordinary variance.
MAX_MODEL_CALLS = 20


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
        parse_fdx(FIXTURES / "feature-pair-1.fdx"),
        parse_fdx(FIXTURES / "feature-pair-2.fdx"),
    )


@pytest.fixture(scope="module")
def key():
    return load_answer_key(FIXTURES / "feature-answer-key.json")


@pytest.fixture(scope="module")
def live_run(credentials, drafts):
    """One real run of the whole semantic pipeline over the feature pair."""
    before, after = drafts
    stream = CollectingStream()
    client = ModelClient(stream=stream, budget=RunBudget(max_calls=MAX_MODEL_CALLS))

    alignment = align(before, after, stream=stream)
    mechanical = diff_drafts(alignment, stream=stream)
    touched = sorted(scenes_worth_extracting(alignment, mechanical))
    elements = extract_draft(after, client, stream=stream, scenes=touched)
    result = reason_about_diff(mechanical, client, elements=elements, stream=stream)
    return result, client, before, after


def test_a_feature_length_script_costs_a_revision_sized_number_of_calls(live_run):
    """The scale claim, made falsifiable. 120 scenes, 4 changed: the model
    call count must track the revision, not the script."""
    _, client, before, after = live_run

    summary = client.budget.summary()
    print("\n" + "=" * 70)
    print(f"scenes: {before.scene_count} -> {after.scene_count}")
    print(f"budget: {summary}")
    print("=" * 70)

    assert before.scene_count > 100, "fixture is no longer feature-length"
    assert summary["calls"] <= MAX_MODEL_CALLS, (
        f"{summary['calls']} model calls for a {before.scene_count}-scene script: "
        "scoping to the changed scenes may have regressed"
    )


def test_the_feature_pair_meets_its_own_answer_key(live_run, key):
    """Same correctness bar as the small pair, at scale. Printed rather than
    only asserted, so a failure shows which judgment went wrong."""
    result, client, _, _ = live_run
    card = score(key, result)

    print("\n" + "=" * 70)
    for entry in card.scores:
        status = "OK  " if entry.correct else "WRONG"
        print(f"{status} {entry.change_id}")
        if entry.said_forbidden:
            print(f"      FORBIDDEN: {entry.said_forbidden}")
    print(card.summary())
    print(client.budget.summary())
    print("=" * 70)

    assert card.passed, f"scorecard: {card.summary()}"
