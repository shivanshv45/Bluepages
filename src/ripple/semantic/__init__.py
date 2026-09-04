"""The semantic layer (Layers 3.3 and 3.4).

Extraction (3.3) says what each scene requires. Reasoning (3.4) says what the
changes between two drafts mean and who needs to know. Scoring checks the
second against the labelled answer key, because a semantic result with nothing
to check it against is not verified.
"""

from ripple.semantic.elements import (
    CATEGORY_DEPARTMENT,
    DraftElements,
    ElementCategory,
    ExtractedElement,
    SceneExtraction,
    extract_draft,
    extract_scene,
)
from ripple.semantic.reasoning import (
    Finding,
    SemanticResult,
    reason_about_diff,
)
from ripple.semantic.scoring import ChangeScore, Scorecard, score

__all__ = [
    "CATEGORY_DEPARTMENT",
    "ChangeScore",
    "DraftElements",
    "ElementCategory",
    "ExtractedElement",
    "Finding",
    "SceneExtraction",
    "Scorecard",
    "SemanticResult",
    "extract_draft",
    "extract_scene",
    "reason_about_diff",
    "score",
]
