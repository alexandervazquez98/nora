"""NORA core domain packages — Phase 2 SessionJournal surface.

Public modules:
    session_journal    — Public API for the journal (`SessionJournal`, typed exceptions).
    session_models     — Pydantic models: `SessionState`, `SessionStep`, `Outcome`.
    session_paths      — Atomic JSON write + 0o600 mode + path helpers.
    session_redaction  — R10 frozen redaction list + recursive walker.
    session_rotation   — NDJSON rotation helper for trace overflow (R5).

Air-gap rule (R8): NO source file in this package may import `requests`,
`httpx`, `urllib.request`, `socket`, `ssl`, or `http.client`. Enforced by
the AST scan test in `tests/core/test_session_journal_airgap.py`.
"""
