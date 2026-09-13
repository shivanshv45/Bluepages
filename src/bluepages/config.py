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
    # Verified against a live key on 2026-09-05. llama-3.3-70b-versatile, the
    # previous default, is no longer served. gpt-oss-120b is a reasoning model:
    # it spends max_tokens on hidden reasoning first, so a small ceiling
    # returns empty content. TruncatedResponseError catches that rather than
    # letting it read as "nothing to report".
    groq_model: str = Field(default="openai/gpt-oss-120b")
    gemini_model: str = Field(default="gemini-2.5-flash")

    # Bedrock first is the documented default (DECISIONS.md: resilience, not
    # cost). "fallback_first" reverses the chain so Groq/Gemini are tried
    # before Bedrock, for when Bedrock access is provisioned but throttled to
    # zero throughput, e.g. a fresh account waiting on a quota increase.
    bluepages_chain_order: str = Field(default="bedrock_first")

    # --- Storage (Layers 4 and 6) ------------------------------------------
    s3_bucket: str | None = Field(default=None)
    supabase_url: str | None = Field(default=None)
    supabase_service_key: str | None = Field(default=None)
    # The direct Postgres connection string, which is not the API URL. Absent
    # means the element database falls back to a local SQLite file, so Layer 4
    # works before any cloud setup exists.
    supabase_db_url: str | None = Field(default=None)

    # --- Email (Layer 8) ----------------------------------------------------
    resend_api_key: str | None = Field(default=None)
    resend_from: str | None = Field(default=None)

    # --- Agent runtime ------------------------------------------------------
    # "strands" runs every Bedrock call through a Strands agent; "boto3" uses
    # the raw Converse call. Both enforce the same guards, and the escape hatch
    # exists so a Strands-level bug is never the thing that blocks a demo.
    bluepages_agent_runtime: str = Field(default="strands")

    # --- Cost guards --------------------------------------------------------
    # CLAUDE.md: never an unbounded loop, always max_tokens, cache while iterating.
    bluepages_max_tokens: int = Field(default=4096, gt=0, le=64000)
    bluepages_max_llm_calls_per_run: int = Field(default=400, gt=0)
    bluepages_cache_llm: bool = Field(default=True)
    bluepages_log_level: str = Field(default="INFO")

    # Set when the frontend is deployed to its own origin (e.g. Cloudflare
    # Pages) rather than served through the local Vite proxy. Its presence is
    # also what flips the session cookie to `Secure` + `SameSite=None`: a
    # cross-site cookie needs both, and `Secure` breaks plain-http local dev.
    bluepages_frontend_origin: str | None = Field(default=None)

    @field_validator("bluepages_log_level")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()

    @field_validator("bluepages_agent_runtime")
    @classmethod
    def _known_runtime(cls, v: str) -> str:
        v = v.lower().strip()
        if v not in {"strands", "boto3"}:
            raise ValueError(f"agent runtime must be 'strands' or 'boto3', got {v!r}")
        return v

    @field_validator("bluepages_chain_order")
    @classmethod
    def _known_chain_order(cls, v: str) -> str:
        v = v.lower().strip()
        if v not in {"bedrock_first", "fallback_first"}:
            raise ValueError(
                f"chain order must be 'bedrock_first' or 'fallback_first', got {v!r}"
            )
        return v

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
