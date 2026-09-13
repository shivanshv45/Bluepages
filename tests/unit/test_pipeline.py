"""The pipeline as one callable (Layer 6).

The CLI, the Lambda and the API all run the same sequence through this module,
so what matters here is that the stages compose in the right order, that asking
for a prefix of the work actually stops there, and that a run which cannot spend
money says so before it starts rather than failing partway.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bluepages.events import CollectingStream, EventKind
from bluepages.pipeline import PipelineResult, Stage, run_pipeline
from bluepages.pipeline.run import persist
from tests.unit.fakes import ScriptedClient, elements_answer
from tests.unit.test_cli_fanout import DEPARTMENT_RULES
from tests.unit.test_cli_reason import CORRECT_RULES
from tests.unit.test_cli_store import ELEMENT_RULES

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
DRAFT1 = FIXTURES / "small-draft-1.fdx"
DRAFT2 = FIXTURES / "small-draft-2.fdx"

ALL_RULES = ELEMENT_RULES + CORRECT_RULES + DEPARTMENT_RULES


@pytest.fixture
def client() -> ScriptedClient:
    return ScriptedClient(rules=ALL_RULES, default=elements_answer())


# --- stage gating ---------------------------------------------------------


def test_the_free_stages_run_without_a_client() -> None:
    """Parsing and the mechanical diff cost nothing and must not need Bedrock.

    A user with no AWS account should still get the diff, which is most of what
    `bluepages diff` is for.
    """
    result = run_pipeline(DRAFT1, DRAFT2, stop_after=Stage.DIFF)

    assert result.diff is not None
    assert result.alignment is not None
    assert result.findings is None
    assert result.fan is None


def test_asking_for_a_model_stage_without_a_client_raises_up_front() -> None:
    """Before any work, not partway through.

    Parsing a feature then failing at extraction wastes the parse and reports
    the problem at the wrong stage.
    """
    with pytest.raises(ValueError, match="needs a model client"):
        run_pipeline(DRAFT1, DRAFT2, stop_after=Stage.REASON)


def test_stopping_at_elements_with_extraction_skipped_needs_no_client() -> None:
    """The one model stage that spends nothing."""
    result = run_pipeline(
        DRAFT1, DRAFT2, stop_after=Stage.ELEMENTS, skip_elements=True
    )
    assert result.elements is None
    assert result.diff is not None


def test_stopping_at_parse_does_not_align(client: ScriptedClient) -> None:
    result = run_pipeline(DRAFT1, DRAFT2, client=client, stop_after=Stage.PARSE)

    assert result.before is not None
    assert result.after is not None
    assert result.alignment is None


def test_stopping_at_reason_does_not_fan_out(client: ScriptedClient) -> None:
    """The fan-out is the most expensive stage; asking to stop must stop it."""
    result = run_pipeline(DRAFT1, DRAFT2, client=client, stop_after=Stage.REASON)

    assert result.findings is not None
    assert result.fan is None
    assert result.clearance is None


def test_the_full_run_reaches_every_stage(client: ScriptedClient) -> None:
    result = run_pipeline(DRAFT1, DRAFT2, client=client)

    assert result.diff is not None
    assert result.elements is not None
    assert result.findings is not None
    assert result.fan is not None
    assert result.clearance is not None
    assert result.schedule is not None


# --- what the run reports -------------------------------------------------


def test_the_run_emits_start_and_finish(client: ScriptedClient) -> None:
    """Layer 7 consumes these, so they are a contract, not logging."""
    stream = CollectingStream()
    run_pipeline(DRAFT1, DRAFT2, client=client, stream=stream)

    assert stream.count(EventKind.RUN_STARTED) == 1
    assert stream.count(EventKind.RUN_FINISHED) == 1


def test_the_finish_event_carries_the_summary(client: ScriptedClient) -> None:
    stream = CollectingStream()
    run_pipeline(DRAFT1, DRAFT2, client=client, stream=stream)

    finished = stream.of_kind(EventKind.RUN_FINISHED)[0]
    assert finished.data["stopped_after"] == "fan_out"
    assert "diff" in finished.data
    assert "fan_out" in finished.data


def test_the_summary_omits_stages_that_did_not_run() -> None:
    """A caller must be able to tell "no findings" from "never reasoned"."""
    result = run_pipeline(DRAFT1, DRAFT2, stop_after=Stage.DIFF)
    summary = result.summary()

    assert "diff" in summary
    assert "findings" not in summary
    assert "fan_out" not in summary


def test_the_budget_is_reported(client: ScriptedClient) -> None:
    result = run_pipeline(DRAFT1, DRAFT2, client=client)

    assert result.budget["calls"] > 0
    assert result.budget["max_calls"] > 0


def test_the_production_defaults_to_the_draft_name() -> None:
    result = run_pipeline(DRAFT1, DRAFT2, stop_after=Stage.PARSE)
    assert result.production == DRAFT1.stem


def test_an_explicit_production_wins() -> None:
    result = run_pipeline(
        DRAFT1, DRAFT2, stop_after=Stage.PARSE, production="The Farm"
    )
    assert result.production == "The Farm"


# --- extraction scope -----------------------------------------------------


def test_extraction_is_scoped_to_the_scenes_that_changed(
    client: ScriptedClient,
) -> None:
    """Extracting every scene of a feature is the easiest way to overspend."""
    result = run_pipeline(DRAFT1, DRAFT2, client=client, stop_after=Stage.REASON)

    extracted = set(result.elements.by_scene())
    all_scenes = {str(s.number) for s in result.after.scenes if s.number}
    assert extracted, "nothing was extracted at all"
    assert extracted < all_scenes, "extraction was not scoped to the changed scenes"


def test_relocation_candidates_pull_in_both_scenes(client: ScriptedClient) -> None:
    """Judging whether an object moved needs both ends described."""
    from bluepages.pipeline.run import scenes_worth_extracting

    result = run_pipeline(DRAFT1, DRAFT2, client=client, stop_after=Stage.DIFF)
    scenes = scenes_worth_extracting(result.alignment, result.diff)

    for candidate in result.diff.relocation_candidates:
        assert candidate.from_scene in scenes
        assert candidate.to_scene in scenes


# --- persistence ----------------------------------------------------------


def test_persist_writes_the_run(client: ScriptedClient, tmp_path: Path) -> None:
    result = run_pipeline(DRAFT1, DRAFT2, client=client, production="The Farm")
    persisted = persist(result, db_path=tmp_path / "elements.db")

    assert persisted.changes > 0
    assert result.persisted is persisted


def test_persist_refuses_a_run_that_never_reasoned(tmp_path: Path) -> None:
    """Half a run in the inventory is worse than none: it looks complete."""
    result = run_pipeline(DRAFT1, DRAFT2, stop_after=Stage.DIFF)

    with pytest.raises(ValueError, match="did not reach"):
        persist(result, db_path=tmp_path / "elements.db")


def test_persisted_reports_start_unapproved(
    client: ScriptedClient, tmp_path: Path
) -> None:
    """The approval gate holds however the run was triggered.

    This is the property the autonomous trigger must not break: an agent that
    wakes on an upload and mails eight departments unasked is the thing an AD
    turns off.
    """
    from bluepages.store import Repository, open_database

    db_path = tmp_path / "elements.db"
    result = run_pipeline(DRAFT1, DRAFT2, client=client, production="The Farm")
    persisted = persist(result, db_path=db_path)

    with open_database(path=db_path) as db:
        repo = Repository(db)
        saved = repo.reports_for_draft(persisted.to_draft_id)
        # "Pending" is Layer 8's queue: approved and not yet sent. Nothing is
        # in it until a human approves, which is the gate itself.
        queued_to_send = repo.pending_reports(persisted.to_draft_id)

    assert saved, "the fan-out produced no reports"
    assert all(r["approved_at"] is None for r in saved)
    assert queued_to_send == []


def test_an_empty_result_summarises_without_crashing() -> None:
    """A run that failed before parsing still has to report something."""
    assert PipelineResult().summary()["stopped_after"] == "fan_out"


def test_every_model_call_says_what_it_is_for(client: ScriptedClient) -> None:
    """A run of identical "bulk (bedrock)" log lines says nothing.

    The label reaches CloudWatch and the live view, and it is the difference
    between watching an agent work and watching a progress bar.
    """
    run_pipeline(DRAFT1, DRAFT2, client=client)

    assert client.labels, "no calls were made"
    assert all(label for label in client.labels), (
        f"a model call carried no label: {client.labels}"
    )
