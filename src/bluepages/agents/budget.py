"""Department budgets and what the agent's purchases draw against them.

The purchase itself is a proposal: Bluepages is not connected to a vendor, so
nothing is actually ordered. The **budget arithmetic is real**. Approving a
proposal commits its amount, the remaining figure is computed from what has
actually been approved, and a proposal that would overrun its department says
so before you approve it rather than after.

That split is the point. A production office does not want a tool that
quietly buys things; it wants one that says "this revision costs Props $1,340
of their remaining $4,000, approve or not".

Line items carry a real product found on the live web by `sourcing.py`, with
the supplier, the price and the citations, because "raise a purchase order" is
not actionable and a specific listing with a price is.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

# What a department is given for a shooting block, when nothing is set. These
# are starting figures a user is expected to edit, not a claim about any real
# production's finances.
DEFAULT_ALLOCATION: dict[str, float] = {
    "props": 12_000,
    "wardrobe": 15_000,
    "transport": 25_000,
    "locations": 40_000,
    "art": 30_000,
    "sfx": 18_000,
    "cast": 60_000,
    "stunts": 20_000,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_defaults(db: Any, production_id: str) -> None:
    """Give a production its starting allocations, once."""
    existing = {
        row["department"]
        for row in db.query(
            "SELECT department FROM budget WHERE production_id = ?", (production_id,)
        )
    }
    for department, allocated in DEFAULT_ALLOCATION.items():
        if department in existing:
            continue
        db.execute(
            "INSERT INTO budget (id, production_id, department, allocated, currency, "
            "updated_at) VALUES (?, ?, ?, ?, 'USD', ?)",
            (secrets.token_hex(10), production_id, department, allocated, _now()),
        )
    db.commit()


def set_allocation(
    db: Any,
    production_id: str,
    department: str,
    amount: float,
    auto_approve: float | None = None,
) -> None:
    row = db.one(
        "SELECT id FROM budget WHERE production_id = ? AND department = ?",
        (production_id, department),
    )
    if row:
        if auto_approve is None:
            db.execute(
                "UPDATE budget SET allocated = ?, updated_at = ? WHERE id = ?",
                (amount, _now(), row["id"]),
            )
        else:
            db.execute(
                "UPDATE budget SET allocated = ?, auto_approve = ?, updated_at = ? "
                "WHERE id = ?",
                (amount, auto_approve, _now(), row["id"]),
            )
    else:
        db.execute(
            "INSERT INTO budget (id, production_id, department, allocated, "
            "auto_approve, currency, updated_at) VALUES (?, ?, ?, ?, ?, 'USD', ?)",
            (secrets.token_hex(10), production_id, department, amount, auto_approve or 0.0, _now()),
        )
    db.commit()


def summary(db: Any, production_id: str) -> list[dict[str, Any]]:
    """Allocation, committed and remaining per department.

    Committed counts only approved procurement decisions. A proposal that is
    still pending is shown separately, because it is money at risk rather than
    money spent. Every allocated department is returned, whether or not this
    revision has touched it yet: a fresh production's budget screen must never
    be empty.
    """
    ensure_defaults(db, production_id)

    allocations: dict[str, float] = {}
    auto_approve: dict[str, float] = {}
    for row in db.query(
        "SELECT department, allocated, auto_approve FROM budget WHERE production_id = ?",
        (production_id,),
    ):
        allocations[row["department"]] = float(row["allocated"])
        auto_approve[row["department"]] = float(row["auto_approve"] or 0.0)

    rows = db.query(
        "SELECT department, status, payload FROM decision "
        "WHERE production_id = ? AND kind = 'procure'",
        (production_id,),
    )

    committed: dict[str, float] = {}
    pending: dict[str, float] = {}
    for row in rows:
        department = row["department"] or ""
        amount = _amount_of(row["payload"])
        bucket = committed if row["status"] == "approved" else pending
        if row["status"] == "rejected":
            continue
        bucket[department] = bucket.get(department, 0.0) + amount

    out: list[dict[str, Any]] = []
    for department, allocated in sorted(allocations.items()):
        spent = committed.get(department, 0.0)
        at_risk = pending.get(department, 0.0)
        out.append(
            {
                "department": department,
                "allocated": allocated,
                "committed": round(spent, 2),
                "pending": round(at_risk, 2),
                "remaining": round(allocated - spent, 2),
                "over": spent > allocated,
                "auto_approve": auto_approve.get(department, 0.0),
            }
        )
    return out


def _amount_of(payload: Any) -> float:
    import json

    if not payload:
        return 0.0
    try:
        data = json.loads(payload) if isinstance(payload, str) else payload
        return float(data.get("total", 0.0))
    except (ValueError, TypeError, AttributeError):
        return 0.0
