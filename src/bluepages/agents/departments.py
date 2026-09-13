"""Department agents (Layer 5.1).

One diff, N agents, each writing the same revision up in its own department's
vocabulary. Props talks about objects and continuity. Transport talks about
picture vehicles and drivers. Locations talks about places and re-quotes. The
same DAY to NIGHT flip is a lighting and re-quote question for locations and a
call-sheet question for scheduling, and neither wants the other's phrasing.

Two decisions shape this module.

**Agents receive their own slice, not the whole diff.** Layer 3.4 already
decided which departments each finding belongs to, with the full mechanical
evidence in front of it. Handing every finding to every department would cost
seven times as much to re-make a decision that is already made, and worse, it
would let a department agent quietly disagree with the routing and report on a
change that was never meant for it. An agent here phrases and advises; it does
not re-judge.

**They genuinely run in parallel.** Each department is an independent call over
an independent slice, so they fan out across a thread pool. That is the
architecture's visible payoff and it is also just the right shape: seven
sequential calls for work with no dependencies is three minutes of waiting for
no reason.
"""

from __future__ import annotations

import concurrent.futures
from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict, Field

from bluepages.agents.sourcing import Sourced, Sourcer
from bluepages.events import EventKind, EventStream, NullStream
from bluepages.llm import ModelClient
from bluepages.llm.structured import SchemaError, parse_as
from bluepages.semantic.reasoning import Finding, SemanticResult
from bluepages.testdata import ChangeKind, Department

# The kinds that imply something has to be obtained, hired or booked. Mirrors
# agents/decisions.py's ACQUIRING: sourcing happens here, in the fan-out pool,
# so decisions.py only reads the result off the report.
_ACQUIRING = {ChangeKind.ELEMENT_ADDED.value, ChangeKind.SCENE_INSERTED.value}


@dataclass(frozen=True)
class DepartmentSpec:
    """What one department is, and how it should be spoken to.

    `concerns` and `ignores` are both in the prompt on purpose. Telling an agent
    what is *not* its business is what keeps props out of a scheduling change,
    and it is the difference between a report a department head reads and one
    they learn to skim.
    """

    department: Department
    title: str
    role: str
    concerns: str
    ignores: str


# The four from DECISIONS.md, plus the three non-department consumers of the
# same diff. Adding an eighth is a spec entry, not code.
SPECS: dict[Department, DepartmentSpec] = {
    Department.PROPS: DepartmentSpec(
        department=Department.PROPS,
        title="Props",
        role="the property master",
        concerns=(
            "objects an actor handles: what has to be bought, hired, made, "
            "doubled or continuity-tracked. Whether an object is new stock or "
            "something already on the truck that has moved scenes."
        ),
        ignores=(
            "scheduling, time of day, and anything nobody physically touches. "
            "A rewritten line that involves the same object in the same numbers "
            "is not a props change, however different the wording."
        ),
    ),
    Department.WARDROBE: DepartmentSpec(
        department=Department.WARDROBE,
        title="Wardrobe",
        role="the costume supervisor",
        concerns=(
            "what people wear: costume changes, doubles for stunts or damage, "
            "continuity across scenes, and anything a time-of-day or weather "
            "change implies for what a character has on."
        ),
        ignores="props an actor carries rather than wears, vehicles, and locations.",
    ),
    Department.TRANSPORT: DepartmentSpec(
        department=Department.TRANSPORT,
        title="Transport",
        role="the transport captain",
        concerns=(
            "picture vehicles, their condition and doubles, precision drivers, "
            "and unit moves implied by a location change. A vehicle that appears "
            "in shot is yours; one merely parked in the background is set dressing."
        ),
        ignores="hand props, costume, and dialogue.",
    ),
    Department.LOCATIONS: DepartmentSpec(
        department=Department.LOCATIONS,
        title="Locations",
        role="the location manager",
        concerns=(
            "places: new locations to find and secure, locations released by a "
            "cut, and re-quotes. A day-to-night flip at a practical location is "
            "priced differently and may need a fresh permit and neighbour notice."
        ),
        ignores="what is inside the location once it is secured.",
    ),
    Department.CAST: DepartmentSpec(
        department=Department.CAST,
        title="Cast",
        role="the casting director",
        concerns=(
            "roles and bookings: genuinely new parts, parts released by a cut, "
            "and changes to a character's scene count. A renamed character is "
            "NOT a new booking, and saying so would start an unnecessary casting "
            "call that costs real money."
        ),
        ignores="what the characters do, wear or carry.",
    ),
    Department.ART: DepartmentSpec(
        department=Department.ART,
        title="Art",
        role="the production designer",
        concerns=(
            "set dressing and construction: what is in shot but not handled, "
            "sets to build or strike, and dressing implied by a new location."
        ),
        ignores="objects an actor handles, which are the property master's.",
    ),
    Department.SFX: DepartmentSpec(
        department=Department.SFX,
        title="SFX",
        role="the special effects supervisor",
        concerns="practical effects, atmospherics, weather, fire, breakaways and rigs.",
        ignores="anything with no physical effect to rig.",
    ),
    Department.STUNTS: DepartmentSpec(
        department=Department.STUNTS,
        title="Stunts",
        role="the stunt coordinator",
        concerns="action requiring a performer at risk, doubles, rigging and rehearsal time.",
        ignores="ordinary physical action that carries no risk.",
    ),
    Department.SCHEDULE: DepartmentSpec(
        department=Department.SCHEDULE,
        title="Schedule",
        role="the 1st assistant director building the shooting schedule",
        concerns=(
            "what moves on the board: day-to-night flips, scenes omitted or "
            "inserted, company moves, and changes to how many days a location or "
            "a cast member is needed. Say what the consequence is, not what the "
            "new schedule should be: this surfaces impact, it does not solve it."
        ),
        ignores="which specific objects or costumes are involved.",
    ),
    Department.CLEARANCE: DepartmentSpec(
        department=Department.CLEARANCE,
        title="Clearance",
        role="the production lawyer handling rights",
        concerns=(
            "anything owned by someone else that the revision introduces: brands, "
            "song titles, book titles, artwork, recognisable real buildings and "
            "real people. State the risk and what the fix is, which is usually "
            "either written permission or a generic substitute."
        ),
        ignores="anything already cleared in an earlier draft, and generic objects.",
    ),
    Department.AD: DepartmentSpec(
        department=Department.AD,
        title="AD",
        role="the 1st assistant director",
        concerns=(
            "changes that alter how a scene is shot without changing any "
            "department's kit: a different physical action is a different camera "
            "setup, and that is time on the day."
        ),
        ignores="anything already routed to a department that owns it.",
    ),
    Department.SOCIAL: DepartmentSpec(
        department=Department.SOCIAL,
        title="Social",
        role="the production's social media coordinator",
        concerns=(
            "changes worth a public post: a striking new location, prop or "
            "vehicle, or a notable cast change. Write copy a production account "
            "could actually post, teasing without spoiling the story."
        ),
        ignores=(
            "anything routine: ordinary rewrites, scheduling changes, and "
            "continuity fixes nobody outside the crew would care about."
        ),
    ),
}


class DepartmentNote(BaseModel):
    """One line item in a department's report."""

    scene: str = Field(description="The scene number this concerns")
    note: str = Field(description="What changed, in this department's terms")
    action: str = Field(default="", description="What this department should do about it")
    # Whether this needs a decision before the shoot day, as opposed to being
    # information. The AD's approval screen sorts on this.
    urgent: bool = False


class DepartmentReport(BaseModel):
    """What one department is told about this revision.

    The model returns `summary` and `notes`; the rest is filled in by the caller
    so provenance survives into the database and the email.
    """

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(description="One or two sentences: the headline for this department")
    notes: list[DepartmentNote]


@dataclass
class Report:
    """A finished department report, with how it was produced."""

    department: Department
    title: str
    summary: str
    notes: list[DepartmentNote]
    finding_count: int = 0
    model_name: str = ""
    via_fallback: bool = False
    # Element name -> what was found for it. Populated for findings that add
    # a new element, so decisions.py can build a procurement proposal without
    # searching the web a second time.
    sourced: dict[str, Sourced] = field(default_factory=dict)

    @property
    def urgent_notes(self) -> list[DepartmentNote]:
        return [n for n in self.notes if n.urgent]

    @property
    def is_empty(self) -> bool:
        return not self.notes


@dataclass
class FanOut:
    """Every department's report for one revision."""

    reports: list[Report]
    departments_notified: int = 0
    departments_silent: int = 0
    fallbacks: int = 0

    def by_department(self, department: Department) -> Report | None:
        for report in self.reports:
            if report.department is department:
                return report
        return None

    def summary(self) -> dict[str, object]:
        return {
            "reports": len(self.reports),
            "notified": self.departments_notified,
            "silent": self.departments_silent,
            "notes": sum(len(r.notes) for r in self.reports),
            "urgent": sum(len(r.urgent_notes) for r in self.reports),
            "fallbacks": self.fallbacks,
        }


def _system_prompt(spec: DepartmentSpec) -> str:
    """The department's voice.

    Says what it cares about and, as importantly, what it does not. An agent
    that reports on everything trains its reader to skim.
    """
    return f"""You are {spec.role} on a film production, reading a script revision.

You have been given only the changes that were already routed to {spec.title}. Someone else decided they are yours; your job is to say what each one MEANS for your department and what you should do about it.

{spec.title} cares about: {spec.concerns}

{spec.title} does not care about: {spec.ignores}

Rules:
- Write for a head of department reading at the end of a shooting day. Plain, direct, specific.
- One note per change you were given. Do not invent changes you were not shown, and do not merge two changes into one note.
- `action` is what your department physically does next: buy, hire, re-dress, re-quote, book, check, or nothing. Leave it empty when there is genuinely nothing to do.
- Mark `urgent` only when a decision is needed before the scene shoots. Marking everything urgent is the same as marking nothing.
- If a change turns out not to affect you at all, say so plainly in the note rather than inventing work.

Return only JSON matching the schema. No prose outside it."""


def _prompt(spec: DepartmentSpec, findings: list[Finding]) -> str:
    """The department's slice of the revision."""
    lines = [
        f"{len(findings)} change(s) routed to {spec.title} in this revision.",
        "",
    ]
    for finding in findings:
        where = (
            f"scene {finding.from_scene} -> scene {finding.scene}"
            if finding.from_scene and finding.from_scene != finding.scene
            else f"scene {finding.scene}"
        )
        lines.append(f"[{finding.kind.value}] {where}")
        lines.append(f"  {finding.summary}")
        if finding.reasoning:
            lines.append(f"  why: {finding.reasoning}")
        if finding.element:
            lines.append(f"  element: {finding.element}")
        if finding.risk:
            lines.append(f"  risk: {finding.risk}")
        if finding.uncertain:
            # Passed through rather than hidden: a department head reading an
            # uncertain call should know it is uncertain.
            lines.append("  NOTE: the reasoning layer was not confident about this one")
        lines.append("")

    lines.append(
        'Return JSON: {"summary": "...", "notes": [{"scene": "...", "note": "...", '
        '"action": "...", "urgent": true|false}]}'
    )
    return "\n".join(lines)


def write_report(
    spec: DepartmentSpec,
    findings: list[Finding],
    client: ModelClient,
    stream: EventStream | None = None,
    sourcer: Sourcer | None = None,
) -> Report:
    """One department's report: sourcing (if this slice adds anything new),
    then one model call over that department's slice.

    Sourcing runs here, inside the fan-out thread pool, rather than serially
    afterward in decisions.py: it is a real web search and page fetch, the
    slowest thing in a run, and there is no reason seven departments should
    wait on each other's searches.
    """
    stream = stream or NullStream()
    stream.emit(
        EventKind.AGENT_STARTED,
        f"{spec.title}: {len(findings)} change(s)",
        department=spec.department.value,
        findings=len(findings),
    )

    sourced: dict[str, Sourced] = {}
    if sourcer is not None:
        for finding in findings:
            kind = finding.kind.value if hasattr(finding.kind, "value") else str(finding.kind)
            element = finding.element
            if kind not in _ACQUIRING or not element or element in sourced:
                continue
            stream.emit(
                EventKind.AGENT_STEP,
                f"{spec.title}: sourcing {element}",
                department=spec.department.value,
                step="sourcing",
                element=element,
            )
            sourced[element] = sourcer.find(element, spec.department.value, finding.summary)

    stream.emit(
        EventKind.AGENT_STEP,
        f"{spec.title}: drafting the brief",
        department=spec.department.value,
        step="drafting",
    )
    completion = client.complete(
        prompt=_prompt(spec, findings),
        system=_system_prompt(spec),
        # Phrasing for a department is not the judgment call the product turns
        # on, and the routing is already decided. The bulk model is the right
        # tier here, and it keeps a seven-way fan-out affordable.
        judgment=False,
        max_tokens=1500,
        cache_key_extra=f"department-{spec.department.value}-v1",
        label=spec.title,
    )
    parsed = parse_as(completion.text, DepartmentReport)

    report = Report(
        department=spec.department,
        title=spec.title,
        summary=parsed.summary,
        notes=parsed.notes,
        finding_count=len(findings),
        model_name=completion.model_name,
        via_fallback=completion.via_fallback,
        sourced=sourced,
    )
    stream.emit(
        EventKind.AGENT_FINISHED,
        f"{spec.title}: {len(report.notes)} note(s)"
        + (f", {len(report.urgent_notes)} urgent" if report.urgent_notes else ""),
        department=spec.department.value,
        notes=len(report.notes),
        urgent=len(report.urgent_notes),
        model=completion.model_name,
        via_fallback=completion.via_fallback,
    )
    return report


def fan_out(
    result: SemanticResult,
    client: ModelClient,
    stream: EventStream | None = None,
    departments: list[Department] | None = None,
    max_workers: int = 4,
    sourcer: Sourcer | None = None,
) -> FanOut:
    """Fan one revision out to every department that has something to hear.

    A department with no findings is skipped rather than asked to report that
    nothing happened. Silence is the correct output for a department a revision
    does not touch, and a "no changes for you" email every night is how a
    notification channel gets muted.

    One `Sourcer` is shared across every worker, so its search budget and
    on-disk cache apply to the whole run rather than to each department alone.
    """
    stream = stream or NullStream()
    sourcer = sourcer or Sourcer()

    wanted = departments or list(SPECS)
    slices: list[tuple[DepartmentSpec, list[Finding]]] = []
    silent = 0
    for department in wanted:
        spec = SPECS.get(department)
        if spec is None:
            continue
        findings = result.by_department(department)
        if findings:
            slices.append((spec, findings))
        else:
            silent += 1

    stream.emit(
        EventKind.INFO,
        f"fanning out to {len(slices)} departments ({silent} have nothing)",
        departments=[s.department.value for s, _ in slices],
    )

    reports: list[Report] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(write_report, spec, findings, client, stream, sourcer): spec
            for spec, findings in slices
        }
        for future in concurrent.futures.as_completed(futures):
            spec = futures[future]
            try:
                reports.append(future.result())
            except SchemaError as exc:
                # One department's report failing must not lose the other six.
                # It is reported rather than dropped: a department that silently
                # receives nothing cannot tell that from having no changes.
                stream.emit(
                    EventKind.PARSE_WARNING,
                    f"{spec.title}: report returned unusable JSON, skipped",
                    department=spec.department.value,
                    error=str(exc)[:200],
                )

    reports.sort(key=lambda r: r.title)
    fan = FanOut(
        reports=reports,
        departments_notified=len(reports),
        departments_silent=silent,
        fallbacks=sum(1 for r in reports if r.via_fallback),
    )
    stream.emit(
        EventKind.INFO,
        f"{len(reports)} department reports, "
        f"{sum(len(r.notes) for r in reports)} notes",
        fan_out=fan.summary(),
    )
    return fan


__all__ = [
    "SPECS",
    "DepartmentNote",
    "DepartmentReport",
    "DepartmentSpec",
    "FanOut",
    "Report",
    "fan_out",
    "write_report",
]
