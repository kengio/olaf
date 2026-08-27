# Protecting OLAF control data

OLAF control data is sensitive. The config, member mapping, generated mapping,
audit log, role backups, and review artifacts contain principal identifiers and
authorization or recovery state. The reserved boundary is the four configured
control-table paths plus the complete `/Files/security` subtree.

OLAF uses a fail-closed operating model for every sensitive write,
including first setup, workbook import, generate, plan, apply, reset, rollback,
and backup creation. These modes are disabled by default until the technical
DAR check passes. The per-run operator attestation is recorded, never required.

## Before uploading real data

OLAF cannot protect a workbook before an operator uploads it. Before placing a real
workbook under `Files/security`, complete an external review of workspace and item
sharing, dynamic default-reader/`ReadAll` access, Admin/Member/Contributor and other
elevated roles, shortcuts, and automation that can change access. Remove broad
`ReadAll` paths and restrict bypass-capable identities to authorized operators for
the whole run. Microsoft's documented workspace and data-access model is the
authority for those access paths:
[OneLake security and workspace permissions](https://learn.microsoft.com/en-us/fabric/onelake/security/data-access-control-model#onelake-security-and-workspace-permissions).

Do not upload a workbook containing real principal data until that review is
complete. The repository workbook contains synthetic examples only and is not an
operator attestation.

## Two independent gates

Every sensitive operation requires the first of these and records the second:

1. **DAR snapshot gate — enforced.** OLAF performs a bounded, complete DAR list, requires a
   collection ETag, classifies every returned role/rule/member shape, and rejects
   any case-insensitive, slash-normalized, segment-aware read overlap with a
   reserved control path. Unknown or dynamic membership on an overlapping rule is
   unsafe even when an array appears empty. The role name is diagnostic only.
2. **Workspace isolation attestation — optional, recorded, never enforced.** The
   operator may supply a short, non-secret evidence reference for the run, pointing at
   the external sharing and elevated-access review. OLAF records it and cannot verify
   its truth, so it gates nothing: a run without one proceeds and is recorded as
   `unknown`. It was briefly mandatory; requiring an unverifiable string only taught
   callers to supply a placeholder, which is worse than an honest `unknown`.

These facts are reported separately as `dar_snapshot_safe` and
`workspace_isolation=attested|unknown`. Neither result means “workspace isolation
proved.” A DAR ETag covers the DAR collection only; it cannot observe workspace or
item sharing, dynamic membership events, elevated roles, shortcuts, caches, prior
reads or copies, or a grant that appears and disappears between snapshots. The
official DAR list/ETag contract is here:
[List data access roles](https://learn.microsoft.com/en-us/rest/api/fabric/core/onelake-data-access-security/list-data-access-roles).

`if_match=False` may opt out of sending a conditional mutation, but it cannot bypass
the privacy gate or the requirement to observe an ETag.

## Operation sentinel and race containment

Before the first sensitive write, OLAF exclusively creates and reads back a fixed,
constant-content, PII-free sentinel under `Files/security`. Immediately before
**each** sensitive write, it revalidates that sentinel and the captured DAR snapshot.
Creation, verification, existing-sentinel, readability, or revalidation uncertainty
blocks before sensitive data is written.

The owning run clears its sentinel only after the post-write boundary check succeeds.
Otherwise the sentinel remains as an incident marker and blocks later sensitive
modes. Generic cleanup preserves it. An explicit clearance requires a fresh safe
DAR snapshot and a documented access-review reference passed to `clear_incident`.

A changed, missing, unreadable, or unsafe post-write snapshot is reported as
`possible_exposure`. The result identifies the affected phase and available recovery
pointers. Deleting an artifact is containment only: it cannot erase Delta history,
caches, exports, copies, prior reads, or third-party logs.

## Same-lakehouse limitation

The default design stores control data in the same lakehouse that OLAF manages. It
does not provide cryptographic isolation, a separately administered control plane,
or a transaction spanning workspace sharing, Delta/file writes, audit rows, and
Fabric REST. The operator-attestation model deliberately trusts authorized
administrators and external access controls for the unobservable interval.

Organizations that cannot accept that trusted-administrator boundary should not
import real principal data or run sensitive modes in this release. Use a separately
secured control store or wait for a design that provides the required isolation.

## Bootstrap sequence

1. Attach the intended lakehouse and run the read-only health diagnostic.
2. Review the returned DAR snapshot, ETag status, and reserved-path overlaps.
3. Externally review workspace/item access and remove broad `ReadAll`, dynamic
   default-reader access, and unauthorized elevated roles.
4. Keep real workbooks outside the lakehouse until steps 2–3 are complete.
5. Record the review in your change system and pass that per-run evidence reference
   as `control_data_isolation_attestation`.
6. Run setup or import. Stop on any sentinel, snapshot, or post-check failure and
   follow the recovery pointers; do not silently refresh the approved snapshot.
7. Optionally re-record the evidence reference for later sensitive operations; a
   run without one proceeds and is recorded as `workspace_isolation=unknown`.

Health is point-in-time diagnostic evidence, not a lock. Cleanup reduces retained
data during containment only; it preserves incident sentinels and never reports
that exposure was erased.
