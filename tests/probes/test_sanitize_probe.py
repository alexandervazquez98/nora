"""Tests for `nora.probes.sanitize` (issue #61 / PR3 WU-3.14).

The Zero-Leakage contract: every string that lands in the rendered
PDF, the Markdown summary, or the ``icmp_list_probe_runs`` JSON
response MUST NOT carry a raw private IPv4 literal or community
string. PR3 ships a local bypass list (``run_id``, ``sector``,
``operator``, etc.) so typed scalars / filenames pass through
verbatim; everything else flows through the Sanitizer.

Tests:

1. Bypass fields stay verbatim (``run_id`` / ``sector``).
2. Non-bypass fields get the default Sanitizer treatment
   (``target_ip`` / ``device_id`` -> private IP literal masked).
3. Markdown free-text substrings are masked (PR3 WU-3.5 contract).
4. ``sanitize_pdf_bytes`` is the identity function (sanitisation
   is pull, not push).
"""

from __future__ import annotations

from nora.probes.sanitize import (
    BYPASS_FIELDS,
    sanitize_markdown,
    sanitize_pdf_bytes,
    sanitize_run_metadata,
)


def test_sanitize_run_metadata_leaves_run_id_intact() -> None:
    """``run_id`` and ``sector`` are bypass fields and stay verbatim."""
    payload = {"run_id": "abcd1234", "sector": "norte"}
    sanitized = sanitize_run_metadata(payload)
    assert sanitized["run_id"] == "abcd1234"
    assert sanitized["sector"] == "norte"


def test_sanitize_run_metadata_masks_private_ip_in_extra_fields() -> None:
    """``target_ip`` is NOT a bypass field — the literal is masked."""
    payload = {"run_id": "abcd1234", "target_ip": "10.0.0.5"}
    sanitized = sanitize_run_metadata(payload)
    # The run_id is on the bypass list (verbatim).
    assert sanitized["run_id"] == "abcd1234"
    # The target_ip is masked — the literal ``10.0.0.5`` MUST NOT
    # appear verbatim.
    assert "10.0.0.5" not in sanitized["target_ip"], (
        f"expected private IP to be masked; got target_ip={sanitized['target_ip']!r}"
    )
    # The Sanitizer replaces private IPs with ``RADIO_NODE_X`` aliases.
    assert "RADIO_NODE_" in sanitized["target_ip"]


def test_sanitize_markdown_removes_private_ip_substrings() -> None:
    """A private IPv4 literal in a Markdown body is replaced."""
    md = "AP 10.0.0.5 is the leader of sector norte"
    sanitized = sanitize_markdown(md)
    assert "10.0.0.5" not in sanitized, (
        f"private IPv4 literal leaked into Markdown; got {sanitized!r}"
    )


def test_sanitize_pdf_bytes_is_identity() -> None:
    """``sanitize_pdf_bytes`` returns the input bytes verbatim."""
    pdf = b"%PDF-1.7 fake content"
    assert sanitize_pdf_bytes(pdf) is pdf
    # Also accept a separate instance: equal bytes.
    assert sanitize_pdf_bytes(pdf) == pdf


def test_bypass_fields_constant_is_frozen() -> None:
    """``BYPASS_FIELDS`` is a frozenset (cannot be mutated at runtime)."""
    assert isinstance(BYPASS_FIELDS, frozenset)
    assert "run_id" in BYPASS_FIELDS
    assert "sector" in BYPASS_FIELDS
    assert "operator" in BYPASS_FIELDS
    assert "pdf_path" in BYPASS_FIELDS


def test_sanitize_run_metadata_walks_nested_dicts_and_lists() -> None:
    """Nested dicts / lists inside metadata are walked recursively."""
    payload = {
        "run_id": "abcd1234",
        "rationale": "ap-10.0.0.5 is slow",
        "verdict": {"sector_verdict": "EXCELLENT", "rationale": "ap-172.16.0.1 OK"},
        "per_sm": [{"luid": "002", "rationale": "ap-192.168.1.1 reach"}],
    }
    sanitized = sanitize_run_metadata(payload)
    assert sanitized["run_id"] == "abcd1234"
    # Top-level rationale — masked.
    assert "10.0.0.5" not in sanitized["rationale"]
    # Nested rationale (verdict.rationale) — masked.
    assert "172.16.0.1" not in sanitized["verdict"]["rationale"]
    # Nested inside a list (per_sm[*].rationale) — masked.
    assert "192.168.1.1" not in sanitized["per_sm"][0]["rationale"]
