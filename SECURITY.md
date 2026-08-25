# Security policy

OLAF changes authorization state and stores principal/access metadata. A defect or
misconfiguration can grant unintended access, remove required access, or expose
control data. Treat this policy as part of the operating contract.

> **Release status:** OLAF v1.0.0 is an independent community Preview for
> evaluation and development, not a production-ready security product. The bulk
> DAR mutation endpoint on which it depends is officially Preview:
> [Microsoft REST reference](https://learn.microsoft.com/en-us/rest/api/fabric/core/onelake-data-access-security/create-or-update-data-access-roles).

## Reporting a vulnerability

Use **GitHub private vulnerability reporting** when it is enabled for this
repository: open the repository's **Security** tab, choose **Advisories**, then
**Report a vulnerability**. GitHub documents that this route exists only after a
repository owner enables private reporting:
[Privately reporting a security vulnerability](https://docs.github.com/en/code-security/how-tos/report-and-fix-vulnerabilities/report-privately).

Before making a release public, a repository owner must manually confirm that private
vulnerability reporting is enabled and that the route above opens. The local release
checks cannot verify a remote GitHub repository setting. If the private form is
unavailable, open a public issue containing **no vulnerability details** and ask the
maintainers to restore the private channel. Do not send secrets, exploit details,
real identifiers, or customer data through an issue, discussion, pull request,
commit, or public comment.

Include only sanitized evidence:

- affected OLAF version and release commit;
- affected mode or method;
- the smallest synthetic reproduction;
- expected and observed behavior;
- whether a sensitive local or DAR write was confirmed, rejected, or ambiguous;
- recovery pointers with principal, tenant, workspace, lakehouse, and item values
  replaced by synthetic placeholders.

Maintainers will coordinate acknowledgment, triage, remediation, and disclosure in
the private advisory. This community project makes no response-time or remediation
SLA.

## Supported versions

Security fixes target the latest released community Preview. Older snapshots and
unreleased commits are not maintained. Verify the imported notebook's version and
release commit before reporting or operating it.

OLAF supports Fabric Runtime 1.3 / Spark 3.5 or newer. Bundled library versions vary
with supported Fabric runtimes; the project does not promise a fixed preinstalled
package set or that platform updates remediate every dependency on a particular
schedule. Verify the selected runtime and packages in the target environment. See
Microsoft's [runtime lifecycle](https://learn.microsoft.com/en-us/fabric/data-engineering/lifecycle)
and [OneLake security limitations](https://learn.microsoft.com/en-us/fabric/onelake/security/data-access-control-model#onelake-security-limitations).

## Trust model

### What OLAF controls

OLAF validates authored configuration, resolves it into a mapping, saves a plan,
constructs DAR requests, and records intended/observed outcomes. It uses fixture-based
tests to verify those code paths.

Microsoft Fabric enforces data access. The effective result depends on workspace and
item permissions, engine/access mode, shortcuts, identity configuration, and service
propagation. Review Microsoft's current
[engine and user access model](https://learn.microsoft.com/en-us/fabric/onelake/security/data-access-control-model#engine-and-user-access-to-data)
and [SQL endpoint enforcement guidance](https://learn.microsoft.com/en-us/fabric/onelake/security/troubleshoot-onelake-security-for-sql-analytics-endpoints#access-modes-and-enforcement).

OLAF cannot turn a successful dry run, REST response, or policy calculation into proof
of propagated enforcement.

### Sensitive assets

The following contain principal identifiers, policy, provenance, or recovery state:

| Asset | Sensitive content |
|---|---|
| Config workbook and config table | intended roles, scopes, predicates, column rules, and principal names |
| Member sheet and member table | principal type, display label, and Entra object ID |
| Generated mapping and review CSV | resolved grants and provenance |
| Audit log | run identity, policy hashes, actions, and recovery references |
| `Files/security` | imported workbooks, mapping history, backups, and incident sentinel |

The four configured control-table paths and the whole `/Files/security` subtree are
reserved. A desired or live overlapping `Read`/`ReadWrite` grant is unsafe. Unknown
membership shape and any overlapping dynamic `fabricItemMembers` rule fail closed,
regardless of role name.

### No pre-upload protection

OLAF cannot guard a real workbook before an operator uploads it. Complete the external
workspace/item access review first, remove broad `ReadAll` and unauthorized elevated
access, and keep automation from reopening it for the whole run. Only then upload real
principal data. Microsoft documents the relevant workspace and item permission paths:
[OneLake security and workspace permissions](https://learn.microsoft.com/en-us/fabric/onelake/security/data-access-control-model#onelake-security-and-workspace-permissions).

The repository workbook is synthetic and is not evidence that an operator environment
is safe.

### DAR snapshot and workspace attestation are different

Every sensitive mode — first setup, workbook import, generate, plan, apply, reset,
rollback, and backup creation — **requires** a complete bounded DAR snapshot with a
collection ETag and no reserved-path overlap.

It also **records**, optionally, a per-run `control_data_isolation_attestation`
reference pointing at the operator's external access review. That reference is not
required and does not gate anything. OLAF cannot verify it, so demanding it bought no
safety while breaking callers that had no review to cite; a run that supplies nothing
is recorded as `workspace_isolation=unknown` rather than refused.

OLAF reports `dar_snapshot_safe` separately from
`workspace_isolation=attested|unknown`. The ETag covers only the returned DAR
collection; it does not observe workspace sharing, elevated roles, dynamic membership
events, shortcuts, caches, prior reads, copies, or an access grant that appears and
disappears between reads. The official list contract documents the collection ETag:
[List data access roles](https://learn.microsoft.com/en-us/rest/api/fabric/core/onelake-data-access-security/list-data-access-roles).

The attestation is an evidence reference, not a secret, permission, bypass, or proof.
OLAF records it but cannot validate its truth — which is exactly why it is optional. A
missing or malformed reference is reported as `unknown`, never as `attested` and never
as safe. An unreadable, partial, malformed, unsafe, or ETag-less DAR snapshot **does**
block before the sensitive write; that gate verifies something. `if_match=False` never
bypasses it.

### Sentinel and partial-operation handling

Before the first sensitive write, OLAF exclusively creates and reads back a constant,
PII-free sentinel under `Files/security`. Immediately before every sensitive write,
it revalidates that sentinel and the captured DAR snapshot. Existing, unreadable, or
uncertain sentinel state blocks. The owning run clears it only after a safe post-write
check; otherwise it remains as an incident marker and blocks later sensitive modes.
Generic cleanup preserves it. Explicit clearance requires a fresh safe snapshot, a
new attestation, and a documented access review.

The sentinel is containment, not evidence that disclosure did not occur. A detected
or unknown post-write race is `possible_exposure`; preserve exact phase information,
prepared records, and recovery pointers. A REST write, Delta write, audit append,
file write, and Delta `RESTORE` are not one transaction. Never describe cleanup,
artifact deletion, or sentinel clearance as erasing Delta history, caches, copies,
exports, prior reads, or external logs.

Read the full operator sequence in
[Protecting OLAF control data](docs/control-data-security.md).

### Member resolution

OLAF deliberately resolves principal labels from the imported member table and does
not call Microsoft Graph. That is a project design choice, not a platform claim that
a Graph token is universally impossible. NotebookUtils publishes its supported
audience keys and notes their constraints:
[Get a token](https://learn.microsoft.com/en-us/fabric/data-engineering/notebookutils/notebookutils-credentials#get-token).

Verify every `member_id` against Entra before loading. A syntactically valid object ID
can still identify the wrong principal; OLAF cannot infer operator intent from the ID.

### DAR write and recovery limitations

The official bulk `PUT` contract describes create/update input plus optional
`If-Match`; it does not promise atomic replacement, deletion of omitted roles, stable
role IDs, or exact backup restoration. OLAF does not elevate any observed behavior to
a platform guarantee:
[Create or update data access roles](https://learn.microsoft.com/en-us/rest/api/fabric/core/onelake-data-access-security/create-or-update-data-access-roles).

The same-lakehouse design is not cryptographic or transactional isolation. If the
trusted-administrator and externally controlled sharing boundary is unacceptable,
do not import real principal data or run sensitive modes in v1.0.0.

## Operational recommendations

1. Run read-only health before setup and before every access cutover.
2. Finish the external sharing/elevated-role review before uploading real data.
3. Use a unique per-run attestation reference tied to an auditable change record.
4. Restrict the four control tables and all of `Files/security` to authorized
   operators; do not grant those paths through DAR rules.
5. Verify every object ID against Entra and prefer explicit principal entries over
   broad patterns.
6. Review the saved plan and request payload; do not infer service behavior beyond
   the official Preview contract.
7. Preserve prepared intent, backup pointers, and incident sentinels after an
   ambiguous or partial operation.
8. Treat policy output as engine-specific. SQL endpoint CLS differs from non-SQL
   behavior; see Microsoft's
   [CLS semantics](https://learn.microsoft.com/en-us/fabric/onelake/security/data-access-control-model#column-level-security).
9. Use a dedicated deployment identity with only the permissions required for its
   operation. Admin or Member plus `OneLake.ReadWrite.All` is required to edit DARs;
   Contributor is not sufficient. See the official
   [workspace model](https://learn.microsoft.com/en-us/fabric/onelake/security/data-access-control-model#onelake-security-and-workspace-permissions).

## Sensitive-data incident response

If a credential, token, secret, real principal identifier, customer value, or other
sensitive data is committed or published:

1. **Revoke or rotate the credential first.** Rewriting Git does not make an active
   credential safe.
2. Make the repository private when continued exposure is possible and preserve the
   incident facts outside the repository.
3. Identify every affected ref, pull request, issue/comment, Actions log/artifact,
   release asset, cache, clone, and fork before rewriting.
4. Rewrite and force-update only the approved refs, then verify anonymously that old
   object IDs and hosted surfaces are no longer reachable.
5. Contact GitHub Support when cached views, pull-request refs, or server-owned data
   remain. Existing third-party clones and forks cannot be erased by rewriting the
   source repository.
6. Record the rotation, removal scope, residual exposure, and notification decision
   without copying the prohibited value into the report.

GitHub documents the limits and coordination requirements of sensitive-data removal:
[Removing sensitive data from a repository](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/removing-sensitive-data-from-a-repository).

## Scope note: credentials

OLAF configuration must not contain credentials. The runtime requests a Fabric REST
token from the ambient notebook identity using the documented `pbi` audience. Token
acquisition does not prove that the identity has the required API permission or
workspace role. Do not place tokens, connection strings, keys, or confidential
attestation content in the workbook, control tables, notebooks, logs, issues, or
reports.
