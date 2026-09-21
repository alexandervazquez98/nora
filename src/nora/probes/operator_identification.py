"""Resolve the operator alias for a probe-run output (issue #61 / PR3).

The PDF report, the Markdown summary, and the
``icmp_list_probe_runs`` listing all carry an "Operator" field so a
downstream support engineer can tell who ran the probe. The alias
is resolved at the @mcp.tool boundary (NOT inside ``run_probe``),
so the coordinator stays protocol-agnostic — pure ICMP, no
transport coupling.

Resolution order (highest priority first):

1. ``settings.nora_operator_alias`` — the operator-configured
   default. Read via :func:`getattr` so a deployment that has NOT
   added the new setting keeps booting (PR3 ships the resolver
   alongside the new ``Settings`` field; older settings objects
   fall through to step 2 / step 3).
2. ``header["X-NORA-Operator"]`` — when running under FastMCP
   HTTP transport, the orchestrator passes the operator alias as
   an HTTP header. Header lookup is case-insensitive.
3. The literal string ``"nora-operator"`` — the documented default
   when neither setting nor header is set.

The resolved alias is always passed through the project-wide
:class:`Sanitizer` so a private IPv4 literal or community string
the operator accidentally typed into the alias is masked before
it ever reaches the PDF / Markdown / MCP response.
"""

from __future__ import annotations

from typing import Any, Mapping

from nora.sanitizer import Sanitizer

__all__ = ["resolve_operator"]


# Sentinel / fallback operator alias. The literal value MUST stay
# ASCII-only and free of private IPv4 literals (the Sanitizer still
# passes it through unchanged, but the literal is the published
# default).
_DEFAULT_OPERATOR_ALIAS = "nora-operator"

# HTTP header name carrying the operator alias. Lowercased; the
# resolver does a case-insensitive lookup against `header`.
_OPERATOR_HEADER = "x-nora-operator"


def resolve_operator(
    *,
    settings: Any,
    transport: str | None = None,
    header: Mapping[str, str] | None = None,
    sanitizer: Sanitizer | None = None,
) -> str:
    """Return the operator alias for a probe run (Sanitizer-cleaned).

    Args:
        settings: The boot-time :class:`nora.config.Settings` instance.
            The resolver reads ``settings.nora_operator_alias`` when
            present; older settings objects without that attribute
            fall through to the header / default lookup. Tests pass
            a ``types.SimpleNamespace`` so the resolver stays
            decoupled from the live ``Settings`` Pydantic model.
        transport: Optional transport name (``"http"``, ``"stdio"``,
            ...). Currently informational only — the resolver
            accepts the header regardless of transport because
            ``stdio`` deployments also inject header values via the
            orchestrator. Future transports that ship header-less
            may consult this argument.
        header: Optional HTTP-header mapping. The lookup is
            case-insensitive (HTTP headers are case-insensitive per
            RFC 7230 §3.2). When the header is missing or its value
            is empty after stripping whitespace, the resolver falls
            through to the default.
        sanitizer: Optional :class:`Sanitizer` instance. When
            ``None``, a fresh ``Sanitizer()`` is instantiated — the
            alias map stays scoped to this single resolve call. A
            session-wide sanitizer keeps the alias stable across
            multiple resolves.

    Returns:
        The operator alias, Sanitizer-cleaned. Default
        ``"nora-operator"`` when neither settings nor header
        provides a value.

    Pure function — no I/O, no logging, no clock. Never raises
    (a missing header / a missing settings attribute is a normal
    fall-through case, not an error).
    """
    san = sanitizer if sanitizer is not None else Sanitizer()

    # Step 1: settings.nora_operator_alias (when set and non-empty).
    alias_from_settings = getattr(settings, "nora_operator_alias", None)
    if isinstance(alias_from_settings, str):
        candidate = alias_from_settings.strip()
        if candidate:
            return _clean(candidate, san)

    # Step 2: HTTP header (case-insensitive). The header lookup is
    # explicit so an operator who runs PR3 in stdio transport still
    # gets the right alias when the orchestrator injects the header.
    if header is not None:
        for key, value in header.items():
            if not isinstance(key, str) or not isinstance(value, str):
                continue
            if key.lower() == _OPERATOR_HEADER:
                candidate = value.strip()
                if candidate:
                    return _clean(candidate, san)
                break  # Empty header — fall through to default.

    # Step 3: default alias. The Sanitizer still runs (an alias
    # configured via env var could conceivably carry a private IP
    # literal in tests).
    return _clean(_DEFAULT_OPERATOR_ALIAS, san)


def _clean(alias: str, sanitizer: Sanitizer) -> str:
    """Run ``alias`` through ``sanitizer`` and return the cleaned text.

    Defensive against a Sanitizer that lacks the alias bucket (a
    stripped-down test double) — fall back to the literal alias.
    """
    try:
        sanitized = sanitizer.sanitize(alias)
    except (AttributeError, TypeError):
        return alias
    return sanitized.text
