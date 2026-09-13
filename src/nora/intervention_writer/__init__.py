"""Atomic-write companion to `nora.intervention_memory`.

This package OWNS the writer surface for `nora_interventions_dir`. The
sibling `nora.intervention_memory` package is HARD read-only (R2 — its
AST guard in `tests/intervention_memory/test_no_writes.py` enforces
zero write calls); the writer lives here so adding a write to this
package cannot regress the reader's read-only guarantee.

One-way dependency direction (locked by `intervention-writer` W7):

  writer  ──imports──▶  reader (only `nora.intervention_memory.models`
                         for `InterventionMemoryRecord`, and
                         `nora.intervention_memory.sanitize` for
                         `_sanitize_value` / `Sanitizer`)
  reader  ──MUST NOT──▶  writer

The reader's R2 guard (AST scan + banned imports) remains untouched by
this slice. A new AST scan in `tests/intervention_writer/test_banned_imports.py`
asserts the writer does NOT import pytest / nora.server / nora.drivers /
the reader's webui shim.

Public surface:

- `save_intervention_record(settings, payload) -> dict[str, Any]` — the
  library function exposed for issue #15 reuse.
- `InvalidFilenameComponent` — typed error from `filenames.build_filename`.

The 5th `@mcp.tool` wrapper lives in `src/nora/server.py` (mirrors the
existing four read-only tool wrappers; keeps `_ToolLogMiddleware` and
`_SERVER_INSTRUCTIONS` in one place).
"""

from nora.intervention_writer.filenames import InvalidFilenameComponent, build_filename
from nora.intervention_writer.writer import save_intervention_record

__all__ = [
    "save_intervention_record",
    "build_filename",
    "InvalidFilenameComponent",
]
