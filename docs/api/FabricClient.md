# FabricClient

Back to [API index](../api-reference.md) - [docs](../README.md).

`FabricClient` is OLAF's internal HTTP adapter for a single workspace/lakehouse target. It reads
and submits OneLake data access roles and resolves a lakehouse display name. Keep bearer tokens,
real identifiers, request bodies, and responses out of repository artifacts and public issues.

> **Preview boundary:** Microsoft labels the bulk create/update DAR endpoint **Preview**, for
> evaluation and development, and not recommended for production use. OLAF v1.1.0 is therefore a
> community Preview. The official contract says the supplied roles are created or updated; it does
> not promise atomic full-set replacement or deletion of roles omitted from the body.
> [Official bulk endpoint](https://learn.microsoft.com/en-us/rest/api/fabric/core/onelake-data-access-security/create-or-update-data-access-roles).

## Timeouts

Requests are bounded by runtime constants. Treat those values as implementation details and
inspect the current notebook source before relying on them. A timeout leaves outcome uncertainty;
do not infer that a write failed to land. Re-read the DAR, compare it with the reviewed request,
and follow the recovery procedure in [the runbook](../runbook.md).

## Retries

Transient responses may be retried within a bounded budget. `Retry-After` is an HTTP field whose
syntax includes both delay-seconds and an HTTP date; the normative contract is
[RFC 9110 §10.2.3](https://www.rfc-editor.org/info/rfc9110). A retry of a write is not
proof of atomicity or idempotent service behavior. After any ambiguous response, re-read and
classify the post-state before deciding whether to retry or restore.

An HTTP `412 Precondition Failed` means the submitted `If-Match` condition did not hold. Stop and
obtain a fresh snapshot and ETag; do not resend a stale token. Microsoft documents conditional
requests for this endpoint through its `ETag` response and `If-Match` request headers in the
[official bulk endpoint](https://learn.microsoft.com/en-us/rest/api/fabric/core/onelake-data-access-security/create-or-update-data-access-roles).

## resolve_lakehouse(name)

Resolves a lakehouse name in the configured workspace and returns its canonical display name and
item ID. A resolved attachment is not a security boundary: sensitive operations also require the pre-write
sentinel, and separately record an optional per-run workspace-isolation attestation
(`attested` when the run supplies a well-formed evidence reference, `unknown` when it is absent or
malformed — it is recorded, never enforced), both described in
[control-data security](../control-data-security.md).

Returns: `(display_name, item_id)`.

## list_roles(timeout=None, attempts=RETRY_ATTEMPTS)

Reads all currently returned DAR pages and captures the collection ETag from the response when the
service supplies one. Continuation URLs are restricted to the configured Fabric API origin before
the bearer token is sent.

Returns: `list[dict]` containing the service response objects.

```python
client = FabricClient(workspace_id, item_id)
roles = client.list_roles()
etag = client.roles_etag
```

## list_roles_quick()

Performs a bounded best-effort post-failure read for diagnostics. A failed or partial diagnostic
read does not establish the final write state.

Returns: `list[dict]` when the read succeeds.

## put_roles(roles, dry_run=False, etag=None, *, allow_unconditional=False)

Submits the official bulk DAR request. The calling identity must be a workspace **Admin or Member**
and the request requires `OneLake.ReadWrite.All`, as documented by Microsoft. When `etag` is
present, the real request uses `If-Match`; `dry_run=True` requests validation without applying. A
real (non-dry-run) PUT with no `etag` is REFUSED with `UsageError` unless the caller explicitly
passes the keyword-only `allow_unconditional=True`; dry-run stays zero-write and exempt.

The endpoint is Preview. Do not describe this method as GA, atomic replacement, deletion by
omission, or exact restoration. Before a real call, OLAF's public control contract requires:

1. an approved external-access review for the control store;
2. optionally, a per-run workspace-isolation attestation — a valid evidence reference (1–128
   characters of letters, digits, dot, underscore, colon, slash, hash, or hyphen) is recorded as
   `workspace_isolation: attested`; an absent or malformed reference is recorded as `unknown`.
   Nothing is refused for its absence;
3. a fresh immutable DAR snapshot and ETag;
4. a sentinel created/read before the first write and revalidated with the captured
   snapshot immediately before each sensitive write; and
5. a post-write read and comparison.

The technical snapshot/ETag and the isolation attestation are independent evidence. A backup is a
recovery input, not proof of transactional isolation. See
[control-data security](../control-data-security.md) and [modes](../modes.md).

Returns: the HTTP status code when the request succeeds.

OLAF deliberately does not call Microsoft Graph. NotebookUtils documents supported token audience
keys, including `pbi` for Fabric APIs, and notes that audiences can evolve; this framework choice
must not be presented as a universal token impossibility.
[NotebookUtils credentials](https://learn.microsoft.com/en-us/fabric/data-engineering/notebookutils/notebookutils-credentials#get-token).
