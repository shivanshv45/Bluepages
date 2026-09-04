"""The `ripple reason` command, wired end to end with a scripted model.

This covers the part that unit tests on the individual layers miss: that the
CLI actually connects parse -> align -> diff -> extract -> reason -> score, and
that a failing score exits non-zero so CI can gate on it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from ripple.cli import app
from tests.unit.fakes import ScriptedClient, elements_answer, findings_answer

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
DRAFT1 = str(FIXTURES / "small-draft-1.fdx")
DRAFT2 = str(FIXTURES / "small-draft-2.fdx")
KEY = str(FIXTURES / "small-answer-key.json")

runner = CliRunner()


# The findings a correct run produces, keyed by the scene prompt that asks for
# them. Written as the model would answer, so the CLI path is exercised whole.
CORRECT_RULES = [
    (
        "SCENE 2",
        findings_answer({
            "kind": "time_of_day_changed",
            "summary": "Scene 2 flips DAY to NIGHT.",
            "scene": "2",
            "reasoning": "A scheduling change and a possible location re-quote.",
            "departments": ["schedule", "locations"],
            "confidence": 0.97,
        }),
    ),
    (
        "MARKED OMITTED",
        findings_answer({
            "kind": "scene_omitted",
            "summary": "Scene 5 is marked OMITTED.",
            "scene": "5",
            "reasoning": "The number survives. The sheriff's office is released.",
            "departments": ["schedule", "locations", "cast"],
            "confidence": 0.99,
        }),
    ),
    (
        "IS NEW IN THIS DRAFT",
        findings_answer(
            {
                "kind": "scene_inserted",
                "summary": "Scene 5A is inserted between 5 and 6.",
                "scene": "5A",
                "reasoning": "A new exterior and a picture vehicle.",
                "departments": ["locations", "transport", "schedule", "cast"],
                "confidence": 0.94,
            },
            {
                "kind": "clearance_risk",
                "summary": "A Ford Bronco is named in the new scene 5A.",
                "scene": "5A",
                "element": "Ford Bronco",
                "reasoning": "A named brand introduced by the revision.",
                "risk": "medium",
                "departments": ["clearance", "transport"],
                "confidence": 0.93,
            },
        ),
    ),
    (
        "SCENE 4",
        findings_answer({
            "kind": "character_renamed",
            "summary": "JANITOR is renamed CUSTODIAN.",
            "scene": "4",
            "reasoning": "Same scene, same function, identical dialogue.",
            "departments": ["cast"],
            "confidence": 0.95,
        }),
    ),
    (
        "SCENE 7",
        findings_answer(
            {
                "kind": "element_relocated",
                "summary": "The brass letter opener moves from scene 3 to scene 7.",
                "scene": "7",
                "from_scene": "3",
                "element": "brass letter opener",
                "reasoning": "Scene 7 says it sits where he left it.",
                "departments": ["props"],
                "confidence": 0.92,
            },
            {
                "kind": "element_added",
                "summary": "Wire-rimmed reading glasses are added in scene 7.",
                "scene": "7",
                "element": "wire-rimmed reading glasses",
                "reasoning": "They appear nowhere in the earlier draft.",
                "departments": ["props"],
                "confidence": 0.9,
            },
            {
                "kind": "action_rewritten",
                "summary": "Handing the envelope becomes sliding it across the table.",
                "scene": "7",
                "reasoning": "The same single envelope. A different camera setup.",
                "departments": ["ad"],
                "confidence": 0.88,
            },
        ),
    ),
]


@pytest.fixture
def scripted(monkeypatch):
    """Replace the real ModelClient wherever the CLI constructs one."""
    client = ScriptedClient(
        rules=CORRECT_RULES,
        default=elements_answer({"name": "manila envelope", "category": "prop"}),
    )

    def build(*args, **kwargs):
        return client

    monkeypatch.setattr("ripple.llm.ModelClient", build)
    monkeypatch.setattr("ripple.llm.client.ModelClient", build)
    return client


def test_reason_scores_a_correct_run_and_exits_zero(scripted):
    result = runner.invoke(
        app, ["reason", DRAFT1, DRAFT2, "--key", KEY, "--no-elements"]
    )
    assert result.exit_code == 0, result.output
    assert "PASS" in result.output
    assert "8/8" in result.output


def test_reason_exits_non_zero_when_the_key_is_not_met(monkeypatch):
    """A failing score must fail the command, or CI cannot gate on correctness."""
    client = ScriptedClient(default={"findings": []})
    monkeypatch.setattr("ripple.llm.ModelClient", lambda *a, **k: client)

    result = runner.invoke(
        app, ["reason", DRAFT1, DRAFT2, "--key", KEY, "--no-elements"]
    )
    assert result.exit_code == 1
    assert "FAIL" in result.output


def test_reason_writes_json(scripted, tmp_path):
    out = tmp_path / "findings.json"
    result = runner.invoke(
        app,
        ["reason", DRAFT1, DRAFT2, "--no-elements", "--json", str(out)],
    )
    assert result.exit_code == 0, result.output
    assert out.exists()

    import json

    data = json.loads(out.read_text(encoding="utf-8"))
    assert len(data["findings"]) == 8


def test_reason_runs_extraction_by_default(scripted):
    """Without --no-elements, Layer 3.3 runs and its output reaches the report."""
    result = runner.invoke(app, ["reason", DRAFT1, DRAFT2])
    assert result.exit_code == 0, result.output
    assert "elements" in result.output
    assert any("SCENE" in p and "category is one of" in p for p in scripted.prompts)


def test_the_run_budget_ceiling_is_enforced():
    """The guard against a runaway bill. It has to actually fire.

    Checked on the real client rather than through the CLI, because the fake
    carries its own budget and would prove nothing about the guard.
    """
    from ripple.config import Settings
    from ripple.llm import BudgetExceededError, ModelClient, RunBudget

    client = ModelClient(
        settings=Settings(ripple_cache_llm=False), budget=RunBudget(max_calls=2)
    )
    client.budget.calls_made = 2

    with pytest.raises(BudgetExceededError) as exc:
        client.complete("anything", max_tokens=10)
    assert "ceiling of 2" in str(exc.value)


def test_max_calls_option_reaches_the_budget(monkeypatch):
    """--max-calls has to land on the budget, or the flag is decoration."""
    seen: dict[str, int] = {}

    from ripple.llm import RunBudget as RealBudget

    def spy(max_calls: int, **kwargs):
        seen["max_calls"] = max_calls
        return RealBudget(max_calls=max_calls, **kwargs)

    monkeypatch.setattr("ripple.llm.RunBudget", spy)
    monkeypatch.setattr(
        "ripple.llm.ModelClient", lambda *a, **k: ScriptedClient(default={"findings": []})
    )

    runner.invoke(app, ["reason", DRAFT1, DRAFT2, "--no-elements", "--max-calls", "7"])
    assert seen["max_calls"] == 7
