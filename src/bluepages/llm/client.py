"""The model client: Bedrock first, with a logged fallback chain.

Three rules from CLAUDE.md are enforced structurally here rather than left to
the caller's discipline, because the failure mode is a surprise AWS bill hours
after the fact:

1. `max_tokens` is always set. There is no code path that omits it.
2. Every call is counted against a per-run ceiling. Exceeding it raises rather
   than continuing, so no loop can run away.
3. Responses are cached on disk while iterating, keyed by the exact request, so
   re-running the pipeline on an unchanged prompt costs nothing.

The fallback chain is `judgment -> bulk -> Groq/Gemini`, both Bedrock rungs
first. `BLUEPAGES_CHAIN_ORDER=fallback_first` reverses the two groups for an
account whose Bedrock access is granted but throttled to zero. Which model
answered is always recorded and emitted, because fallback output is weaker and
that must be visible rather than silent.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bluepages.config import Settings, get_settings
from bluepages.events import EventKind, EventStream, NullStream
from bluepages.llm.fallback import is_retryable


class BudgetExceededError(RuntimeError):
    """The per-run LLM call ceiling was hit. A guard against runaway loops."""


class TruncatedResponseError(ValueError):
    """A provider returned no usable text, usually by exhausting max_tokens.

    A `ValueError` on purpose: the classifier treats Python data errors as
    fatal, and the same over-long prompt truncates identically everywhere, so
    burning the rest of the chain on it helps nobody. It must never be
    swallowed into an empty answer, which reads as "nothing to report".
    """


class AllModelsFailedError(RuntimeError):
    """Every model in the chain failed. Carries what each one said."""

    def __init__(self, failures: list[tuple[str, BaseException]]):
        self.failures = failures
        detail = "; ".join(f"{name}: {type(e).__name__}: {e}" for name, e in failures)
        super().__init__(f"all models in the chain failed -> {detail}")


@dataclass(slots=True)
class ModelRole:
    """One rung of the fallback chain."""

    name: str          # the role, e.g. "judgment". Not the model: that is model_id
    model_id: str      # provider model id
    provider: str      # "bedrock" | "groq" | "gemini"


@dataclass(slots=True)
class Completion:
    """A model's answer, plus which model actually produced it."""

    text: str
    model_name: str
    model_id: str
    provider: str
    # True when this did not come from the first choice.
    via_fallback: bool = False
    input_tokens: int = 0
    output_tokens: int = 0
    latency_s: float = 0.0
    cached: bool = False

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class RunBudget:
    """Bounds one pipeline run. Shared across every call in that run."""

    max_calls: int
    calls_made: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    by_model: dict[str, int] = field(default_factory=dict)

    def charge(self, completion: Completion) -> None:
        self.calls_made += 1
        self.input_tokens += completion.input_tokens
        self.output_tokens += completion.output_tokens
        self.by_model[completion.model_name] = self.by_model.get(completion.model_name, 0) + 1

    def check(self) -> None:
        if self.calls_made >= self.max_calls:
            raise BudgetExceededError(
                f"run hit its ceiling of {self.max_calls} model calls. "
                "Raise BLUEPAGES_MAX_LLM_CALLS_PER_RUN only if this is expected."
            )

    def summary(self) -> dict[str, Any]:
        return {
            "calls": self.calls_made,
            "max_calls": self.max_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "by_model": dict(self.by_model),
        }


class ModelClient:
    """Calls a model, falling back down the chain on infrastructure failures.

    `judgment=True` starts at the Sonnet-class model (semantic reasoning);
    otherwise it starts at Haiku-class (bulk extraction). Both fall back through
    the remaining rungs.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        stream: EventStream | None = None,
        budget: RunBudget | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.stream = stream or NullStream()
        self.budget = budget or RunBudget(max_calls=self.settings.bluepages_max_llm_calls_per_run)
        self._models: dict[str, Any] = {}

    # --- chain construction ------------------------------------------------

    def chain(self, judgment: bool) -> list[ModelRole]:
        """The ordered fallback chain for this kind of work.

        Falls back *within* Bedrock first and only leaves AWS on provider-level
        failure, per DECISIONS.md. `BLUEPAGES_CHAIN_ORDER=fallback_first`
        reverses the two groups for when Bedrock is reachable but throttled to
        zero throughput; the order *within* each group is unchanged.
        """
        s = self.settings
        # The label says the role, not the model: whatever id is configured for
        # judgment is the judgment rung even when it is not a Sonnet.
        judgment_rung = ModelRole("judgment", s.bedrock_model_judgment, "bedrock")
        bulk_rung = ModelRole("bulk", s.bedrock_model_bulk, "bedrock")

        bedrock = [judgment_rung, bulk_rung] if judgment else [bulk_rung, judgment_rung]

        free: list[ModelRole] = []
        if s.groq_api_key:
            free.append(ModelRole("groq", s.groq_model, "groq"))
        if s.gemini_api_key:
            free.append(ModelRole("gemini", s.gemini_model, "gemini"))

        if s.bluepages_chain_order == "fallback_first":
            return free + bedrock
        return bedrock + free

    # --- the call ----------------------------------------------------------

    def complete(
        self,
        prompt: str,
        system: str | None = None,
        judgment: bool = False,
        max_tokens: int | None = None,
        temperature: float = 0.0,
        cache_key_extra: str = "",
        label: str = "",
    ) -> Completion:
        """Run one completion through the chain. Always bounded, always logged.

        `label` says what the call is *for*, e.g. "scene 7". It reaches the
        event stream and therefore CloudWatch and the live view, where a run of
        identical "bulk (bedrock)" lines says nothing about what the agent is
        working on.
        """
        self.budget.check()

        # CLAUDE.md: always set max_tokens. No path leaves this None.
        max_tokens = max_tokens or self.settings.bluepages_max_tokens

        cache_path = self._cache_path(
            prompt, system, judgment, max_tokens, temperature, cache_key_extra
        )
        if cache_path is not None and cache_path.exists():
            cached = self._read_cache(cache_path)
            if cached is not None:
                self.stream.emit(
                    EventKind.MODEL_CALL_FINISHED,
                    f"{label}: cached" if label else f"cache hit ({cached.model_name})",
                    model=cached.model_name,
                    label=label,
                    cached=True,
                )
                return cached

        rungs = self.chain(judgment)
        failures: list[tuple[str, BaseException]] = []

        for depth, role in enumerate(rungs):
            self.stream.emit(
                EventKind.MODEL_CALL_STARTED,
                f"{label}: {role.name}" if label else f"{role.name} ({role.provider})",
                model=role.name,
                label=label,
                model_id=role.model_id,
                provider=role.provider,
                attempt=depth + 1,
            )
            started = time.time()
            try:
                text, usage = self._invoke(role, prompt, system, max_tokens, temperature)
            except Exception as exc:
                failures.append((role.name, exc))
                if not is_retryable(exc):
                    # Schema or prompt error: fails identically everywhere.
                    # Burning the rest of the chain on it helps nobody.
                    self.stream.emit(
                        EventKind.RUN_FAILED,
                        f"{role.name} failed fatally: {type(exc).__name__}: {exc}",
                        model=role.name,
                        error=type(exc).__name__,
                        fatal=True,
                    )
                    raise
                is_last = depth == len(rungs) - 1
                self.stream.emit(
                    EventKind.MODEL_FALLBACK,
                    f"{role.name} unavailable ({type(exc).__name__}); "
                    + ("chain exhausted" if is_last else f"falling back to {rungs[depth + 1].name}"),
                    from_model=role.name,
                    to_model=None if is_last else rungs[depth + 1].name,
                    error=type(exc).__name__,
                )
                continue

            completion = Completion(
                text=text,
                model_name=role.name,
                model_id=role.model_id,
                provider=role.provider,
                via_fallback=depth > 0,
                input_tokens=usage.get("inputTokens", 0),
                output_tokens=usage.get("outputTokens", 0),
                latency_s=time.time() - started,
            )
            self.budget.charge(completion)
            self.stream.emit(
                EventKind.MODEL_CALL_FINISHED,
                f"{label}: {role.name} in {completion.latency_s:.1f}s"
                if label
                else f"{role.name} answered in {completion.latency_s:.1f}s"
                + (" (via fallback)" if completion.via_fallback else ""),
                model=role.name,
                label=label,
                via_fallback=completion.via_fallback,
                input_tokens=completion.input_tokens,
                output_tokens=completion.output_tokens,
            )
            self.stream.emit(
                EventKind.TOKENS_SPENT,
                f"{completion.total_tokens} tokens",
                model=role.name,
                input_tokens=completion.input_tokens,
                output_tokens=completion.output_tokens,
                run_total=self.budget.input_tokens + self.budget.output_tokens,
            )
            if cache_path is not None:
                self._write_cache(cache_path, completion)
            return completion

        raise AllModelsFailedError(failures)

    # --- providers ---------------------------------------------------------

    def _invoke(
        self,
        role: ModelRole,
        prompt: str,
        system: str | None,
        max_tokens: int,
        temperature: float,
    ) -> tuple[str, dict[str, int]]:
        if role.provider == "bedrock":
            return self._invoke_bedrock(role, prompt, system, max_tokens, temperature)
        if role.provider == "groq":
            return self._invoke_groq(role, prompt, system, max_tokens, temperature)
        if role.provider == "gemini":
            return self._invoke_gemini(role, prompt, system, max_tokens, temperature)
        raise ValueError(f"unknown provider {role.provider!r}")

    def _invoke_bedrock(
        self,
        role: ModelRole,
        prompt: str,
        system: str | None,
        max_tokens: int,
        temperature: float,
    ) -> tuple[str, dict[str, int]]:
        """Call Bedrock, through Strands by default.

        `BLUEPAGES_AGENT_RUNTIME=boto3` drops to the raw Converse call below.
        Both paths return the same `(text, usage)` pair, so everything above
        this method, the cache, the budget and the fallback classifier, cannot
        tell which one answered.
        """
        if self.settings.bluepages_agent_runtime == "strands":
            return self._invoke_strands(role, prompt, system, max_tokens, temperature)
        return self._invoke_converse(role, prompt, system, max_tokens, temperature)

    def _invoke_strands(
        self,
        role: ModelRole,
        prompt: str,
        system: str | None,
        max_tokens: int,
        temperature: float,
    ) -> tuple[str, dict[str, int]]:
        """One completion through a Strands agent.

        The agent is built per call. These are stateless single completions and
        a reused agent would carry one scene's conversation into the next.
        """
        from bluepages.llm.agent_runtime import build_agent, text_of, usage_from

        agent = build_agent(
            role=role,
            system=system,
            max_tokens=max_tokens,
            temperature=temperature,
            budget=self.budget,
            region=self.settings.aws_region,
        )
        result = agent(prompt)
        return text_of(result), usage_from(result)

    def _invoke_converse(
        self,
        role: ModelRole,
        prompt: str,
        system: str | None,
        max_tokens: int,
        temperature: float,
    ) -> tuple[str, dict[str, int]]:
        """The raw boto3 Converse path, kept as the escape hatch."""
        client = self._bedrock_runtime()
        kwargs: dict[str, Any] = {
            "modelId": role.model_id,
            "messages": [{"role": "user", "content": [{"text": prompt}]}],
            "inferenceConfig": {"maxTokens": max_tokens, "temperature": temperature},
        }
        if system:
            kwargs["system"] = [{"text": system}]

        response = client.converse(**kwargs)
        text = "".join(
            block.get("text", "")
            for block in response["output"]["message"]["content"]
        )
        usage = response.get("usage", {})
        return text, {
            "inputTokens": usage.get("inputTokens", 0),
            "outputTokens": usage.get("outputTokens", 0),
        }

    def _bedrock_runtime(self) -> Any:
        if "bedrock" not in self._models:
            import boto3
            from botocore.config import Config

            self._models["bedrock"] = boto3.client(
                "bedrock-runtime",
                region_name=self.settings.aws_region,
                config=Config(
                    # Our own chain handles fallback; botocore retrying underneath
                    # would multiply latency before we ever see the throttle.
                    retries={"max_attempts": 2, "mode": "standard"},
                    read_timeout=120,
                    connect_timeout=10,
                ),
            )
        return self._models["bedrock"]

    def _invoke_groq(
        self,
        role: ModelRole,
        prompt: str,
        system: str | None,
        max_tokens: int,
        temperature: float,
    ) -> tuple[str, dict[str, int]]:
        from groq import Groq

        if "groq" not in self._models:
            self._models["groq"] = Groq(api_key=self.settings.groq_api_key)
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        resp = self._models["groq"].chat.completions.create(
            model=role.model_id,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        choice = resp.choices[0]
        text = choice.message.content or ""
        # A reasoning model spends max_tokens on hidden reasoning before it
        # writes anything, so a tight ceiling returns finish_reason="length"
        # and empty content. Returning that would record "this scene needs
        # nothing", which is a real answer and hides the failure completely.
        if not text.strip():
            raise TruncatedResponseError(
                f"{role.model_id} returned no content "
                f"(finish_reason={choice.finish_reason!r}, max_tokens={max_tokens}). "
                "Raise BLUEPAGES_MAX_TOKENS or use a non-reasoning model."
            )
        usage = resp.usage
        return text, {
            "inputTokens": getattr(usage, "prompt_tokens", 0),
            "outputTokens": getattr(usage, "completion_tokens", 0),
        }

    def _invoke_gemini(
        self,
        role: ModelRole,
        prompt: str,
        system: str | None,
        max_tokens: int,
        temperature: float,
    ) -> tuple[str, dict[str, int]]:
        from google import genai
        from google.genai import types

        if "gemini" not in self._models:
            self._models["gemini"] = genai.Client(api_key=self.settings.gemini_api_key)

        resp = self._models["gemini"].models.generate_content(
            model=role.model_id,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=system,
                max_output_tokens=max_tokens,
                temperature=temperature,
            ),
        )
        text = resp.text or ""
        if not text.strip():
            # Same trap as Groq: MAX_TOKENS truncation yields empty text, and
            # an empty answer is indistinguishable from "nothing to report".
            reason = getattr(
                (resp.candidates or [None])[0], "finish_reason", None
            )
            raise TruncatedResponseError(
                f"{role.model_id} returned no content (finish_reason={reason!r}, "
                f"max_tokens={max_tokens})."
            )
        meta = getattr(resp, "usage_metadata", None)
        return text, {
            "inputTokens": getattr(meta, "prompt_token_count", 0) or 0,
            "outputTokens": getattr(meta, "candidates_token_count", 0) or 0,
        }

    # --- cache -------------------------------------------------------------

    def _cache_path(self, *parts: Any) -> Path | None:
        """A stable path for this exact request, or None when caching is off."""
        if not self.settings.bluepages_cache_llm:
            return None
        digest = hashlib.sha256(
            json.dumps([str(p) for p in parts], sort_keys=True).encode()
        ).hexdigest()[:24]
        return self.settings.cache_dir / f"llm-{digest}.json"

    def _read_cache(self, path: Path) -> Completion | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None  # A corrupt cache entry is not worth failing a run over.
        return Completion(
            text=data["text"],
            model_name=data["model_name"],
            model_id=data.get("model_id", ""),
            provider=data.get("provider", ""),
            via_fallback=data.get("via_fallback", False),
            cached=True,
        )

    def _write_cache(self, path: Path, completion: Completion) -> None:
        # Caching is an optimisation; never fail a run for it.
        with contextlib.suppress(OSError):
            path.write_text(
                json.dumps({
                    "text": completion.text,
                    "model_name": completion.model_name,
                    "model_id": completion.model_id,
                    "provider": completion.provider,
                    "via_fallback": completion.via_fallback,
                }),
                encoding="utf-8",
            )
