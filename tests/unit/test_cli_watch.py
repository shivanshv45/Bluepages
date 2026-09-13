"""The `bluepages watch` command (Layer 6, locally).

The same handler the Lambda runs, driven by a folder. What matters here is the
behaviour that keeps a watcher from being expensive: drafts already present when
it starts are recorded rather than processed, and a first draft costs nothing.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from bluepages.cli import app
from tests.unit.fakes import ScriptedClient, elements_answer
from tests.unit.test_cli_fanout import DEPARTMENT_RULES
from tests.unit.test_cli_reason import CORRECT_RULES
from tests.unit.test_cli_store import ELEMENT_RULES

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
ALL_RULES = ELEMENT_RULES + CORRECT_RULES + DEPARTMENT_RULES

runner = CliRunner()


def _production(root: Path, *drafts: tuple[str, str, int]) -> Path:
    """A production folder. Each draft is (fixture, filename, hours ago)."""
    folder = root / "The Farm"
    folder.mkdir(parents=True, exist_ok=True)
    now = datetime.now(tz=UTC)
    for fixture, name, hours in drafts:
        path = folder / name
        shutil.copyfile(FIXTURES / fixture, path)
        stamp = (now - timedelta(hours=hours)).timestamp()
        import os

        os.utime(path, (stamp, stamp))
    return folder


@pytest.fixture
def client(monkeypatch) -> ScriptedClient:
    scripted = ScriptedClient(rules=ALL_RULES, default=elements_answer())
    monkeypatch.setattr("bluepages.llm.ModelClient", lambda *a, **k: scripted)
    return scripted


def test_once_processes_the_drafts_it_finds(
    client: ScriptedClient, tmp_path: Path
) -> None:
    _production(
        tmp_path,
        ("small-draft-1.fdx", "draft-1.fdx", 2),
        ("small-draft-2.fdx", "draft-2.fdx", 1),
    )

    result = runner.invoke(
        app,
        ["watch", str(tmp_path), "--once", "--db", str(tmp_path / "e.db")],
    )

    assert result.exit_code == 0, result.output
    assert "The Farm" in result.output
    assert "department report" in result.output


def test_the_approval_gate_is_stated(client: ScriptedClient, tmp_path: Path) -> None:
    """The run stops at the AD's desk, and the output says so."""
    _production(
        tmp_path,
        ("small-draft-1.fdx", "draft-1.fdx", 2),
        ("small-draft-2.fdx", "draft-2.fdx", 1),
    )

    result = runner.invoke(
        app, ["watch", str(tmp_path), "--once", "--db", str(tmp_path / "e.db")]
    )

    assert "nothing is sent until it is approved" in result.output


def test_a_lone_first_draft_costs_nothing(
    client: ScriptedClient, tmp_path: Path
) -> None:
    _production(tmp_path, ("small-draft-1.fdx", "draft-1.fdx", 1))

    result = runner.invoke(
        app, ["watch", str(tmp_path), "--once", "--db", str(tmp_path / "e.db")]
    )

    assert result.exit_code == 0, result.output
    assert "first draft" in result.output
    assert client.prompts == []


def test_a_missing_folder_fails_clearly(tmp_path: Path) -> None:
    result = runner.invoke(app, ["watch", str(tmp_path / "nope"), "--once"])

    assert result.exit_code == 1
    assert "not a folder" in result.output
