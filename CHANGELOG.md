# Changelog

Notable changes to OLAF — OneLake Access Framework — are recorded here using
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) structure. Runtime
version `1.1.0` maps to release tag `v1.1.0`.

## [Unreleased]

No changes yet.

## [1.1.0] - 2026-08-27

### Changed

- Guardrail **G3** is no longer a runtime warning. `NOT IN` without `OR <col> IS NULL` used to add
  one warning per row, on every run, for the life of the config — and it named only one of the two
  valid mitigations, so a config that deliberately asserts the column is never NULL could never be
  clean. Permanent warnings hide the occasional real one. The trap itself is real and unchanged;
  it is now documented in `docs/architecture.md` as guidance rather than emitted per row.
- `RLS.null_safety_warning()` is removed along with it.

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
