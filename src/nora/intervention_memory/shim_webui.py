"""webui.db mirror shim — `class Tools` with 3 `async def` methods.

The shim exposes the same three read-only tool surfaces as the MCP
wrappers in `src/nora/server.py`. openchat's deploy script consumes
`inspect.getsource(Tools)` and pastes the rendered text into the
`webui.db` `tool.content` column (mirroring `deploy_v7_intervention_memory.py`).

Dependency direction is one-way: `shim_webui.py` imports from
`nora.intervention_memory.tools`. Production modules MUST NOT import
from `shim_webui` (enforced by `tests/intervention_memory/test_no_writes.py`).

Constants on the class (`INTERVENTIONS_DIR`, `KEYWORD_SEARCH_MAX_RECORDS`,
`CORRELATE_SCAN_LIMIT`) are read by openchat's deploy script and
overwritten at deploy time. They are placeholders here.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from nora.config import Settings
# Import the module (not the names) so tests can monkey-patch
# `tools.search_intervention_history` and the shim picks up the patched
# call via attribute access. Local-name imports would create a static
# binding that survives `mock.patch.object(tools_mod, ...)`.
from nora.intervention_memory import tools as tools_mod
from nora.sanitizer import Sanitizer

# Module-level Sanitizer for stable aliases across multiple shim
# invocations in the same process. Tests can monkey-patch this with a
# fresh instance if they need isolated alias maps.
_shim_sanitizer = Sanitizer()


def _settings_from_env_or_singleton() -> Settings:
    """Build a hermetic `Settings` instance from env defaults.

    The shim never reaches into `nora.server`'s runtime state — it
    constructs its own `Settings` from the process env so openchat's
    in-process execution does NOT depend on NORA's boot sequence.
    """
    return Settings(_env_file=None, _env_file_encoding=None)


class Tools:
    """webui.db mirror shim — openchat's `Tools` class.

    Each method is `async def` to match openwebui's in-process execution
    convention. The method bodies are 1:1 delegators to the library
    functions in `tools.py`, so MCP-exposed tools and webui.db-registered
    tools cannot drift.

    The `__event_emitter__` keyword is accepted and ignored — openwebui
    passes it to every tool it spawns.
    """

    # Constants the openchat deploy script overwrites at deploy time.
    INTERVENTIONS_DIR: str = ""
    KEYWORD_SEARCH_MAX_RECORDS: int = 1000
    CORRELATE_SCAN_LIMIT: int = 50

    async def search_intervention_history(
        self,
        target_ip: Optional[str] = None,
        ticket_number: Optional[str] = None,
        stage: Optional[str] = None,
        keyword: Optional[str] = None,
        limit: int = 5,
        __event_emitter__: Any = None,
        **_unused: Any,
    ) -> str:
        """Mirror of MCP `search_intervention_history`. Returns JSON string (webui.db convention)."""
        settings = _settings_from_env_or_singleton()
        results = tools_mod.search_intervention_history(
            settings=settings,
            target_ip=target_ip,
            ticket_number=ticket_number,
            stage=stage,
            keyword=keyword,
            limit=limit,
            sanitizer=_shim_sanitizer,
        )
        return json.dumps(results)

    async def get_device_lifecycle_summary(
        self,
        target_ip: str,
        __event_emitter__: Any = None,
        **_unused: Any,
    ) -> str:
        """Mirror of MCP `get_device_lifecycle_summary`. Returns JSON string."""
        settings = _settings_from_env_or_singleton()
        summary = tools_mod.get_device_lifecycle_summary(
            settings=settings,
            target_ip=target_ip,
            sanitizer=_shim_sanitizer,
        )
        return json.dumps(summary)

    async def correlate_sector_interference(
        self,
        tower_name: str,
        target_frequency_mhz: float,
        channel_width_mhz: float = 20.0,
        __event_emitter__: Any = None,
        **_unused: Any,
    ) -> str:
        """Mirror of MCP `correlate_sector_interference`. Returns JSON string."""
        settings = _settings_from_env_or_singleton()
        result = tools_mod.correlate_sector_interference(
            settings=settings,
            tower_name=tower_name,
            target_frequency_mhz=target_frequency_mhz,
            channel_width_mhz=channel_width_mhz,
            sanitizer=_shim_sanitizer,
        )
        return json.dumps(result)


__all__ = ["Tools"]