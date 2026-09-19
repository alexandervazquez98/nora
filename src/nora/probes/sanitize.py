"""Zero-leakage sanitizer wrappers for probe-run output (issue #61 / PR3).

PR3 ships three new outbound surfaces that operators may copy into
support tickets — the PDF report, the inline Markdown summary, and
the ``icmp_list_probe_runs`` JSON listing. Every string that lands
in any of those surfaces MUST be Sanitizer-cleaned per SCOPE.md §2
so a private IPv4 literal or community string the operator
accidentally included in a sector alias / device id / run comment
is masked before it leaves the process.

PR3's bypass list
=================

The existing project-wide bypass list (in
``src/nora/intervention_memory/sanitize.py::_BYPASS_FIELDS``) was
authored for the intervention-memory record schema and lives
outside this package's allowed edit surfaces. PR3 ships its own
local bypass list scoped to probe-run metadata; the two lists are
disjoint by construction (intervention memory uses
``intervention_id`` / ``target_ip`` / ``stage``; probe-run metadata
uses ``run_id`` / ``sector`` / ``operator``).

Bypassed (probe-run metadata fields — typed scalars / filenames,
not free text):

* ``run_id``               — 8 hex chars from ``secrets.token_hex(4)``
* ``sector``               — operator alias; character class
                              ``^[A-Za-z0-9_-]+$`` per
                              ``build_probe_filename``
* ``operator``             — sanitized operator alias (this module's
                              own output; already passed through the
                              Sanitizer once)
* ``started_at_iso``       — ISO-8601 UTC timestamp from
                              ``datetime.utcfromtimestamp``
* ``finished_at_iso``      — same as above
* ``duration_seconds``     — int; never carries a private literal
* ``samples_count``        — int; never carries a private literal
* ``pdf_path``             — same character class as ``sector``;
                              the same regex applies verbatim

Every other free-text field — ``record_name`` / ``agent_name`` /
``findings_and_dictamen`` / ``rationale`` / etc. — flows through
``Sanitizer.sanitize`` so a private IPv4 literal the operator
typed in a comment is masked before the PDF is written.

PDF bytes are identity
======================

``sanitize_pdf_bytes`` is intentionally a no-op. The Sanitizer
operates on strings; reading a serialised PDF binary and walking
its compressed object streams would (a) require a heavy PDF parser
dependency, and (b) be redundant — every string fed to
``reportlab`` has already been Sanitizer-cleaned at the call site
(in ``render_probe_pdf``). The function exists so callers that
post-process rendered PDF bytes (e.g. a future ``--attach`` helper
that uploads to OpenChat) can wire it into a uniform sanitisation
pipeline without surprises.
"""

from __future__ import annotations

from typing import Any, Final, FrozenSet

from nora.sanitizer import Sanitizer

__all__ = [
    "BYPASS_FIELDS",
    "sanitize_markdown",
    "sanitize_pdf_bytes",
    "sanitize_run_metadata",
]


# Local bypass list for probe-run metadata. Mirror the documented
# list in the module docstring verbatim — every entry is a
# ``str``-typed scalar that cannot carry a private IPv4 literal
# or community string by construction. The verdict enum literals
# (`sector_verdict` / `ap_verdict` / `per_sm.*.verdict`) are
# typed ``DiagnosticVerdict`` strings — mirroring the existing
# convention in `src/nora/intervention_memory/sanitize.py`
# where `stage` and `status` enum strings bypass the sanitizer.
# The Sanitizer's serial regex would otherwise mistype ``EXCELLENT``
# (9 uppercase chars) as a serial and replace it with ``SERIAL_A``,
# producing a misleading tool response.
BYPASS_FIELDS: Final[FrozenSet[str]] = frozenset(
    {
        "run_id",
        "sector",
        "operator",
        "started_at_iso",
        "finished_at_iso",
        "duration_seconds",
        "samples_count",
        "pdf_path",
        # Typed DiagnosticVerdict enum literals (PR2 contracts).
        "sector_verdict",
        "ap_verdict",
        "verdict",
        # Per-SM verdict key inside the nested per_sm list. The walker
        # recurses into the dict first; the scalar `verdict: "EXCELLENT"`
        # inside a per_sm entry is matched here. Same diagnostic for
        # `luid`: typed LUID string (e.g. "002"); never free text.
        "luid",
        # `evaluated_at_unix` — float, never a private IPv4 literal.
        "evaluated_at_unix",
    }
)


def sanitize_run_metadata(
    metadata: dict[str, Any],
    *,
    sanitizer: Sanitizer | None = None,
) -> dict[str, Any]:
    """Walk ``metadata`` through the Sanitizer with a local bypass list.

    Args:
        metadata: The metadata dict that lands in PDF metadata AND
            in the Markdown header. Every value is recursively
            walked — dicts honour the bypass list at the top level;
            lists are walked item-by-item; strings are sanitized.
        sanitizer: Optional Sanitizer instance. Defaults to a fresh
            ``Sanitizer()`` so a one-shot call does not need to
            thread a session-wide instance through.

    Returns:
        A new dict with the same shape as ``metadata``; every
        free-text string has been replaced by its Sanitizer-cleaned
        equivalent and every bypass-key value is preserved
        verbatim.

    Pure: never mutates the input. Never raises.
    """
    san = sanitizer if sanitizer is not None else Sanitizer()
    result: dict[str, Any] = _sanitize_value(metadata, bypass=BYPASS_FIELDS, sanitizer=san)
    return result


def sanitize_markdown(
    md: str,
    *,
    sanitizer: Sanitizer | None = None,
) -> str:
    """Sanitize every free-text string in a Markdown summary.

    Args:
        md: The rendered Markdown body (typically produced by
            :func:`nora.probes.markdown.render_probe_markdown`).
        sanitizer: Optional Sanitizer instance. Defaults to a fresh
            ``Sanitizer()``.

    Returns:
        The same Markdown shape with every private IPv4 literal /
        MAC / serial / hostname replaced by its stable alias.

    Pure: never mutates the input. Never raises.
    """
    san = sanitizer if sanitizer is not None else Sanitizer()
    return san.sanitize(md).text


def sanitize_pdf_bytes(
    pdf: bytes,
    *,
    sanitizer: Sanitizer | None = None,  # noqa: ARG001 — kept for API uniformity
) -> bytes:
    """Identity sanitizer for rendered PDF bytes (issue #61 / PR3 WU-3.5).

    The function is a no-op by design — sanitisation happens at the
    *string-input* boundary inside :func:`nora.probes.pdf.render_probe_pdf`,
    not at the *binary-output* boundary here. Walking a serialised
    PDF's compressed object streams would require a heavy PDF
    parser dependency (the Sanitizer operates on plain text).

    The function exists so a uniform sanitisation pipeline
    (``sanitize_run_metadata`` / ``sanitize_markdown`` /
    ``sanitize_pdf_bytes``) can be applied at every surface
    without conditional branches. Returning ``pdf`` unchanged is
    the documented contract.

    Args:
        pdf: The PDF bytes from :func:`render_probe_pdf`.
        sanitizer: Optional Sanitizer instance. Unused; the
            parameter is accepted for API uniformity with the other
            helpers in this module.

    Returns:
        The input ``pdf`` unchanged. The return type is the same
        ``bytes`` object (identity-preserving).
    """
    return pdf


# ---------------------------------------------------------------------------
# Internal helpers (private — not exported).
# ---------------------------------------------------------------------------


def _sanitize_value(
    value: Any,
    *,
    bypass: FrozenSet[str],
    sanitizer: Sanitizer,
) -> Any:
    """Walk ``value`` recursively; bypass scalar (str) values whose key is in ``bypass``.

    Mirrors the recursive walker in
    ``src/nora/intervention_memory/sanitize.py`` — dict → check
    bypass list first, then recurse; list → recurse on items;
    ``str`` → ``sanitizer.sanitize(s).text``; other types → copy.

    The bypass check is **scalar-only**: when a key in ``bypass``
    maps to a ``dict`` or ``list``, the walker recurses into the
    container so nested free-text fields (e.g. ``verdict.rationale``)
    still get sanitized. This matches the convention in
    ``intervention_memory/sanitize.py`` where the bypass list is
    populated with typed enum scalars (`stage`, `status`,
    `target_ip`, ...) — none of which is a container.

    Pure: returns a new structure; never mutates the input.
    """
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and key in bypass and not isinstance(item, (dict, list)):
                out[key] = item
            else:
                out[key] = _sanitize_value(item, bypass=bypass, sanitizer=sanitizer)
        return out
    if isinstance(value, list):
        return [_sanitize_value(v, bypass=bypass, sanitizer=sanitizer) for v in value]
    if isinstance(value, str):
        return sanitizer.sanitize(value).text
    return value
