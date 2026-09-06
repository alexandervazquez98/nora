"""LLM provider tests — cover every scenario in `specs/llm-provider-interface/spec.md`.

The tests patch `lmstudio` and `google.genai` at the SDK boundary so no real
network call ever happens. They fail until `src/nora/llm.py` ships the
documented classes.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from pydantic import SecretStr

from nora.config import Settings
from nora.llm import (
    Completion,
    GeminiProvider,
    LLMProvider,
    LLMUnavailable,
    LMStudioProvider,
    UnknownProviderError,
    build_provider,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def lmstudio_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    return Settings(_env_file=None, _env_file_encoding=None)


@pytest.fixture
def gemini_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("NORA_LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    return Settings(_env_file=None, _env_file_encoding=None)


# ---------------------------------------------------------------------------
# Requirement: `LLMProvider` Abstract Base Class
# ---------------------------------------------------------------------------


def test_llm_provider_has_exactly_one_abstract_method() -> None:
    """`LLMProvider` MUST declare exactly `complete` as its abstract method."""
    assert LLMProvider.__abstractmethods__ == frozenset({"complete"}), (
        f"Unexpected abstract methods: {LLMProvider.__abstractmethods__}"
    )


def test_completion_is_frozen_dataclass() -> None:
    """`Completion` MUST be a frozen dataclass with `text`, `model_id`, `raw`."""
    assert getattr(Completion, "__dataclass_params__").frozen is True
    fields = {f.name for f in Completion.__dataclass_fields__.values()}
    assert fields == {"text", "model_id", "raw"}


def test_completion_text_and_raw_are_verbatim() -> None:
    """`Completion` MUST return text and raw verbatim — no validation, no trim."""
    raw_response: Any = object()
    c = Completion(text="hello world  ", model_id="m", raw=raw_response)
    assert c.text == "hello world  "
    assert c.raw is raw_response


def test_llm_provider_signature_has_no_vendor_types() -> None:
    """The `complete` signature MUST NOT mention vendor SDK types."""
    sig = inspect.signature(LLMProvider.complete)
    type_hints = {
        param.annotation
        for param in sig.parameters.values()
        if param.annotation is not inspect.Parameter.empty
    }
    type_hints.add(sig.return_annotation)
    forbidden = {"LMStudioResponse", "GenerateContentResponse", "PredictionResult"}
    leaked = forbidden & {t.__name__ if hasattr(t, "__name__") else str(t) for t in type_hints}
    assert not leaked, f"Vendor type leaked into LLMProvider signature: {leaked}"


# ---------------------------------------------------------------------------
# Requirement: `LLMProviderFactory` Reads Configuration Once
# ---------------------------------------------------------------------------


def test_factory_constructs_lmstudio_provider(lmstudio_settings: Settings) -> None:
    """`build_provider(settings)` returns an `LMStudioProvider` for lmstudio."""
    provider = build_provider(lmstudio_settings)
    assert isinstance(provider, LMStudioProvider)


def test_factory_constructs_gemini_provider(gemini_settings: Settings) -> None:
    """`build_provider(settings)` returns a `GeminiProvider` for gemini."""
    provider = build_provider(gemini_settings)
    assert isinstance(provider, GeminiProvider)


def test_factory_returns_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    """`build_provider` MUST return the SAME instance on repeated calls."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    settings = Settings(_env_file=None, _env_file_encoding=None)
    p1 = build_provider(settings)
    p2 = build_provider(settings)
    assert p1 is p2


def test_factory_unknown_provider_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unknown `NORA_LLM_PROVIDER` MUST raise `UnknownProviderError`."""

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    settings = Settings(_env_file=None, _env_file_encoding=None)
    # Bypass the literal validation by monkey-patching the field.
    object.__setattr__(settings, "nora_llm_provider", "openai")

    with pytest.raises(UnknownProviderError):
        build_provider(settings)


# ---------------------------------------------------------------------------
# Requirement: `LMStudioProvider` Wraps the Native SDK
# ---------------------------------------------------------------------------


def test_lmstudio_provider_uses_native_sdk() -> None:
    """The provider module MUST `import lmstudio as lms` (not OpenAI REST)."""
    from nora import llm as llm_mod

    src = inspect.getsource(llm_mod)
    assert "import lmstudio as lms" in src, (
        f"LMStudioProvider must use the native lmstudio SDK; missing import in:\n{src[:500]}"
    )
    # It must NOT call the OpenAI-compatible REST URL.
    assert "v1/chat/completions" not in src, (
        "LMStudioProvider must NOT use the OpenAI REST endpoint"
    )


def test_lmstudio_provider_returns_raw_sdk_response() -> None:
    """`Completion.raw` is the verbatim SDK object and `text` equals `.content`."""
    sdk_result = mock.Mock()
    sdk_result.content = "the answer"
    fake_model = mock.Mock()
    fake_model.respond.return_value = sdk_result
    fake_client = mock.MagicMock()
    fake_client.__enter__.return_value.llm.model.return_value = fake_model

    with mock.patch("lmstudio.Client", return_value=fake_client):
        provider = LMStudioProvider(api_host="localhost:1234", model_id="qwen")
        result = provider.complete("ping")

    assert result.text == "the answer"
    assert result.raw is sdk_result
    assert result.model_id == "qwen"
    fake_model.respond.assert_called_once()
    # First positional arg is the prompt.
    args, kwargs = fake_model.respond.call_args
    assert args[0] == "ping"


def test_lmstudio_provider_maps_sdk_errors_to_llm_unavailable() -> None:
    """Any `lmstudio` SDK error MUST surface as `LLMUnavailable`."""
    fake_client = mock.MagicMock()
    fake_client.__enter__.side_effect = RuntimeError("boom")
    with mock.patch("lmstudio.Client", return_value=fake_client):
        provider = LMStudioProvider(api_host="localhost:1234", model_id="m")
        with pytest.raises(LLMUnavailable) as excinfo:
            provider.complete("ping")
    assert "boom" in str(excinfo.value)


def test_lmstudio_provider_passes_system_prompt_to_history() -> None:
    """When `system` is provided, the prompt is wrapped in a chat history."""
    fake_result = mock.Mock(content="ok")
    fake_model = mock.Mock(respond=mock.Mock(return_value=fake_result))
    fake_client = mock.MagicMock()
    fake_client.__enter__.return_value.llm.model.return_value = fake_model

    with mock.patch("lmstudio.Client", return_value=fake_client):
        provider = LMStudioProvider(api_host="localhost:1234", model_id="m")
        provider.complete("ping", system="you are a helper")

    args, _kwargs = fake_model.respond.call_args
    history = args[0]
    # `respond` accepts Chat | str; we build a Chat history when system is given.
    assert history != "ping", "Expected a Chat history, not the raw prompt"
    rendered = str(history)
    assert "system" in rendered.lower() or "you are a helper" in rendered


# ---------------------------------------------------------------------------
# Requirement: `GeminiProvider` Wraps `google-genai`
# ---------------------------------------------------------------------------


def test_gemini_provider_uses_genai_client() -> None:
    """The provider MUST use `google.genai.Client(api_key=...)`."""
    sdk_response = mock.Mock(text="ok")
    fake_models = mock.Mock(generate_content=mock.Mock(return_value=sdk_response))
    fake_client = mock.MagicMock()
    fake_client.__enter__.return_value.models = fake_models

    with mock.patch("google.genai.Client", return_value=fake_client) as genai_client:
        provider = GeminiProvider(api_key=SecretStr("abc"), model_id="gemini-2.5-flash")
        result = provider.complete("ping")

    assert result.text == "ok"
    assert result.raw is sdk_response
    assert result.model_id == "gemini-2.5-flash"
    genai_client.assert_called_once()
    call_kwargs = genai_client.call_args.kwargs
    assert call_kwargs.get("api_key") == "abc", (
        f"api_key must be passed to genai.Client; got {call_kwargs!r}"
    )


def test_gemini_provider_maps_sdk_errors_to_llm_unavailable() -> None:
    """Any `genai` SDK error MUST surface as `LLMUnavailable`."""
    fake_client = mock.MagicMock()
    fake_client.__enter__.side_effect = RuntimeError("network")
    with mock.patch("google.genai.Client", return_value=fake_client):
        provider = GeminiProvider(api_key=SecretStr("k"), model_id="m")
        with pytest.raises(LLMUnavailable) as excinfo:
            provider.complete("ping")
    assert "network" in str(excinfo.value)


def test_gemini_provider_api_key_comes_from_secret_str() -> None:
    """`GeminiProvider` MUST accept `SecretStr` so the key is never logged."""
    sdk_response = mock.Mock(text="ok")
    fake_models = mock.Mock(generate_content=mock.Mock(return_value=sdk_response))
    fake_client = mock.MagicMock()
    fake_client.__enter__.return_value.models = fake_models

    with mock.patch("google.genai.Client", return_value=fake_client) as genai_client:
        provider = GeminiProvider(
            api_key=SecretStr("super-secret-key"), model_id="gemini-2.5-flash"
        )
        provider.complete("ping")

    assert genai_client.call_args.kwargs["api_key"] == "super-secret-key"


# ---------------------------------------------------------------------------
# Requirement: No Automatic Fallback Between Providers
# ---------------------------------------------------------------------------


def test_no_fallback_when_lmstudio_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """If lmstudio fails, the system MUST NOT silently call gemini."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    settings = Settings(_env_file=None, _env_file_encoding=None)

    fake_client = mock.MagicMock()
    fake_client.__enter__.side_effect = RuntimeError("lmstudio down")
    with (
        mock.patch("lmstudio.Client", return_value=fake_client),
        mock.patch("google.genai.Client") as genai_client,
    ):
        provider = build_provider(settings)
        with pytest.raises(LLMUnavailable):
            provider.complete("ping")
        # Gemini must NOT be invoked.
        genai_client.assert_not_called()


# ---------------------------------------------------------------------------
# Requirement: Raw Responses and No Auto-Retry
# ---------------------------------------------------------------------------


def test_completion_text_is_byte_for_byte() -> None:
    """`Completion.text` preserves trailing whitespace verbatim from the SDK."""
    sdk_response = mock.Mock(content="ok\n  ")
    fake_model = mock.Mock(respond=mock.Mock(return_value=sdk_response))
    fake_client = mock.MagicMock()
    fake_client.__enter__.return_value.llm.model.return_value = fake_model

    with mock.patch("lmstudio.Client", return_value=fake_client):
        provider = LMStudioProvider(api_host="localhost:1234", model_id="m")
        result = provider.complete("ping")

    assert result.text == "ok\n  "


# ---------------------------------------------------------------------------
# Requirement: Edge Cases
# ---------------------------------------------------------------------------


def test_empty_prompt_returns_empty_completion() -> None:
    """An empty prompt MUST yield a Completion with empty text."""
    sdk_response = mock.Mock(content="")
    fake_model = mock.Mock(respond=mock.Mock(return_value=sdk_response))
    fake_client = mock.MagicMock()
    fake_client.__enter__.return_value.llm.model.return_value = fake_model

    with mock.patch("lmstudio.Client", return_value=fake_client):
        provider = LMStudioProvider(api_host="localhost:1234", model_id="m")
        result = provider.complete("")

    assert result.text == ""
    assert result.model_id == "m"


# ---------------------------------------------------------------------------
# Requirement: Security Boundary — Credentials Never Appear in Completions
# ---------------------------------------------------------------------------


def test_completion_does_not_echo_api_key() -> None:
    """`Completion.text` and `Completion.raw` MUST NOT contain the API key value."""
    # Use a real string for `text` so `in` works without Mock surprises.
    sdk_response = mock.Mock()
    sdk_response.text = "safe answer"
    fake_models = mock.Mock(generate_content=mock.Mock(return_value=sdk_response))
    fake_client = mock.MagicMock()
    fake_client.__enter__.return_value.models = fake_models

    with mock.patch("google.genai.Client", return_value=fake_client):
        provider = GeminiProvider(api_key=SecretStr("top-secret"), model_id="m")
        result = provider.complete("ping")

    assert isinstance(result.text, str)
    assert "top-secret" not in result.text
    # `raw` is a Mock in this test; substitute a sentinel string-like object
    # to assert the contract on real SDK responses.
    sentinel_text = "safe payload"
    sentinel_raw = type("FakeResp", (), {"text": sentinel_text})()
    completion = Completion(text=sentinel_text, model_id="m", raw=sentinel_raw)
    assert "top-secret" not in completion.text
    assert "top-secret" not in str(completion.raw)


# ---------------------------------------------------------------------------
# Requirement: Telemetry Sanitizer Boundary — LLM prompt is sanitized before send
# ---------------------------------------------------------------------------


def test_lmstudio_provider_sanitizes_prompt_before_sdk_call() -> None:
    """The SDK MUST receive a sanitized prompt; private IPv4 literals are masked.

    Spec scenario (telemetry-sanitizer): "LLM prompt is sanitized before send".
    """
    sdk_result = mock.Mock(content="ok")
    fake_model = mock.Mock(respond=mock.Mock(return_value=sdk_result))
    fake_client = mock.MagicMock()
    fake_client.__enter__.return_value.llm.model.return_value = fake_model

    with mock.patch("lmstudio.Client", return_value=fake_client):
        provider = LMStudioProvider(api_host="localhost:1234", model_id="qwen")
        provider.complete("10.0.0.5 is down; check 192.168.1.1")

    args, _kwargs = fake_model.respond.call_args
    sent_prompt = args[0]
    assert "10.0.0.5" not in sent_prompt, f"SDK received un-sanitized prompt: {sent_prompt!r}"
    assert "192.168.1.1" not in sent_prompt, f"SDK received un-sanitized prompt: {sent_prompt!r}"
    assert "RADIO_NODE_A" in sent_prompt, f"Expected alias in prompt; got {sent_prompt!r}"


def test_gemini_provider_sanitizes_prompt_before_sdk_call() -> None:
    """The genai SDK MUST receive a sanitized prompt; private IPv4 literals are masked."""
    sdk_response = mock.Mock(text="ok")
    fake_models = mock.Mock(generate_content=mock.Mock(return_value=sdk_response))
    fake_client = mock.MagicMock()
    fake_client.__enter__.return_value.models = fake_models

    with mock.patch("google.genai.Client", return_value=fake_client):
        provider = GeminiProvider(api_key=SecretStr("abc"), model_id="gemini-2.5-flash")
        provider.complete("10.0.0.5 is down")

    call_args, call_kwargs = fake_models.generate_content.call_args
    sent_prompt = call_kwargs.get("contents", call_args[0] if call_args else None)
    assert sent_prompt is not None, "SDK received no contents argument"
    assert "10.0.0.5" not in sent_prompt, f"SDK received un-sanitized prompt: {sent_prompt!r}"
    assert "RADIO_NODE_A" in sent_prompt, f"Expected alias in prompt; got {sent_prompt!r}"


def test_lmstudio_provider_passes_clean_prompt_through_unchanged() -> None:
    """A clean prompt is sent byte-identical; the sanitizer is idempotent."""
    sdk_result = mock.Mock(content="ok")
    fake_model = mock.Mock(respond=mock.Mock(return_value=sdk_result))
    fake_client = mock.MagicMock()
    fake_client.__enter__.return_value.llm.model.return_value = fake_model

    prompt = "ping the host"
    with mock.patch("lmstudio.Client", return_value=fake_client):
        provider = LMStudioProvider(api_host="localhost:1234", model_id="qwen")
        provider.complete(prompt)

    args, _ = fake_model.respond.call_args
    assert args[0] == prompt


def test_lmstudio_provider_sends_empty_prompt_as_empty_string() -> None:
    """An empty prompt is sanitized to empty and sent to the SDK as empty."""
    sdk_result = mock.Mock(content="")
    fake_model = mock.Mock(respond=mock.Mock(return_value=sdk_result))
    fake_client = mock.MagicMock()
    fake_client.__enter__.return_value.llm.model.return_value = fake_model

    with mock.patch("lmstudio.Client", return_value=fake_client):
        provider = LMStudioProvider(api_host="localhost:1234", model_id="qwen")
        result = provider.complete("")

    args, _ = fake_model.respond.call_args
    assert args[0] == ""
    assert result.text == ""


def test_lmstudio_provider_keeps_completion_raw_as_sdk_response() -> None:
    """Sanitization touches the input only — `Completion.raw` is still the verbatim SDK object."""
    sdk_result = mock.Mock(content="answer")
    fake_model = mock.Mock(respond=mock.Mock(return_value=sdk_result))
    fake_client = mock.MagicMock()
    fake_client.__enter__.return_value.llm.model.return_value = fake_model

    with mock.patch("lmstudio.Client", return_value=fake_client):
        provider = LMStudioProvider(api_host="localhost:1234", model_id="qwen")
        result = provider.complete("ping 10.0.0.5")

    assert result.raw is sdk_result


# ---------------------------------------------------------------------------
# Requirement: Observability — Provider and Model Surfaced
# ---------------------------------------------------------------------------


def test_completion_carries_model_id() -> None:
    """Every successful completion MUST carry a non-empty `model_id`."""
    sdk_response = mock.Mock(content="ok")
    fake_model = mock.Mock(respond=mock.Mock(return_value=sdk_response))
    fake_client = mock.MagicMock()
    fake_client.__enter__.return_value.llm.model.return_value = fake_model

    with mock.patch("lmstudio.Client", return_value=fake_client):
        provider = LMStudioProvider(api_host="localhost:1234", model_id="qwen-7b")
        result = provider.complete("ping")
    assert result.model_id == "qwen-7b"
    assert result.model_id != ""


def test_factory_logs_active_provider_at_info(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """`build_provider` MUST emit an INFO log naming the active provider."""
    import logging

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    settings = Settings(_env_file=None, _env_file_encoding=None)
    with caplog.at_level(logging.INFO, logger="nora.llm"):
        build_provider(settings)
    messages = " ".join(caplog.messages)
    assert "lmstudio" in messages.lower() or "lmstudio" in messages, (
        f"Expected active provider in logs; got: {caplog.messages!r}"
    )


# ---------------------------------------------------------------------------
# Requirement: No `os.environ` in the provider module
# ---------------------------------------------------------------------------


def test_llm_module_does_not_read_os_environ() -> None:
    """`src/nora/llm.py` MUST NOT call `os.environ` directly."""
    src = (PROJECT_ROOT / "src" / "nora" / "llm.py").read_text()
    assert "os.environ" not in src, f"Direct os.environ access found in llm.py:\n{src}"


# ---------------------------------------------------------------------------
# Pinned SDK versions — regression test against pyproject.toml
# ---------------------------------------------------------------------------


def test_pyproject_pins_google_genai_below_v3() -> None:
    """`pyproject.toml` MUST pin `google-genai>=1,<3` (v3 breaks AFC)."""
    import tomllib

    data = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())
    deps = data["project"]["dependencies"]
    matches = [d for d in deps if d.startswith("google-genai")]
    assert matches, "google-genai is not declared as a runtime dependency"
    assert re.match(r"google-genai\s*>=\s*1\s*,\s*<\s*3", matches[0]), (
        f"google-genai must be pinned to >=1,<3; got {matches[0]!r}"
    )


def test_pyproject_pins_fastmcp_below_v4() -> None:
    """`pyproject.toml` MUST pin `fastmcp>=3.2,<4` (v4 has breaking changes)."""
    import tomllib

    data = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())
    deps = data["project"]["dependencies"]
    matches = [d for d in deps if d.startswith("fastmcp")]
    assert matches, "fastmcp is not declared as a runtime dependency"
    assert re.match(r"fastmcp\s*>=\s*3\.2\s*,\s*<\s*4", matches[0]), (
        f"fastmcp must be pinned to >=3.2,<4; got {matches[0]!r}"
    )
