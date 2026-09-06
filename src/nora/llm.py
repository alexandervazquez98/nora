"""LLM provider interface and adapters.

This module owns the `LLMProvider` ABC and the factory that builds exactly
one provider at startup. Each adapter wraps its vendor SDK as a context
manager and converts any SDK error into `LLMUnavailable`. There is no
automatic fallback between providers.

The factory caches the constructed provider in module-level state so a
second call returns the SAME instance. Switching providers requires editing
`.env` and restarting the process.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from pydantic import SecretStr

from nora.config import Settings
from nora.sanitizer import Sanitizer

logger = logging.getLogger("nora.llm")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class LLMUnavailable(Exception):
    """Raised when the active provider cannot fulfil a request."""


class UnknownProviderError(ValueError):
    """Raised when `NORA_LLM_PROVIDER` names a provider the factory cannot build."""


# ---------------------------------------------------------------------------
# Completion value object
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Completion:
    """Result of a provider `complete()` call."""

    text: str
    model_id: str
    raw: Any


# ---------------------------------------------------------------------------
# Abstract base class
# ---------------------------------------------------------------------------


class LLMProvider(ABC):
    """Provider interface — exactly one abstract method, no vendor types."""

    @abstractmethod
    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> Completion:
        """Run a single completion against the active model.

        `system` is an optional system prompt; `context` is opaque, reserved
        for future use (Phase 3+) and intentionally ignored in Phase 1.
        """


# ---------------------------------------------------------------------------
# Errors that should map to `LLMUnavailable`. Each SDK raises its own typed
# errors; we catch broadly so transient network or model failures are
# surfaced uniformly.
# ---------------------------------------------------------------------------

_SAN: Sanitizer = Sanitizer()
_SDK_ERROR_MAP: tuple[type[BaseException], ...] = (
    Exception,  # Catch-all at the boundary; we re-raise as LLMUnavailable.
)


def _to_unavailable(exc: BaseException) -> LLMUnavailable:
    """Wrap any SDK exception as `LLMUnavailable` with a sanitized message."""
    logger.warning(
        "llm provider call failed: class=%s message=%s",
        type(exc).__name__,
        exc,
    )
    return LLMUnavailable(f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# LM Studio provider
# ---------------------------------------------------------------------------


class LMStudioProvider(LLMProvider):
    """LM Studio provider using the native `lmstudio` Python SDK."""

    def __init__(self, api_host: str, model_id: str) -> None:
        self._api_host = api_host
        self._model_id = model_id

    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> Completion:
        # Late import keeps `import nora` cheap when the user only touches
        # configuration or the sanitizer.
        import lmstudio as lms

        try:
            with lms.Client(api_host=self._api_host) as client:
                model = client.llm.model(self._model_id)
                result = model.respond(self._build_history(_SAN.sanitize(prompt).text, system))
        except _SDK_ERROR_MAP as exc:
            raise _to_unavailable(exc) from exc

        # `PredictionResult` exposes the generated text on `.content`.
        text: str = getattr(result, "content", "") or ""
        return Completion(text=text, model_id=self._model_id, raw=result)

    @staticmethod
    def _build_history(prompt: str, system: str | None) -> Any:
        """Build a chat history payload for `model.respond`.

        With no system prompt, pass the raw string. With a system prompt,
        build a `Chat` so the model sees a structured role message.
        """
        if system is None:
            return prompt

        try:
            from lmstudio import Chat

            chat = Chat()
            chat.add_system_prompt(system)
            chat.add_user_message(prompt)
            return chat
        except ImportError:  # pragma: no cover - lmstudio not installed
            # If the helper isn't available, fall back to the raw prompt.
            return prompt


# ---------------------------------------------------------------------------
# Gemini provider
# ---------------------------------------------------------------------------


class GeminiProvider(LLMProvider):
    """Google Gemini provider using the official `google-genai` SDK."""

    def __init__(self, api_key: SecretStr, model_id: str) -> None:
        self._api_key = api_key
        self._model_id = model_id

    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> Completion:
        # Late import keeps `import nora` cheap.
        from google import genai

        try:
            with genai.Client(api_key=self._api_key.get_secret_value()) as client:
                config: dict[str, Any] | None = {"system_instruction": system} if system else None
                response = client.models.generate_content(
                    model=self._model_id,
                    contents=_SAN.sanitize(prompt).text,
                    config=config,  # type: ignore[arg-type]
                )
        except _SDK_ERROR_MAP as exc:
            raise _to_unavailable(exc) from exc

        text: str = getattr(response, "text", "") or ""
        return Completion(text=text, model_id=self._model_id, raw=response)


# ---------------------------------------------------------------------------
# Factory — singleton, no in-process switching.
# ---------------------------------------------------------------------------


_factory_cache: LLMProvider | None = None


def build_provider(settings: Settings) -> LLMProvider:
    """Construct (or return the cached) provider for `settings`.

    The same `Settings` instance can be passed multiple times — the factory
    returns the SAME provider object. To switch providers, edit `.env` and
    restart the process.
    """
    global _factory_cache
    if _factory_cache is not None:
        return _factory_cache

    name = settings.nora_llm_provider
    if name == "lmstudio":
        provider: LLMProvider = LMStudioProvider(
            api_host=settings.lmstudio_api_host,
            model_id=settings.lmstudio_model_id,
        )
        logger.info(
            "llm provider initialised: name=%s model=%s",
            name,
            settings.lmstudio_model_id,
        )
    elif name == "gemini":
        # `Settings` enforces presence of gemini_api_key when provider='gemini'.
        assert settings.gemini_api_key is not None  # for type checkers
        provider = GeminiProvider(
            api_key=settings.gemini_api_key,
            model_id=settings.gemini_model_id,
        )
        logger.info(
            "llm provider initialised: name=%s model=%s",
            name,
            settings.gemini_model_id,
        )
    else:
        raise UnknownProviderError(
            f"NORA_LLM_PROVIDER={name!r} is not supported; expected 'lmstudio' or 'gemini'."
        )

    _factory_cache = provider
    return provider


def _reset_factory_cache() -> None:
    """Test-only hook to clear the singleton between isolated unit tests."""
    global _factory_cache
    _factory_cache = None


__all__ = [
    "Completion",
    "LLMProvider",
    "LLMUnavailable",
    "UnknownProviderError",
    "LMStudioProvider",
    "GeminiProvider",
    "build_provider",
]
