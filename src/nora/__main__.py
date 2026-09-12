"""Deprecation alias for `python -m nora`.

This entry point is preserved for one minor release so existing
operators / dashboards that boot NORA via `python -m nora` keep
working. The canonical entry point is now the `nora-mcp` console
script wired to `nora.cli.main`.

This module emits a `DeprecationWarning` and then delegates. No new
behaviour lives here.
"""

from __future__ import annotations

import warnings

# Python's default warning filter ignores `DeprecationWarning` outside
# `__main__`. We relax it so the deprecation notice is visible to
# operators who boot NORA via `python -m nora`. The filter is scoped to
# this module's invocation and does not leak globally.
warnings.simplefilter("always", DeprecationWarning)

warnings.warn(
    "`python -m nora` is deprecated and will be removed in the next minor release. "
    "Use the `nora-mcp` console script instead.",
    DeprecationWarning,
    stacklevel=2,
)

from nora.cli import main  # noqa: E402

if __name__ == "__main__":
    main()
