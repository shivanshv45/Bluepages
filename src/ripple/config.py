"""Settings, loaded from the environment.

One source of truth for every knob. Model IDs live here rather than in code so
that switching a Bedrock model, or pointing at an inference-profile ARN, is a
config change and never an edit to the pipeline.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Load .env from the project root before Settings reads the environment.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_PROJECT_ROOT / ".env")


class Settings(BaseSettings):
    """Runtime configuration.

    Every field has a working default except the credentials, which are resolved
    by boto3's own chain (profile, env, instance role) rather than read here.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- AWS / Bedrock -----------------------------------------------------
    aws_region: str = Field(default="us-west-2")

    bedrock_model_judgment: str = Field(
        default="anthropic.claude-sonnet-5",
        description="Semantic reasoning. Layer 3.4. Sonnet-class.",
    )
    bedrock_model_bulk: str = Field(
        default="anthropic.claude-haiku-4-5",
        description="High-volume element extraction. Layer 3.3. Haiku-class.",
    )

    # --- Fallback providers (Layer 3.5) ------------------------------------
    groq_api_key: str | None = Field(default=None)
    gemini_api_key: str | None = Field(default=None)
    groq_model: str = Field(default="llama-3.3-70b-versatile")
    gemini_model: str = Field(default="gemini-2.0-flash")

    # --- Storage (Layers 4 and 6) ------------------------------------------
    s3_bucket: str | None = Field(default=None)
    supabase_url: str | None = Field(default=None)
    supabase_service_key: str | None = Field(default=None)

    # --- Email (Layer 8) ----------------------------------------------------
    resend_api_key: str | None = Field(default=None)
    resend_from: str | None = Field(default=None)

    # --- Cost guards --------------------------------------------------------
    # CLAUDE.md: never an unbounded loop, always max_tokens, cache while iterating.
    ripple_max_tokens: int = Field(default=4096, gt=0, le=64000)
    ripple_max_llm_calls_per_run: int = Field(default=400, gt=0)
    ripple_cache_llm: bool = Field(default=True)
    ripple_log_level: str = Field(default="INFO")

    @field_validator("ripple_log_level")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()

    # --- Derived ------------------------------------------------------------
    @property
    def project_root(self) -> Path:
        return _PROJECT_ROOT

    @property
    def cache_dir(self) -> Path:
        d = _PROJECT_ROOT / ".cache"
        d.mkdir(exist_ok=True)
        return d

    @property
    def has_fallback_provider(self) -> bool:
        return bool(self.groq_api_key or self.gemini_api_key)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """The process-wide settings singleton."""
    return Settings()
