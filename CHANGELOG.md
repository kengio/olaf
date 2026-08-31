# Changelog

Notable changes to OLAF — OneLake Access Framework — are recorded here using
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) structure. The runtime's
`__version__` maps to the release tag `v{__version__}`.

## [Unreleased]

### Added

- **`olaf_master_workflow` takes a `rebuild` parameter, defaulting to `False`**, and passes it to
  `generate`. The runner previously called `generate` with no arguments, so there was no way to
  ask for a rebuild from the notebook a pipeline actually runs — the framework had the parameter,
  the workflow did not expose it. The default is an access decision rather than a performance one:
  `generate`'s idempotency skip is keyed on `config_hash`, which fingerprints the config rows and
  cannot see the catalog, so a table created since the last generate that matches an existing
  wildcard stays out of the mapping and ungranted until somebody deliberately passes
  `rebuild = True`. Taking it in is all-or-nothing — every wildcard is re-resolved, so every table
  added since the last generate arrives with it.

### Fixed

- `docs/config-examples.md` said a new table matching `sales.*` "shows up in the mapping on the
  next generate with no config change needed". The first half is right and the second half is
  what makes it wrong: an unchanged config takes the idempotency skip, so the *next* generate is
  usually the one that rebuilds nothing. The example now says the table needs a generate that
  actually runs, and names `rebuild=True` as the way to get one. The CLS blacklist example, which
  described a newly discovered column the same way, is corrected alongside it.
- `docs/architecture.md`'s second key invariant read "New tables are absent from the saved mapping
  until the next generate", which is true only of a generate that rebuilds. It now says so, and
  says why `config_hash` cannot see the difference.
- `docs/modes.md` documented every mode except the one behaviour an operator meets most often: its
  `generate` section never mentioned the idempotency skip, even though the result-envelope table
  above it already advertises `status=skipped`. It now carries the skip, what `config_hash` can and
  cannot see, the five exceptions that defeat it, and what `rebuild=True` costs.
- `docs/api/Deployment.md` was headed `generate(rebuild=False)` and never said what the parameter
  did. It does now.
- `docs/runbook.md` had no entry for the operational case an estate using a table glob will meet
  first — a new table matching `sales.*` that is not granted, after a run that reported success.
  It now has 3i, covering why the skip holds, what `rebuild=True` re-resolves and drops, the
  `exclude_tables` hold-back, the stale converse when a table is deleted, and the unrelated
  conditions that force a re-resolution as a side effect. The word `rebuild` previously appeared
  in the runbook **only** in 3e — `setup(rebuild=True)`, which drops a control table and loses its
  data — so an operator searching for it found the destructive one and nothing else. 3e now says
  so and points at 3i.

## [1.1.0] - 2026-08-27

### Changed

- Guardrail **G3** is no longer a runtime warning. The trap it named is real and unchanged —
  `NOT IN` against a NULL is UNKNOWN, `WHERE` keeps only TRUE, and the row disappears from a
  deny-list's result. **The direction is over-rejection: it removes rows a role should see and
  cannot widen access to any row.** What went was the check, because it could not tell the two
  apart. It was a substring test on the raw condition rather than the literal-stripping lexer
  rules C9/C11/C13 share, so it warned on `status = 'CANNOT INVOICE'` — which contains no `NOT IN`
  operator — and stayed silent on `region NOT IN ('a') AND type IS NULL`, where an unrelated
  column's `IS NULL` muted it. Both directions wrong, on every run, for the life of a config.
  It also named only one of the two valid mitigations, so a config that deliberately asserts the
  column is never NULL could never come back clean.
- The `Key invariants` entry in `docs/architecture.md` now carries the trap, both mitigations, and
  the direction of failure. The starter workbook's own note said `Every NOT IN needs OR <col> IS
  NULL`; it now names both mitigations too.

### Removed

- `RLS.null_safety_warning()`, the helper behind G3. It was listed in `docs/api/functions.md` as a
  directly-callable helper, so this is a public-API removal. Shipped as MINOR rather than MAJOR as
  a recorded deviation, not an oversight: nothing is distributed as a package — the runtime is a
  notebook users copy — `SECURITY.md` supports only the latest Preview, and the helper had no
  caller inside or outside the framework. The repo states no Preview exemption from SemVer, so
  this note is the exemption.

### Upgrade note

- **The first `generate` after upgrading re-stamps the mapping, even on an unchanged config.** The
  idempotent-skip fast path requires the stored `framework_version` to equal `__version__`, so on
  the first run it does not match: `changed` is `True` rather than `False`, the status is `success`
  rather than `skipped`, two log rows are written instead of one, and the mapping table is rewritten
  with `framework_version` `1.1.0`. `config_hash` does not move — it hashes config rows only — and
  the run after that skips again. A pipeline gated on `envelope["changed"]` will therefore re-plan
  and re-apply once per deployment on upgrade. Nothing fails; both `success` and `skipped` exit
  normally.
- `mapping_hash` projects to the mapping columns and excludes provenance, so it is unchanged: a
  saved-plan row stamped `1.0.0` still opens the apply gate, and the mapping-history CSV is reused
  verbatim. That CSV's own `framework_version` column therefore still reads `1.0.0` while the
  mapping table reads `1.1.0` — expected, and the reason the plan gate survives an upgrade at all.

## [1.0.0] - 2026-08-26

First public release, positioned as an independent community Preview for
evaluation and development. The required bulk DAR mutation endpoint is officially
Preview and is not presented as a production contract:
[Microsoft REST reference](https://learn.microsoft.com/en-us/rest/api/fabric/core/onelake-data-access-security/create-or-update-data-access-roles).

### Added

- A self-contained Fabric notebook with setup, validate, generate, plan, apply,
  rollback, show, and trace workflows.
- An Excel-authored synthetic starter config and member preload, plus a generated
  mapping and audit/provenance model.
- Saved-plan, drift, target-identity, mapping-provenance, and conditional-write
  guards with durable prepared intent and recovery pointers.
- A fail-closed control-data boundary: reserved paths, bounded ETag-bearing DAR
  snapshots, and a PII-free incident sentinel for every sensitive write. The per-run
  workspace-isolation attestation is **optional** and sits outside that boundary:
  OLAF records it as `attested` or `unknown` and never gates on it.
- Engine-explicit effective-access reporting for Spark, Direct Lake, and SQL
  analytics endpoint CLS semantics.
- Grant provenance reported at both ends — `first_applied`/`first_granted_by` and
  `last_applied`/`last_granted_by` — because the log cannot see a role removed
  outside OLAF, so no single timestamp can claim continuous access.
- Worked examples: `olaf_cookbook` covers every facade call with expected output
  shapes, and `olaf_master_workflow` runs the deployment one stage per cell, each
  stage's result deciding the next.
- Pipeline wrappers (`olaf_runner`) for one activity per mode.
- Architecture, data-model, mode-lifecycle, apply-semantics and destructive-utility
  diagrams, with the generator that produces them.
- Fixture-based pytest coverage, notebook structure checks, formatting, internal
  link checks, and repository privacy/secret hygiene gates.
- Public governance files, a private vulnerability-reporting route (when enabled),
  incident-response guidance, and privacy-preserving maintainer identity policy.
- Official-source-backed documentation that separates OLAF behavior from Microsoft
  platform guarantees and records the same-lakehouse/non-transactional limitations.
- A published roadmap whose every item names the specific condition blocking it,
  including a local authoring tool that would validate a config against a real
  workspace from a developer machine.
