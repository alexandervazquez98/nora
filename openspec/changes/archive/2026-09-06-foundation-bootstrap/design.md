# Design: Toolchain Foundation for NORA (Phase 1)

## Technical Approach

This change builds the greenfield Python 3.12 toolchain that every later phase inherits. Five modules ship under `src/nora/`: `config` (Pydantic `Settings`), `sanitizer` (fixed-policy masker), `llm` (`LLMProvider` ABC + factory + LMStudio/Gemini adapters), `server` (FastMCP boot + `nora_health`), and `__main__`. The factory reads `NORA_LLM_PROVIDER`, constructs exactly one provider at startup, and holds it for the process lifetime. Provider switch = `.env` edit + restart (logged at INFO). Both adapters wrap their SDK as context managers and surface any SDK failure as `LLMUnavailable` — never an automatic fallback.

The sanitizer sits in front of every value leaving the process (prompts and free-text tool responses). `Settings` values bypass it. FastMCP boots over stdio; a single `logging.StreamHandler(sys.stderr)` is the only configured handler, and `ruff`'s `T201` rule bans `print(...)` inside `src/nora/` to keep stdout clean for JSON-RPC. Spec mapping: `project-toolchain`, `secure-configuration`, `telemetry-sanitizer`, `llm-provider-interface`, `nora-mcp-server`.

## Architecture Decisions

### Decision: LLMProvider is a one-method ABC

| Choice | Alternatives | Rationale |
|---|---|---|
| `complete(prompt, *, system=None, context=None) -> Completion`; `Completion` is frozen with `text`, `model_id`, `raw`. No vendor types in signatures. | Richer multimodal ABC now; mixins; concrete-only providers. | Tiny surface lets change #2 evolve. Spec scenario "exactly one abstract method exists" enforces this. |

### Decision: lmstudio native SDK over OpenAI-compatible REST

| Choice | Alternatives | Rationale |
|---|---|---|
| `pip install lmstudio`; `import lmstudio as lms`; `lms.Client(api_host=...)` context-managed. | OpenAI-compatible REST at `localhost:1234/v1`; raw `httpx`. | Typed errors (`LMStudioError` family), model loading, streaming, chat helpers. Spec scenario "native SDK is used (not the OpenAI REST endpoint)" is testable via import scan. |

### Decision: google-genai pinned <3.0.0

| Choice | Alternatives | Rationale |
|---|---|---|
| `google-genai>=1,<3`; `from google import genai`. | v3 with AFC migration; raw REST via `google-auth` + `httpx`. | Official SDK warning: v3 breaks Automatic Function Calling. Pin is cheap insurance before Phase 3 tool-use wiring. |

### Decision: fastmcp>=3.2,<4

| Choice | Alternatives | Rationale |
|---|---|---|
| `fastmcp>=3.2,<4`; `FastMCP("nora")`; `@mcp.tool`. | fastmcp 4.x (background-task breaking changes); raw `mcp` low-level server. | v3 stable; v4 ships breaking changes. Phase 1 needs only stdio + tool registration. |

### Decision: stderr-only logging via stdlib logging

| Choice | Alternatives | Rationale |
|---|---|---|
| `logging.basicConfig(stream=sys.stderr)` once in `__main__`; `ruff` `T201` enabled for `src/nora/`. | `print()`; `structlog` default stdout; per-module handlers. | stdout is reserved for MCP JSON-RPC. Spec scenario "stdout is reserved for JSON-RPC" is enforced by lint, not discipline. |

### Decision: factory constructs exactly one provider at startup

| Choice | Alternatives | Rationale |
|---|---|---|
| `LLMProviderFactory.create(settings)` returns a singleton; no `set_provider()`, no `reload()`. Switching = `.env` edit + restart, logged at INFO. | Lazy per-call construction; runtime-reloadable chain; automatic fallback. | Matches the user-decided "no auto-fallback + edit .env + restart" decision. Spec scenarios "factory constructs the named provider once" and "provider switch requires restart" both pass trivially. |

### Decision: sanitizer is a pure function with no I/O

| Choice | Alternatives | Rationale |
|---|---|---|
| `Sanitizer.sanitize(text: str) -> SanitizedText`; instance holds a per-session alias dict; no `open`, `socket`, `os.environ`, `datetime` reads. | Module-level functions; file-backed alias persistence; clock-seeded nonces. | Determinism = byte-identical output for identical input. No side channels = no accidental telemetry leak. |

### Decision: sanitizer fixed-policy mask of four categories

| Choice | Alternatives | Rationale |
|---|---|---|
| Exactly: private IPv4 (`10/8`, `172.16/12`, `192.168/16`, `127/8`), MAC, serial, hostname. No per-call override, no regex plugins. | Pluggable regex registry; user-supplied rules; LLM-driven redaction. | Spec "Fixed Mask Categories" is a hard requirement. Phase 1 keeps the surface auditable; credentials stay out of scope (gated by `secure-configuration`). |

## Data Flow

### 1. Process boot

```
__main__.py          Settings()           LLMProviderFactory       FastMCP
    │                    │                       │                     │
    ├── configure log ──►│                       │                     │
    │   (stderr only)    │                       │                     │
    │                    ├── load .env ──►       │                     │
    │                    ├── parse fields        │                     │
    │                    ├──────────────────────►│                     │
    │                                            ├── create(settings) ─┤
    │                                            ├── returns singleton │
    │                                            ├────────────────────►│
    │                                            │                     ├── build server
    │                                            │                     ├── @mcp.tool register
    ├────────────────────────────────────────────┴────────────────────►├── mcp.run()  (stdio)
    │                                                                    │
    ▼
Claude Desktop / operator agent connects over JSON-RPC on stdin/stdout.
```

### 2. Sanitizer in flow

```
Operator prompt ──► MCP tool handler ──► provider.complete(prompt)
                                                │
                                                ▼
                                         sanitizer.sanitize(prompt)
                                                │
                                                ├─► replace private IPv4 / MAC / serial / hostname
                                                ├─► SanitizedText(text, counts)
                                                ▼
                                         SDK client.ask(sanitized.text)
                                                │
                                                ▼
                                         Completion(text, model_id, raw)
                                                │
                                                ▼
                                         tool returns Completion.text verbatim
```

### 3. Provider error → LLM_UNAVAILABLE

```
SDK raises LMStudioError / genai.APIError / socket.timeout
                       │
                       ▼
provider.complete() except clause
                       │
                       ├── log at WARNING (stderr): tool, provider, error class
                       │
                       ▼
LLMUnavailable(error_class, sanitized_message)
                       │
                       ▼
tool returns error envelope (free-text sanitized first)
                       │
                       ▼
Claude Desktop surfaces to operator
```

## File Changes

Toolchain: `pyproject.toml` (PEP 621 + deps + dev deps + `[tool.ruff]` T201 ban + `[tool.mypy]` strict), `uv.lock`, `.python-version` (`3.12`), `.env.example` (placeholders only), `Makefile` (targets `test`, `lint`, `type`, `format`, `run`, `lock`).

Source (`src/nora/`): `__init__.py` (`__version__`), `config.py` (`Settings` + `loaded_from`), `sanitizer.py` (`Sanitizer` + `SanitizedText` + `SanitizerInputError`), `llm.py` (ABC + factory + adapters + exceptions), `server.py` (`FastMCP("nora")` + `nora_health` + stderr logging), `__main__.py` (wires Settings → factory → server).

Tests (mirror layout): `tests/conftest.py`, `tests/test_config.py`, `tests/test_sanitizer.py`, `tests/test_llm.py`, `tests/test_server.py`.

Other: `.gitignore` modified to add `.pytest_cache/`, `.coverage`, `*.egg-info/` (`.venv/` and `__pycache__/` already present).

## Interfaces / Contracts

```python
# src/nora/llm.py
from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

@dataclass(frozen=True)
class Completion:
    text: str
    model_id: str
    raw: Any

class LLMUnavailable(Exception):
    """Raised when the active provider cannot fulfill a request."""

class UnknownProviderError(ValueError):
    """Raised when NORA_LLM_PROVIDER names a provider the factory cannot build."""

class LLMProvider(ABC):
    @abstractmethod
    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        context: dict | None = None,
    ) -> Completion: ...

class LMStudioProvider(LLMProvider):
    def __init__(self, api_host: str, model_id: str) -> None: ...

class GeminiProvider(LLMProvider):
    def __init__(self, api_key: str, model_id: str) -> None: ...

def build_provider(settings: Settings) -> LLMProvider: ...
```

```python
# src/nora/sanitizer.py
from __future__ import annotations
from dataclasses import dataclass

class SanitizerInputError(TypeError): ...

@dataclass(frozen=True)
class SanitizedText:
    text: str
    counts: dict[str, int]  # {"ip": int, "mac": int, "serial": int, "hostname": int}

class Sanitizer:
    def sanitize(self, text: str) -> SanitizedText: ...
```

```python
# src/nora/config.py
from __future__ import annotations
from typing import Literal
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )
    nora_llm_provider: Literal["lmstudio", "gemini"] = "lmstudio"
    lmstudio_api_host: str = "localhost:1234"
    lmstudio_model_id: str = "qwen2.5-7b-instruct"
    gemini_api_key: SecretStr | None = None
    gemini_model_id: str = "gemini-2.5-flash"
    loaded_from: Literal[".env", "process_env", "defaults"] = "defaults"
```

## Testing Strategy

| Layer | What | How |
|---|---|---|
| Unit | `sanitizer.sanitize` for all four categories + edge cases; `Settings()` precedence and `repr` masking; factory branching for `lmstudio` / `gemini` / unknown. | `pytest` with deterministic fixtures; no I/O; assert `Completion` shape and byte-identical sanitiser output. |
| Integration | `LMStudioProvider` against a mocked `lmstudio.Client`; `GeminiProvider` against a mocked `google.genai.Client`. | `unittest.mock` patches at the SDK boundary; assert `Completion.text` / `Completion.raw`; assert SDK errors map to `LLMUnavailable`. |
| MCP boot | `FastMCP` builds, `nora_health` registered, stdio starts, stderr gets structured log line, stdout gets only JSON-RPC. | Subprocess smoke: launch `python -m nora`, send `tools/list` on stdin, assert stderr startup line and stdout response. |

## Threat Matrix

N/A — Phase 1 ships no routing, shell, subprocess, VCS automation, executable-file classification, or process-integration boundary beyond the MCP stdio server (covered in the testing strategy).

## Migration / Rollout

No migration required — greenfield. Future changes will add modules under `src/nora/`; this change establishes the layout they will follow.

## Open Questions

None.
