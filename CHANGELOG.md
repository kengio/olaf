# Changelog

Notable changes to OLAF — OneLake Access Framework — are recorded here using
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) structure. Runtime
version `1.0.0` maps to release tag `v1.0.0`.

## [Unreleased]

### Added

- The five diagram pairs the release build used to hold back — apply semantics, destructive
  utilities, mode lifecycle, pipeline, pipeline branching — and `scripts/gen-olaf-diagrams.py`,
  which generates them. They were withheld because no published document referenced them, never
  because a check objected: the release scanner reported no finding against any of them.
- `README.md` regained **What is OneLake security?**, **Why OLAF?** and a **Roadmap** checklist,
  along with the Arctic Owl mascot. A second badge row states what CI proves and what OLAF runs on.
- A roadmap entry for a local authoring tool that would validate and build a config against a real
  workspace from a developer machine, with the conditions that block it.
- The example notebooks are complete again: `olaf_cookbook` carries 33 code cells and
  `olaf_master_workflow` the full stage-by-stage pipeline, instead of 3 each.

### Changed

- **This repository is now the only one.** Development happened in a private repository whose
  history was withheld from an orphan-root release commit. Everything worth keeping is here, so the
  subtraction machinery — an exclude list, a mirror-back guard against silently reverting anything
  merged here first — has no remaining purpose and does not travel with it.
- The masthead art is renamed `olaf-banner-light.png` / `olaf-banner-dark.png`. It was
  `olaf-lockup-*`, and overwriting those files left every cache serving the previous image from an
  unchanged URL. A new name is the only thing that reliably invalidates it.
- The social preview carries the wordmark, the project name and the plan → review → apply sequence,
  where it was previously the logo on a plain ground.

### Fixed

- `olaf_master_workflow` read `deleted` from the apply envelope, a key the runtime renamed to
  `omitted_role_candidates`. It printed `deleted 0` on every run regardless of what the payload
  actually left out.
- The same notebook sorted per-role verdicts on `{"delete": 0, ...}`. The runtime emits `omit` and
  never `delete`, so omitted roles fell to the default rank and sorted last under a heading that
  promised them first.
- `README.md` no longer pins a version number in prose; the release badge reads the tag.

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
  snapshots, per-run workspace isolation attestation, and a PII-free incident
  sentinel for every sensitive write.
- Engine-explicit effective-access reporting for Spark, Direct Lake, and SQL
  analytics endpoint CLS semantics.
- Fixture-based pytest coverage, notebook structure checks, formatting, internal
  link checks, and repository privacy/secret hygiene gates.
- Public governance files, a private vulnerability-reporting route (when enabled),
  incident-response guidance, and privacy-preserving maintainer identity policy.
- Official-source-backed documentation that separates OLAF behavior from Microsoft
  platform guarantees and records the same-lakehouse/non-transactional limitations.
