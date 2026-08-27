# Runbook — setup, config, and operations

OLAF v1.1.0 is an independent community Preview for evaluation and development,
not a production-ready security product. Its mutating path depends on Microsoft's
Preview bulk DAR endpoint:
[Create or update data access roles](https://learn.microsoft.com/en-us/rest/api/fabric/core/onelake-data-access-security/create-or-update-data-access-roles).

Read [Protecting OLAF control data](control-data-security.md) and
[the platform contract](platform-contract.md) before using real principal data.

## 1. Prerequisites

- Microsoft Fabric Runtime 1.3 / Spark 3.5 or newer. Verify the selected runtime
  and required imports in the environment; bundled package versions are not a
  permanent contract. See Microsoft's
  [runtime lifecycle](https://learn.microsoft.com/en-us/fabric/data-engineering/lifecycle)
  and [OneLake security limitations](https://learn.microsoft.com/en-us/fabric/onelake/security/data-access-control-model#onelake-security-limitations).
- An attached lakehouse dedicated to the authorized evaluation scope.
- Local lakehouse permissions for the control tables and `Files/security`.
- For DAR edits, a workspace **Admin or Member** identity plus
  `OneLake.ReadWrite.All`. Contributor is not sufficient to edit DAR definitions.
  See [workspace permissions](https://learn.microsoft.com/en-us/fabric/onelake/security/data-access-control-model#onelake-security-and-workspace-permissions)
  and [endpoint authorization](https://learn.microsoft.com/en-us/rest/api/fabric/core/onelake-data-access-security/create-or-update-data-access-roles).
- An external access review covering workspace/item sharing, dynamic
  default-reader/`ReadAll` access, Admin/Member/Contributor and equivalent elevated
  identities, shortcuts, and automation that can change access during the run.
- An auditable, non-secret per-run evidence reference accepted by the runtime as
  `control_data_isolation_attestation`.

OLAF cannot protect a workbook before upload. Do not place real principal data in
the lakehouse until the external review is complete.

## 2. Author config

Start from [`../configs/onelake_security.xlsx`](../configs/onelake_security.xlsx).
Every shipped data row is synthetic and must be replaced. Keep the working copy
outside Fabric until the prerequisite access review is complete, then upload it only
under `Files/security`.

The authored tables are:

- `config`: role name, target lakehouse label, table/folder includes and excludes,
  permission, optional RLS/CLS, and member labels;
- `member`: principal type, label, and Entra object ID used by OLAF's directory-free
  resolution path.

Verify every object ID against Entra before import. A valid GUID can still name the
wrong principal, and OLAF cannot infer operator intent. The detailed schemas are in
[data-model.md](data-model.md); worked synthetic rows are in
[config-examples.md](config-examples.md).

Platform rules change. Use Microsoft's current sources for authoring decisions:

- [Permissions and supported items](https://learn.microsoft.com/en-us/fabric/onelake/security/data-access-control-model#permissions-and-supported-items)
- [RLS syntax](https://learn.microsoft.com/en-us/fabric/onelake/security/row-level-security-syntax)
- [Table, column, and row security](https://learn.microsoft.com/en-us/fabric/onelake/security/table-column-row-security)
- [OneLake security limitations](https://learn.microsoft.com/en-us/fabric/onelake/security/data-access-control-model#onelake-security-limitations)

OLAF's parser is intentionally conservative; its accepted operator subset is an OLAF
guard, not an exhaustive statement of every expression the service may accept.

## 2b. Member resolution table (No-Graph)

`onelake_security_member` is OLAF's explicit principal-label-to-object-ID preload.
OLAF deliberately does not call Microsoft Graph. This is a project design choice,
not a platform claim that a Graph token is universally unobtainable. NotebookUtils
documents supported token audiences and constraints here:
[Get a token](https://learn.microsoft.com/en-us/fabric/data-engineering/notebookutils/notebookutils-credentials#get-token).

Operational rules:

- include every principal label referenced by config;
- use synthetic labels in examples and reports;
- reject missing, malformed, duplicate, or case-colliding entries;
- verify the object ID and principal type against Entra before loading;
- treat member patterns as grant-affecting input because a newly matching row may
  change the generated access set.

## 3. Run

### Bootstrap

1. Import the runtime using [fabric-import.md](fabric-import.md).
2. Attach the intended lakehouse.
3. Configure the target label and run the read-only health diagnostic before setup:

   ```python
   OLAF.configure(lakehouse_name="SampleLakehouse")
   OLAF.health()
   ```

4. Remediate every DAR overlap/unknown and complete the separate workspace/item
   review. Health is point-in-time diagnostic evidence, not a lock.
5. Set a per-run evidence reference, then run setup:

   ```python
   OLAF.configure(
       lakehouse_name="SampleLakehouse",
       control_data_isolation_attestation="change-review-123",
   )
   OLAF.setup()
   ```

6. Upload and load only a reviewed workbook:

   ```python
   OLAF.load_config("member", "Files/security/onelake_security.xlsx", "member")
   OLAF.load_config("config", "Files/security/onelake_security.xlsx", "config")
   ```

7. Optionally supply a fresh attestation reference for a sensitive stage. It is recorded, not
   required; without one the run is logged as `workspace_isolation=unknown`:

   ```python
   OLAF.generate()
   OLAF.plan()
   OLAF.apply()
   ```

`validate`, `show`, `trace`, and read-only audit queries remain diagnostic paths only
when they perform no log, mapping, file, cache, or repair write. Any repair or append
path is sensitive and passes the full gate.

### Gate results

The technical and operator facts remain separate:

- `dar_snapshot_safe`: complete bounded DAR response, collection ETag, and no
  reserved-path overlap;
- `workspace_isolation=attested|unknown`: whether the operator supplied a valid
  per-run evidence reference.

Neither is proof of workspace isolation. Missing/partial/malformed DAR state, missing
ETag, unknown membership shape, changed snapshot, or an
existing/unreadable sentinel blocks before a sensitive write. See
[control-data-security.md](control-data-security.md#two-independent-gates).

## 3a. Operating policy — config is the intended source of truth

OLAF builds a reviewed desired payload from the generated mapping. With
`keep_unmanaged=true`, OLAF includes live unmanaged roles in the submitted payload;
without it, the payload represents the configured set. In both cases, inspect the
plan and post-read.

The official Preview bulk `PUT` reference says that supplied roles are created or
updated. It does **not** promise atomic full-set replacement, deletion by omission,
stable role IDs, or exact restoration. Do not call the operation a guaranteed
replace, and do not claim what happens to omitted/default roles unless the current
official contract explicitly says so:
[Bulk DAR `PUT`](https://learn.microsoft.com/en-us/rest/api/fabric/core/onelake-data-access-security/create-or-update-data-access-roles).

Treat any first live mutation as an announced access cutover. Capture the bounded
pre-state, review the exact request, and verify the post-state per engine/access mode.

## 3b. Lakehouse target guard — config names it, generate verifies the attachment (security)

The config carries a lakehouse display label. OLAF resolves the attached target and
stamps target identity into generated provenance so a copied mapping is not silently
deployed elsewhere. A missing, ambiguous, mismatched, or unknown target blocks.

This is an OLAF behavior verified by fixture-based tests; it is not a Microsoft
guarantee that display labels are unique or that runtime context never changes.
Review the intended attachment before every run and keep real identifiers out of
logs, issues, documentation, and commits.

## 3c. Recovery — break-glass incident procedure (no public replay API)

A role backup is sensitive recovery evidence, not a guaranteed exact restore point.
The Preview endpoint does not promise atomic replacement or exact restoration. REST,
Delta, audit, and file changes are not one transaction.

Before a real apply/reset, OLAF records prepared intent and a backup pointer. If an
operation becomes ambiguous, preserve the incident sentinel, prepared row, and backup.
Stop later sensitive modes; do not automatically restore over a concurrent change.

OLAF v1.1.0 has **no supported public backup-replay method**. Do not call
`FabricClient.put_roles()` directly from a public workflow and do not treat a backup
pointer as a local file path. A low-level call bypasses the mandatory sentinel and
control-data gates, and no high-level guarded recovery method exists yet.

Until that method exists, handle recovery as an incident:

1. Preserve the sentinel, prepared record, backup pointer, and redacted operation
   evidence; do not delete or copy sensitive backup content into an issue.
2. Complete a fresh external workspace/item-access review and record a new per-run
   isolation attestation before deciding whether any write is authorized.
3. Obtain a fresh bounded DAR list and collection ETag, then compare the observed
   state with the reviewed recovery evidence in a secured, authorized environment.
4. Stop on a changed ETag or an ambiguous response. Escalate to the platform owner or
   Microsoft support rather than presenting an unguarded REST replay as an OLAF-safe
   procedure.

Microsoft documents the collection ETag, optional quoted `If-Match`, and
`PreconditionFailed` response:

- [List data access roles](https://learn.microsoft.com/en-us/rest/api/fabric/core/onelake-data-access-security/list-data-access-roles)
- [Create or update data access roles](https://learn.microsoft.com/en-us/rest/api/fabric/core/onelake-data-access-security/create-or-update-data-access-roles)

The list ETag and optional `If-Match` support are concurrency signals only. A `412`
requires a fresh review; neither an ETag nor a backup proves that an unguarded replay
is safe, complete, or exact.

## 3d. An apply that aborts with `role backup write failed` — a poisoned backup directory

Stop before retrying. Confirm the configured directory remains a descendant of
`Files/security`, inspect permissions and capacity without exposing its contents,
and run a fresh health/access review. Do not redirect backups to a broad or public
path. Do not delete an incident sentinel as generic cleanup.

## 3e. A control table whose column types drifted — repair with `setup(rebuild=True)`

Rebuild is destructive and sensitive. Export a sanitized schema-only description,
capture a recovery point, run health and the external access review, supply a fresh
attestation, then review the rebuild result. Never put a real table sample in a public
issue or pull request.

## 3f. `Files/security/` grows without bound — what to prune, and what not to

Define an organization-owned retention policy for imported workbooks, review CSVs,
mapping history, and backups. Do not prune during a deployment window. Preserve the
current recovery point, prepared/ambiguous-operation evidence, and any incident
sentinel. Deletion is containment only; it does not prove removal from Delta history,
caches, copies, exports, prior reads, or external logs.

## 3g. How much a run prints — the `verbosity` parameter

Use the lowest verbosity that remains operationally useful. Treat every notebook and
pipeline log as a potential public artifact: principal labels, object IDs, target
identifiers, policy predicates, attestation details, ETags, and backup bodies must not
be copied into public reports. The attestation value is a reference, not confidential
review content.

## 3h. `reset()` and `cleanup()` — destructive utilities

### `OLAF.reset()` — submits an empty DAR payload

Reset passes the same DAR snapshot, attestation, sentinel, backup, prepared-intent,
conditional-write, and post-check sequence as apply. If the initial snapshot shows
broad/default-reader access over reserved control paths, reset blocks before backup;
remediate exposure through authorized Fabric administration first.

The official endpoint does not promise omission/deletion semantics, so describe the
request body and observed post-state rather than claiming that every role was deleted:
[Bulk DAR `PUT`](https://learn.microsoft.com/en-us/rest/api/fabric/core/onelake-data-access-security/create-or-update-data-access-roles).

### `OLAF.cleanup()` — containment, not erasure

Cleanup may run without attestation only to reduce retained control data during
containment. It preserves an operation/incident sentinel and reports remaining
tables/files. It must return that exposure remediation is not proved. It cannot erase
prior readers, Delta history, caches, copies, exports, or external logs.

## 4. Test / CI gate

Local CI uses synthetic fixtures and no live Fabric tenant:

```bash
pytest --cov
scripts/lint.sh
pytest tests/test_doc_links.py
```

The coverage gate measures the runtime extracted from `notebooks/olaf.ipynb`.
Notebook output and execution counts must remain empty/null. See
[testing.md](testing.md).

CI can verify request construction, state transitions, and fail-closed decisions. It
cannot verify token permissions, service behavior, propagation, workspace sharing,
or engine enforcement.

## 4b. Pipeline integration (result contract)

Every run returns a result envelope with `status`, `changed`, `message`, `data`, and
recovery/error fields. A confirmed local or DAR write uses `changed=true`; an
ambiguous DAR outcome uses `changed=null`; neither is reported as `false`.

Pass `control_data_isolation_attestation` explicitly for each sensitive activity and
never store real review content in the parameter. Stop the pipeline on any blocked,
error, possible-exposure, or incident-sentinel result. Do not automatically retry a
real write with a refreshed snapshot because that would deploy a state that was not
the approved one. See [error-handling.md](error-handling.md).

## 5. Deploy

Import the exact reviewed notebook and verify its checksum/version. Bind the intended
lakehouse using the portal attachment or wrapper described in
[fabric-import.md](fabric-import.md). Keep deployment-specific identifiers in the
target environment, never in repository files.

After deployment, run read-only health first. A 2xx response or successful dry run
does not prove role propagation or enforcement. Microsoft publishes approximate,
non-SLA [OneLake security latencies](https://learn.microsoft.com/en-us/fabric/onelake/security/data-access-control-model#latencies-in-onelake-security).

## 6. Known TODOs (verify on a live workspace before relying on them)

The public release has no exact-SHA live Fabric evidence. Therefore:

- do not claim atomic replacement, deletion by omission, stable role IDs, exact
  restore, or DefaultReader re-creation behavior;
- do not claim universal enforcement or universal privileged-role bypass;
- do not claim a case-mismatched RLS predicate fails open; the current official
  guidance says no rows or query errors:
  [RLS syntax](https://learn.microsoft.com/en-us/fabric/onelake/security/row-level-security-syntax);
- do not claim Graph tokens are universally impossible; OLAF simply does not use
  Graph;
- treat future service observations as dated, versioned, exact-SHA evidence only.

The authorized protocol is [live-smoke-test.md](live-smoke-test.md). It is not
permission to access a tenant.

## 7. Platform limits and fallbacks

Microsoft's role/member/path/predicate limits and propagation estimates can change.
Consult the official pages at operation time instead of treating copied values as
permanent:

- [OneLake security limitations](https://learn.microsoft.com/en-us/fabric/onelake/security/data-access-control-model#onelake-security-limitations)
- [OneLake security latencies](https://learn.microsoft.com/en-us/fabric/onelake/security/data-access-control-model#latencies-in-onelake-security)
- [SQL endpoint troubleshooting](https://learn.microsoft.com/en-us/fabric/onelake/security/troubleshoot-onelake-security-for-sql-analytics-endpoints)
- [RLS syntax](https://learn.microsoft.com/en-us/fabric/onelake/security/row-level-security-syntax)

Distribution-list and mail-enabled-group restrictions are documented for specific SQL
endpoint/Direct Lake access paths, not as a universal Entra membership rule. Keep any
fallback scoped to the access path named by Microsoft's current
[limitations page](https://learn.microsoft.com/en-us/fabric/onelake/security/data-access-control-model#onelake-security-limitations).

### Known limitations

- Bulk DAR mutation is Preview.
- Same-lakehouse control data has no cryptographic isolation.
- DAR ETags do not cover workspace sharing or elevated access.
- REST, Delta, audit, and file writes are not one transaction.
- Delta `RESTORE` has no reviewed conditional/CAS contract in this release.
- Policy calculations do not prove enforcement or propagation.
- Cleanup cannot retract disclosure.
- CI has no live Fabric evidence.

### Identity required per mode

Do not infer platform authorization from an OLAF method name. Local table/file
operations require the corresponding lakehouse permissions. DAR list/mutation requires
the documented API permission; mutation additionally requires workspace Admin or
Member. Contributor is not sufficient to edit DAR definitions:

- [OneLake security and workspace permissions](https://learn.microsoft.com/en-us/fabric/onelake/security/data-access-control-model#onelake-security-and-workspace-permissions)
- [List DAR authorization](https://learn.microsoft.com/en-us/rest/api/fabric/core/onelake-data-access-security/list-data-access-roles)
- [Bulk DAR authorization](https://learn.microsoft.com/en-us/rest/api/fabric/core/onelake-data-access-security/create-or-update-data-access-roles)

`validate`, `show`, and `trace` are described as read-only OLAF intent only when their
path performs no repair/log/cache write. That intent is not a statement that Fabric
will authorize the caller.

### Running under a pipeline or service principal

OLAF uses the ambient notebook identity and requests a Fabric REST token with the
documented `pbi` audience. Token acquisition does not prove the effective identity,
workspace role, API permission, or access to local control paths. Verify all four in
the target environment. Microsoft documents NotebookUtils audience and service-
principal constraints here:
[NotebookUtils credentials](https://learn.microsoft.com/en-us/fabric/data-engineering/notebookutils/notebookutils-credentials#constraints-and-safety).

Use a dedicated non-personal deployment identity and an auditable external approval
record. Keep tenant/workspace/item/principal values outside repository configuration,
logs copied to issues, and public workflow artifacts.

## 8. Audit trail & out-of-band grants

Audit rows record OLAF's intended and observed actions. They do not attribute portal
or external changes and do not prove continuous access. `out_of_band` means no matching
OLAF provenance was found; it does not identify who made the change.

Use the audit APIs in [api/Audit.md](api/Audit.md) and engine-explicit
`effective_access(..., engine=...)`. SQL endpoint CLS uses deny/intersection behavior
while non-SQL paths use union behavior:
[Column-level security](https://learn.microsoft.com/en-us/fabric/onelake/security/data-access-control-model#column-level-security).

Treat log, mapping, and backup content as sensitive. Share only redacted structure and
hash/pointer evidence. A successful chain means the exact generated/planned/applied
hash pair appears in successful completion records; it is not evidence that every
engine enforced the policy continuously.
