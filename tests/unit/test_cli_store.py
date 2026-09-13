"""The persistence commands, wired end to end.

Layer 4's done-when is "a diff run persists, and draft N+1's result can be
queried against draft N's state". These tests are that sentence, executed:
`reason --save` writes, and `inventory` / `report` / `history` read it back.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from bluepages.cli import app
from tests.unit.fakes import ScriptedClient, elements_answer
from tests.unit.test_cli_reason import CORRECT_RULES

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
DRAFT1 = str(FIXTURES / "small-draft-1.fdx")
DRAFT2 = str(FIXTURES / "small-draft-2.fdx")

runner = CliRunner()

# Element answers per scene. Each is matched on the scene number *and* on
# "category is one of", which only the extraction prompt carries. Without that
# second half these collide with the reasoning rules on "SCENE 7", and whichever
# loses silently receives the wrong payload shape and returns nothing.
_EXTRACTION = "category is one of"

ELEMENT_RULES = [
    (
        ("SCENE 5A", _EXTRACTION),
        elements_answer(
            {"name": "Ford Bronco", "category": "vehicle", "branded": True},
            {"name": "manila envelope", "category": "prop"},
        ),
    ),
    (
        ("SCENE 7", _EXTRACTION),
        elements_answer(
            {"name": "brass letter opener", "category": "prop"},
            {"name": "wire-rimmed reading glasses", "category": "prop"},
        ),
    ),
    (
        ("SCENE 3", _EXTRACTION),
        elements_answer({"name": "ledger", "category": "set_dressing"}),
    ),
]


@pytest.fixture
def saved(monkeypatch, tmp_path):
    """Run the pipeline once with --save and hand back the database path."""
    db_path = tmp_path / "elements.db"
    client = ScriptedClient(
        rules=ELEMENT_RULES + CORRECT_RULES, default=elements_answer()
    )
    monkeypatch.setattr("bluepages.llm.ModelClient", lambda *a, **k: client)

    result = runner.invoke(
        app,
        [
            "reason", DRAFT1, DRAFT2,
            "--save", "--production", "The Farm", "--db", str(db_path),
        ],
    )
    assert result.exit_code == 0, result.output
    return db_path, result


def test_reason_save_reports_what_it_persisted(saved):
    _, result = saved
    assert "saved" in result.output
    assert "The Farm" in result.output


def test_inventory_reads_back_the_elements(saved):
    db_path, _ = saved
    result = runner.invoke(app, ["inventory", "The Farm", "--db", str(db_path)])

    assert result.exit_code == 0, result.output
    assert "brass letter opener" in result.output
    assert "Ford Bronco" in result.output
    assert "branded" in result.output


def test_element_history_shows_the_trail(saved):
    """The letter opener's appearances, which is why identity is tracked."""
    db_path, _ = saved
    result = runner.invoke(
        app,
        ["inventory", "The Farm", "--element", "brass letter opener", "--db", str(db_path)],
    )

    assert result.exit_code == 0, result.output
    assert "brass letter opener" in result.output
    assert "scene 7" in result.output


def test_report_routes_by_department(saved):
    """Props gets objects. Locations gets the DAY to NIGHT flip. Not the reverse."""
    db_path, _ = saved
    result = runner.invoke(app, ["report", "The Farm", "--db", str(db_path)])

    assert result.exit_code == 0, result.output
    assert "props" in result.output
    assert "clearance" in result.output
    assert "letter opener" in result.output


def test_report_can_filter_to_one_department(saved):
    db_path, _ = saved
    result = runner.invoke(
        app, ["report", "The Farm", "--department", "props", "--db", str(db_path)]
    )

    assert result.exit_code == 0, result.output
    assert "letter opener" in result.output
    # A scheduling change must not appear in the props report.
    assert "DAY to NIGHT" not in result.output


def test_unknown_department_is_an_error_not_an_empty_report(saved):
    """Silently returning nothing would read as "no changes", which is a lie."""
    db_path, _ = saved
    result = runner.invoke(
        app, ["report", "The Farm", "--department", "catering", "--db", str(db_path)]
    )

    assert result.exit_code == 1
    assert "unknown department" in result.output


def test_history_lists_the_run(saved):
    db_path, _ = saved
    result = runner.invoke(app, ["history", "The Farm", "--db", str(db_path)])

    assert result.exit_code == 0, result.output
    assert "ok" in result.output


def test_querying_an_unknown_production_fails_clearly(tmp_path):
    """Better than an empty table, which looks like a production with no changes."""
    result = runner.invoke(
        app, ["inventory", "Nonexistent", "--db", str(tmp_path / "empty.db")]
    )

    assert result.exit_code == 1
    assert "no production" in result.output


def test_a_second_run_does_not_duplicate_the_production(monkeypatch, tmp_path):
    """Ingesting draft 3 next week must extend the production, not fork it."""
    db_path = tmp_path / "elements.db"
    client = ScriptedClient(rules=ELEMENT_RULES + CORRECT_RULES, default=elements_answer())
    monkeypatch.setattr("bluepages.llm.ModelClient", lambda *a, **k: client)

    for _ in range(2):
        result = runner.invoke(
            app,
            [
                "reason", DRAFT1, DRAFT2,
                "--save", "--production", "The Farm", "--db", str(db_path),
            ],
        )
        assert result.exit_code == 0, result.output

    from bluepages.store import Database

    with Database(path=db_path) as db:
        assert len(db.query("SELECT id FROM production")) == 1
        # Two runs recorded against the one production.
        assert len(db.query("SELECT id FROM run")) == 2
