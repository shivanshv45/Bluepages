"""Model access: Bedrock first, with a logged fallback chain (Layer 3.5)."""

from ripple.llm.client import (
    AllModelsFailedError,
    BudgetExceededError,
    Completion,
    ModelClient,
    ModelRole,
    RunBudget,
)
from ripple.llm.fallback import Retryability, classify, is_retryable

__all__ = [
    "AllModelsFailedError",
    "BudgetExceededError",
    "Completion",
    "ModelClient",
    "ModelRole",
    "Retryability",
    "RunBudget",
    "classify",
    "is_retryable",
]
