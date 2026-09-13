"""One revision, end to end.

`run_pipeline` is the whole product as a function: two drafts in, department
reports out, every stage emitting Layer 3.6 events as it goes. The CLI calls it,
the Lambda calls it, and the API calls it, so there is one sequence to get right
rather than three that drift apart.

Two things it deliberately does not do.

It does not print. The caller owns presentation, which is why the CLI can render
tables while the Lambda writes JSON to the database and neither needs the other's
formatting.

It does not send anything. Reports persist unapproved and stay that way until a
human approves them, which is what makes "surfaces only for a real decision"
true rather than a claim. That holds when the trigger is an S3 upload just as it
does when a person typed the command: an agent that wakes on its own and emails
eight departments without asking is exactly the thing the AD would turn off.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from bluepages.events import EventKind, EventStream, NullStream


class Stage(str, Enum):
    """The pipeline stages, in order.

    Named so a caller can ask for a prefix of the work: the mechanical diff
    costs nothing and is worth running on its own, while the fan-out is the
    most expensive stage and is skipped when only the findings are wanted.
    """

    PARSE = "parse"
    DIFF = "diff"
    ELEMENTS = "elements"
    REASON = "reason"
    FAN_OUT = "fan_out"


_ORDER = [Stage.PARSE, Stage.DIFF, Stage.ELEMENTS, Stage.REASON, Stage.FAN_OUT]


def _reaches(stop_after: Stage, stage: Stage) -> bool:
    return _ORDER.index(stage) <= _ORDER.index(stop_after)


@dataclass
class PipelineResult:
    """Everything one run produced, whatever stage it stopped at.

    Fields are None rather than absent when a stage did not run, so a caller
    reading `result.fan` after asking to stop at `REASON` gets None instead of
    an attribute error.
    """

    before: Any = None
    after: Any = None
    alignment: Any = None
    diff: Any = None
    elements: Any = None
    findings: Any = None
    fan: Any = None
    clearance: Any = None
    schedule: Any = None
    finance: Any = None
    decisions: Any = None
    budget: dict[str, Any] = field(default_factory=dict)
    stopped_after: Stage = Stage.FAN_OUT
    # Set by `persist`, so a caller can report what was written without
    # re-opening the database.
    persisted: Any = None
    production: str = ""

    def summary(self) -> dict[str, Any]:
        """What happened, in the shape the API and the Lambda both return."""
        out: dict[str, Any] = {
            "production": self.production,
            "stopped_after": self.stopped_after.value,
            "budget": dict(self.budget),
        }
        if self.alignment is not None:
            out["alignment"] = self.alignment.summary()
        if self.diff is not None:
            out["diff"] = self.diff.summary()
        if self.elements is not None:
            out["elements"] = self.elements.summary()
        if self.findings is not None:
            out["findings"] = self.findings.summary()
        if self.fan is not None:
            out["fan_out"] = self.fan.summary()
        if self.clearance is not None:
            out["clearance"] = self.clearance.summary()
        if self.schedule is not None:
            out["schedule"] = self.schedule.summary()
        if self.finance is not None:
            out["finance"] = self.finance.summary()
        return out


def scenes_worth_extracting(alignment: Any, diff: Any) -> set[str]:
    """Scene numbers worth spending an extraction call on.

    Scoped to what changed. Extracting an unchanged 120-scene draft is 120 calls
    to learn what the previous run already knows, and it is the single easiest
    way to turn a cheap run into an expensive one.

    Relocation candidates pull in both ends, because deciding whether the letter
    opener in scene 7 is the one that left scene 3 needs both scenes described.
    """
    from bluepages.diff import AlignmentKind

    return (
        {s.number for s in diff.scenes}
        | {p.number for p in alignment.of_kind(AlignmentKind.INSERTED)}
        | {c.to_scene for c in diff.relocation_candidates}
        | {c.from_scene for c in diff.relocation_candidates}
    )


def run_pipeline(
    before: Path,
    after: Path,
    client: Any = None,
    stream: EventStream | None = None,
    stop_after: Stage = Stage.FAN_OUT,
    departments: list[Any] | None = None,
    production: str = "",
    skip_elements: bool = False,
) -> PipelineResult:
    """Parse, align, diff, extract, reason, fan out.

    `client` is optional so the free stages run without one: a caller that only
    wants the mechanical diff should not need Bedrock configured to get it.
    Asking for a model stage without a client is a programming error and raises
    rather than quietly returning nothing.
    """
    from bluepages.diff import align, diff_drafts
    from bluepages.parse import parse_script

    stream = stream or NullStream()
    result = PipelineResult(
        stopped_after=stop_after,
        production=production or before.stem,
    )

    if _needs_model(stop_after, skip_elements) and client is None:
        raise ValueError(
            f"stop_after={stop_after.value} needs a model client; "
            "pass one, or stop at 'diff' for the stages that cost nothing"
        )

    stream.emit(
        EventKind.RUN_STARTED,
        f"{before.name} -> {after.name}",
        before=str(before),
        after=str(after),
        production=result.production,
        stop_after=stop_after.value,
    )

    result.before = parse_script(before, stream=stream)
    result.after = parse_script(after, stream=stream)
    if not _reaches(stop_after, Stage.DIFF):
        return _finish(result, client, stream)

    result.alignment = align(result.before, result.after, stream=stream)
    result.diff = diff_drafts(result.alignment, stream=stream)
    if not _reaches(stop_after, Stage.ELEMENTS):
        return _finish(result, client, stream)

    if not skip_elements:
        from bluepages.semantic import extract_draft

        touched = sorted(scenes_worth_extracting(result.alignment, result.diff))
        result.elements = extract_draft(
            result.after, client, stream=stream, scenes=touched
        )
    if not _reaches(stop_after, Stage.REASON):
        return _finish(result, client, stream)

    from bluepages.semantic import reason_about_diff

    result.findings = reason_about_diff(
        result.diff, client, elements=result.elements, stream=stream
    )

    if result.findings.ripple_questions:
        from bluepages.semantic.ripples import check_ripples, confirmed_findings

        ripples = check_ripples(
            result.findings.ripple_questions, result.after, client, stream=stream
        )
        confirmed = confirmed_findings(ripples)
        if confirmed:
            result.findings.findings.extend(confirmed)
            result.findings.findings.sort(
                key=lambda f: (f.scene or "", f.kind.value)
            )

    if not _reaches(stop_after, Stage.FAN_OUT):
        return _finish(result, client, stream)

    from bluepages.agents import clearance_report, fan_out, schedule_report

    result.fan = fan_out(result.findings, client, stream=stream, departments=departments)
    # Both read the diff that already exists and neither costs a model call,
    # but they still get agent nodes on the graph: clearance is the console's
    # opening beat and should not be invisible next to the paid departments.
    result.clearance = clearance_report(
        result.findings, elements=result.elements, stream=stream
    )
    result.schedule = schedule_report(
        result.diff, result.before, result.after, stream=stream
    )
    # Decisions and Finance both need the production's persisted budget row
    # to know whether a procurement clears under a limit, so both are derived
    # in `persist`, once the production exists in the database, not here.
    return _finish(result, client, stream)


def _needs_model(stop_after: Stage, skip_elements: bool) -> bool:
    """Whether this run will make a model call at all.

    Stopping at ELEMENTS with extraction skipped is the one combination that
    reaches a model stage and still spends nothing, so it does not require a
    client.
    """
    if not _reaches(stop_after, Stage.ELEMENTS):
        return False
    return not (skip_elements and stop_after is Stage.ELEMENTS)


def _finish(result: PipelineResult, client: Any, stream: EventStream) -> PipelineResult:
    """Record the spend and close the run."""
    if client is not None and getattr(client, "budget", None) is not None:
        result.budget = client.budget.summary()
    stream.emit(
        EventKind.RUN_FINISHED,
        f"run finished at stage {result.stopped_after.value}",
        **result.summary(),
    )
    return result


def persist(
    result: PipelineResult,
    db_path: Path | None = None,
) -> Any:
    """Write a finished run to the element database.

    Separate from `run_pipeline` because persistence is the caller's decision:
    the CLI persists on `--save`, the Lambda always does, and a scoring run
    against the answer key should not litter the inventory with test data.

    Where it writes is `open_database`'s call: an explicit path, then Supabase
    when configured, then a local file.
    """
    from bluepages.store import Repository, open_database

    if result.diff is None or result.findings is None:
        raise ValueError("nothing to persist: the run did not reach the reasoning stage")

    with open_database(path=db_path) as db:
        db.create_schema()
        repo = Repository(db)
        persisted = repo.persist_run(
            title=result.production,
            before=result.before,
            after=result.after,
            diff=result.diff,
            result=result.findings,
            elements=result.elements,
            budget=result.budget,
        )
        if result.fan is not None:
            repo.save_reports(persisted.to_draft_id, result.fan)

        # What the agent decided, as opposed to what it found. Needs the
        # production's own budget row to know whether a procurement clears
        # under a department's auto-approve limit, so it runs here, once the
        # production exists, rather than in run_pipeline.
        from bluepages.agents import budget as budget_log
        from bluepages.agents import decisions as decision_log
        from bluepages.agents import finance_report

        budget_rows = {row["department"]: row for row in budget_log.summary(db, persisted.production_id)}
        reports = list(getattr(result.fan, "reports", []) or []) if result.fan else []
        result.decisions = decision_log.from_findings(
            result.findings.findings, reports, budget=budget_rows
        )
        decision_log.persist(db, persisted.production_id, persisted.run_id, result.decisions)
        result.finance = finance_report(result.decisions)

    result.persisted = persisted
    return persisted


__all__ = [
    "PipelineResult",
    "Stage",
    "persist",
    "run_pipeline",
    "scenes_worth_extracting",
]
