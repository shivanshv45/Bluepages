"""The event stream: one interface, many consumers.

The pipeline calls `stream.emit(...)`. What happens next is the consumer's
business. The CLI prints; SSE forwards; tests collect. No stage knows which.
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol


class EventKind(str, Enum):
    """Every kind of thing the pipeline can report.

    Named for what happened, not for which module emitted it. Layer 7's live
    surface subscribes to these, so the vocabulary is a contract.
    """

    # Run lifecycle
    RUN_STARTED = "run.started"
    RUN_FINISHED = "run.finished"
    RUN_FAILED = "run.failed"

    # Parsing (Layer 1)
    PARSE_STARTED = "parse.started"
    SCENE_PARSED = "scene.parsed"
    PARSE_FINISHED = "parse.finished"
    PARSE_WARNING = "parse.warning"

    # Diff (Layer 3)
    ALIGN_STARTED = "align.started"
    SCENE_ALIGNED = "scene.aligned"
    CHANGE_DETECTED = "change.detected"

    # Elements (Layer 3.3)
    ELEMENT_FOUND = "element.found"

    # Models (Layers 3.4, 3.5)
    MODEL_CALL_STARTED = "model.call.started"
    MODEL_CALL_FINISHED = "model.call.finished"
    MODEL_FALLBACK = "model.fallback"
    TOKENS_SPENT = "tokens.spent"

    # Agents (Layer 5)
    AGENT_STARTED = "agent.started"
    AGENT_FINISHED = "agent.finished"

    # Generic
    INFO = "info"


@dataclass(slots=True)
class Event:
    """One thing that happened, at a point in time, with structured detail."""

    kind: EventKind
    message: str
    data: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "message": self.message,
            "data": self.data,
            "ts": self.ts,
        }

    def to_sse(self) -> str:
        """Server-sent-events wire format, for Layer 7."""
        return f"event: {self.kind.value}\ndata: {json.dumps(self.to_dict())}\n\n"


class EventStream(Protocol):
    """What every pipeline stage is handed."""

    def emit(self, kind: EventKind, message: str, **data: Any) -> None: ...


class NullStream:
    """Discards everything. The default, so no stage needs a None check."""

    def emit(self, kind: EventKind, message: str, **data: Any) -> None:
        return None


class CollectingStream:
    """Keeps every event in memory. For tests and for the CLI's final summary."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    def emit(self, kind: EventKind, message: str, **data: Any) -> None:
        self.events.append(Event(kind=kind, message=message, data=data))

    def of_kind(self, kind: EventKind) -> list[Event]:
        return [e for e in self.events if e.kind is kind]

    def count(self, kind: EventKind) -> int:
        return sum(1 for e in self.events if e.kind is kind)


# Colour per event family. Keeps a long run readable at a glance.
_STYLES: dict[EventKind, str] = {
    EventKind.RUN_STARTED: "bold cyan",
    EventKind.RUN_FINISHED: "bold green",
    EventKind.RUN_FAILED: "bold red",
    EventKind.PARSE_WARNING: "yellow",
    EventKind.MODEL_FALLBACK: "bold yellow",
    EventKind.CHANGE_DETECTED: "magenta",
    EventKind.AGENT_STARTED: "blue",
    EventKind.AGENT_FINISHED: "blue",
}


class RichConsoleStream:
    """Prints events as they happen.

    `verbose=False` suppresses the per-scene chatter, which is thousands of lines
    on a feature, while keeping every milestone and every warning.
    """

    _NOISY = frozenset({EventKind.SCENE_PARSED, EventKind.ELEMENT_FOUND, EventKind.SCENE_ALIGNED})

    def __init__(self, verbose: bool = False, console: Any = None) -> None:
        self.verbose = verbose
        if console is None:
            from rich.console import Console

            console = Console(stderr=True)
        self.console = console
        self._t0 = time.time()

    def emit(self, kind: EventKind, message: str, **data: Any) -> None:
        if kind in self._NOISY and not self.verbose:
            return
        style = _STYLES.get(kind, "dim")
        elapsed = time.time() - self._t0
        self.console.print(
            f"[dim]{elapsed:6.2f}s[/dim] [{style}]{kind.value:<22}[/{style}] {message}"
        )


class JsonLinesStream:
    """One JSON object per line, to any writable. Machine-readable runs."""

    def __init__(self, fp: Any = None) -> None:
        self.fp = fp or sys.stdout

    def emit(self, kind: EventKind, message: str, **data: Any) -> None:
        self.fp.write(json.dumps(Event(kind=kind, message=message, data=data).to_dict()) + "\n")
        self.fp.flush()


class TeeStream:
    """Fans one emit out to several streams: print it and keep it."""

    def __init__(self, *streams: EventStream) -> None:
        self.streams = streams

    def emit(self, kind: EventKind, message: str, **data: Any) -> None:
        for s in self.streams:
            s.emit(kind, message, **data)
