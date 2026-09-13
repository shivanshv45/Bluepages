"""The decision log (Layer 10).

A finding is an observation: "the letter opener moved from scene 3 to scene 7".
A decision is the agent choosing to act on one: routing it to Props, calling it
urgent, drafting the email, holding it for the AD.

Those decisions were always being made; they were simply invisible, buried in
the routing and the approval gate. This module records them so the console can
show what the agent decided and why, rather than only what it found.

**On simulated decisions.** An element added by a revision genuinely implies an
acquisition: a new picture vehicle has to come from somewhere. Bluepages is not
connected to a vendor, a purchase order system or a payment rail, so it cannot
make that purchase, and a record claiming it did would be a fabrication.

Those decisions are therefore recorded with `simulated = 1` and a status of
`proposed`. They say what the agent would file, against which system, and they
never claim the transaction happened. The UI renders them as proposals. This is
the honest version of the feature and it is also the more useful one: a
production wants to approve a purchase order, not discover one was placed.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from bluepages.agents.approval import should_auto_approve
from bluepages.agents.sourcing import Sourcer
from bluepages.testdata import ChangeKind

# The kinds that imply something has to be obtained, hired or booked. These are
# the only ones that produce a procurement proposal.
ACQUIRING = {
    ChangeKind.ELEMENT_ADDED.value,
    ChangeKind.SCENE_INSERTED.value,
}

# What a department would actually file, and with whom. Naming the real system
# is what keeps a proposal specific rather than a generic "take action" card.
PROCUREMENT: dict[str, tuple[str, str]] = {
    "props": ("purchase order", "the props house"),
    "transport": ("vehicle booking", "the picture vehicle supplier"),
    "wardrobe": ("costume order", "the costume house"),
    "locations": ("location quote", "the location owner"),
    "cast": ("casting request", "the casting director"),
    "art": ("build order", "the construction coordinator"),
    "sfx": ("effects request", "the SFX supervisor"),
    "stunts": ("stunt booking", "the stunt coordinator"),
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _article(word: str) -> str:
    return "an" if word[:1].lower() in "aeiou" else "a"


@dataclass
class Decision:
    """One thing the agent decided, and why."""

    kind: str
    summary: str
    rationale: str = ""
    action: str = ""
    department: str | None = None
    scene_number: str | None = None
    simulated: bool = False
    status: str = "done"
    payload: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: secrets.token_hex(10))
    created_at: str = field(default_factory=_now)

    def to_row(self, production_id: str, run_id: str | None) -> tuple[Any, ...]:
        return (
            self.id,
            production_id,
            run_id,
            self.kind,
            self.department,
            self.scene_number,
            self.summary,
            self.rationale,
            self.action,
            1 if self.simulated else 0,
            self.status,
            json.dumps(self.payload) if self.payload else None,
            self.created_at,
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["simulated"] = 1 if self.simulated else 0
        return data


def from_findings(
    findings: list[Any],
    reports: list[Any] | None = None,
    sourcer: Sourcer | None = None,
    budget: dict[str, dict[str, float]] | None = None,
) -> list[Decision]:
    """Derive the decisions a completed run actually made.

    Everything here is read off the run's own output. Nothing is invented: a
    routing decision exists because the reasoner really did route that finding,
    and an urgency decision exists because a department really did mark a note
    urgent.

    Sourcing itself already happened in `agents/departments.py::write_report`,
    inside the fan-out thread pool, and its results ride on each report's
    `sourced` dict. `sourcer` is only used as a fallback for a caller with no
    reports (a headless run stopped before fan-out, or a test).

    `budget` is `{department: {"allocated": ..., "committed": ..., "auto_approve": ...}}`
    from `agents/budget.py::summary`. Without it every procurement is left
    pending, which is the safe default: a caller that has not looked up the
    budget cannot know whether one clears under a limit.
    """
    decisions: list[Decision] = []
    sourced_by_element: dict[str, Any] = {}
    for report in reports or []:
        sourced_by_element.update(getattr(report, "sourced", {}) or {})

    budget = budget or {}
    # Tracks what this run has already proposed to spend per department, so a
    # second procurement against the same limit sees the first one's total
    # already eaten into what remains, rather than each checking in isolation.
    spent_this_run: dict[str, float] = {}

    for finding in findings:
        departments = [
            d.value if hasattr(d, "value") else str(d)
            for d in getattr(finding, "departments", [])
        ]
        kind = getattr(finding, "kind", "")
        kind = kind.value if hasattr(kind, "value") else str(kind)
        scene = getattr(finding, "scene", None)
        summary = getattr(finding, "summary", "")
        reasoning = getattr(finding, "reasoning", "")
        confidence = float(getattr(finding, "confidence", 0.8))
        element = getattr(finding, "element", None)

        if departments:
            decisions.append(
                Decision(
                    kind="route",
                    summary=f"Routed to {', '.join(departments)}",
                    rationale=reasoning or summary,
                    action=f"{len(departments)} department brief(s) queued",
                    scene_number=scene,
                    payload={"departments": departments, "confidence": confidence},
                )
            )

        # Low confidence is a real decision: the agent chose to flag rather
        # than assert, and the AD is meant to check it.
        if confidence < 0.6:
            decisions.append(
                Decision(
                    kind="await_approval",
                    summary="Held for human review",
                    rationale=(
                        f"Confidence {confidence:.0%} is below the threshold to "
                        "state this without a check."
                    ),
                    action="Flagged on the revision screen",
                    scene_number=scene,
                    status="pending",
                )
            )

        if kind in ACQUIRING and element:
            for department in departments:
                if department not in PROCUREMENT:
                    continue
                document, counterparty = PROCUREMENT[department]
                # A real web search for a real product, with citations. Read
                # off the report when fan-out already did it; only a caller
                # with no reports falls back to searching here.
                found = sourced_by_element.get(element)
                if found is None:
                    sourcer = sourcer or Sourcer()
                    found = sourcer.find(element, department, summary)
                quantity = 1
                total = round(found.price * quantity, 2)

                dept_budget = budget.get(department, {})
                limit = float(dept_budget.get("auto_approve", 0.0))
                already_spent = spent_this_run.get(department, 0.0)
                remaining = float(dept_budget.get("allocated", 0.0)) - float(
                    dept_budget.get("committed", 0.0)
                ) - already_spent
                cleared = should_auto_approve(
                    total=total, limit=limit, remaining=remaining, grounded=found.grounded
                )

                decision = Decision(
                    kind="procure",
                    summary=f"{found.product} for {element}",
                    rationale=(
                        f"{summary} This element is new in this revision, so "
                        f"{department} has nothing on order for it."
                        + (
                            f" Auto-approved: at or under the {department} "
                            f"auto-approve limit of ${limit:,.0f}."
                            if cleared
                            else ""
                        )
                    ),
                    action=f"Raise {_article(document)} {document} with {found.supplier}",
                    department=department,
                    scene_number=scene,
                    # Not connected to a vendor system. Recorded as a
                    # proposal, never as a completed purchase, whether or not
                    # it auto-approves: Finance still completes the payment on
                    # its own schedule, per agents/consumers.py.
                    simulated=True,
                    status="approved" if cleared else "proposed",
                    payload={
                        "element": element,
                        "document": document,
                        "counterparty": counterparty,
                        "product": found.product,
                        "supplier": found.supplier,
                        "unit_price": found.price,
                        "unit": found.unit,
                        "quantity": quantity,
                        "total": total,
                        "lead_time": found.lead_time,
                        "note": found.note,
                        "sources": found.sources,
                        "url": found.url,
                        "image": found.image,
                        "grounded": found.grounded,
                        "auto_approved": cleared,
                    },
                )
                if cleared:
                    spent_this_run[department] = already_spent + total
                decisions.append(decision)

    for report in reports or []:
        department = getattr(report, "department", None)
        department = (
            department.value if hasattr(department, "value") else str(department or "")
        )
        notes = getattr(report, "notes", [])
        urgent = [n for n in notes if getattr(n, "urgent", False)]
        model = getattr(report, "model_name", "") or ""
        via_fallback = bool(getattr(report, "via_fallback", False))

        if notes and department == "social":
            # Social gets its own decision kind rather than draft_email: it is
            # a post to publish, not a brief to send to a department head.
            for note in notes:
                decisions.append(
                    Decision(
                        kind="social_post",
                        summary=f"Drafted a social post for scene {getattr(note, 'scene', '')}",
                        rationale=getattr(report, "summary", ""),
                        action="Awaiting AD approval before posting",
                        department=department,
                        scene_number=getattr(note, "scene", None),
                        status="pending",
                        payload={
                            "platform": "instagram",
                            "copy": getattr(note, "note", ""),
                        },
                    )
                )
        elif notes:
            decisions.append(
                Decision(
                    kind="draft_email",
                    summary=f"Drafted the {department} brief",
                    rationale=(
                        getattr(report, "summary", "") or f"{len(notes)} note(s) to send."
                    ),
                    action=f"{len(notes)} note(s), awaiting AD approval before sending",
                    department=department,
                    status="pending",
                    payload={"notes": len(notes), "model": model},
                )
            )

        if urgent:
            decisions.append(
                Decision(
                    kind="urgency",
                    summary=f"Marked {len(urgent)} {department} note(s) urgent",
                    rationale="These affect scenes that shoot before the next revision lands.",
                    action="Raised to the top of the department brief",
                    department=department,
                )
            )

        # A fallback is a decision the client made under throttling, worth
        # surfacing on its own. Which model actually answered stays out of
        # anything a person reads: it is an internal cost/reliability detail,
        # logged for CloudWatch but never named in a report or decision.
        if via_fallback:
            decisions.append(
                Decision(
                    kind="fallback",
                    summary=f"Retried on a fallback model for {department}",
                    rationale="The judgment model was unavailable or throttled.",
                    action="Continued on the next model in the chain",
                    department=department,
                )
            )

    return decisions


def persist(db: Any, production_id: str, run_id: str | None, items: list[Decision]) -> int:
    """Write the decisions for a run.

    Scoped to this run, not the whole production: each run gets a fresh id, so
    a re-run of the same revision replaces only its own prior rows and every
    other run's decisions stay put. The log is an append-only trail across
    runs, current state within one.
    """
    if run_id is not None:
        db.execute("DELETE FROM decision WHERE run_id = ?", (run_id,))
    else:
        db.execute(
            "DELETE FROM decision WHERE production_id = ? AND run_id IS NULL",
            (production_id,),
        )
    for decision in items:
        db.execute(
            "INSERT INTO decision (id, production_id, run_id, kind, department, "
            "scene_number, summary, rationale, action, simulated, status, payload, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            decision.to_row(production_id, run_id),
        )
    db.commit()
    return len(items)


def for_production(db: Any, production_id: str, limit: int = 200) -> list[dict[str, Any]]:
    return db.query(
        "SELECT id, run_id, kind, department, scene_number, summary, rationale, "
        "action, simulated, status, payload, created_at FROM decision "
        "WHERE production_id = ? ORDER BY created_at DESC, id DESC LIMIT ?",
        (production_id, limit),
    )


def set_status(db: Any, decision_id: str, status: str) -> bool:
    """Approve or reject one decision. Returns whether a row changed."""
    if status not in {"pending", "approved", "rejected", "done", "proposed"}:
        raise ValueError(f"unknown status: {status}")
    cursor = db.execute(
        "UPDATE decision SET status = ? WHERE id = ?", (status, decision_id)
    )
    db.commit()
    return bool(getattr(cursor, "rowcount", 0))
