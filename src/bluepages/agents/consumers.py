"""Clearance and schedule impact (Layers 5.2 and 5.3).

Both are consumers of the same diff rather than department voices, and both are
mostly derivable without a model. That is the point: the expensive judgment was
already made upstream, so these two read what is already known.

**Clearance** (5.2) lists what the revision introduced that belongs to someone
else. The branded flag on an extracted element and the `clearance_risk` findings
are both already computed, so this assembles rather than re-decides. The one
thing it adds is the check that matters commercially: an object already present
in the previous draft is not a *new* liability, and re-flagging it every draft is
how a legal report gets ignored.

**Schedule impact** (5.3) is explicitly not a solver, per PRD and DECISIONS. It
surfaces what moved on the board: day-to-night flips, omitted and inserted
scenes, and what each one touches. A real constraint solver is a separate
project; this is nearly free given the diff already exists.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from bluepages.diff import AlignmentKind, DraftDiff
from bluepages.events import EventKind, EventStream, NullStream
from bluepages.model import Screenplay
from bluepages.semantic.elements import DraftElements
from bluepages.semantic.reasoning import SemanticResult
from bluepages.testdata import ChangeKind

# Ordered by how much a mistake costs, so a report reads worst-first.
_RISK_ORDER = {"high": 0, "medium": 1, "low": 2, None: 3}


@dataclass
class ClearanceFlag:
    """One rights liability introduced by this revision."""

    element: str
    scene: str
    risk: str
    reason: str = ""
    # The line that introduced it. Legal needs to see the wording, not a summary.
    quote: str = ""
    # False when the object was already in the previous draft. Kept rather than
    # dropped so a re-run does not look like the flag disappeared.
    newly_introduced: bool = True

    @property
    def sort_key(self) -> tuple[int, str]:
        return (_RISK_ORDER.get(self.risk, 3), self.scene)


@dataclass
class ClearanceReport:
    """Everything the revision introduced that someone else owns."""

    flags: list[ClearanceFlag] = field(default_factory=list)

    @property
    def new_flags(self) -> list[ClearanceFlag]:
        """Only what this revision introduced. What the phone call is about."""
        return [f for f in self.flags if f.newly_introduced]

    @property
    def high_risk(self) -> list[ClearanceFlag]:
        return [f for f in self.flags if f.risk == "high"]

    def summary(self) -> dict[str, object]:
        return {
            "flags": len(self.flags),
            "new": len(self.new_flags),
            "high_risk": len(self.high_risk),
            "by_risk": {
                risk: sum(1 for f in self.flags if f.risk == risk)
                for risk in ("high", "medium", "low")
            },
        }


@dataclass
class ScheduleImpact:
    """One thing that moved on the board."""

    kind: str
    scene: str
    detail: str
    # What this touches: a location, a cast day, a company move. Surfaced, not
    # solved: saying what is affected is useful, rescheduling is another project.
    touches: list[str] = field(default_factory=list)


@dataclass
class FinanceLine:
    """One procurement decision, restated in finance terms."""

    department: str
    summary: str
    total: float
    status: str
    completed: bool


@dataclass
class FinanceReport:
    """What this revision costs, read off decisions and budget rows already
    computed. No model call: this is arithmetic over facts that exist."""

    lines: list[FinanceLine] = field(default_factory=list)

    @property
    def completed_total(self) -> float:
        return round(sum(l.total for l in self.lines if l.completed), 2)

    @property
    def queued_total(self) -> float:
        return round(sum(l.total for l in self.lines if not l.completed), 2)

    def summary(self) -> dict[str, object]:
        return {
            "lines": len(self.lines),
            "completed_total": self.completed_total,
            "queued_total": self.queued_total,
        }


def finance_report(
    decisions: list[Any],
    stream: EventStream | None = None,
) -> FinanceReport:
    """Restate procurement decisions as completed vs queued spend.

    Bluepages places no orders, so "completed" means a human approved it, not
    that a vendor was paid; agents/budget.py's own docstring establishes the
    same distinction. An auto-approved decision (agents/approval.py) counts as
    completed the same as a human approval: both are approved.
    """
    stream = stream or NullStream()
    lines: list[FinanceLine] = []
    for decision in decisions:
        kind = getattr(decision, "kind", "")
        if kind != "procure":
            continue
        department = getattr(decision, "department", None) or ""
        status = getattr(decision, "status", "")
        payload = getattr(decision, "payload", {}) or {}
        total = float(payload.get("total", 0.0))
        lines.append(
            FinanceLine(
                department=department,
                summary=getattr(decision, "summary", ""),
                total=total,
                status=status,
                completed=status == "approved",
            )
        )

    report = FinanceReport(lines=lines)
    stream.emit(
        EventKind.INFO,
        f"finance: {report.completed_total} completed, {report.queued_total} queued",
        finance=report.summary(),
    )
    return report


@dataclass
class ScheduleReport:
    """What this revision does to the shooting schedule."""

    impacts: list[ScheduleImpact] = field(default_factory=list)

    def of_kind(self, kind: str) -> list[ScheduleImpact]:
        return [i for i in self.impacts if i.kind == kind]

    def summary(self) -> dict[str, object]:
        by_kind: dict[str, int] = {}
        for impact in self.impacts:
            by_kind[impact.kind] = by_kind.get(impact.kind, 0) + 1
        return {"impacts": len(self.impacts), "by_kind": dict(sorted(by_kind.items()))}


def clearance_report(
    result: SemanticResult,
    elements: DraftElements | None = None,
    previous: DraftElements | None = None,
    stream: EventStream | None = None,
) -> ClearanceReport:
    """Assemble the clearance flags for one revision.

    Reads two sources that already exist: `clearance_risk` findings from the
    semantic layer, and elements the extractor marked `branded`. Deduplicated on
    the object, because the same Ford Bronco arriving from both sources is one
    phone call, not two.
    """
    stream = stream or NullStream()
    stream.emit(EventKind.AGENT_STARTED, "Clearance: checking this revision", department="clearance")
    seen: dict[str, ClearanceFlag] = {}

    for finding in result.by_kind(ChangeKind.CLEARANCE_RISK):
        name = (finding.element or finding.summary).strip()
        key = name.lower()
        seen[key] = ClearanceFlag(
            element=name,
            scene=finding.scene,
            risk=finding.risk or "medium",
            reason=finding.reasoning or finding.summary,
        )

    if elements is not None:
        known_before = _branded_names(previous) if previous is not None else set()
        for scene_number, element in elements.branded:
            key = element.name.strip().lower()
            existing = seen.get(key)
            if existing is not None:
                # The finding already covers it; just carry the quote across so
                # legal can read the line rather than a paraphrase.
                if not existing.quote:
                    existing.quote = element.quote
                existing.newly_introduced = key not in known_before
                continue
            seen[key] = ClearanceFlag(
                element=element.name,
                scene=scene_number,
                risk="medium",
                reason="A branded or titled item named in the script.",
                quote=element.quote,
                newly_introduced=key not in known_before,
            )

    report = ClearanceReport(flags=sorted(seen.values(), key=lambda f: f.sort_key))
    for flag in report.flags:
        stream.emit(
            EventKind.CHANGE_DETECTED,
            f"clearance: {flag.element} in scene {flag.scene} ({flag.risk})"
            + ("" if flag.newly_introduced else " [already in the previous draft]"),
            element=flag.element,
            scene_number=flag.scene,
            risk=flag.risk,
            clearance=True,
            newly_introduced=flag.newly_introduced,
        )
    stream.emit(
        EventKind.INFO,
        f"clearance: {len(report.new_flags)} new flag(s) of {len(report.flags)}",
        clearance=report.summary(),
    )
    stream.emit(
        EventKind.AGENT_FINISHED,
        f"Clearance: {len(report.new_flags)} new flag(s)",
        department="clearance",
    )
    return report


def _branded_names(elements: DraftElements) -> set[str]:
    return {e.name.strip().lower() for _, e in elements.branded}


def schedule_report(
    diff: DraftDiff,
    before: Screenplay,
    after: Screenplay,
    stream: EventStream | None = None,
) -> ScheduleReport:
    """Surface what this revision does to the board.

    Read from the alignment and the parsed headings rather than from a model:
    a day-to-night flip and an omitted scene are structural facts, and spending
    a model call to restate them would be paying for arithmetic.
    """
    stream = stream or NullStream()
    stream.emit(EventKind.AGENT_STARTED, "Schedule: reading the board", department="schedule")
    impacts: list[ScheduleImpact] = []
    alignment = diff.alignment

    for pair in alignment.matched:
        if pair.time_of_day_changed and pair.before and pair.after:
            impacts.append(
                ScheduleImpact(
                    kind="time_of_day",
                    scene=pair.number,
                    detail=(
                        f"{pair.before.time_of_day.value} -> "
                        f"{pair.after.time_of_day.value}"
                    ),
                    touches=_touches(pair.after.location, "night shoot pricing"),
                )
            )
        if pair.location_changed and pair.before and pair.after:
            impacts.append(
                ScheduleImpact(
                    kind="location",
                    scene=pair.number,
                    detail=f"{pair.before.location} -> {pair.after.location}",
                    touches=_touches(pair.after.location, "possible company move"),
                )
            )

    for pair in alignment.of_kind(AlignmentKind.OMITTED):
        scene = pair.before
        impacts.append(
            ScheduleImpact(
                kind="omitted",
                scene=pair.number,
                detail=f"cut: {scene.heading if scene else ''}",
                touches=_released(scene),
            )
        )

    for pair in alignment.of_kind(AlignmentKind.INSERTED):
        scene = pair.after
        impacts.append(
            ScheduleImpact(
                kind="inserted",
                scene=pair.number,
                detail=f"new: {scene.heading if scene else ''}",
                touches=_required(scene),
            )
        )

    # A scene that kept its number but changed position shoots in a different
    # order. Nothing in the scene changed, which is exactly why it is easy to miss.
    moved = [p for p in alignment.matched if p.moved]
    for pair in moved:
        impacts.append(
            ScheduleImpact(
                kind="moved",
                scene=pair.number,
                detail="same scene, different position in the draft",
                touches=["shooting order"],
            )
        )

    impacts.sort(key=lambda i: _scene_sort_key(i.scene))
    report = ScheduleReport(impacts=impacts)
    for impact in impacts:
        stream.emit(
            EventKind.CHANGE_DETECTED,
            f"schedule: scene {impact.scene} {impact.kind} ({impact.detail})",
            scene_number=impact.scene,
            impact=impact.kind,
            schedule=True,
        )
    stream.emit(
        EventKind.INFO,
        f"schedule: {len(impacts)} impact(s)",
        schedule=report.summary(),
    )
    stream.emit(
        EventKind.AGENT_FINISHED,
        f"Schedule: {len(impacts)} impact(s)",
        department="schedule",
    )
    return report


def _touches(location: str, note: str) -> list[str]:
    touched = [note]
    if location:
        touched.append(f"location: {location}")
    return touched


def _released(scene: object) -> list[str]:
    """What an omitted scene gives back."""
    released = ["a scene off the board"]
    location = getattr(scene, "location", "")
    if location:
        released.append(f"location released: {location}")
    characters = getattr(scene, "characters", [])
    if characters:
        released.append(f"cast released: {', '.join(characters)}")
    return released

def _required(scene: object) -> list[str]:
    """What an inserted scene costs."""
    required = ["a scene added to the board"]
    location = getattr(scene, "location", "")
    if location:
        required.append(f"location needed: {location}")
    characters = getattr(scene, "characters", [])
    if characters:
        required.append(f"cast needed: {', '.join(characters)}")
    return required


def _scene_sort_key(number: str) -> tuple[int, str]:
    digits = "".join(c for c in number if c.isdigit())
    return (int(digits) if digits else 10**9, number)


__all__ = [
    "ClearanceFlag",
    "ClearanceReport",
    "FinanceLine",
    "FinanceReport",
    "ScheduleImpact",
    "ScheduleReport",
    "clearance_report",
    "finance_report",
    "schedule_report",
]
