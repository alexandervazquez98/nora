"""SessionJournal — public API + typed exception hierarchy.

This module owns:

* The typed exception hierarchy (`SessionJournalError` + subclasses).
* The `SessionJournal` class itself (recording contract, persistence).
* The module-level `_journal` singleton + `init_session_journal` /
  `get_journal` accessors used by both the auto-trace middleware and the
  three explicit recall tools.

The full class lands in commit #4. This commit ships only the exception
hierarchy so that callers can already `from nora.core.session_journal
import SessionNotFoundError` without `ImportError`.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Typed exception hierarchy (R7, R11, R15).
#
# One base — `SessionJournalError` — keeps catch-blocks concise at the call
# site. Subclasses carry no extra fields beyond the message; the underlying
# file path is intentionally NOT stored on the exception (operators read it
# from the log line instead — keeps the on-disk contract simple).
# ---------------------------------------------------------------------------


class SessionJournalError(Exception):
    """Base for every typed error raised by the SessionJournal package."""


class SessionNotFoundError(SessionJournalError):
    """Raised when `nora_session_resume(session_id)` finds no canonical file (R7-S6)."""


class JournalCorruptError(SessionJournalError):
    """Raised when a canonical file contains bytes that are not valid JSON (R11-S1)."""


class JournalDisabledError(SessionJournalError):
    """Raised when the explicit tools are invoked while journal is disabled (R15-S2)."""


__all__ = [
    "SessionJournalError",
    "SessionNotFoundError",
    "JournalCorruptError",
    "JournalDisabledError",
]
