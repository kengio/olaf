# Roadmap

OLAF is a community Preview. Roadmap items are options, not promises or
release dates. They must not weaken the control-data boundary or turn an observed
service behavior into a Microsoft platform contract.

## Differential apply — per-role writes instead of one bulk `PUT`

**Status: blocked on a stable, concurrency-safe official contract.**

Microsoft currently documents the single-role create/update and delete endpoints as
Preview. OLAF will not switch merely because a request succeeds in one environment.
The design needs documented precondition behavior, a recovery model for a partially
completed role set, and tests that never report an ambiguous write as unchanged:

- [Create or update one data access role](https://learn.microsoft.com/en-us/rest/api/fabric/core/onelake-data-access-security/create-or-update-single-data-access-role)
- [Delete one data access role](https://learn.microsoft.com/en-us/rest/api/fabric/core/onelake-data-access-security/delete-data-access-role)

The current bulk endpoint is also Preview and does not publicly guarantee atomic
replacement or deletion by omission:
[Create or update data access roles](https://learn.microsoft.com/en-us/rest/api/fabric/core/onelake-data-access-security/create-or-update-data-access-roles).

## Catching the RLS + CLS cross-grant collision at `generate`

**Status: retain the conservative rule; improve membership analysis only with
evidence.**

Microsoft documents unsupported RLS/CLS combinations and different multi-role
evaluation semantics. OLAF must not infer behavior across engines, tables, direct
membership, and group-mediated membership beyond those sources:

- [Combine table, column, and row security](https://learn.microsoft.com/en-us/fabric/onelake/security/table-column-row-security#combine-row-level-and-column-level-security)
- [Evaluate multiple OneLake security roles](https://learn.microsoft.com/en-us/fabric/onelake/security/data-access-control-model#evaluating-multiple-onelake-security-roles)

A future change may improve how the imported member table reveals collisions, but
OLAF deliberately does not call Microsoft Graph. That is a project design choice,
not a claim that Graph tokens are universally unavailable:
[NotebookUtils token audiences](https://learn.microsoft.com/en-us/fabric/data-engineering/notebookutils/notebookutils-credentials#get-token).

## Local authoring tool — validate and build config against a real workspace

**Status: planned; blocked on where real identifiers are allowed to live.**

Today a config is authored blind. The workbook is filled in against what the author
believes the lakehouse contains, and the first time anything checks it against reality
is `generate` inside Fabric. A tool that runs on a developer machine, authenticates
with Fabric credentials, and reads the live workspace could resolve table and column
names, check member principals resolve, and surface rule violations while the config
is still being written — a `plan` for the config itself, before a notebook is involved.

What has to be settled first is not the API surface. It is that such a tool moves real
tenant data onto a laptop:

- **Credentials.** It would hold or broker a token with `OneLake.ReadWrite.All` scope
  outside the Fabric boundary. The token, its cache, and its lifetime all become the
  tool's responsibility, and a stolen laptop becomes a DAR-write capability. Read-only
  scope for authoring is the obvious first constraint; whether that is sufficient to
  build a config is not yet established.
- **Real identifiers on disk.** Resolving members means reading real principal object
  ids and display names. v1's whole control-data posture says do not put those where
  they are not governed — see [Protecting OLAF control data](control-data-security.md).
  A local cache, a shell history, an editor's undo file and a crash dump are all places
  they would land by default.
- **What it must never become.** An authoring tool reads and validates. It must not
  grow a write path to the DAR collection, because that would move the reviewed
  plan → approve → apply sequence off the audited runtime and onto a machine with no
  audit trail. Validation only, with the deployment path unchanged.

Until those are answered the honest position is that authoring stays in the workbook and
validation stays in `generate` and `validate`, which already run every rule with zero
writes.

## Scheduled drift detection

**Status: candidate automation after the read-only contract is stable.**

Any scheduled check must remain read-only, name the engine/access-path limitation,
distinguish desired/live identity drift from policy drift, and report point-in-time
evidence rather than continuous enforcement. It must not store real principal values
in CI logs or public artifacts. See [Protecting OLAF control data](control-data-security.md)
and [Evidence status](platform-contract.md#evidence-status).

## Separate control store

**Status: design option for a stronger threat model.**

The same-lakehouse v1 design cannot provide cryptographic isolation or one transaction
across workspace sharing, Delta/file writes, audit rows, and Fabric REST. A separately
secured control lakehouse/store or encryption with separately managed keys would
reduce trust in administrators but is not a minimal v1 change. Organizations that
cannot accept the v1 trusted-administrator boundary should not import real principal
data or run sensitive modes. See [Same-lakehouse limitation](control-data-security.md#same-lakehouse-limitation).

## Not planned

- Replacing Microsoft Fabric enforcement with an OLAF query/runtime layer.
- Managing workspace or item RBAC as though it were visible in the DAR collection.
- Claiming production readiness while the required mutation endpoint is Preview.
- Calling Microsoft Graph for automatic directory resolution in v1.
- Hiding the per-run workspace-isolation attestation behind a default or bypass.
- Automatic counter-restore after an ambiguous or concurrent write.
- Describing cleanup as proof that sensitive data was never read or copied.

## Recently shipped

The first public v1.0.0 candidate adds the plan/review/apply workflow, fixture-based
tests, durable write-state evidence, engine-explicit access calculations, and the
fail-closed control-data gate. See [CHANGELOG.md](../CHANGELOG.md).
