# Mode manual

OLAF v1.0.0 is an independent community Preview for evaluation and development.
The bulk DAR mutation endpoint used by `apply`, `reset`, and rollback's apply leg is
officially Preview and is not a production contract:
[Microsoft REST reference](https://learn.microsoft.com/en-us/rest/api/fabric/core/onelake-data-access-security/create-or-update-data-access-roles).

Sensitive modes are disabled by default. First setup, workbook import, generate,
plan, apply, reset, rollback, and backup creation require a safe ETag-bearing
DAR snapshot and record an optional per-run workspace isolation attestation, followed
by the operation-sentinel protocol. Read [control-data-security.md](control-data-security.md).

## The result envelope

Every mode returns the same top-level shape:

| Key | Meaning |
|---|---|
| `mode` | requested mode |
| `status` | `success`, `skipped`, `blocked`, or `error` |
| `changed` | `true`, `false`, or `null` when a real write outcome is unknown |
| `message` | concise operator summary without secrets |
| `params` | effective non-secret parameter summary |
| `data` | mode-specific rows, hashes, and recovery pointers |
| `error` | structured category/message when blocked or failed |
| `batch_id`, `run_id` | correlation values |
| `config_hash` | active config fingerprint when available |

`changed=false` means OLAF knows that no relevant write occurred. It is never used
after a confirmed write or an ambiguous real request. A confirmed write followed by
an audit failure remains `changed=true`; an attempted request with no authoritative
outcome is `changed=null`. See [error-handling.md](error-handling.md).

Control-boundary output reports `dar_snapshot_safe` and
`workspace_isolation=attested|unknown` separately. Neither is proof of workspace
isolation.

## setup — create + migrate the control tables (runnable anytime)

`setup` creates or additively migrates the config, member, mapping, and log tables.
`rebuild=True` is destructive and requires an explicit recovery plan.

Setup is a sensitive write even when tables do not exist. There is no identity-free
bootstrap exception:

1. run read-only health;
2. remediate reserved-path DAR overlap and complete the external workspace/item
   access review;
3. optionally supply a per-run attestation reference (recorded, never required);
4. create/read back the PII-free sentinel;
5. revalidate the sentinel and immutable snapshot immediately before each sensitive
   write, then create/migrate tables;
6. perform the post-write boundary check before clearing the owning sentinel.

Unknown/missing ETag, malformed DAR state, or existing/unreadable
sentinel blocks before schema or audit writes.

## generate — validate + freeze the config (run after every config edit)

`generate` validates config/member data, resolves scopes and principal labels, and
writes the versioned mapping plus review artifact and audit evidence.

Before any CSV, mapping, or log write it:

- rejects desired scopes that overlap a configured control table or any part of
  `/Files/security` using case-insensitive, segment-aware comparison;
- requires the complete DAR snapshot; records the optional per-run attestation;
- creates and reads back the operation sentinel;
- reconfirms the sentinel, immutable ETag, and live-role digest immediately before
  each sensitive write.

A changed or unknown post-write boundary stops the chain, leaves the sentinel, and
reports `possible_exposure` with the affected artifact/table version. Deleting the
artifact would be containment only and is not described as erasing exposure.

OLAF's RLS parser is a conservative guard, not an exhaustive service grammar. The
current platform source is Microsoft's
[RLS syntax](https://learn.microsoft.com/en-us/fabric/onelake/security/row-level-security-syntax).
For multi-role RLS/CLS validation see
[table/column/row security](https://learn.microsoft.com/en-us/fabric/onelake/security/table-column-row-security#combine-row-level-and-column-level-security)
and [role evaluation](https://learn.microsoft.com/en-us/fabric/onelake/security/data-access-control-model#evaluating-multiple-onelake-security-roles).

## validate — dry-run generate's validation, zero writes

`validate` runs the same authored-config validation without writing a mapping, CSV,
audit row, backup, cache, or DAR state. If a future repair/append path is added, that
path becomes sensitive and must use the full gate.

Validation cannot prove that a principal ID is the one the operator intended, that
workspace sharing is safe, or that any engine will enforce the eventual policy.

## plan — diff desired vs live, record it (unlocks apply)

`plan` compares the generated mapping with a bounded live DAR snapshot and records
the reviewed hashes/actions. The log contains principal and policy provenance, so
plan is a sensitive write and requires the full control-data gate and sentinel.

A saved plan authorizes only its exact config hash, mapping hash, target identity,
and live snapshot. A changed ETag or role digest blocks rather than silently
refreshing the authorization baseline.

The diff is OLAF's calculation. It does not turn the Preview endpoint into an atomic
replace contract or prove what the service will do with an omitted role.

## apply — submit the reviewed DAR payload

`apply` is the live DAR mutation path. Its ordered safety protocol is:

1. validate uniform mapping provenance and the saved plan;
2. capture a complete, overlap-free DAR snapshot with collection ETag;
3. record the optional per-run workspace isolation attestation;
4. exclusively create/read the PII-free sentinel;
5. reconfirm the sentinel, exact ETag, and role digest immediately before each
   sensitive write;
6. write a sensitive role backup;
7. execute the service dry run where supported;
8. durably append `push/prepared` with payload hash and recovery pointer;
9. send the real request using the captured immutable ETag unless the operator
   explicitly opted out of conditional mutation;
10. append completion evidence and perform a bounded post-read before the owning
    run clears its sentinel.

`if_match=False` may opt out of sending `If-Match`; it does not bypass the privacy
gate or the requirement to observe an ETag.

With `keep_unmanaged=true`, OLAF constructs a payload that includes the live
unmanaged roles it intends to preserve. Without it, the body represents the
configured set. Microsoft's public Preview contract says supplied roles are
created/updated; it does not guarantee atomic full-set replacement or deletion by
omission. Always inspect the request and post-state:
[Bulk DAR `PUT`](https://learn.microsoft.com/en-us/rest/api/fabric/core/onelake-data-access-security/create-or-update-data-access-roles).

A first-attempt conditional `412` is a rejected request and does not trigger an
automatic restore. An ambiguous request preserves the backup, prepared row, and
sentinel. A confirmed `2xx` followed by audit failure reports `changed=true` and
the recovery pointers; it never says nothing changed.

## rollback — restore a prior config version, then re-run the whole chain

Rollback preflights the historical config, target, validation inputs, mapping/log
schemas, artifact paths, DAR payload, current config version, control boundary, and
service dry run before Delta `RESTORE`.

It records prepared intent before restore, checks the target config hash after
restore and before each later stage, then runs generate → plan → apply through the
same gates. Observable concurrency blocks the remaining stages. There is no reviewed
conditional/CAS contract for Delta `RESTORE`, so a final race window remains. Never
counter-restore automatically over a concurrent author.

A partial rollback reports exactly which config, artifact, audit, or DAR phase
changed, retains the sentinel, and preserves recovery pointers. REST, Delta, audit,
and file writes are not one transaction.

## show — read-only live pivot

`show(by=..., subject=...)` provides a live DAR pivot enriched with available OLAF
provenance. It is point-in-time output, not continuous access proof and not
attribution for changes made outside OLAF.

The effective outcome depends on engine/access mode, workspace/item permissions,
shortcuts, and propagation. Microsoft documents those boundaries in
[engine and user access](https://learn.microsoft.com/en-us/fabric/onelake/security/data-access-control-model#engine-and-user-access-to-data).

## trace — read-only operational snapshot

`trace` reports current generation, last successful completed deployment, mapping
freshness, desired/live counts, identity drift, policy drift, and out-of-band rows
when a bounded client read is available. Missing live state is reported as unknown,
not inferred from client construction.

### Where each number comes from

| Result | Source |
|---|---|
| current config/mapping hashes | uniformly stamped mapping generation |
| last deployment | successful `complete/success` apply or rollback apply leg |
| desired counts | current validated mapping |
| live counts | bounded DAR snapshot for this invocation |
| missing/unexpected | desired/live grant identity comparison |
| policy mismatch | normalized permission/RLS/CLS comparison |
| out of band | live grant with no matching OLAF deployment provenance |
| in sync | exact identity and normalized policy comparison for the sampled state |

### The three grains, and why the numbers differ

- an authored config row may contain patterns and several member labels;
- a mapping row represents one resolved role/scope policy plus resolved members;
- a grant identity represents one role/scope/member reachability edge.

Counts at different grains are not expected to match. None proves enforcement.

### `unexpected` and `out_of_band` are not the same question

`unexpected` means a live grant identity is absent from the current desired mapping.
`out_of_band` means no matching OLAF deployment provenance was found. A previous OLAF
generation can be unexpected now without being out of band; an externally created
grant may be both.

## Failure catalog

Use the structured error category and exact phase rather than matching a long message
string. Public reports must redact all identifiers and payloads.

| Category | Meaning | Operator action |
|---|---|---|
| `validation` | authored data, path, provenance, parameter, or runtime prerequisite is invalid | fix input; rerun validate/health |
| `control_boundary` | DAR snapshot, ETag, overlap classification, or sentinel is unsafe/unknown | stop; complete access review; do not upload/write real data |
| `conflict` | approved DAR/config snapshot changed | stop; re-read, review, and create a new plan/attestation |
| `backup` | recovery artifact could not be safely created | stop before live mutation; repair only inside `Files/security` |
| `audit` | confirmed write occurred but completion evidence was not confirmed | treat as changed; preserve sentinel and recovery pointers |
| `dar` | service request failed or outcome is ambiguous | distinguish rejected from unknown; never auto-restore over concurrency |
| `target` | attached target is missing, ambiguous, or mismatched | correct the attachment/config; do not redirect silently |
| `runtime` | supported Spark/runtime or required import is unavailable | select supported Fabric Runtime and verify packages |

Microsoft permits both delta-seconds and HTTP-date forms for `Retry-After`; OLAF
parses both and caps waits. The generic HTTP contract is
[RFC 9110 §10.2.3](https://www.rfc-editor.org/info/rfc9110).

For recovery, see [runbook §3c](runbook.md#3c-recovery--break-glass-incident-procedure-no-public-replay-api).
