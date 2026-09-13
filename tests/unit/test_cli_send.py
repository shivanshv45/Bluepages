"""The `recipients` and `send` commands (Layer 8).

Layer 8's done-when is "approval sends different emails to different addresses".
These run that, and the guard in front of it: `send` before `approve` sends
nothing and says why.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from bluepages.cli import app
from tests.unit.fakes import ScriptedClient, elements_answer
from tests.unit.test_cli_fanout import DEPARTMENT_RULES
from tests.unit.test_cli_reason import CORRECT_RULES
from tests.unit.test_cli_store import ELEMENT_RULES

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
DRAFT1 = str(FIXTURES / "small-draft-1.fdx")
DRAFT2 = str(FIXTURES / "small-draft-2.fdx")

runner = CliRunner()


@pytest.fixture(autouse=True)
def no_real_resend(monkeypatch) -> None:
    """These assert on the console transport's own output, not a live send.
    A real RESEND_API_KEY in .env must never make this suite depend on the
    network or an account's Cloudflare posture. pydantic-settings reads .env
    directly, so deleting the environment variable alone does not clear it:
    the settings singleton itself has to say there is no key."""
    from bluepages.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "resend_api_key", None)


@pytest.fixture
def saved(monkeypatch, tmp_path: Path) -> Path:
    """A production with a persisted, unapproved fan-out."""
    db_path = tmp_path / "e.db"
    client = ScriptedClient(
        rules=ELEMENT_RULES + CORRECT_RULES + DEPARTMENT_RULES,
        default=elements_answer(),
    )
    monkeypatch.setattr("bluepages.llm.ModelClient", lambda *a, **k: client)

    result = runner.invoke(
        app,
        [
            "fan-out", DRAFT1, DRAFT2,
            "--save", "--production", "The Farm", "--db", str(db_path),
        ],
    )
    assert result.exit_code == 0, result.output
    return db_path


def _run(*args: str):
    return runner.invoke(app, list(args))


# --- recipients -----------------------------------------------------------


def test_a_recipient_can_be_added_and_listed(saved: Path) -> None:
    add = _run(
        "recipients", "The Farm", "--add", "props=props@example.com",
        "--name", "Jo Smith", "--db", str(saved),
    )

    assert add.exit_code == 0, add.output
    assert "props@example.com" in add.output
    assert "Jo Smith" in add.output


def test_an_unknown_department_is_refused(saved: Path) -> None:
    """A typo would silently create a list nothing ever sends to."""
    result = _run(
        "recipients", "The Farm", "--add", "prop=a@example.com", "--db", str(saved)
    )

    assert result.exit_code == 1
    assert "unknown department" in result.output


def test_a_malformed_address_is_refused(saved: Path) -> None:
    result = _run(
        "recipients", "The Farm", "--add", "props=notanemail", "--db", str(saved)
    )

    assert result.exit_code == 1
    assert "not an email" in result.output


def test_the_empty_list_says_what_to_do(saved: Path) -> None:
    """No screen is ever a dead end."""
    result = _run("recipients", "The Farm", "--db", str(saved))

    assert "nobody is on" in result.output
    assert "--add" in result.output


def test_an_unknown_production_fails_clearly(tmp_path: Path) -> None:
    result = _run("recipients", "Nope", "--db", str(tmp_path / "e.db"))

    assert result.exit_code == 1
    assert "no production named" in result.output


# --- send -----------------------------------------------------------------


def test_send_before_approve_sends_nothing(saved: Path) -> None:
    """The gate, at the command line."""
    _run(
        "recipients", "The Farm", "--add", "props=props@example.com",
        "--db", str(saved),
    )

    result = _run("send", "The Farm", "--db", str(saved))

    assert "not approved yet" in result.output
    assert "bluepages approve" in result.output


def test_approve_then_send_delivers(saved: Path) -> None:
    """Layer 8's done-when: approval sends the fan-out."""
    _run(
        "recipients", "The Farm", "--add", "props=props@example.com",
        "--db", str(saved),
    )
    _run(
        "recipients", "The Farm", "--add", "cast=cast@example.com",
        "--db", str(saved),
    )
    approved = _run("approve", "The Farm", "--yes", "--db", str(saved))
    assert approved.exit_code == 0, approved.output

    result = _run("send", "The Farm", "--db", str(saved))

    assert result.exit_code == 0, result.output
    assert "props@example.com" in result.output
    assert "cast@example.com" in result.output


def test_each_department_gets_its_own_subject(saved: Path) -> None:
    """Different emails to different addresses, not one mail to a list."""
    _run("recipients", "The Farm", "--add", "props=p@example.com", "--db", str(saved))
    _run("recipients", "The Farm", "--add", "cast=c@example.com", "--db", str(saved))
    _run("approve", "The Farm", "--yes", "--db", str(saved))

    result = _run("send", "The Farm", "--db", str(saved))

    assert "Props:" in result.output
    assert "Cast:" in result.output


def test_a_dry_run_sends_nothing_and_leaves_the_reports_unsent(saved: Path) -> None:
    _run("recipients", "The Farm", "--add", "props=p@example.com", "--db", str(saved))
    _run("approve", "The Farm", "--yes", "--db", str(saved))

    dry = _run("send", "The Farm", "--dry-run", "--db", str(saved))
    assert "dry run" in dry.output

    # Still sendable afterwards: a dry run that marked things sent would be a
    # trap, since the real send would then skip everything.
    real = _run("send", "The Farm", "--db", str(saved))
    assert "already sent" not in real.output


def test_sending_twice_does_not_resend(saved: Path) -> None:
    _run("recipients", "The Farm", "--add", "props=p@example.com", "--db", str(saved))
    _run("approve", "The Farm", "--yes", "--db", str(saved))
    _run("send", "The Farm", "--db", str(saved))

    again = _run("send", "The Farm", "--db", str(saved))

    assert "already sent" in again.output


def test_a_department_with_no_recipient_is_named(saved: Path) -> None:
    """Approved with nobody to send to is a state worth surfacing."""
    _run("recipients", "The Farm", "--add", "props=p@example.com", "--db", str(saved))
    _run("approve", "The Farm", "--yes", "--db", str(saved))

    result = _run("send", "The Farm", "--db", str(saved))

    assert "no recipient for" in result.output


def test_preview_writes_the_emails_without_sending(saved: Path, tmp_path: Path) -> None:
    """The fastest way to see what a department head receives, and it is free."""
    out = tmp_path / "previews"

    result = _run(
        "send", "The Farm", "--dry-run", "--preview", str(out), "--db", str(saved)
    )

    assert result.exit_code == 0, result.output
    written = list(out.glob("*.html"))
    assert written, "no previews written"
    assert any(p.name == "props.html" for p in written)
    assert "<!doctype html>" in written[0].read_text(encoding="utf-8").lower()
