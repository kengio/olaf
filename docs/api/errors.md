# Errors

Back to [API index](../api-reference.md) - [docs](../README.md).

> These are OLAF runtime classifications, not service-level guarantees. The bulk DAR mutation
> endpoint is Preview; after any ambiguous failure, re-read and classify the post-state.

**Audience:** internal -- the full `OLAFError` exception hierarchy the runtime raises. Most of it
never reaches a caller directly: `generate`'s **collect-all** validation catches `ValidationError`
(and its `ZeroMatchError`/`TargetResolutionError` children) and folds it into the aggregated error
list instead of aborting immediately, so a bad `lakehouse_name` or a dead scope entry blocks the run
with a clear reason alongside every other validation/member error; the runtime then prints the
message and exits with `SystemExit` (see the [failure catalog](../modes.md#failure-catalog)).
`DARHTTPError` and `UsageError`, by contrast, are the two classes that *do* reach a direct caller of
the lower-level classes (`FabricClient`, `Audit`, or `OLAF`'s live-DAR passthroughs) as a real raised
exception. Member resolution is **No-Graph** -- there are no Graph error classes; a member absent
from `onelake_security_member` is a plain string error in `generate`'s collect-all list, not an
exception.

## The hierarchy

```
OLAFError (category="unexpected")           base class; category feeds the audit error_category field
├── ValidationError (category="validation")  config / rule A·B·C violation
│   ├── ZeroMatchError                       an include/exclude entry matched no table or folder
│   └── TargetResolutionError                the lakehouse target guard can't resolve the config's lakehouse
│       ├── TargetNotFound                   matched no lakehouse in the attached workspace
│       └── TargetAmbiguous                  matched more than one, differing only by case
├── DARHTTPError (category="http")           a Fabric DAR REST call failed (>=400, or the paginated budget was exceeded)
│   └── DARConflictError                     the PUT's If-Match precondition failed (412) — live roles changed since this run's read; re-plan
├── UsageError (category="validation")       a method was called in a way it cannot service (missing client, refused parameter, ...)
├── ControlDataGuardError (category="guard") a sensitive operation could not establish its narrow DAR/attestation boundary
│   └── PostWriteBoundaryError               a confirmed sensitive write was followed by an unsafe or unreadable boundary
└── PostWriteAuditError (category="audit")   a sensitive write returned success but its durable completion row was not confirmed
```

`OLAFError.classify(exc)` (see [functions.md](functions.md)) maps **any** exception, not just this
hierarchy's own, to the audit `error_category` vocabulary (`http`/`validation`/`guard`/`unexpected`)
-- an `OLAFError` reports its own `category`; a `SystemExit` classifies as `guard`; a plain
`ValueError` as `validation`; anything that looks like an HTTP failure (`requests` in its module, an
`HTTP`-named type, or a `.response` attribute) as `http`; everything else as `unexpected`.

## OLAFError

Base for every framework-raised error. Never raised directly -- always one of the subclasses below.
Carries `category`, the class attribute `classify()` reads.

## ValidationError

A config / rule A·B·C violation -- the parent of `ZeroMatchError` and `TargetResolutionError`. Most
`ValidationError`s are plain instances raised directly from a rule check (e.g. rule C1's member-list
mismatch), collected by `generate`'s collect-all pass. `category = "validation"`.

## ZeroMatchError

An include/exclude entry (a table or a folder) matched nothing. Distinct from its siblings because
it is a statement about the **operator's config**, not the environment: a `TargetResolutionError`
means the environment could not be read at all, and blaming that on the row's scope would point the
reader at the wrong thing. A caller wanting to explain the consequence of a dead scope catches this
and nothing else. Raised by `Catalog.resolve_tables`/`resolve_folders`; caught by
`Generate._scope_pair` and folded into `generate`'s collect-all list, same as any other
`ValidationError`.

## TargetResolutionError

Base class for the lakehouse target-guard failures below. Never raised directly -- always one of the
two subclasses. One small hierarchy, for the single resolution step `generate` performs against
Fabric: the config's declared `lakehouse_name` against the **attached** workspace's lakehouses.
Raised by `FabricClient.resolve_lakehouse`, caught by `generate` and folded into its collect-all
list.

### TargetNotFound

`config.lakehouse_name` matched no lakehouse in the attached workspace.

Message: `"lakehouse '{name}' not found in the attached workspace ({workspace_id}) -- check
onelake_security_config.lakehouse_name"`.

### TargetAmbiguous

Two lakehouses in the attached workspace have display names that differ only by case, so the
config name is ambiguous.

Message: `"ambiguous lakehouse names differing only by case: {spellings} -- rename to disambiguate"`.

> A resolved lakehouse that is **not** the one the notebook is attached to is not one of these
> exceptions -- `_resolve_lakehouse_target` returns it as a plain collect-all error
> ("config names lakehouse ... but the notebook is attached to ...").

## DARConflictError

Raised by `FabricClient.put_roles` on a **412** when the submitted `If-Match` condition does not
hold. Stop, obtain a fresh DAR snapshot and ETag, and repeat the review; do not resend a stale
token. If any earlier attempt received an ambiguous response, the final state remains unknown
until a fresh read classifies it. Neither OLAF's classification nor the ETag contract establishes
atomic full-set replacement. See the
[official bulk endpoint](https://learn.microsoft.com/en-us/rest/api/fabric/core/onelake-data-access-security/create-or-update-data-access-roles).

## DARHTTPError

A Fabric DAR REST call failed: `FabricClient.put_roles` raises it on any response `>= 400` (after
retrying the transient statuses -- see [FabricClient § Retries](FabricClient.md#retries) -- carrying
the API's response body so a policy-validation error is actionable), and `FabricClient._get_paged`
raises it when a paginated GET exceeds `PAGED_BUDGET_SECONDS` (naming the page count reached and the
next URL). `category = "http"`.

## UsageError

A method was called in a way it structurally cannot service -- not a config problem, a call
problem. Raised by: the four live-DAR audit utilities (`out_of_band`, `effective_access`,
`who_can_access`, `drift`, on both `Audit` and their `OLAF` passthroughs) when no `FabricClient`
resolved; `OLAF.configure()` for the per-call-only `keep_unmanaged`/`rebuild` keys and for an `env`
value that fails its `^[A-Za-z0-9_-]{1,64}$` validation; `OLAF.reset()` and `OLAF.clear_incident()` off-Fabric, before either
submits anything (both need a live client and neither is routed through `run_mode`); `OLAF.load_config()`
for a non-loadable table, a path that does not resolve below `Files/security`, a sheet with no rows, or a
sheet whose columns do not match the target table's exactly; and `FabricClient.put_roles` when a REAL PUT
carries no collection ETag and the caller did not pass `allow_unconditional=True` (the zero-write dry run
is exempt). `category =
"validation"`.

## Related

- [functions.md](functions.md) -- `OLAFError.classify(exc)` maps any exception (including these) to
  the audit `error_category` vocabulary.
- [FabricClient.md](FabricClient.md) -- `resolve_lakehouse`/`list_roles`/`put_roles`, the callers
  that raise `TargetResolutionError`/`DARHTTPError`.
- [OLAF.md](OLAF.md#invariants) -- the "never raises on outcome" invariant and its `UsageError`
  exceptions.
