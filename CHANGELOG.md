# Changelog

Notable changes to OLAF — OneLake Access Framework — are recorded here using
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) structure. Runtime
version `1.0.0` maps to release tag `v1.0.0`.

## [Unreleased]

No changes yet.

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
