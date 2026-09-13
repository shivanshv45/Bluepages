"""The fan-out and approval commands (Layer 5).

Layer 5's done-when is "one revision produces N distinct, correct department
reports". These run that sentence, and check the approval gate the PRD puts in
front of sending: the agent drafts, a human approves.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from bluepages.cli import app
from tests.unit.fakes import ScriptedClient, elements_answer
from tests.unit.test_cli_reason import CORRECT_RULES
from tests.unit.test_cli_store import ELEMENT_RULES

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
DRAFT1 = str(FIXTURES / "small-draft-1.fdx")
DRAFT2 = str(FIXTURES / "small-draft-2.fdx")

runner = CliRunner()


def _report(summary: str, *notes: dict) -> dict:
    return {"summary": summary, "notes": list(notes)}


# One rule per department, matched on the phrase the department prompt opens
# with. Distinct wording per department so a test can tell them apart.
DEPARTMENT_RULES = [
    (
        "routed to Props",
        _report(
            "One prop moves scenes; one is new stock.",
            {
                "scene": "7",
                "note": "The brass letter opener is now in the kitchen.",
                "action": "Update continuity. Same opener, no new buy.",
                "urgent": False,
            },
        ),
    ),
    (
        "routed to Cast",
        _report(
            "A rename, not a new booking.",
            {
                "scene": "4",
                "note": "JANITOR is now CUSTODIAN, same part.",
                "action": "Rename on the cast list.",
                "urgent": False,
            },
        ),
    ),
    (
        "routed to Locations",
        _report(
            "A night shoot needs a re-quote.",
            {
                "scene": "2",
                "note": "The kitchen is now a night scene.",
                "action": "Re-quote for night.",
                "urgent": True,
            },
        ),
    ),
    (
        "routed to Transport",
        _report(
            "A picture vehicle is required.",
            {
                "scene": "5A",
                "note": "A county Ford Bronco is a picture vehicle.",
                "action": "Source the Bronco and a driver.",
                "urgent": True,
            },
        ),
    ),
    (
        "routed to Clearance",
        _report(
            "One brand introduced.",
            {
                "scene": "5A",
                "note": "Ford Bronco is a named brand.",
                "action": "Get permission or dress it generic.",
                "urgent": True,
            },
        ),
    ),
    (
        "routed to Schedule",
        _report(
            "One flip, one cut, one insert.",
            {"scene": "2", "note": "DAY to NIGHT.", "action": "Night block.", "urgent": False},
        ),
    ),
    (
        "routed to AD",
        _report(
            "One setup changes.",
            {"scene": "7", "note": "Handing becomes sliding.", "action": "", "urgent": False},
        ),
    ),
]

ALL_RULES = ELEMENT_RULES + DEPARTMENT_RULES + CORRECT_RULES


@pytest.fixture
def client(monkeypatch):
    scripted = ScriptedClient(rules=ALL_RULES, default=elements_answer())
    monkeypatch.setattr("bluepages.llm.ModelClient", lambda *a, **k: scripted)
    return scripted


@pytest.fixture
def saved(client, tmp_path):
    """One fan-out run, persisted, ready to approve."""
    db_path = tmp_path / "elements.db"
    result = runner.invoke(
        app,
        [
            "fan-out", DRAFT1, DRAFT2,
            "--save", "--production", "The Farm", "--db", str(db_path),
        ],
    )
    assert result.exit_code == 0, result.output
    return db_path, result


def test_one_revision_becomes_n_department_reports(client):
    """Layer 5's done-when, stated as a test."""
    result = runner.invoke(app, ["fan-out", DRAFT1, DRAFT2])

    assert result.exit_code == 0, result.output
    for title in ("Props", "Cast", "Locations", "Transport", "Clearance", "Schedule"):
        assert title in result.output


def test_each_report_is_in_its_own_vocabulary(client):
    """The point of the fan-out: props talks objects, cast talks bookings."""
    result = runner.invoke(app, ["fan-out", DRAFT1, DRAFT2])

    assert "no new buy" in result.output.lower()
    assert "rename on the cast list" in result.output.lower()
    assert "re-quote" in result.output.lower()


def test_urgent_notes_are_marked(client):
    result = runner.invoke(app, ["fan-out", DRAFT1, DRAFT2])
    assert result.exit_code == 0, result.output
    assert "urgent" in result.output


def test_the_schedule_view_costs_no_model_call(client):
    """5.3 is derived from the alignment, so it appears without being asked."""
    result = runner.invoke(app, ["fan-out", DRAFT1, DRAFT2])

    assert "Schedule impact" in result.output
    assert "DAY -> NIGHT" in result.output
    # No prompt was ever built for the schedule view.
    assert not any("shooting schedule" in p for p in client.prompts)


def test_the_clearance_detail_lists_the_brand(client):
    result = runner.invoke(app, ["fan-out", DRAFT1, DRAFT2])

    assert "Clearance detail" in result.output
    assert "Ford Bronco" in result.output


def test_fan_out_can_be_limited_to_one_department(client):
    result = runner.invoke(app, ["fan-out", DRAFT1, DRAFT2, "--department", "props"])

    assert result.exit_code == 0, result.output
    assert "Props" in result.output
    assert "Cast" not in result.output


def test_an_unknown_department_is_rejected(client):
    result = runner.invoke(app, ["fan-out", DRAFT1, DRAFT2, "--department", "catering"])

    assert result.exit_code == 1
    assert "unknown department" in result.output


def test_reports_are_written_to_json(client, tmp_path):
    out = tmp_path / "reports.json"
    result = runner.invoke(app, ["fan-out", DRAFT1, DRAFT2, "--json", str(out)])

    assert result.exit_code == 0, result.output

    import json

    payload = json.loads(out.read_text(encoding="utf-8"))
    assert len(payload["departments"]) == 7
    assert payload["clearance"]
    assert payload["schedule"]


# --- persistence and approval ---------------------------------------------


def test_fan_out_persists_the_reports(saved):
    db_path, result = saved
    assert "department reports" in result.output

    from bluepages.store import Database, Repository

    with Database(path=db_path) as db:
        repo = Repository(db)
        pid = str(db.one("SELECT id FROM production")["id"])
        draft = repo.latest_draft(pid)
        reports = repo.reports_for_draft(str(draft["id"]))

    assert len(reports) == 7
    assert all(r["notes"] for r in reports)


def test_nothing_is_approved_until_the_ad_approves(saved):
    """The PRD's human decision point. Reports persist unapproved."""
    db_path, _ = saved

    from bluepages.store import Database, Repository

    with Database(path=db_path) as db:
        repo = Repository(db)
        pid = str(db.one("SELECT id FROM production")["id"])
        draft = repo.latest_draft(pid)
        assert repo.pending_reports(str(draft["id"])) == []


def test_approve_marks_the_reports_sendable(saved):
    db_path, _ = saved
    result = runner.invoke(app, ["approve", "The Farm", "--db", str(db_path), "--yes"])

    assert result.exit_code == 0, result.output
    assert "approved" in result.output

    from bluepages.store import Database, Repository

    with Database(path=db_path) as db:
        repo = Repository(db)
        pid = str(db.one("SELECT id FROM production")["id"])
        draft = repo.latest_draft(pid)
        # Layer 8 picks these up.
        assert len(repo.pending_reports(str(draft["id"]))) == 7


def test_approving_twice_is_idempotent(saved):
    """Re-running must not queue every report for a second send."""
    db_path, _ = saved
    runner.invoke(app, ["approve", "The Farm", "--db", str(db_path), "--yes"])
    second = runner.invoke(app, ["approve", "The Farm", "--db", str(db_path), "--yes"])

    assert second.exit_code == 0
    assert "already approved" in second.output


def test_declining_the_prompt_approves_nothing(saved):
    """The gate has to be a real gate."""
    db_path, _ = saved
    result = runner.invoke(
        app, ["approve", "The Farm", "--db", str(db_path)], input="n\n"
    )

    assert result.exit_code == 1
    assert "not approved" in result.output

    from bluepages.store import Database, Repository

    with Database(path=db_path) as db:
        repo = Repository(db)
        pid = str(db.one("SELECT id FROM production")["id"])
        draft = repo.latest_draft(pid)
        assert repo.pending_reports(str(draft["id"])) == []


def test_approving_with_no_reports_is_an_error(tmp_path, client):
    """Better than silently approving nothing."""
    db_path = tmp_path / "elements.db"
    runner.invoke(
        app,
        ["reason", DRAFT1, DRAFT2, "--save", "--production", "The Farm",
         "--db", str(db_path), "--no-elements"],
    )
    result = runner.invoke(app, ["approve", "The Farm", "--db", str(db_path), "--yes"])

    assert result.exit_code == 1
    assert "no department reports" in result.output


def test_re_running_the_fan_out_replaces_the_reports(client, tmp_path):
    """A department must never have two reports it could be sent."""
    db_path = tmp_path / "elements.db"
    for _ in range(2):
        result = runner.invoke(
            app,
            [
                "fan-out", DRAFT1, DRAFT2,
                "--save", "--production", "The Farm", "--db", str(db_path),
            ],
        )
        assert result.exit_code == 0, result.output

    from bluepages.store import Database

    with Database(path=db_path) as db:
        assert len(db.query("SELECT id FROM report")) == 7
