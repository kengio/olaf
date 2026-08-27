# Optional live Fabric validation protocol

The public release has fixture-based CI evidence only. It does **not** claim
that the release commit was run against a live Microsoft Fabric tenant.

This page defines the minimum record for a future, separately authorized live
validation. It is not permission to access or mutate a tenant, and an unrecorded run
must not be converted into a public product or platform claim.

Microsoft's bulk DAR mutation endpoint is officially Preview and not recommended for
production use:
[Create or update data access roles](https://learn.microsoft.com/en-us/rest/api/fabric/core/onelake-data-access-security/create-or-update-data-access-roles).

## Authorization gate

Before any live action, record outside the repository:

- exact OLAF release commit and notebook checksum;
- Fabric Runtime and Spark versions;
- API test date;
- authorized tenant/workspace/item **class** (never copy its real identifier into
  repository content);
- operator identity class and required permissions;
- exact roles, paths, and access effect that may change;
- start/end time and maximum duration;
- pre-test DAR snapshot and recovery pointer;
- cleanup owner and success criteria;
- separate per-run `control_data_isolation_attestation` evidence reference.

No authorization means no live run. The public repository must never contain the
real tenant, workspace, item, principal, group, user, role, or customer values from
the test.

## Preflight

1. Use a disposable non-production target containing synthetic data only.
2. Run read-only health before setup. A complete ETag-bearing DAR snapshot and the
   operator attestation are separate facts; neither proves workspace isolation.
3. Externally review workspace/item sharing, dynamic default-reader/`ReadAll`
   membership, Admin/Member/Contributor and equivalent elevated identities,
   shortcuts, and automation that can reopen access. Microsoft's access model is the
   source for those paths:
   [OneLake security and workspace permissions](https://learn.microsoft.com/en-us/fabric/onelake/security/data-access-control-model#onelake-security-and-workspace-permissions).
4. Do not upload a real workbook. Use synthetic principals and the repository's
   reserved-example values only after the access review.
5. Confirm a workspace Admin or Member caller with `OneLake.ReadWrite.All` for DAR
   edits. Contributor is not sufficient to edit DAR definitions. See the official
   [workspace model](https://learn.microsoft.com/en-us/fabric/onelake/security/data-access-control-model#onelake-security-and-workspace-permissions)
   and [endpoint authorization](https://learn.microsoft.com/en-us/rest/api/fabric/core/onelake-data-access-security/create-or-update-data-access-roles).

Follow the complete [control-data security sequence](control-data-security.md).

## Read-only checks first

Before any mutation, record sanitized pass/fail results for:

- target identity resolution;
- Spark 3.5+ prerequisite;
- bounded DAR list completeness and collection ETag;
- reserved-path overlap classification;
- `workspace_isolation=attested|unknown` separately from DAR safety;
- current config/mapping provenance;
- engine-explicit access calculations for `spark`, `direct_lake`, and
  `sql_endpoint`.

SQL endpoint enforcement depends on user-identity mode and CLS combination differs
from non-SQL paths. A calculated policy is not evidence of enforcement:

- [Engine and user access](https://learn.microsoft.com/en-us/fabric/onelake/security/data-access-control-model#engine-and-user-access-to-data)
- [SQL endpoint access modes](https://learn.microsoft.com/en-us/fabric/onelake/security/troubleshoot-onelake-security-for-sql-analytics-endpoints#access-modes-and-enforcement)
- [Column-level security semantics](https://learn.microsoft.com/en-us/fabric/onelake/security/data-access-control-model#column-level-security)

## Mutation checks

Mutation requires the explicit authorization gate above. Stop on any missing/changed/unknown
snapshot, existing sentinel, write ambiguity, or partial recovery state. Supplying a per-run
isolation attestation is optional -- it is recorded as `workspace_isolation=attested|unknown`
and gates nothing, so do not treat its absence as a stop condition.

Test only the observable OLAF contract:

1. setup/import blocks before sensitive writes, and a run WITHOUT an attestation still
   proceeds while recording `workspace_isolation=unknown` -- assert the recorded value, never
   a refusal;
2. sentinel create/read uncertainty blocks the operation;
3. generate and plan record the intended hashes and provenance;
4. a missing or changed DAR ETag blocks rather than silently refreshing;
5. a prepared record and backup pointer exist before a real DAR request;
6. a first-attempt `412` is handled as a rejected conditional request;
7. an ambiguous real request reports `changed=null` rather than `false`;
8. a confirmed write followed by audit failure reports `changed=true` with recovery
   pointers;
9. rollback preflight blocks before `RESTORE` on missing artifacts or observable
   config/DAR races;
10. post-write boundary uncertainty leaves the incident sentinel and reports
    `possible_exposure`.

Microsoft documents the collection ETag, optional `If-Match`, and `412` surface but
does not promise OLAF's forensic conclusions:

- [List data access roles](https://learn.microsoft.com/en-us/rest/api/fabric/core/onelake-data-access-security/list-data-access-roles)
- [Create or update data access roles](https://learn.microsoft.com/en-us/rest/api/fabric/core/onelake-data-access-security/create-or-update-data-access-roles)

Do not test or claim atomic replacement, deletion by omission, exact restore,
DefaultReader re-creation behavior, or role-ID stability unless Microsoft adds that
property to the public contract. A dated observation is not a platform guarantee.

## Enforcement observations

If enforcement is tested, use only synthetic data and separately authorized test
identities. Record the engine and access mode for every query. Do not say “enforced
everywhere” or that all elevated identities behave identically across every access
path. Microsoft documents engine, shortcut, SQL identity-mode, and workspace-role
exceptions:

- [Engine and user access](https://learn.microsoft.com/en-us/fabric/onelake/security/data-access-control-model#engine-and-user-access-to-data)
- [SQL endpoint security](https://learn.microsoft.com/en-us/fabric/onelake/security/sql-analytics-endpoint-onelake-security)

For RLS column spelling, the canonical guidance says invalid or mismatched predicates
can return no rows or query errors. Do not publish a fail-open claim from an isolated
observation:
[RLS syntax](https://learn.microsoft.com/en-us/fabric/onelake/security/row-level-security-syntax).

For RLS/CLS combinations, record only the tested engine/table/role shape and retain
OLAF's conservative validation rule unless the public contract changes:

- [Combine row and column security](https://learn.microsoft.com/en-us/fabric/onelake/security/table-column-row-security#combine-row-level-and-column-level-security)
- [Evaluate multiple roles](https://learn.microsoft.com/en-us/fabric/onelake/security/data-access-control-model#evaluating-multiple-onelake-security-roles)

## Cleanup and sign-off

Cleanup is containment, not erasure. It cannot retract prior reads or guarantee
removal from Delta history, caches, copies, exports, or external logs. Preserve an
incident sentinel until a fresh safe snapshot and a documented access review support explicit
clearance. An attestation is optional throughout and is never part of that bar.

A retained live report must record, without prohibited identifiers:

- exact release SHA and runtime/API context;
- authorized scope and access effect;
- each check and observed result;
- changed/unknown phases and recovery pointers;
- restore and cleanup result;
- residual limitations;
- reviewer and date represented by non-personal role labels.

Only then may documentation say “observed on `<date>` for `<release SHA>` using
`<runtime/API context>`.” It must still link to the official contract and must not
generalize to other engines, tenants, or future service versions.

## Notebook scaffold

[`../tests/olaf_test_smoke.ipynb`](../tests/olaf_test_smoke.ipynb) is a sanitized
operator scaffold. It ships with no output, execution counts, target values, or live
result. Review it against the current runtime API before every authorized use. The
notebook is never executed by public CI and is not evidence that any check passed.
