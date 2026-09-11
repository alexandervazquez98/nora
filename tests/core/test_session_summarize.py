"""`nora_session_summarize()` Markdown projection — R17 unit tests.

The integration test in `tests/test_server_session_tools.py` drives the
tool through a FastMCP Client. These unit tests pin the markdown template
shape, including the result_summary truncation contract (R17 refactor).
"""

from __future__ import annotations

from datetime import datetime, timezone


def _make_journal(journal_dir):
    from nora.core.session_journal import SessionJournal
    from nora.sanitizer import Sanitizer

    return SessionJournal(
        journal_dir=journal_dir,
        max_trace_steps=50,
        sanitizer=Sanitizer(),
        enabled=True,
    )


def test_summarize_includes_focus_device_id(journal_dir) -> None:
    """The summary MUST contain the `focus_device_id` string."""
    from nora.core.session_models import SessionState

    journal = _make_journal(journal_dir)
    state = SessionState.model_validate(journal.get_state().model_dump())
    state.focus_device_id = "ap-7400-01"
    state.devices_reviewed = ["ap-7400-01"]
    journal._state = state  # type: ignore[attr-defined]
    journal._persist(state)  # type: ignore[attr-defined]

    md = journal.summarize()
    assert "ap-7400-01" in md, f"focus_device_id missing from summary: {md!r}"


def test_summarize_includes_all_devices_reviewed(journal_dir) -> None:
    """The summary MUST list every `devices_reviewed` entry."""
    journal = _make_journal(journal_dir)
    journal.set_focus("ap-7400-01")
    journal.set_focus("ap-7400-02")
    journal.set_focus("ap-7400-03")

    md = journal.summarize()
    for did in ("ap-7400-01", "ap-7400-02", "ap-7400-03"):
        assert did in md, f"device {did!r} missing from summary: {md!r}"


def test_summarize_includes_tool_name_from_trace(journal_dir) -> None:
    """The summary MUST include at least one tool name from the trace."""
    journal = _make_journal(journal_dir)
    journal.record_step(
        tool="nora_session_set_focus",
        input_args={"device_id": "ap-7400-01"},
        result_summary="ok",
        duration_ms=2,
        outcome="success",
        ts=datetime.now(timezone.utc),
    )

    md = journal.summarize()
    assert "nora_session_set_focus" in md, f"Tool name missing from summary: {md!r}"


def test_summarize_truncates_long_result_summary(journal_dir) -> None:
    """A `result_summary` longer than 120 chars MUST be truncated to 120."""
    journal = _make_journal(journal_dir)
    long_summary = "x" * 500
    journal.record_step(
        tool="nora_health",
        input_args={},
        result_summary=long_summary,
        duration_ms=1,
        outcome="success",
        ts=datetime.now(timezone.utc),
    )

    md = journal.summarize()
    # The truncated summary in the table has 120 x's max; the full 500
    # must NOT appear.
    assert "x" * 121 not in md, f"summary not truncated: {md!r}"
    # And at least 1 x from the start of the summary IS present.
    assert "x" * 1 in md


def test_summarize_on_empty_trace_is_non_empty_markdown(journal_dir) -> None:
    """An empty trace produces non-empty Markdown describing the state."""
    journal = _make_journal(journal_dir)

    md = journal.summarize()
    assert md.strip(), "Empty trace summary returned empty Markdown"
    assert "Session" in md, f"Heading missing from summary: {md!r}"
    # Focus and devices labels are still present.
    assert "**Focus**" in md, f"Focus label missing: {md!r}"
    assert "**Devices reviewed**" in md, f"Devices label missing: {md!r}"
