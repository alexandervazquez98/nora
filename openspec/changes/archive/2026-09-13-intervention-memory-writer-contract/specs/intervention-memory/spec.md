# Delta for intervention-memory

## MODIFIED Requirements

### Requirement: R2 — Storage Layer Is Strictly Read-Only

No source file under `src/nora/intervention_memory/` SHALL invoke `open(...)`
with a writable mode (`"w"`, `"a"`, `"x"`, `"+"`), `Path.write_text`,
`Path.write_bytes`, `Path.unlink`, `os.replace`, `os.remove`,
`os.removedirs`, `os.makedirs`, or `shutil.rmtree`. The constraint SHALL be
enforced by an AST scan in `tests/intervention_memory/test_no_writes.py` that
walks every `.py` file under the package and fails the build on any match.
The package SHALL NOT create `Settings.nora_interventions_dir` if absent.
The write surface for `nora_interventions_dir` lives in the sibling
capability `intervention-writer` (see
`openspec/specs/intervention-writer/spec.md`); the reader package remains
read-only and MUST NOT import from the writer. One-way dependency direction
is preserved: writer → reader (for schema only), never reader → writer.
(Previously: R2 had no acknowledgement of the sibling writer capability;
this addition is a cross-reference, not a relaxation of the read-only
guarantee.)

#### Scenario: AST scan finds zero writable file calls under the package

- GIVEN every `.py` under `src/nora/intervention_memory/`
- WHEN `tests/intervention_memory/test_no_writes.py` runs its AST walker
- THEN the assertion passes
- AND the build exits 0

#### Scenario: an injected `Path.write_text` call breaks the build

- GIVEN a developer adds `Path("/tmp/x").write_text("x")` to `storage.py`
- WHEN `tests/intervention_memory/test_no_writes.py` runs
- THEN the test fails with the offending `(file, line, "write_text")` triple

#### Scenario: `open(..., "r")` reads are allowed

- GIVEN `storage.py` opens a file with `open(path, "r", encoding="utf-8")`
- WHEN the AST scan runs
- THEN the call is NOT flagged as a write

#### Scenario: the reader does not import from the writer sibling

- GIVEN every `.py` under `src/nora/intervention_memory/` except its `__init__.py`
- WHEN a static grep scans for `from nora.intervention_writer`, `import nora.intervention_writer`, `from nora.server`, or `import nora.server`
- THEN zero matches are found (one-way dep writer → reader, never the reverse)
