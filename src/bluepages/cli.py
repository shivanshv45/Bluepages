"""The `bluepages` command line.

`doctor` is the Layer 0 gate: it proves a script can call Bedrock and get a
response back, and when it cannot, it says exactly which console step is
missing rather than surfacing a raw botocore traceback.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from bluepages.config import get_settings
from bluepages.events import CollectingStream, EventKind, RichConsoleStream, TeeStream

app = typer.Typer(
    name="bluepages",
    help="Diff film script drafts and route the consequences per department.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()

OK = "[green]OK[/green]"
FAIL = "[red]FAIL[/red]"
WARN = "[yellow]--[/yellow]"


@app.command()
def parse(
    path: Annotated[Path, typer.Argument(help="Script file to parse (.fdx)")],
    out: Annotated[Path | None, typer.Option("-o", "--out", help="Write JSON here")] = None,
    stats: Annotated[bool, typer.Option("--stats", help="Print a summary, not the full JSON")] = False,
    verbose: Annotated[bool, typer.Option("-v", "--verbose", help="Show per-scene events")] = False,
) -> None:
    """Parse a draft into the structured script model."""
    from bluepages.parse import UnsupportedFormatError, parse_script
    from bluepages.parse.fdx import FdxParseError

    collected = CollectingStream()
    stream = TeeStream(collected, RichConsoleStream(verbose=verbose))

    try:
        screenplay = parse_script(path, stream=stream)
    except (FdxParseError, UnsupportedFormatError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    warnings = collected.count(EventKind.PARSE_WARNING)

    if stats:
        _print_stats(screenplay, warnings)
    elif out is None:
        console.print_json(screenplay.model_dump_json())

    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(screenplay.model_dump_json(indent=2), encoding="utf-8")
        console.print(f"[green]wrote[/green] {out} ({out.stat().st_size:,} bytes)")


def _print_stats(screenplay, warnings: int) -> None:
    s = screenplay.stats()
    table = Table(title=s["title"] or "(untitled)", show_header=False, title_justify="left")
    table.add_column(style="dim")
    table.add_column()

    table.add_row("source tier", s["source_tier"])
    table.add_row("scenes", str(s["scenes"]))
    table.add_row(
        "numbered",
        f"{s['numbered_scenes']}"
        + ("" if s["has_scene_numbers"] else "  [yellow](alignment falls back to headings)[/yellow]"),
    )
    table.add_row("omitted", str(s["omitted_scenes"]))
    table.add_row("revised scenes", str(s["revised_scenes"]))
    table.add_row("revision sets", str(s["revision_sets"]))
    table.add_row("characters", str(s["characters"]))
    table.add_row(
        "elements",
        ", ".join(f"{k}={v}" for k, v in s["elements_by_type"].items()),
    )
    if warnings:
        table.add_row("warnings", f"[yellow]{warnings}[/yellow]")
    console.print(table)


@app.command()
def doctor() -> None:
    """Check the environment: config, credentials, Bedrock access, fallbacks.

    This is the Layer 0 gate. It ends by actually calling Bedrock, because
    "credentials resolve" and "the model answers" are different things.
    """
    settings = get_settings()
    problems: list[str] = []

    console.print("\n[bold]Bluepages: environment check[/bold]\n")

    # --- Python -----------------------------------------------------------
    v = sys.version_info
    py_ok = (3, 12) <= (v.major, v.minor) < (3, 14)
    console.print(f"  {OK if py_ok else WARN} python {v.major}.{v.minor}.{v.micro}")
    if not py_ok:
        console.print(
            "       [dim]3.12 or 3.13 recommended: matches Lambda and AgentCore runtimes[/dim]"
        )

    # --- .env -------------------------------------------------------------
    env_file = settings.project_root / ".env"
    if env_file.exists():
        console.print(f"  {OK} .env found")
    else:
        console.print(f"  {WARN} no .env (using defaults; copy .env.example)")

    console.print(f"  {OK} region {settings.aws_region}")
    console.print(f"       [dim]judgment: {settings.bedrock_model_judgment}[/dim]")
    console.print(f"       [dim]bulk:     {settings.bedrock_model_bulk}[/dim]")

    # --- AWS credentials ---------------------------------------------------
    console.print("\n[bold]AWS[/bold]\n")
    identity = None
    try:
        import boto3

        sts = boto3.client("sts", region_name=settings.aws_region)
        identity = sts.get_caller_identity()
        console.print(f"  {OK} credentials resolve")
        console.print(f"       [dim]account {identity['Account']}[/dim]")
        console.print(f"       [dim]{identity['Arn']}[/dim]")
    except Exception as exc:
        console.print(f"  {FAIL} no AWS credentials ({type(exc).__name__})")
        problems.append(
            "Configure AWS credentials: run `aws configure`, or set AWS_ACCESS_KEY_ID "
            "and AWS_SECRET_ACCESS_KEY in .env"
        )

    # --- Bedrock model access ---------------------------------------------
    if identity is not None:
        _check_bedrock(settings, problems)

    # --- Fallback providers ------------------------------------------------
    console.print("\n[bold]Fallback providers[/bold] [dim](optional; resilience only)[/dim]\n")
    console.print(f"  {OK if settings.groq_api_key else WARN} Groq")
    console.print(f"  {OK if settings.gemini_api_key else WARN} Gemini")
    if not settings.has_fallback_provider:
        console.print(
            "       [dim]without these the chain is Sonnet -> Haiku only, both on Bedrock[/dim]"
        )

    # --- Later layers ------------------------------------------------------
    console.print("\n[bold]Later layers[/bold] [dim](not needed yet)[/dim]\n")
    console.print(f"  {OK if settings.s3_bucket else WARN} S3 bucket        [dim]Layer 6[/dim]")
    console.print(f"  {OK if settings.supabase_url else WARN} Supabase         [dim]Layer 4[/dim]")
    console.print(f"  {OK if settings.resend_api_key else WARN} Resend           [dim]Layer 8[/dim]")

    # --- Cost guards -------------------------------------------------------
    console.print("\n[bold]Cost guards[/bold]\n")
    console.print(f"  {OK} max_tokens per call: {settings.bluepages_max_tokens}")
    console.print(f"  {OK} max model calls per run: {settings.bluepages_max_llm_calls_per_run}")
    console.print(
        f"  {OK if settings.bluepages_cache_llm else WARN} response caching: "
        f"{'on' if settings.bluepages_cache_llm else 'off'}"
    )

    # --- Verdict -----------------------------------------------------------
    if problems:
        console.print("\n[bold red]Not ready.[/bold red] To fix:\n")
        for i, p in enumerate(problems, 1):
            console.print(f"  {i}. {p}")
        console.print()
        raise typer.Exit(1)

    console.print("\n[bold green]Ready.[/bold green] Layer 0 complete.\n")


def _check_bedrock(settings, problems: list[str]) -> None:
    """Verify the two models are enabled, then actually call one."""
    import boto3

    console.print("\n[bold]Bedrock[/bold]\n")

    wanted = {
        "judgment": settings.bedrock_model_judgment,
        "bulk": settings.bedrock_model_bulk,
    }

    # Listing is a cheap, free check that the account can see the catalogue.
    # It also lets us tell "the model is not enabled" apart from "that model id
    # does not exist in this region", which are different console fixes.
    available: set[str] = set()
    try:
        bedrock = boto3.client("bedrock", region_name=settings.aws_region)
        available = {
            m["modelId"] for m in bedrock.list_foundation_models(byProvider="anthropic")[
                "modelSummaries"
            ]
        }
        console.print(f"  {OK} catalogue reachable ({len(available)} Anthropic models)")
    except Exception as exc:
        console.print(f"  {WARN} could not list models ({type(exc).__name__})")

    for role, model_id in wanted.items():
        if not available:
            continue
        # An inference-profile id ("us.anthropic...", "global.anthropic...")
        # will not appear verbatim in the catalogue; match on the base id.
        base = model_id.split(".", 1)[-1] if model_id.count(".") > 1 else model_id
        if not any(base in candidate for candidate in available):
            console.print(f"  {WARN} {role:9} id not in this region's catalogue: {model_id}")
            close = sorted(m for m in available if "claude" in m)[:3]
            if close:
                console.print(f"       [dim]available e.g. {', '.join(close)}[/dim]")

    # The real test: one tiny call. `max_tokens` is small on purpose.
    from bluepages.llm import ModelClient, RunBudget

    for role, model_id in wanted.items():
        client = ModelClient(settings=settings, budget=RunBudget(max_calls=4))
        try:
            completion = client.complete(
                prompt="Reply with the single word: ready",
                judgment=(role == "judgment"),
                max_tokens=16,
                cache_key_extra="doctor",
            )
            answered = completion.model_id
            console.print(
                f"  {OK} {role:9} answered [dim]({answered})[/dim]"
                + ("  [yellow]via fallback[/yellow]" if completion.via_fallback else "")
            )
        except Exception as exc:
            console.print(f"  {FAIL} {role:9} {type(exc).__name__}: {str(exc)[:120]}")
            problems.append(
                f"Enable Bedrock model access for the {role} model ({model_id}) in "
                f"{settings.aws_region}: Bedrock console -> Model access -> Modify model access"
            )


@app.command()
def diff(
    before: Annotated[Path, typer.Argument(help="The earlier draft")],
    after: Annotated[Path, typer.Argument(help="The newer draft")],
    verbose: Annotated[bool, typer.Option("-v", "--verbose", help="Show progress events")] = False,
    key_path: Annotated[
        Path | None, typer.Option("--key", help="Score against an answer key")
    ] = None,
) -> None:
    """Diff two drafts: align scenes, then diff inside each aligned pair.

    The mechanical layer only. It reports what changed, not what the changes
    mean; the semantic pass is Layer 3.4 and needs Bedrock.
    """
    from bluepages.diff import align, diff_drafts
    from bluepages.parse import parse_script

    stream = RichConsoleStream(verbose=verbose)
    old_draft = parse_script(before, stream=stream)
    new_draft = parse_script(after, stream=stream)

    alignment = align(old_draft, new_draft, stream=stream)
    result = diff_drafts(alignment, stream=stream)

    console.print()
    _print_diff(alignment, result)

    if key_path is not None:
        _score_against_key(key_path, alignment, result)


def _print_diff(alignment, result) -> None:
    from bluepages.diff import AlignmentKind

    summary = alignment.summary()
    table = Table(title="alignment", show_header=False, title_justify="left")
    table.add_column(style="dim")
    table.add_column()
    degraded = "  [yellow](degraded)[/yellow]" if summary["degraded"] else ""
    table.add_row("method", f"{summary['method']}{degraded}")
    table.add_row(
        "matched",
        f"{summary['matched']}  ({summary['changed']} changed, "
        f"{summary['unchanged']} identical)",
    )
    table.add_row("inserted", str(summary["inserted"]))
    table.add_row("omitted", str(summary["omitted"]))
    if summary["removed"]:
        table.add_row(
            "removed",
            f"[yellow]{summary['removed']}  (gone without an OMITTED marker)[/yellow]",
        )
    table.add_row("moved", str(summary["moved"]))
    console.print(table)

    for pair in alignment.of_kind(AlignmentKind.OMITTED):
        console.print(f"\n[bold]scene {pair.number}[/bold]  [red]OMITTED[/red]")
        if pair.before:
            console.print(f"    was: {pair.before.heading}")

    for pair in alignment.of_kind(AlignmentKind.INSERTED):
        console.print(f"\n[bold]scene {pair.number}[/bold]  [green]INSERTED[/green]")
        if pair.after:
            console.print(f"    {pair.after.heading}")

    for scene in result.scenes:
        console.print(f"\n[bold]scene {scene.number}[/bold]")
        pair = scene.pair
        if scene.heading_changed and pair.before and pair.after:
            console.print(f"    [magenta]heading[/magenta]  {pair.before.heading}")
            console.print(f"             {pair.after.heading}")
        for span in scene.spans:
            console.print(f"    [dim]{span.kind.value} {span.element_type.value}[/dim]")
            if span.before:
                console.print(f"      [red]-[/red] {span.before.text}")
            if span.after:
                console.print(f"      [green]+[/green] {span.after.text}")

    if result.relocation_candidates:
        console.print(
            "\n[bold]possible relocations[/bold] "
            "[dim](for the semantic layer to judge)[/dim]"
        )
        for candidate in result.relocation_candidates:
            console.print(
                f"    {candidate.phrase!r}  "
                f"scene {candidate.from_scene} -> {candidate.to_scene}"
            )


def _score_against_key(key_path: Path, alignment, result) -> None:
    """Compare what the diff found against the labelled ground truth."""
    from bluepages.diff import AlignmentKind
    from bluepages.testdata import load_answer_key

    answer_key = load_answer_key(key_path)
    expected = answer_key.scenes_touched()
    found = (
        {s.number for s in result.scenes}
        | {p.number for p in alignment.of_kind(AlignmentKind.OMITTED)}
        | {p.number for p in alignment.of_kind(AlignmentKind.INSERTED)}
    )
    missed = sorted(expected - found)
    spurious = sorted(found - expected)

    console.print("\n[bold]scored against the answer key[/bold]")
    console.print(f"    expected {len(expected)}: {', '.join(sorted(expected))}")
    console.print(f"    found    {len(found)}: {', '.join(sorted(found))}")
    if missed:
        console.print(f"    [red]missed: {', '.join(missed)}[/red]")
    if spurious:
        console.print(f"    [yellow]false positives: {', '.join(spurious)}[/yellow]")
    if not missed and not spurious:
        console.print("    [green]every labelled scene found, nothing spurious[/green]")
    console.print()


@app.command()
def key(
    path: Annotated[Path, typer.Argument(help="Answer key JSON")],
    fixtures: Annotated[
        Path | None, typer.Option("--fixtures", help="Where the drafts live")
    ] = None,
) -> None:
    """Show a Layer 2 answer key and check it against its drafts.

    A key that has drifted from its fixtures is worse than no key: it reports
    success against scenes that no longer exist. This validates every claim.
    """
    from bluepages.testdata import load_answer_key, summarise, validate

    try:
        answer_key = load_answer_key(path)
    except (FileNotFoundError, ValueError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    summary = summarise(answer_key)
    table = Table(
        title=f"answer key: {summary['pair']}", show_header=False, title_justify="left"
    )
    table.add_column(style="dim")
    table.add_column()
    table.add_row("drafts", summary["drafts"])
    table.add_row("changes", str(summary["changes"]))
    table.add_row("by kind", ", ".join(f"{k}={v}" for k, v in summary["by_kind"].items()))
    table.add_row(
        "by dept", ", ".join(f"{k}={v}" for k, v in summary["by_department"].items())
    )
    table.add_row("scenes touched", ", ".join(summary["scenes_touched"]))
    console.print(table)

    for change in answer_key.changes:
        where = f"{change.from_scene or '-'} -> {change.to_scene or '-'}"
        console.print(
            f"  [bold]{change.id}[/bold]  [dim]{change.kind.value}[/dim]  {where}"
        )
        console.print(f"      {change.summary}")

    problems = validate(answer_key, fixtures or path.parent)
    if problems:
        console.print(f"\n[bold red]{len(problems)} problems:[/bold red]")
        for problem in problems:
            console.print(f"  - {problem}")
        raise typer.Exit(1)
    console.print("\n[bold green]Key validates against its drafts.[/bold green]\n")


@app.command()
def reason(
    before: Annotated[Path, typer.Argument(help="The earlier draft")],
    after: Annotated[Path, typer.Argument(help="The newer draft")],
    key_path: Annotated[
        Path | None, typer.Option("--key", help="Score against an answer key")
    ] = None,
    verbose: Annotated[bool, typer.Option("-v", "--verbose", help="Show progress events")] = False,
    skip_elements: Annotated[
        bool,
        typer.Option("--no-elements", help="Skip Layer 3.3 extraction (fewer calls)"),
    ] = False,
    max_calls: Annotated[
        int | None, typer.Option("--max-calls", help="Ceiling for this run")
    ] = None,
    json_out: Annotated[
        Path | None, typer.Option("--json", help="Write the findings here")
    ] = None,
    save: Annotated[
        bool, typer.Option("--save", help="Persist the run to the element database")
    ] = False,
    production: Annotated[
        str | None, typer.Option("--production", help="Production title for --save")
    ] = None,
    db_path: Annotated[
        Path | None, typer.Option("--db", help="SQLite file to use instead of the default")
    ] = None,
) -> None:
    """The full pipeline: parse, align, diff, extract elements, reason.

    This is Layers 3.3 and 3.4 and it costs money. Every call is bounded by the
    run budget and cached on disk, so re-running on an unchanged pair is free.
    """
    from bluepages.llm import ModelClient, RunBudget
    from bluepages.pipeline import Stage, run_pipeline

    settings = get_settings()
    stream = RichConsoleStream(verbose=verbose)
    budget = RunBudget(
        max_calls=max_calls or settings.bluepages_max_llm_calls_per_run
    )
    client = ModelClient(settings=settings, stream=stream, budget=budget)

    run = run_pipeline(
        before,
        after,
        client=client,
        stream=stream,
        stop_after=Stage.REASON,
        production=production or before.stem,
        skip_elements=skip_elements,
    )
    result, elements = run.findings, run.elements

    console.print()
    _print_findings(result, elements)

    spend = run.budget
    console.print(
        f"\n[dim]{spend['calls']} model calls, "
        f"{spend['input_tokens'] + spend['output_tokens']} tokens, "
        f"{', '.join(f'{k}={v}' for k, v in spend['by_model'].items()) or 'all cached'}[/dim]"
    )

    if json_out is not None:
        json_out.write_text(result.model_dump_json(indent=2), encoding="utf-8")
        console.print(f"[dim]findings written to {json_out}[/dim]")

    if save:
        _persist(run, db_path)

    if key_path is not None:
        card = _score_semantic(key_path, result)
        if not card.passed:
            raise typer.Exit(1)


def _persist(run, db_path):
    """Write the run to the element database and report what it recorded."""
    from bluepages.pipeline.run import persist

    persisted = persist(run, db_path=db_path)

    console.print()
    console.print(
        f"[bold]saved[/bold]  {persisted.changes} changes, "
        f"{persisted.elements} elements to production {run.production!r}"
    )
    if persisted.identities_matched:
        console.print(
            f"       [dim]{persisted.identities_matched} elements matched an object "
            f"already in the inventory, {persisted.identities_created} are new[/dim]"
        )


def _scenes_touched(alignment, mechanical) -> set[str]:
    """Scene numbers worth spending an extraction call on."""
    from bluepages.diff import AlignmentKind

    return (
        {s.number for s in mechanical.scenes}
        | {p.number for p in alignment.of_kind(AlignmentKind.INSERTED)}
        | {c.to_scene for c in mechanical.relocation_candidates}
        | {c.from_scene for c in mechanical.relocation_candidates}
    )


def _print_findings(result, elements) -> None:
    """The AD's screen, in the terminal."""
    if elements is not None:
        summary = elements.summary()
        console.print(
            f"[bold]elements[/bold]  {summary['elements']} found "
            f"({summary['distinct']} distinct) across {summary['scenes']} scenes"
            + (f", [yellow]{summary['branded']} branded[/yellow]" if summary["branded"] else "")
        )

    if not result.findings:
        console.print("\n[yellow]no findings[/yellow]")
        return

    console.print(f"\n[bold]{len(result.findings)} findings[/bold]\n")
    for finding in result.findings:
        where = (
            f"{finding.from_scene} -> {finding.scene}"
            if finding.from_scene and finding.from_scene != finding.scene
            else f"scene {finding.scene}"
        )
        flag = "  [yellow]uncertain[/yellow]" if finding.uncertain else ""
        console.print(f"  [bold]{where}[/bold]  [dim]{finding.kind.value}[/dim]{flag}")
        console.print(f"      {finding.summary}")
        if finding.reasoning:
            console.print(f"      [dim]{finding.reasoning}[/dim]")
        routes = ", ".join(d.value for d in finding.departments) or "[red]unrouted[/red]"
        risk = f"  risk: {finding.risk}" if finding.risk else ""
        console.print(f"      [cyan]{routes}[/cyan]  [dim]{finding.confidence:.0%}{risk}[/dim]")
        console.print()

    counts = result.department_counts
    if counts:
        console.print(
            "[bold]by department[/bold]  "
            + "  ".join(f"{k} [bold]{v}[/bold]" for k, v in counts.items())
        )
    if result.fallbacks:
        console.print(f"[yellow]{result.fallbacks} scenes answered via fallback[/yellow]")
    if result.rejected:
        console.print(
            f"[dim]{len(result.rejected)} findings dropped for naming an unchanged scene[/dim]"
        )


def _score_semantic(key_path: Path, result):
    """Score the semantic run against the labelled ground truth."""
    from bluepages.semantic import score
    from bluepages.testdata import load_answer_key

    answer_key = load_answer_key(key_path)
    card = score(answer_key, result)

    console.print("\n[bold]scored against the answer key[/bold]\n")
    table = Table(show_header=True, header_style="dim")
    table.add_column("change")
    table.add_column("found")
    table.add_column("kind")
    table.add_column("departments")
    table.add_column("forbidden")

    for entry in card.scores:
        found = "[green]yes[/green]" if entry.found else "[red]NO[/red]"
        if not entry.found:
            kind = "[dim]-[/dim]"
        elif entry.kind_correct:
            kind = f"[green]{entry.expected_kind.value}[/green]"
        else:
            kind = (
                f"[red]{entry.found_kind.value if entry.found_kind else '?'}[/red] "
                f"[dim]want {entry.expected_kind.value}[/dim]"
            )
        if entry.departments_missed:
            depts = "[red]missed " + ",".join(d.value for d in entry.departments_missed) + "[/red]"
        elif entry.found:
            depts = "[green]all[/green]"
        else:
            depts = "[dim]-[/dim]"
        forbidden = (
            "[red]" + "; ".join(entry.said_forbidden) + "[/red]"
            if entry.said_forbidden
            else ""
        )
        table.add_row(entry.change_id, found, kind, depts, forbidden)
    console.print(table)

    summary = card.summary()
    console.print(
        f"\n  recall     {summary['found']}/{summary['changes']}"
        f"   [dim]labelled changes found[/dim]"
    )
    console.print(
        f"  accuracy   {summary['fully_correct']}/{summary['changes']}"
        f"   [dim]found, judged right, nothing forbidden said[/dim]"
    )
    console.print(
        f"  routing    {summary['department_recall']:.0%}"
        f"   [dim]of the departments the key expects[/dim]"
    )
    if card.forbidden_said:
        console.print(
            f"  [bold red]{card.forbidden_said} forbidden phrases said[/bold red]"
            "   [dim]the expensive errors[/dim]"
        )
    if card.findings_in_unchanged_scenes:
        console.print(
            f"  [bold red]{len(card.findings_in_unchanged_scenes)} findings in scenes "
            "the key calls unchanged[/bold red]"
        )
        for note in card.findings_in_unchanged_scenes:
            console.print(f"      [red]{note}[/red]")
    if card.unmatched_findings:
        console.print(
            f"\n  [dim]{len(card.unmatched_findings)} findings the key does not label "
            "(not penalised, read them):[/dim]"
        )
        for note in card.unmatched_findings:
            console.print(f"      [dim]{note}[/dim]")

    if card.passed:
        console.print("\n[bold green]PASS[/bold green]  every labelled change judged correctly\n")
    else:
        console.print("\n[bold red]FAIL[/bold red]  see above\n")
    return card


@app.command("fan-out")
def fan_out_command(
    before: Annotated[Path, typer.Argument(help="The earlier draft")],
    after: Annotated[Path, typer.Argument(help="The newer draft")],
    department: Annotated[
        str | None, typer.Option("--department", help="One department only")
    ] = None,
    verbose: Annotated[bool, typer.Option("-v", "--verbose", help="Show progress events")] = False,
    max_calls: Annotated[
        int | None, typer.Option("--max-calls", help="Ceiling for this run")
    ] = None,
    json_out: Annotated[
        Path | None, typer.Option("--json", help="Write the reports here")
    ] = None,
    save: Annotated[
        bool, typer.Option("--save", help="Persist the run and the reports")
    ] = False,
    production: Annotated[
        str | None, typer.Option("--production", help="Production title for --save")
    ] = None,
    db_path: Annotated[
        Path | None, typer.Option("--db", help="SQLite file to use instead of the default")
    ] = None,
) -> None:
    """The full pipeline plus the department fan-out (Layer 5).

    One revision becomes N department reports, each in that department's own
    vocabulary, plus the clearance and schedule views of the same diff. The
    department agents run in parallel because their work is independent.
    """
    from bluepages.llm import ModelClient, RunBudget
    from bluepages.pipeline import run_pipeline
    from bluepages.testdata import Department

    known = {d.value for d in Department}
    if department is not None and department not in known:
        console.print(f"[red]unknown department {department!r}[/red]")
        console.print(f"[dim]known: {', '.join(sorted(known))}[/dim]")
        raise typer.Exit(1)

    settings = get_settings()
    stream = RichConsoleStream(verbose=verbose)
    budget = RunBudget(max_calls=max_calls or settings.bluepages_max_llm_calls_per_run)
    client = ModelClient(settings=settings, stream=stream, budget=budget)

    run = run_pipeline(
        before,
        after,
        client=client,
        stream=stream,
        departments=[Department(department)] if department else None,
        production=production or before.stem,
    )
    fan, clearance, schedule = run.fan, run.clearance, run.schedule

    console.print()
    _print_fan_out(fan, clearance, schedule, show_extras=department is None)

    spend = run.budget
    console.print(
        f"\n[dim]{spend['calls']} model calls, "
        f"{spend['input_tokens'] + spend['output_tokens']} tokens[/dim]"
    )

    if save:
        _persist_fan_out(run, db_path)

    if json_out is not None:
        import json as _json

        payload = {
            "departments": [
                {
                    "department": r.department.value,
                    "title": r.title,
                    "summary": r.summary,
                    "notes": [n.model_dump() for n in r.notes],
                    "model": r.model_name,
                }
                for r in fan.reports
            ],
            "clearance": [vars(f) for f in clearance.flags],
            "schedule": [vars(i) for i in schedule.impacts],
        }
        json_out.write_text(_json.dumps(payload, indent=2), encoding="utf-8")
        console.print(f"[dim]reports written to {json_out}[/dim]")


def _print_fan_out(fan, clearance, schedule, show_extras: bool = True) -> None:
    """The AD's approval screen, in the terminal."""
    if not fan.reports:
        console.print("[yellow]no department has anything to hear[/yellow]")
        return

    for report in fan.reports:
        flag = "  [yellow](via fallback)[/yellow]" if report.via_fallback else ""
        console.print(f"[bold cyan]{report.title}[/bold cyan]{flag}")
        console.print(f"  {report.summary}")
        for note in report.notes:
            mark = "[red]![/red] " if note.urgent else "  "
            console.print(f"    {mark}scene {note.scene}  {note.note}")
            if note.action:
                console.print(f"        [dim]-> {note.action}[/dim]")
        console.print()

    # The clearance *agent* already reported above in its own voice. This is the
    # element-level flag list behind it, which is what legal actually works from,
    # so it is labelled as the detail rather than repeating the heading.
    if show_extras and clearance.flags:
        console.print("[bold yellow]Clearance detail[/bold yellow]  [dim]elements to clear[/dim]")
        for entry in clearance.flags:
            new = "" if entry.newly_introduced else "  [dim](already in the last draft)[/dim]"
            console.print(f"    [{entry.risk}] scene {entry.scene}  {entry.element}{new}")
        console.print()

    if show_extras and schedule.impacts:
        console.print("[bold]Schedule impact[/bold]")
        for impact in schedule.impacts:
            console.print(f"    scene {impact.scene}  [dim]{impact.kind}[/dim]  {impact.detail}")
            for touch in impact.touches:
                console.print(f"        [dim]{touch}[/dim]")
        console.print()

    summary = fan.summary()
    console.print(
        f"[dim]{summary['notified']} departments notified, "
        f"{summary['silent']} have nothing, "
        f"{summary['notes']} notes, {summary['urgent']} urgent[/dim]"
    )


def _persist_fan_out(run, db_path):
    """Write the run and the department reports to the element database."""
    from bluepages.pipeline.run import persist

    persisted = persist(run, db_path=db_path)

    console.print(
        f"\n[bold]saved[/bold]  {persisted.changes} changes, "
        f"{persisted.elements} elements, {len(run.fan.reports)} department reports "
        f"to production {run.production!r}"
    )
    console.print(
        "       [dim]nothing is sent until it is approved: "
        f"bluepages approve {run.production!r}[/dim]"
    )


@app.command()
def approve(
    production: Annotated[str, typer.Argument(help="Production title")],
    db_path: Annotated[
        Path | None, typer.Option("--db", help="SQLite file to read")
    ] = None,
    yes: Annotated[
        bool, typer.Option("--yes", help="Approve without the confirmation prompt")
    ] = False,
) -> None:
    """The AD's approval. Nothing goes out to a department before this.

    The agent drafts and a human approves, which is what makes "surfaces only
    for a real decision" true rather than a claim. Layer 8 sends what this
    marks approved.
    """
    from bluepages.store import Repository, open_database

    with open_database(path=db_path) as db:
        db.create_schema()
        repo = Repository(db)
        row = db.one("SELECT id FROM production WHERE title = ?", (production,))
        if row is None:
            console.print(f"[red]no production named {production!r}[/red]")
            raise typer.Exit(1)

        latest = repo.latest_draft(str(row["id"]))
        if latest is None:
            console.print("[yellow]no drafts ingested[/yellow]")
            raise typer.Exit(1)
        draft_id = str(latest["id"])

        reports = repo.reports_for_draft(draft_id)
        if not reports:
            console.print(
                f"[yellow]draft {latest['revision']} has no department reports[/yellow]"
            )
            console.print("[dim]run `bluepages fan-out ... --save` first[/dim]")
            raise typer.Exit(1)

        already = [r for r in reports if r["approved_at"]]
        pending = [r for r in reports if not r["approved_at"]]

        console.print(
            f"\n[bold]{production}[/bold]  draft {latest['revision']}: "
            f"{len(reports)} department reports\n"
        )
        for report in reports:
            urgent = sum(1 for n in report["notes"] if n["urgent"])
            mark = "[green]approved[/green]" if report["approved_at"] else "pending"
            console.print(
                f"  [bold]{report['department']}[/bold]  {len(report['notes'])} notes"
                + (f", [red]{urgent} urgent[/red]" if urgent else "")
                + f"  [dim]{mark}[/dim]"
            )
            console.print(f"      {report['summary']}")
        console.print()

        if not pending:
            console.print("[dim]everything is already approved[/dim]\n")
            return

        if not yes and not typer.confirm(
            f"Approve {len(pending)} report(s) for sending?"
        ):
            console.print("[yellow]not approved[/yellow]")
            raise typer.Exit(1)

        count = repo.approve_reports(draft_id)

    console.print(
        f"\n[bold green]approved[/bold green]  {count} report(s) ready to send"
    )
    console.print(
        f"       [dim]{len(already)} were already approved[/dim]\n"
        if already
        else ""
    )


@app.command()
def inventory(
    production: Annotated[str, typer.Argument(help="Production title")],
    element: Annotated[
        str | None, typer.Option("--element", help="Show one element's history instead")
    ] = None,
    db_path: Annotated[
        Path | None, typer.Option("--db", help="SQLite file to read")
    ] = None,
) -> None:
    """The element database: what the production owns, and where it appears.

    With --element, shows that object's trail across every draft. That trail is
    the point of tracking identity: a prop that moved is one row with a history,
    not two unrelated rows.
    """
    from bluepages.store import Repository, open_database

    with open_database(path=db_path) as db:
        db.create_schema()
        repo = Repository(db)
        row = db.one("SELECT id FROM production WHERE title = ?", (production,))
        if row is None:
            console.print(f"[red]no production named {production!r}[/red]")
            console.print("[dim]run `bluepages reason ... --save` first[/dim]")
            raise typer.Exit(1)
        production_id = str(row["id"])

        if element is not None:
            history = repo.element_history(production_id, element)
            if not history:
                console.print(f"[yellow]{element!r} is not in the inventory[/yellow]")
                raise typer.Exit(1)
            console.print(f"\n[bold]{element}[/bold]  across {len(history)} appearances\n")
            for entry in history:
                console.print(
                    f"    draft {entry['revision']}   scene {entry['scene']}"
                    f"   [dim]{entry['department']}[/dim]"
                )
            console.print()
            return

        rows = repo.inventory(production_id)
        if not rows:
            console.print("[yellow]nothing in the inventory yet[/yellow]")
            return

        table = Table(title=f"{production}: element inventory", title_justify="left")
        table.add_column("element")
        table.add_column("category", style="dim")
        table.add_column("department")
        table.add_column("scenes", justify="right")
        for entry in rows:
            name = entry["name"]
            if entry["branded"]:
                name = f"{name}  [yellow](branded)[/yellow]"
            table.add_row(
                name,
                str(entry["category"]),
                str(entry["department"]),
                str(entry["appearances"]),
            )
        console.print(table)
        console.print()


@app.command()
def history(
    production: Annotated[str, typer.Argument(help="Production title")],
    db_path: Annotated[
        Path | None, typer.Option("--db", help="SQLite file to read")
    ] = None,
) -> None:
    """Past runs, newest first. What the agent has done for this production."""
    from bluepages.store import Repository, open_database

    with open_database(path=db_path) as db:
        db.create_schema()
        repo = Repository(db)
        row = db.one("SELECT id FROM production WHERE title = ?", (production,))
        if row is None:
            console.print(f"[red]no production named {production!r}[/red]")
            raise typer.Exit(1)

        runs = repo.runs(str(row["id"]))
        if not runs:
            console.print("[yellow]no runs recorded[/yellow]")
            return

        table = Table(title=f"{production}: runs", title_justify="left")
        table.add_column("started", style="dim")
        table.add_column("status")
        table.add_column("scenes", justify="right")
        table.add_column("findings", justify="right")
        table.add_column("calls", justify="right")
        table.add_column("tokens", justify="right")
        for entry in runs:
            status = entry["status"]
            styled = f"[green]{status}[/green]" if status == "ok" else f"[red]{status}[/red]"
            if entry["fallbacks"]:
                styled += f"  [yellow]{entry['fallbacks']} fallback[/yellow]"
            table.add_row(
                str(entry["started_at"])[:19],
                styled,
                str(entry["scenes_parsed"]),
                str(entry["findings"]),
                str(entry["model_calls"]),
                str(int(entry["input_tokens"]) + int(entry["output_tokens"])),
            )
        console.print(table)
        console.print()


@app.command()
def report(
    production: Annotated[str, typer.Argument(help="Production title")],
    department: Annotated[
        str | None, typer.Option("--department", help="One department only")
    ] = None,
    db_path: Annotated[
        Path | None, typer.Option("--db", help="SQLite file to read")
    ] = None,
) -> None:
    """What each department needs to know about the latest draft.

    The fan-out, read back from the database. Layer 5 turns these into per
    department prose and email; this shows the routing is already correct.
    """
    from bluepages.store import Repository, open_database
    from bluepages.testdata import Department

    known = {d.value for d in Department}
    if department is not None and department not in known:
        # Silently returning nothing would read as "this department has no
        # changes", which is a different and much worse answer than a typo.
        console.print(f"[red]unknown department {department!r}[/red]")
        console.print(f"[dim]known: {', '.join(sorted(known))}[/dim]")
        raise typer.Exit(1)

    with open_database(path=db_path) as db:
        db.create_schema()
        repo = Repository(db)
        row = db.one("SELECT id FROM production WHERE title = ?", (production,))
        if row is None:
            console.print(f"[red]no production named {production!r}[/red]")
            raise typer.Exit(1)
        production_id = str(row["id"])

        latest = repo.latest_draft(production_id)
        if latest is None:
            console.print("[yellow]no drafts ingested[/yellow]")
            return
        draft_id = str(latest["id"])

        counts = repo.department_counts(draft_id)
        if not counts:
            console.print(
                f"[yellow]draft {latest['revision']} produced no findings[/yellow]"
            )
            return

        wanted = [department] if department else list(counts)
        console.print(
            f"\n[bold]{production}[/bold]  draft {latest['revision']}, "
            f"{sum(counts.values())} findings across {len(counts)} departments\n"
        )

        for name in wanted:
            changes = repo.changes_for_department(draft_id, name)
            if not changes:
                console.print(f"[dim]{name}: nothing[/dim]")
                continue
            console.print(f"[bold cyan]{name}[/bold cyan]  ({len(changes)})")
            for change in changes:
                where = (
                    f"{change['from_scene']} -> {change['scene_number']}"
                    if change["from_scene"]
                    and change["from_scene"] != change["scene_number"]
                    else f"scene {change['scene_number']}"
                )
                console.print(f"    [bold]{where}[/bold]  [dim]{change['kind']}[/dim]")
                console.print(f"        {change['summary']}")
                if change["risk"]:
                    console.print(f"        [yellow]risk: {change['risk']}[/yellow]")
            console.print()

        flags = repo.clearance_flags(draft_id)
        if flags and department is None:
            console.print("[bold yellow]clearance[/bold yellow]  branded elements in this draft")
            for flag in flags:
                console.print(f"    scene {flag['scene']}  {flag['name']}")
            console.print()


@app.command()
def models(
    all_providers: bool = typer.Option(
        False, "--all", help="List every provider Bedrock offers, not just Anthropic."
    ),
) -> None:
    """List the Anthropic models Bedrock offers in the configured region.

    Use this to fill in BEDROCK_MODEL_JUDGMENT and BEDROCK_MODEL_BULK with ids
    that actually exist in your region, rather than guessing. Bluepages only
    calls Anthropic models, so that is the default; --all is for checking what
    else the region has, e.g. before enabling a fallback provider's access.
    """
    import boto3

    settings = get_settings()
    try:
        client = boto3.client("bedrock", region_name=settings.aws_region)
        if all_providers:
            summaries = client.list_foundation_models()["modelSummaries"]
        else:
            summaries = client.list_foundation_models(byProvider="anthropic")["modelSummaries"]
    except Exception as exc:
        console.print(f"[red]could not reach Bedrock: {type(exc).__name__}: {exc}[/red]")
        raise typer.Exit(1) from exc

    title = f"{'All' if all_providers else 'Anthropic'} models in {settings.aws_region}"
    table = Table(title=title)
    table.add_column("provider")
    table.add_column("model id")
    table.add_column("name", style="dim")
    table.add_column("inference", style="dim")

    for m in sorted(summaries, key=lambda x: (x.get("providerName", ""), x["modelId"])):
        table.add_row(
            m.get("providerName", ""),
            m["modelId"],
            m.get("modelName", ""),
            ", ".join(m.get("inferenceTypesSupported", [])),
        )
    console.print(table)
    console.print(
        "\n[dim]ON_DEMAND ids work directly. For INFERENCE_PROFILE only, prefix the "
        "id with your region group, e.g. us. or eu.[/dim]\n"
    )


def _production_id(repo, production: str) -> str:
    """The production's id, or a clear exit if it does not exist yet."""
    row = repo.db.one("SELECT id FROM production WHERE title = ?", (production,))
    if row is None:
        console.print(f"[red]no production named {production!r}[/red]")
        console.print("[dim]run `bluepages fan-out ... --save` first[/dim]")
        raise typer.Exit(1)
    return str(row["id"])


@app.command()
def recipients(
    production: Annotated[str, typer.Argument(help="Production title")],
    add: Annotated[
        str | None,
        typer.Option("--add", help="Address to add, as department=email"),
    ] = None,
    name: Annotated[
        str | None, typer.Option("--name", help="Who that address belongs to")
    ] = None,
    remove: Annotated[
        str | None, typer.Option("--remove", help="Address to remove")
    ] = None,
    db_path: Annotated[
        Path | None, typer.Option("--db", help="SQLite file to use")
    ] = None,
) -> None:
    """Who each department's report goes to.

    Per production, because the property master on one show is not the property
    master on the next, and a global address book mails the wrong person on the
    second production.
    """
    from bluepages.store import Repository, open_database
    from bluepages.testdata import Department

    with open_database(path=db_path) as db:
        db.create_schema()
        repo = Repository(db)
        production_id = _production_id(repo, production)

        if add is not None:
            if "=" not in add:
                console.print("[red]--add takes department=email[/red]")
                console.print('[dim]e.g. --add props=props@example.com[/dim]')
                raise typer.Exit(1)
            department, _, email = add.partition("=")
            department, email = department.strip().lower(), email.strip()
            known = {d.value for d in Department}
            if department not in known:
                console.print(f"[red]unknown department {department!r}[/red]")
                console.print(f"[dim]known: {', '.join(sorted(known))}[/dim]")
                raise typer.Exit(1)
            if "@" not in email:
                console.print(f"[red]{email!r} is not an email address[/red]")
                raise typer.Exit(1)
            repo.add_recipient(production_id, department, email, name or "")
            console.print(f"[green]added[/green]  {department}: {email}")

        if remove is not None:
            removed = repo.remove_recipient(production_id, remove.strip())
            if not removed:
                console.print(f"[yellow]{remove} was not on the list[/yellow]")
                raise typer.Exit(1)
            console.print(f"[green]removed[/green]  {remove}")

        rows = repo.recipients(production_id)

    if not rows:
        console.print(f"\n[yellow]nobody is on {production}'s list yet[/yellow]")
        console.print(
            "[dim]bluepages recipients "
            f'"{production}" --add props=props@example.com[/dim]\n'
        )
        return

    table = Table(title=f"{production}: distribution list", title_justify="left")
    table.add_column("department")
    table.add_column("name", style="dim")
    table.add_column("email")
    for row in rows:
        table.add_row(str(row["department"]), str(row["name"] or ""), str(row["email"]))
    console.print()
    console.print(table)
    console.print()


@app.command()
def send(
    production: Annotated[str, typer.Argument(help="Production title")],
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Print what would go out, send nothing"),
    ] = False,
    preview: Annotated[
        Path | None,
        typer.Option("--preview", help="Write the rendered emails here as HTML"),
    ] = None,
    review_url: Annotated[
        str | None, typer.Option("--url", help="Link back to the full revision")
    ] = None,
    resend_sent: Annotated[
        bool, typer.Option("--again", help="Re-send reports already marked sent")
    ] = False,
    db_path: Annotated[
        Path | None, typer.Option("--db", help="SQLite file to use")
    ] = None,
) -> None:
    """Send the approved fan-out (Layer 8).

    Only approved reports go, and each goes once. Without a Resend key this
    prints what it would send rather than refusing, so the whole path is
    demonstrable before any email is configured.
    """
    from bluepages.deliver import ConsoleTransport, build_transport, send_reports
    from bluepages.store import Repository, open_database

    settings = get_settings()
    stream = RichConsoleStream(verbose=False)

    with open_database(path=db_path) as db:
        db.create_schema()
        repo = Repository(db)
        production_id = _production_id(repo, production)
        draft = repo.latest_draft(production_id)
        if draft is None:
            console.print(f"[yellow]{production} has no drafts yet[/yellow]")
            raise typer.Exit(1)
        draft_id = str(draft["id"])

        if preview is not None:
            _write_previews(repo, production_id, draft_id, production, preview)

        transport = (
            ConsoleTransport(console)
            if dry_run
            else build_transport(settings, stream=stream)
        )
        if dry_run:
            console.print("[dim]dry run: nothing will actually be sent[/dim]\n")

        result = send_reports(
            repo=repo,
            production_id=production_id,
            to_draft_id=draft_id,
            transport=transport,
            production=production,
            draft_label=f"draft {draft['revision']}",
            review_url=review_url or "",
            stream=stream,
            resend_sent=resend_sent,
            # A dry run writes nothing. Recorded, it would mark every report
            # sent and the real send would then skip them all.
            record=not dry_run,
        )

    console.print()
    delivered = sum(1 for s in result.sent if s.ok)
    if delivered:
        console.print(
            f"[bold green]sent[/bold green]  {result.reports_sent} report(s) "
            f"to {delivered} address(es)"
        )
    for failure in result.failures:
        console.print(f"[red]failed[/red]  {failure.email}: {failure.error}")
    if result.skipped_unapproved:
        console.print(
            f"[yellow]{result.skipped_unapproved} report(s) not approved yet[/yellow]"
            f"  [dim]bluepages approve {production!r}[/dim]"
        )
    if result.skipped_already_sent:
        console.print(
            f"[dim]{result.skipped_already_sent} already sent, left alone "
            "(--again to re-send)[/dim]"
        )
    if result.skipped_no_recipient:
        console.print(
            f"[yellow]no recipient for: "
            f"{', '.join(result.skipped_no_recipient)}[/yellow]"
        )
        console.print(
            f'[dim]bluepages recipients "{production}" '
            "--add <department>=<email>[/dim]"
        )
    if not delivered and not result.failures:
        console.print("[dim]nothing to send[/dim]")
    console.print()


def _write_previews(repo, production_id, draft_id, production, out: Path) -> None:
    """Render each department's email to disk without sending anything.

    The fastest way to look at what a head of department actually receives, and
    it costs nothing.
    """
    from bluepages.deliver import department_title, notes_from_rows, render

    out.mkdir(parents=True, exist_ok=True)
    written = 0
    for report in repo.reports_for_draft(draft_id):
        department = str(report["department"])
        email = render(
            department=department_title(department),
            production=production,
            summary=str(report.get("summary") or ""),
            notes=notes_from_rows(repo.report_notes(str(report["id"]))),
            draft_label="preview",
        )
        (out / f"{department}.html").write_text(email.html, encoding="utf-8")
        written += 1
    console.print(f"[dim]{written} preview(s) written to {out}[/dim]")


@app.command()
def watch(
    folder: Annotated[
        Path, typer.Argument(help="A folder of productions, one subfolder each")
    ],
    once: Annotated[
        bool, typer.Option("--once", help="Process what is there and exit")
    ] = False,
    interval: Annotated[
        float, typer.Option("--interval", help="Seconds between checks")
    ] = 5.0,
    max_calls: Annotated[
        int | None, typer.Option("--max-calls", help="Ceiling per draft")
    ] = None,
    db_path: Annotated[
        Path | None, typer.Option("--db", help="SQLite file to use instead of the default")
    ] = None,
    verbose: Annotated[bool, typer.Option("-v", "--verbose", help="Show progress events")] = False,
) -> None:
    """Watch a folder and run when a draft lands (Layer 6, locally).

    The same handler the Lambda runs, driven by a directory instead of by S3.
    It exists so the autonomous path can be demonstrated and debugged without
    AWS, and so the deployed code is the code that was tested.

    Drafts already present when it starts are recorded, not processed: waking on
    everything in the folder at boot would re-run the whole history and bill for
    it. Only what arrives after that triggers a run.
    """
    import time

    from bluepages.llm import ModelClient, RunBudget
    from bluepages.trigger import LocalStore, handle_upload

    if not folder.is_dir():
        console.print(f"[red]{folder} is not a folder[/red]")
        raise typer.Exit(1)

    settings = get_settings()
    stream = RichConsoleStream(verbose=verbose)
    store = LocalStore(folder)

    def present() -> set[str]:
        return {
            str(p.relative_to(folder)).replace("\\", "/")
            for p in folder.rglob("*")
            if p.is_file()
        }

    seen = set() if once else present()
    if not once:
        console.print(
            f"[bold cyan]watching[/bold cyan] {folder}  "
            f"[dim]({len(seen)} draft(s) already here, left alone)[/dim]"
        )
        console.print("[dim]drop a new draft into a production folder. ctrl-c to stop.[/dim]\n")

    try:
        while True:
            for key in sorted(present() - seen):
                seen.add(key)
                budget = RunBudget(
                    max_calls=max_calls or settings.bluepages_max_llm_calls_per_run
                )
                client = ModelClient(settings=settings, stream=stream, budget=budget)
                outcome = handle_upload(
                    store, key, client=client, stream=stream, db_path=db_path
                )
                if outcome.ran:
                    fan = outcome.summary.get("fan_out", {})
                    console.print(
                        f"[bold green]{outcome.production}[/bold green]  "
                        f"{fan.get('reports', 0)} department report(s), "
                        f"{fan.get('notes', 0)} note(s)"
                    )
                    console.print(
                        "[dim]nothing is sent until it is approved: "
                        f"bluepages approve {outcome.production!r}[/dim]\n"
                    )
                else:
                    console.print(f"[dim]{outcome.reason}[/dim]")
            if once:
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        console.print("\n[dim]stopped watching[/dim]")


@app.command()
def version() -> None:
    """Print the version."""
    from bluepages import __version__

    console.print(f"bluepages {__version__}")


if __name__ == "__main__":
    app()
