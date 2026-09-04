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
) -> None:
    """The full pipeline: parse, align, diff, extract elements, reason.

    This is Layers 3.3 and 3.4 and it costs money. Every call is bounded by the
    run budget and cached on disk, so re-running on an unchanged pair is free.
    """
    from bluepages.diff import align, diff_drafts
    from bluepages.llm import ModelClient, RunBudget
    from bluepages.parse import parse_script
    from bluepages.semantic import extract_draft, reason_about_diff

    settings = get_settings()
    stream = RichConsoleStream(verbose=verbose)
    budget = RunBudget(
        max_calls=max_calls or settings.bluepages_max_llm_calls_per_run
    )
    client = ModelClient(settings=settings, stream=stream, budget=budget)

    old_draft = parse_script(before, stream=stream)
    new_draft = parse_script(after, stream=stream)
    alignment = align(old_draft, new_draft, stream=stream)
    mechanical = diff_drafts(alignment, stream=stream)

    # Extraction is scoped to the scenes that changed. Running it over an
    # unchanged 120-scene draft is 120 calls to learn nothing new.
    elements = None
    if not skip_elements:
        touched = sorted(_scenes_touched(alignment, mechanical))
        elements = extract_draft(new_draft, client, stream=stream, scenes=touched)

    result = reason_about_diff(mechanical, client, elements=elements, stream=stream)

    console.print()
    _print_findings(result, elements)

    spend = budget.summary()
    console.print(
        f"\n[dim]{spend['calls']} model calls, "
        f"{spend['input_tokens'] + spend['output_tokens']} tokens, "
        f"{', '.join(f'{k}={v}' for k, v in spend['by_model'].items()) or 'all cached'}[/dim]"
    )

    if json_out is not None:
        json_out.write_text(result.model_dump_json(indent=2), encoding="utf-8")
        console.print(f"[dim]findings written to {json_out}[/dim]")

    if key_path is not None:
        card = _score_semantic(key_path, result)
        if not card.passed:
            raise typer.Exit(1)


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


@app.command()
def models() -> None:
    """List the Anthropic models Bedrock offers in the configured region.

    Use this to fill in BEDROCK_MODEL_JUDGMENT and BEDROCK_MODEL_BULK with ids
    that actually exist in your region, rather than guessing.
    """
    import boto3

    settings = get_settings()
    try:
        client = boto3.client("bedrock", region_name=settings.aws_region)
        summaries = client.list_foundation_models(byProvider="anthropic")["modelSummaries"]
    except Exception as exc:
        console.print(f"[red]could not reach Bedrock: {type(exc).__name__}: {exc}[/red]")
        raise typer.Exit(1) from exc

    table = Table(title=f"Anthropic models in {settings.aws_region}")
    table.add_column("model id")
    table.add_column("name", style="dim")
    table.add_column("inference", style="dim")

    for m in sorted(summaries, key=lambda x: x["modelId"]):
        table.add_row(
            m["modelId"],
            m.get("modelName", ""),
            ", ".join(m.get("inferenceTypesSupported", [])),
        )
    console.print(table)
    console.print(
        "\n[dim]ON_DEMAND ids work directly. For INFERENCE_PROFILE only, prefix the "
        "id with your region group, e.g. us. or eu.[/dim]\n"
    )


@app.command()
def version() -> None:
    """Print the version."""
    from bluepages import __version__

    console.print(f"bluepages {__version__}")


if __name__ == "__main__":
    app()
