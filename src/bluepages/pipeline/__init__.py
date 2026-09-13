"""The pipeline as one callable (Layer 6).

Everything through Layer 5 was reachable only by typing a CLI command. The
autonomous trigger needs the same work startable by an S3 event, and Layer 7
needs it startable by an HTTP request, so the sequence lives here and the CLI
becomes one of three callers rather than the only one.

Keeping it in one place is not tidiness. Three copies of "parse, align, diff,
extract, reason, fan out" would drift, and the one that drifts is whichever is
not the one being demoed.
"""

from bluepages.pipeline.run import (
    PipelineResult,
    Stage,
    persist,
    run_pipeline,
    scenes_worth_extracting,
)

__all__ = [
    "PipelineResult",
    "Stage",
    "persist",
    "run_pipeline",
    "scenes_worth_extracting",
]
