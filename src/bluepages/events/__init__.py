"""Structured progress events (Layer 3.6).

Every pipeline stage emits events as it works, not just final results. One
stream interface, two consumers: the CLI prints it, SSE forwards it.

Built here at the start rather than retrofitted, per PLAN.md 3.6 and
DECISIONS.md: adding events later means touching every stage again.
"""

from bluepages.events.stream import (
    CollectingStream,
    Event,
    EventKind,
    EventStream,
    JsonLinesStream,
    NullStream,
    RichConsoleStream,
    TeeStream,
)

__all__ = [
    "CollectingStream",
    "Event",
    "EventKind",
    "EventStream",
    "JsonLinesStream",
    "NullStream",
    "RichConsoleStream",
    "TeeStream",
]
