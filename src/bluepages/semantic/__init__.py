"""The semantic layer (Layers 3.3 and 3.4).

Extraction (3.3) says what each scene requires. Reasoning (3.4) says what the
changes between two drafts mean and who needs to know. Scoring checks the
second against the labelled answer key, because a semantic result with nothing
to check it against is not verified.
"""

from bluepages.semantic.elements import (
    CATEGORY_DEPARTMENT,
    DraftElements,
    ElementCategory,
    ExtractedElement,
    SceneExtraction,
    extract_draft,
    extract_scene,
)
from bluepages.semantic.reasoning import (
    MAX_RIPPLES,
    Finding,
    RippleQuestion,
    SemanticResult,
    reason_about_diff,
)
from bluepages.semantic.scoring import ChangeScore, Scorecard, score

__all__ = [
    "CATEGORY_DEPARTMENT",
    "MAX_RIPPLES",
    "ChangeScore",
    "DraftElements",
    "ElementCategory",
    "ExtractedElement",
    "Finding",
    "RippleQuestion",
    "SceneExtraction",
    "Scorecard",
    "SemanticResult",
    "extract_draft",
    "extract_scene",
    "reason_about_diff",
    "score",
]
