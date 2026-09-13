"""Model access: Bedrock first, with a logged fallback chain (Layer 3.5)."""

from bluepages.llm.client import (
    AllModelsFailedError,
    BudgetExceededError,
    Completion,
    ModelClient,
    ModelRole,
    RunBudget,
    TruncatedResponseError,
)
from bluepages.llm.fallback import Retryability, classify, is_retryable

__all__ = [
    "AllModelsFailedError",
    "BudgetExceededError",
    "Completion",
    "ModelClient",
    "ModelRole",
    "Retryability",
    "RunBudget",
    "TruncatedResponseError",
    "classify",
    "is_retryable",
]
