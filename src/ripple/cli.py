"""The `ripple` command line.

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

from ripple.config import get_settings
from ripple.events import CollectingStream, EventKind, RichConsoleStream, TeeStream

app = typer.Typer(
    name="ripple",
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
    from ripple.parse import UnsupportedFormatError, parse_script
    from ripple.parse.fdx import FdxParseError

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

    console.print("\n[bold]Revision Ripple: environment check[/bold]\n")

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
    console.print(f"  {OK} max_tokens per call: {settings.ripple_max_tokens}")
    console.print(f"  {OK} max model calls per run: {settings.ripple_max_llm_calls_per_run}")
    console.print(
        f"  {OK if settings.ripple_cache_llm else WARN} response caching: "
        f"{'on' if settings.ripple_cache_llm else 'off'}"
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
    from ripple.llm import ModelClient, RunBudget

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
    from ripple import __version__

    console.print(f"revision-ripple {__version__}")


if __name__ == "__main__":
    app()
