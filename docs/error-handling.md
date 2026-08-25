# Error handling

OLAF separates validation, authorization, execution, and recovery so an error does
not silently become an unconditional retry. The result model is documented in the
[mode manual](modes.md#the-result-envelope).

## Two phases: collect-all, then fail-fast

- **Validate/collect:** authored configuration errors are aggregated so the operator
  can correct a coherent set. `validate` writes nothing.
- **Authorize/execute:** target, provenance, control-boundary, sentinel,
  ETag, backup, intent, and write phases fail at the first unsafe or unknown state.

A sensitive write never proceeds on missing/partial DAR state, missing ETag,
unknown membership shape, snapshot mismatch, or an
existing/unreadable sentinel.

## Status: the four outcomes

| Status | Meaning | `changed` |
|---|---|---|
| `success` | requested operation completed and required completion evidence was confirmed | `true` or `false` by operation |
| `skipped` | OLAF proved there was no write to perform | `false` |
| `blocked` | a guard refused the operation before the relevant write | `false` |
| `error` | execution or evidence failed | `true`, `false`, or `null` according to confirmed state |

`changed=false` is a statement that OLAF knows the relevant write did not occur.
After a confirmed write it is `true`; after an attempted real request with no
authoritative outcome it is `null`.

## Native failure — no per-activity `If`

Interactive methods return their envelope. Pipeline wrappers treat `blocked` and
`error` as activity failures while preserving the serialized envelope for diagnosis.
Do not branch only on message text; use `status`, `changed`, `error`, and
the structured recovery fields.

Never configure a pipeline to turn a conflict or ambiguous write into an automatic
unconditional retry. A new snapshot requires a new review, plan, and per-run
attestation.

## Two failure channels (one authoritative)

1. The returned/raised envelope is the immediate operator signal.
2. Durable audit rows are the operation history when their append was confirmed.

They may legitimately differ after an audit-path failure. A confirmed DAR `2xx`
followed by an unconfirmed completion append raises an audit error with
`changed=true`, preserves the prepared row and backup pointer, and states that the
write returned success but audit completion was not confirmed. It never fabricates
a compensating row in the log whose append just failed.

## Does `apply` land as one write?

No cross-system transaction exists. The ordered protocol is boundary snapshot →
sentinel → reconfirmation → backup → dry run → prepared intent → real DAR request →
completion evidence → post-read. A crash can leave a prepared-only state, a confirmed
write without completion evidence, or an ambiguous request.

Microsoft's Preview bulk `PUT` contract describes create/update input and optional
`If-Match`; it does not promise atomic replacement, deletion by omission, or exact
restore:
[Create or update data access roles](https://learn.microsoft.com/en-us/rest/api/fabric/core/onelake-data-access-security/create-or-update-data-access-roles).

Microsoft documents a collection ETag, optional quoted `If-Match`, and `412`
precondition response. OLAF uses those facts without claiming that a response proves
anything about workspace sharing, prior reads, or unrelated storage:

- [List data access roles](https://learn.microsoft.com/en-us/rest/api/fabric/core/onelake-data-access-security/list-data-access-roles)
- [Bulk DAR `PUT`](https://learn.microsoft.com/en-us/rest/api/fabric/core/onelake-data-access-security/create-or-update-data-access-roles)

### State interpretation

| Durable evidence | Meaning | `changed` |
|---|---|---|
| no prepared row and no sensitive write | no real DAR request authorized | `false` |
| prepared row without terminal result | request may not have started, may be in flight, or may have returned before failure | `null` when surfaced |
| prepared plus first-attempt `412/rejected` | conditional request was refused | `false` |
| prepared plus `unknown` | a real request was attempted without authoritative result | `null` |
| prepared plus confirmed completion | request returned success and completion evidence was confirmed | `true` |

Preserve the incident sentinel, backup, and prepared row after unknown/partial state.
Do not auto-restore over a possible concurrent change.

## Retry and timeout behavior

Only explicitly transient HTTP failures are retried within the bounded request budget.
`Retry-After` may be delta-seconds or an HTTP-date; OLAF parses both, floors past
dates at zero, and caps the wait. The generic HTTP source is
[RFC 9110 §10.2.3](https://www.rfc-editor.org/info/rfc9110).

A timeout after a real request can be ambiguous. It is not automatically a known
no-change result. Perform a bounded fresh read and compare against prepared intent and
the backup, while preserving evidence.

## Per-mode error behaviour

| Mode/surface | Failure boundary |
|---|---|
| `health` | read-only; unavailable/partial DAR is a failed check, never a pass from client construction |
| `setup` / `load_config` | full privacy gate; create/read sentinel before the first sensitive write and revalidate it with the snapshot before every sensitive write |
| `validate` | collect authored errors; zero writes |
| `generate` | full gate before CSV/mapping/audit; post-check uncertainty is `possible_exposure` |
| `plan` | full gate because plan appends sensitive policy/principal provenance |
| `apply` / `reset` | backup and prepared intent before real request; rejected, unknown, and confirmed outcomes remain distinct |
| `rollback` | preflight before `RESTORE`; report exactly which layers changed; no automatic counter-restore |
| `show` / `trace` | read-only point-in-time output; missing live state is unknown |
| `cleanup` | containment without attestation; preserve incident sentinel and never claim erasure |

See [Protecting OLAF control data](control-data-security.md) for the proof boundary.

## A write refused by Delta — `DELTA_FAILED_TO_MERGE_FIELDS`

Treat a control-table type mismatch as a schema incident, not a reason to bypass the
gate. Stop, capture a sanitized schema description, preserve recovery evidence, and
run `setup(rebuild=True)` only after a fresh external access review, new attestation,
and explicit destructive approval. Do not paste real rows into an issue.

## Diagnosing a failure

1. Read `status`, `changed`, `error`, operation phase, and
   `possible_exposure`.
2. Preserve the incident sentinel and recovery pointers.
3. Check whether a sensitive local write or real DAR request was confirmed,
   rejected, or ambiguous.
4. Run read-only health with a fresh bounded DAR list. Do not silently update the
   approved snapshot for retry.
5. Review the [failure catalog](modes.md#failure-catalog).
6. For a security concern, use the confidential route in [SECURITY.md](../SECURITY.md)
   and redact all real identifiers.

## See also

- [Mode manual](modes.md)
- [Recovery runbook](runbook.md#3c-recovery--break-glass-incident-procedure-no-public-replay-api)
- [Control-data security](control-data-security.md)
- [Platform contract](platform-contract.md)
- [Errors API](api/errors.md)
