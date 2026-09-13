"""The trigger: a draft lands, the agent wakes (Layer 6).

This is the layer that earns the word "autonomous". Until it exists the project
is a script someone runs, and the pitch, an agent that works while the AD
sleeps, is a claim rather than a demonstration.

What happens on an upload:

1. S3 notifies Lambda that `productions/<title>/<file>` was created.
2. `handle_upload` works out which production that is and finds the draft the
   new one supersedes. Nothing tells it: a file arrived, and which draft it
   replaces is the agent's own judgment.
3. If there is no earlier draft this is the production's first, which is not an
   error. It is recorded and the run ends. There is nothing to diff yet.
4. Otherwise both drafts are downloaded and the full pipeline runs.
5. The reports persist **unapproved**. Nothing is sent.

Point 5 is the one that matters most and the one easiest to get wrong. An agent
that wakes on its own and mails eight department heads without being asked is
precisely the thing a 1st AD disables on day two. The approval gate is not a UI
step bolted on top, it is what makes "surfaces only for a real decision" true
when no human started the run.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bluepages.events import EventKind, EventStream, NullStream
from bluepages.trigger.storage import (
    ScriptStore,
    is_script,
    previous_draft,
    production_of,
)


@dataclass
class TriggerResult:
    """What one upload caused.

    `ran` distinguishes the two non-failure outcomes: a pipeline that ran, and a
    first draft that correctly did nothing. A caller that treats "did nothing"
    as a failure would retry forever on the first upload of every production.
    """

    production: str
    key: str
    ran: bool = False
    reason: str = ""
    summary: dict[str, Any] = field(default_factory=dict)
    previous_key: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "production": self.production,
            "key": self.key,
            "ran": self.ran,
            "reason": self.reason,
            "previous_key": self.previous_key,
            "summary": self.summary,
        }


def s3_records(event: dict[str, Any]) -> list[tuple[str, str]]:
    """`(bucket, key)` for every object creation in an S3 event.

    Keys arrive URL-encoded, so a production called "The Farm" appears as
    `The+Farm`. Decoding is not cosmetic: the production name is what the
    element database keys on, and "The+Farm" and "The Farm" would become two
    productions with half the drafts each.
    """
    from urllib.parse import unquote_plus

    out: list[tuple[str, str]] = []
    for record in event.get("Records", []):
        s3 = record.get("s3") or {}
        bucket = (s3.get("bucket") or {}).get("name")
        key = (s3.get("object") or {}).get("key")
        if bucket and key:
            out.append((bucket, unquote_plus(key)))
    return out


def handle_upload(
    store: ScriptStore,
    key: str,
    client: Any = None,
    stream: EventStream | None = None,
    db_path: Path | None = None,
    workdir: Path | None = None,
    persist_run: bool = True,
) -> TriggerResult:
    """One uploaded draft, from arrival to persisted reports."""
    from bluepages.pipeline import run_pipeline
    from bluepages.pipeline.run import persist

    stream = stream or NullStream()
    production = production_of(key)
    result = TriggerResult(production=production, key=key)

    if not is_script(key):
        # A call sheet or a PDF of notes in the same folder. Ignoring it is the
        # whole point: waking the agent on every object costs money for nothing.
        result.reason = f"not a script: {Path(key).name}"
        stream.emit(EventKind.INFO, result.reason, key=key, ignored=True)
        return result

    stream.emit(
        EventKind.RUN_STARTED,
        f"draft landed: {Path(key).name} ({production})",
        key=key,
        production=production,
        trigger="upload",
    )

    earlier = previous_draft(store, production, key)
    if earlier is None:
        result.reason = (
            f"{Path(key).name} is the first draft of {production!r}; "
            "nothing to diff against yet"
        )
        stream.emit(EventKind.RUN_FINISHED, result.reason, production=production)
        return result

    result.previous_key = earlier.key
    stream.emit(
        EventKind.INFO,
        f"diffing against {earlier.filename}",
        previous_key=earlier.key,
        production=production,
    )

    with _workspace(workdir) as work:
        before = store.download(earlier.key, work / f"before{earlier.suffix}")
        after = store.download(key, work / f"after{Path(key).suffix.lower()}")

        run = run_pipeline(
            before,
            after,
            client=client,
            stream=stream,
            production=production,
        )
        if persist_run:
            persist(run, db_path=db_path)

    result.ran = True
    result.summary = run.summary()
    result.reason = "pipeline ran"
    return result


class _workspace:
    """A working directory, temporary unless one was given.

    Lambda gives 512MB of `/tmp` that survives between warm invocations, so a
    handler that never cleans up eventually fails on a full disk for reasons
    that look nothing like the cause.
    """

    def __init__(self, given: Path | None) -> None:
        self.given = given
        self._tmp: Any = None

    def __enter__(self) -> Path:
        if self.given is not None:
            self.given.mkdir(parents=True, exist_ok=True)
            return self.given
        self._tmp = tempfile.TemporaryDirectory(prefix="bluepages-")
        return Path(self._tmp.name)

    def __exit__(self, *exc: Any) -> None:
        if self._tmp is not None:
            self._tmp.cleanup()


def lambda_handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """AWS Lambda entry point. Wired to S3 object-created notifications.

    Kept thin on purpose: everything it does is available to the CLI and the
    tests through `handle_upload`, so the deployed path is the tested path.
    """
    from bluepages.config import get_settings
    from bluepages.events import JsonLinesStream
    from bluepages.llm import ModelClient, RunBudget
    from bluepages.trigger.storage import S3Store

    settings = get_settings()
    # JSON lines because CloudWatch parses them into queryable fields, and the
    # same events feed Layer 7 over SSE.
    stream = JsonLinesStream()
    results: list[dict[str, Any]] = []

    for bucket, key in s3_records(event):
        budget = RunBudget(max_calls=settings.bluepages_max_llm_calls_per_run)
        client = ModelClient(settings=settings, stream=stream, budget=budget)
        outcome = handle_upload(
            store=S3Store(bucket),
            key=key,
            client=client,
            stream=stream,
            # Lambda has no durable disk, so this is Supabase when configured
            # and an ephemeral file otherwise. `open_database` decides.
            db_path=None,
            workdir=Path("/tmp"),
        )
        results.append(outcome.to_dict())

    return {"statusCode": 200, "results": results}


__all__ = [
    "TriggerResult",
    "handle_upload",
    "lambda_handler",
    "s3_records",
]
