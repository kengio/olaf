# Deployment

Back to [API index](../api-reference.md) - [docs](../README.md).

`Deployment` orchestrates one OLAF run. It remains an evaluation surface: the bulk DAR mutation
API is Preview, and all sensitive modes — including first setup — are disabled by default until
the control-data contract is satisfied. See [platform-contract.md](../platform-contract.md) and
[control-data-security.md](../control-data-security.md).

Every public mode method returns `{changed, message, data}`; the runtime wraps it in the standard
result envelope. Runtime implementation remains authoritative for exact keys and signatures.

## short_rows (property)

The active config rows projected to OLAF's declared authoring columns. These rows contain security
policy and may contain principal identifiers; do not publish them or attach them to issues.

Returns: `list[dict]`.

## config_hash (property)

A content fingerprint over `short_rows`. It detects changes to the declared config content; it is
not a confidentiality control, signature, or proof that external access is isolated.

Returns: `str`.

## setup(rebuild=False)

Creates or migrates the control tables after the external-access review, per-run workspace
and sentinel gate have passed. First setup is sensitive because the tables will hold
policy, identifiers, logs, and recovery pointers. It is disabled by default.

`rebuild=True` may drop and recreate incompatible control tables. Treat it as destructive and
retain an independently protected recovery copy. A same-lakehouse control layout is operational
convenience, not cryptographic or transactional isolation.

Returns: `{changed, message, data}` with created/migrated/rebuilt/unchanged details.

## generate(rebuild=False)

Validates the authored config, resolves synthetic or approved member-cache entries, and generates
the mapping and review artifact. OLAF deliberately does not call Microsoft Graph; this is a design
choice, not a claim that Graph access is universally impossible in Fabric notebooks.

Generated mapping/control artifacts are sensitive. Do not commit a real mapping, output, or
workbook to the public repository.

Returns: `{changed, message, data}`.

## validate()

Runs the configuration validation path without intentionally writing the mapping, review export,
or audit log. “Read-only” describes OLAF's intent, not Fabric authorization or the absence of
external reads. Confirm the exact runtime behavior before treating it as evidence.

Returns: `{changed=False, message, data}`.

## plan()

Reads desired and live DAR state and records the reviewable diff. A plan is not proof that a later
write is safe: apply must bind to the exact mapping, a fresh immutable DAR snapshot/ETag, and a
optional per-run workspace-isolation attestation, which is recorded rather than required.

Returns: `{changed, message, data}`.

## apply(keep_unmanaged=False)

Submits a reviewed bulk DAR request. The endpoint is Preview; Microsoft documents creation or
update of supplied roles but does not document atomic full-set replacement or deletion of roles
omitted from the body. Consequently, `keep_unmanaged` describes OLAF's request construction and
diff intent, not a platform deletion guarantee. Inspect the request and post-state.

Apply is disabled by default. Before the first write it requires the external-access review,
a fresh immutable DAR snapshot and ETag, and a create/read sentinel. The per-run
workspace-isolation attestation is optional and recorded, never enforced — `workspace_isolation`
reads `attested` when the run supplied an evidence reference and `unknown` when it did not. The runtime revalidates the sentinel and captured snapshot immediately
before each sensitive write. The snapshot/ETag is technical concurrency evidence; the isolation
attestation is separate evidence about control-data exposure.

A pre-write backup is a recovery input only. It does not make the Preview request atomic and it
does not guarantee exact restoration. A timeout or ambiguous response requires a fresh DAR read,
post-state classification, containment, and operator decision.

Returns: `{changed, message, data}`. Interpret `push_status` as the HTTP status, not a role count.

Official contract:
[bulk DAR endpoint](https://learn.microsoft.com/en-us/rest/api/fabric/core/onelake-data-access-security/create-or-update-data-access-roles).

## rollback(to_version="", reason="")

Restores a prior config-table version and invokes the deployment chain. It is a sensitive mutation,
disabled by default, and must pass the same external-access, attestation, snapshot/ETag, sentinel,
and post-state checks as apply. Delta time travel is not a substitute for a DAR recovery plan.

Returns: `{changed, message, data}`.

## show(by, subject)

Reads the current DAR and pivots it by table, role, or member, optionally enriching the display
from OLAF's audit log. Live results and logs may contain principal and item identifiers; redact
them before sharing.

Returns: `{changed=False, message, data}`.

## reset() 🔥

Sensitive containment operation, disabled by default and unavailable as a pipeline mode. It may
submit an empty DAR request, but the Preview API does not document deletion-by-omission semantics.
Do not claim that every role was deleted, that a platform default role cannot be recreated, or
that “nobody can read.” Workspace roles, engine/access mode, default roles, and shortcut behavior
remain relevant under Microsoft's access model.

Reset passes the same external-access review, immutable snapshot/ETag, sentinel, and post-state
verification as apply, and records the same optional per-run attestation. Verify the observed
result and preserve the recovery input outside the target's failure domain.

Official access model:
[workspace permissions](https://learn.microsoft.com/en-us/fabric/onelake/security/data-access-control-model#onelake-security-and-workspace-permissions)
and [engine/user access](https://learn.microsoft.com/en-us/fabric/onelake/security/data-access-control-model#engine-and-user-access-to-data).

Returns: a summary with `request="empty_payload"`, `prior_live_role_candidates`, `backup_path`,
and `post_state_review_required=True`. The roles are candidates observed before submission, not
observed platform deletions.

## cleanup() 🔥

Sensitive containment operation, disabled by default and unavailable as a pipeline mode. It is
limited to explicitly configured OLAF tables and paths after validating those boundaries. Cleanup
does not touch live DAR state, prove erasure, remove copies, or create cryptographic/transactional
isolation. Preserve incident evidence and independently protected recovery material before use.

Returns: a summary of attempted and observed containment actions.

See [RUNBOOK §3h](../runbook.md#3h-reset-and-cleanup--destructive-utilities) for the
operator sequence and limitations.
