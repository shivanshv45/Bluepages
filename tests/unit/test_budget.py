"""Department budgets: every allocated department shows, even untouched, and
auto_approve rides along on the same row."""

from __future__ import annotations

from pathlib import Path

import pytest

from bluepages.agents import budget as budget_log
from bluepages.store import Repository, open_database


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "e.db"


def test_a_fresh_production_shows_every_default_department(db_path: Path) -> None:
    """PLAN.md: no screen is ever empty. A brand-new production has spent
    nothing, but its budget rows must still be there to edit."""
    with open_database(path=db_path) as db:
        db.create_schema()
        production_id = Repository(db).ensure_production("The Farm")
        rows = budget_log.summary(db, production_id)

    assert len(rows) == len(budget_log.DEFAULT_ALLOCATION)
    assert {r["department"] for r in rows} == set(budget_log.DEFAULT_ALLOCATION)
    assert all(r["committed"] == 0 for r in rows)
    assert all(r["auto_approve"] == 0 for r in rows)


def test_set_allocation_can_set_auto_approve_alongside_the_amount(db_path: Path) -> None:
    with open_database(path=db_path) as db:
        db.create_schema()
        production_id = Repository(db).ensure_production("The Farm")
        budget_log.set_allocation(db, production_id, "transport", 25_000, auto_approve=2_000)
        rows = budget_log.summary(db, production_id)

    row = next(r for r in rows if r["department"] == "transport")
    assert row["auto_approve"] == 2_000


def test_set_allocation_without_auto_approve_leaves_it_at_zero(db_path: Path) -> None:
    with open_database(path=db_path) as db:
        db.create_schema()
        production_id = Repository(db).ensure_production("The Farm")
        budget_log.set_allocation(db, production_id, "props", 5_000)
        rows = budget_log.summary(db, production_id)

    row = next(r for r in rows if r["department"] == "props")
    assert row["allocated"] == 5_000
    assert row["auto_approve"] == 0
