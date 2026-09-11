"""Shared pytest fixtures for the NORA test suite.

The `nora.llm.build_provider` factory caches its result in module-level
state so repeated calls return the SAME provider instance (per
`llm-provider-interface` spec). Each test that constructs a provider must
start from a clean cache, otherwise the singleton leaks across tests.

We also expose a hermetic `.env` fixture for the MCP server tests in
Phase 5/6.
"""

from __future__ import annotations

import pytest

from nora import llm as llm_mod


@pytest.fixture(autouse=True)
def _reset_llm_factory_cache() -> None:
    """Reset the `build_provider` singleton between tests."""
    llm_mod._reset_factory_cache()
    yield
    llm_mod._reset_factory_cache()
