"""NetOps Persistent Memory & Correlation MCP — read-only intervention memory package.

Hard read-only rule (R2): this package MUST NOT mutate any file on disk.
The hard rule is enforced structurally by an AST scan under
`tests/intervention_memory/test_no_writes.py`. Any future contributor who
adds a write call (`open(... "w" / "a" / "x" / "+")`, `Path.write_text`,
`Path.write_bytes`, `Path.unlink`, `os.replace`, `os.remove`,
`os.removedirs`, `os.makedirs`, `shutil.rmtree`) breaks the build.

The MCP wrappers (in `src/nora/server.py`) and the webui.db mirror shim
(`shim_webui.py`) both delegate 1:1 to the source-of-truth library
functions in `tools.py` so the two surfaces cannot drift.

Writer sibling: the write surface for `nora_interventions_dir` lives in
the sibling capability `intervention-writer` (see
`openspec/specs/intervention-writer/spec.md`). The reader package
remains read-only and MUST NOT import from the writer. One-way
dependency direction is preserved: writer → reader (for schema only),
never reader → writer. The 5th `@mcp.tool` in `src/nora/server.py`
(`save_intervention_record`) registers that writer.
"""
