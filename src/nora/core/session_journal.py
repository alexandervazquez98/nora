"""SessionJournal — public API + typed exception hierarchy.

This module owns:

* The typed exception hierarchy (`SessionJournalError` + subclasses).
* The `SessionJournal` class — the recording contract, persistence, and
  the `init_session_journal` / `get_journal` accessors.
* A module-level `_journal` singleton used by both the auto-trace
  middleware and the 4 explicit recall tools.

Commit map:
    #2 — exceptions only (so call sites can already import them).
    #4 — this commit: load / save / append / get_state (no redaction,
         no rotation, no sanitize yet).
    #5 — wire R10 redaction + Sanitizer into `record_step`.
    #6 — wire NDJSON rotation into `record_step`.
    #9 — add `summarize()` (R17).
    #10 — add the `NORA_SESSION_JOURNAL_ENABLED` gate.
    #11 — add R11 corrupt-file recovery.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nora.config import Settings
from nora.core.session_models import Outcome, SessionState, SessionStep
from nora.core.session_paths import (
    atomic_write_json,
    canonical_path,
    ensure_journal_dir,
    ndjson_path,
)
from nora.core.session_redaction import REDACTION_LIST, redact
from nora.core.session_rotation import rotate_if_needed
from nora.sanitizer import Sanitizer

logger = logging.getLogger("nora.core.session_journal")

# Standard message for the disabled gate (R15). Kept as a module-level
# constant so the test suite can pattern-match it without coupling to the
# Settings field name.
_DISABLED_MSG: str = "session journal disabled via NORA_SESSION_JOURNAL_ENABLED"

# ---------------------------------------------------------------------------
# Typed exception hierarchy (R7, R11, R15).
# ---------------------------------------------------------------------------


class SessionJournalError(Exception):
    """Base for every typed error raised by the SessionJournal package."""


class SessionNotFoundError(SessionJournalError):
    """Raised when `nora_session_resume(session_id)` finds no canonical file (R7-S6)."""


class JournalCorruptError(SessionJournalError):
    """Raised when a canonical file contains bytes that are not valid JSON (R11-S1).

    Carries `.path` so the recovery site knows which file to archive.
    """

    def __init__(self, path: Path) -> None:
        super().__init__(f"Session journal file is corrupt: {path}")
        self.path = path


class JournalDisabledError(SessionJournalError):
    """Raised when the explicit tools are invoked while journal is disabled (R15-S2)."""


# ---------------------------------------------------------------------------
# SessionJournal — core public API.
#
# Concurrency model: lock-free, rely on `os.replace` atomicity. The GIL
# serialises in-memory mutations; the only race is at the file boundary
# which `os.replace` resolves. R4 acceptance: deterministic last-write-
# wins; we accept it explicitly.
# ---------------------------------------------------------------------------


class SessionJournal:
    """Per-session JSON trace with auto-trace + 3 explicit recall tools."""

    def __init__(
        self,
        *,
        journal_dir: Path,
        max_trace_steps: int,
        sanitizer: Sanitizer,
        enabled: bool = True,
        operator_alias: str = "anonymous",
    ) -> None:
        self._journal_dir = ensure_journal_dir(journal_dir)
        self._max_trace_steps = max_trace_steps
        self._sanitizer = sanitizer
        self._enabled = enabled
        self._operator_alias = operator_alias
        # Eager UUIDv4 generation so callers can read `journal.session_id`
        # before the first write (R9-S1 covers this contract).
        self._session_id: str = str(uuid.uuid4())
        # Active state — loaded lazily on first read.
        self._state: SessionState | None = None

    # --- accessors ---------------------------------------------------------

    @property
    def session_id(self) -> str:
        """UUIDv4 session id, generated at construction (R9)."""
        return self._session_id

    @property
    def journal_dir(self) -> Path:
        return self._journal_dir

    @property
    def enabled(self) -> bool:
        return self._enabled

    # --- constructors ------------------------------------------------------

    @classmethod
    def for_settings(cls, settings: Settings, sanitizer: Sanitizer) -> "SessionJournal":
        """Construct a `SessionJournal` from a `Settings` instance."""
        return cls(
            journal_dir=settings.nora_session_journal_dir,
            max_trace_steps=settings.nora_session_trace_max_steps,
            sanitizer=sanitizer,
            enabled=settings.nora_session_journal_enabled,
            operator_alias=settings.nora_operator_alias,
        )

    # --- primary API -------------------------------------------------------

    def record_step(
        self,
        *,
        tool: str,
        input_args: dict[str, Any],
        result_summary: str,
        duration_ms: int,
        outcome: Outcome,
        ts: datetime,
        llm_interpretation: str | None = None,
    ) -> SessionStep:
        """Build, append, and persist one `SessionStep`.

        R10 redaction (commit #5) and sanitize (commit #5) are wired as
        no-ops in this commit; the contract they eventually implement is
        captured by tests in `tests/core/test_session_journal.py` and
        `tests/core/test_session_redaction.py`.
        """
        if not self._enabled:
            # R15-S1: when disabled, auto-trace MUST skip recording
            # entirely. Return None; the middleware never uses the
            # return value, so this is safe.
            logger.debug(
                "session journal disabled; auto-trace skipped for tool=%s",
                tool,
            )
            return None  # type: ignore[return-value]

        state = self._load_or_create_or_recover()
        # R10: redact by key name first, so the [REDACTED] marker never
        # reaches the sanitizer (which would otherwise mangle `REDACTED`
        # because it matches the SERIAL regex).
        redacted_input = redact(input_args)
        # R6: sanitize the redacted tree; skip sanitization at keys on
        # REDACTION_LIST so the marker survives. Free-text values inside
        # un-redacted keys still pass through Sanitizer.sanitize.
        sanitized_input = _sanitize_tree(redacted_input, self._sanitizer)
        # R6: free-text scalars pass directly through sanitize.
        sanitized_summary = self._sanitizer.sanitize(result_summary).text
        sanitized_llm = (
            self._sanitizer.sanitize(llm_interpretation).text
            if llm_interpretation is not None
            else None
        )

        next_step = _next_step(state)
        step = SessionStep(
            ts=ts,
            step=next_step,
            tool=tool,
            input=sanitized_input,
            result_summary=sanitized_summary,
            duration_ms=duration_ms,
            outcome=outcome,
            llm_interpretation=sanitized_llm,
        )
        state.trace.append(step)
        # R5: rotate if the in-memory trace exceeded the threshold. The
        # rotate helper is a no-op below the threshold, so the common
        # case costs one length check.
        state = rotate_if_needed(state, self._ndjson_path(), self._max_trace_steps)
        state.last_updated = datetime.now(timezone.utc)
        self._state = state
        self._persist(state)
        logger.info(
            "session journal recorded: tool=%s step=%d outcome=%s duration_ms=%d",
            tool,
            next_step,
            outcome,
            duration_ms,
        )
        return step

    def get_state(self, *, include_rotated: bool = False) -> SessionState:
        """Return the current `SessionState`.

        R7-S2 — when no canonical file exists, this call CREATES one with
        the empty default state and persists it. When `include_rotated` is
        True, NDJSON steps are merged into `state.trace` in `step` order
        (R16). Default False for backward compatibility.

        Raises `JournalDisabledError` when the journal is disabled (R15-S2).
        """
        if not self._enabled:
            raise JournalDisabledError(_DISABLED_MSG)
        was_loaded = self._state is not None
        if was_loaded and self._state is not None:
            state: SessionState = self._state
        else:
            state = self._load_or_create()
        # R6: re-sanitize on read (defense in depth). Wired as no-op in
        # this commit; added in #5.
        self._state = state
        if not was_loaded:
            # First read after construction: persist the empty canonical
            # file (R7-S2 — get_state MUST create one if missing).
            self._persist(state)
        if include_rotated:
            state = self._rehydrate_rotated(state)
        return state

    def set_focus(self, device_id: str) -> SessionState:
        """Record `device_id` as the active focus and append to `devices_reviewed`.

        R7-S3 / R7-S4 — idempotent: a second `set_focus` with the same
        device id MUST NOT duplicate `devices_reviewed`.

        Raises `JournalDisabledError` when the journal is disabled (R15-S2).
        """
        if not self._enabled:
            raise JournalDisabledError(_DISABLED_MSG)
        state = self._state or self._load_or_create()
        state.focus_device_id = device_id
        if device_id not in state.devices_reviewed:
            state.devices_reviewed.append(device_id)
        state.last_updated = datetime.now(timezone.utc)
        self._state = state
        self._persist(state)
        return state

    def resume(self, session_id: str) -> SessionState:
        """Switch the active session to `session_id`.

        R7-S5 — loads the named canonical file as the active session.
        R7-S6 — raises `SessionNotFoundError` if the file is missing.
        R9-S2 — does NOT mutate the previously-active session on disk.

        Raises `JournalDisabledError` when the journal is disabled (R15-S2).
        """
        if not self._enabled:
            raise JournalDisabledError(_DISABLED_MSG)
        target = canonical_path(self._journal_dir, session_id)
        if not target.exists():
            raise SessionNotFoundError(f"Session not found: no canonical file at {target}")
        with target.open() as f:
            data = json.load(f)
        state = SessionState.model_validate(data)
        self._session_id = session_id
        self._state = state
        return state

    def _rehydrate_rotated(self, state: SessionState) -> SessionState:
        """Merge NDJSON tail into `state.trace` in `step` order (R16-S1).

        Reads `<session_id>.log.ndjson` line-by-line, parses each line as
        a `SessionStep`, and returns a copy with the merged trace.
        """
        path = self._ndjson_path()
        rotated: list[SessionStep] = []
        if path.exists():
            for line in path.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                rotated.append(SessionStep.model_validate(json.loads(line)))
        merged = state.model_copy(deep=True)
        merged.trace = sorted(rotated + state.trace, key=lambda s: s.step)
        return merged

    def summarize(self) -> str:
        """Return a non-empty Markdown digest of the current session (R17).

        Covers:
            * `focus_device_id`
            * `devices_reviewed`
            * the last 10 step summaries (truncated to 120 chars each)

        Pure projection — does NOT mutate the canonical file.

        Raises `JournalDisabledError` when the journal is disabled (R15-S2).
        """
        if not self._enabled:
            raise JournalDisabledError(_DISABLED_MSG)
        state = self._state or self._load_or_create()

        lines: list[str] = []
        lines.append(f"# Session {state.session_id}")
        lines.append("")
        lines.append(f"**Operator**: `{state.operator_alias}`")
        lines.append("")
        focus = state.focus_device_id if state.focus_device_id is not None else "(none)"
        lines.append(f"**Focus**: {focus}")
        lines.append("")
        if state.devices_reviewed:
            devices = ", ".join(state.devices_reviewed)
            lines.append(f"**Devices reviewed**: {devices}")
        else:
            lines.append("**Devices reviewed**: (none)")
        lines.append("")

        recent = state.trace[-10:] if state.trace else []
        if recent:
            lines.append(f"## Recent steps (last {len(recent)})")
            lines.append("")
            lines.append("| step | tool | outcome | duration_ms | result_summary |")
            lines.append("|------|------|---------|-------------|----------------|")
            for step in recent:
                summary = step.result_summary[:120].replace("|", "\\|")
                lines.append(
                    f"| {step.step} | `{step.tool}` | {step.outcome} | "
                    f"{step.duration_ms} | {summary} |"
                )
        else:
            lines.append("## Recent steps")
            lines.append("")
            lines.append("_(no steps recorded yet)_")

        return "\n".join(lines)

    # --- internals ---------------------------------------------------------

    def _path(self) -> Path:
        return canonical_path(self._journal_dir, self._session_id)

    def _ndjson_path(self) -> Path:
        return ndjson_path(self._journal_dir, self._session_id)

    def _load_or_create(self) -> SessionState:
        """Load existing canonical file, or create a fresh empty state.

        Raises `JournalCorruptError` (R11-S1) if the file exists but its
        bytes are not valid JSON. The auto-recovery call site is in
        `record_step` (and the explicit tools) — they catch the
        corruption, archive the bad file, and start fresh.
        """
        path = self._path()
        if not path.exists():
            now = datetime.now(timezone.utc)
            return SessionState(
                session_id=self._session_id,
                operator_alias=self._operator_alias,
                started_at=now,
                last_updated=now,
            )
        try:
            with path.open() as f:
                data = json.load(f)
        except json.JSONDecodeError as exc:
            raise JournalCorruptError(path) from exc
        return SessionState.model_validate(data)

    def _recover_from_corrupt(self, path: Path) -> None:
        """Archive the corrupt file by renaming it with a `.corrupt-<ts>.json` suffix (R11-S2).

        Logged at WARNING so an operator can spot the recovery. The next
        `_load_or_create` will then create a fresh canonical file.
        """
        ts = int(time.time())
        archived = path.with_suffix(f".corrupt-{ts}.json")
        path.rename(archived)
        logger.warning(
            "session journal recovered from corrupt file: archived=%s",
            archived,
        )

    def _load_or_create_or_recover(self) -> SessionState:
        """Wrap `_load_or_create` with R11-S2 auto-recovery.

        On `JournalCorruptError`: archive the corrupt file, then return a
        fresh empty `SessionState`. Reads that surface corruption (e.g.
        `get_state`) still RAISE — recovery is a write-time concern.
        """
        try:
            return self._load_or_create()
        except JournalCorruptError as exc:
            self._recover_from_corrupt(exc.path)
            now = datetime.now(timezone.utc)
            return SessionState(
                session_id=self._session_id,
                operator_alias=self._operator_alias,
                started_at=now,
                last_updated=now,
            )

    def _persist(self, state: SessionState) -> None:
        """Serialize `state` to JSON and atomically write to canonical path.

        Pydantic's `model_dump(mode="json")` returns datetimes as ISO
        strings, which `json.loads` re-parses to `dict[str, Any]`. The
        re-serialise step keeps payload shape consistent with the spec
        (string-keyed dict, no Pydantic-specific markers).
        """
        payload: dict[str, Any] = json.loads(json.dumps(state.model_dump(mode="json")))
        atomic_write_json(self._path(), payload)


def _next_step(state: SessionState) -> int:
    """Return the next monotonic step number for `state.trace`."""
    if not state.trace:
        return 1
    return state.trace[-1].step + 1


def _sanitize_tree(value: Any, sanitizer: Sanitizer) -> Any:
    """Apply `sanitizer` to every free-text string in `value`.

    R6 ordering: redaction has already happened, so any string at a key
    on `REDACTION_LIST` is the literal `_REDACTION_MARKER` — we MUST NOT
    pass it through the sanitizer (else `REDACTED` would be re-aliased
    to `SERIAL_X`). Sibling free-text strings still pass through.

    Pure: returns a new structure; never mutates the input.
    """
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and key in REDACTION_LIST:
                out[key] = item  # leave the marker untouched
            else:
                out[key] = _sanitize_tree(item, sanitizer)
        return out
    if isinstance(value, list):
        return [_sanitize_tree(v, sanitizer) for v in value]
    if isinstance(value, str):
        return sanitizer.sanitize(value).text
    return value


# ---------------------------------------------------------------------------
# Module-level singleton. The middleware and the explicit tools both read
# `_journal` via `get_journal()`. `__main__.py` is the single boot site
# that calls `init_session_journal(settings)`.
# ---------------------------------------------------------------------------


_journal: SessionJournal | None = None


def init_session_journal(settings: Settings) -> SessionJournal:
    """Construct (or replace) the module-level singleton.

    Idempotent: a second call replaces the previous instance (useful for
    test fixtures).
    """
    global _journal
    _journal = SessionJournal.for_settings(settings, _module_sanitizer())
    return _journal


def get_journal() -> SessionJournal:
    """Return the module-level singleton.

    Raises `JournalDisabledError` when the env var is `"false"` (R15-S2).
    """
    if _journal is None:
        raise JournalDisabledError(
            "session journal not initialised; call init_session_journal(settings) "
            "or nora.server.set_runtime_state(...)"
        )
    return _journal


def _module_sanitizer() -> Sanitizer:
    """Build the default `Sanitizer` used by the singleton.

    Each SessionJournal could hold its own Sanitizer (per-session alias
    map); for the module singleton we use a fresh one. Tests that need
    an isolated map construct their own SessionJournal directly.
    """
    return Sanitizer()


__all__ = [
    "SessionJournal",
    "SessionJournalError",
    "SessionNotFoundError",
    "JournalCorruptError",
    "JournalDisabledError",
    "init_session_journal",
    "get_journal",
]
