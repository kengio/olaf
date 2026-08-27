# Changelog

Notable changes to OLAF — OneLake Access Framework — are recorded here using
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) structure. The runtime's
`__version__` maps to the release tag `v{__version__}`.

## [Unreleased]

No changes yet.

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
  directly-callable helper, so this is a public-API removal.

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
