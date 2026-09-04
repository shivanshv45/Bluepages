"""Scene alignment and diffing (Layer 3).

Alignment (3.1) pairs scenes across drafts by their stable numbers. The
mechanical diff (3.2) then works inside each aligned pair, producing the raw
change spans the semantic layer reasons over.
"""

from bluepages.diff.align import (
    Alignment,
    AlignmentKind,
    AlignmentMethod,
    ScenePair,
    align,
)
from bluepages.diff.mechanical import (
    ChangeSpan,
    DraftDiff,
    RelocationCandidate,
    SceneDiff,
    SpanKind,
    diff_drafts,
)

__all__ = [
    "Alignment",
    "AlignmentKind",
    "AlignmentMethod",
    "ChangeSpan",
    "DraftDiff",
    "RelocationCandidate",
    "SceneDiff",
    "ScenePair",
    "SpanKind",
    "align",
    "diff_drafts",
]
