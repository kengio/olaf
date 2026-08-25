# Audit

Back to [API index](../api-reference.md) - [docs](../README.md).

> OLAF v1.0.0 is a community Preview. Audit helpers summarize OLAF control records and the DAR
> responses they can read; they are not universal authorization proofs across every Fabric engine
> or access mode. Use the [platform contract](../platform-contract.md) when interpreting results.

**Audience:** direct-use -- call these methods directly in any Fabric notebook cell or script.

`Audit` is a read-only companion class to `Log`: `Log` WRITES the trail (one row
per audit step across `setup`/`generate`/`plan`/`apply`/`rollback`); `Audit` READS it back
through named methods instead of hand-written SQL -- call a function instead of writing a query.
It is the programmatic surface behind `mode=trace` (see [modes.md](../modes.md#trace--read-only-operational-snapshot)),
and every method documented here also works directly in any Fabric notebook cell against the same
control tables `plan`/`apply`/`show` already use.

Every method below lives in `notebooks/olaf.ipynb` -- the single self-contained runtime,
loaded from a driver notebook via `%run olaf`: the `Audit` class -- a
constructor plus 25 methods -- and the `ChainStatus` dataclass returned by `verify_chain()`.
(Interactively, `OLAF` wraps these and returns DataFrames; see
[api-reference.md](../api-reference.md#olaf----the-primary-interactive-interface).)

## Construction

```python
%run olaf

# zero-client form -- everything except the live-DAR methods below.
# Enough for every log/mapping/config query (runs, log_history, grants, timeline, trace, ...).
trail = Audit(
    spark,
    config_table="olaf.onelake_security_config",
    mapping_table="olaf.onelake_security_mapping",
    log_table="olaf.onelake_security_log",
)

# with a client -- unlocks out_of_band(), effective_access(), who_can_access(), drift(), and
# report()'s out_of_band count, all of which pivot the LIVE DAR (same client the runtime builds
# for mode in {generate, validate, plan, apply, show, trace, rollback}).
client = FabricClient(workspace_id, item_id)
trail = Audit(spark, config_table, mapping_table, log_table, client=client,
              member_table="olaf.onelake_security_member")
```

The three table-name arguments are the same parameters every mode takes (`config_table` /
`mapping_table` / `log_table`, defaults `olaf.onelake_security_config` / `_mapping` / `_log`);
`client` is a `FabricClient`, required only for the live-DAR methods below (`out_of_band()`,
`effective_access()`, `who_can_access()`, `drift()`, and `report()`'s `out_of_band` field).
Everything else reads only the three control tables -- no REST calls.

A fourth, optional table-name argument, `member_table` (default `olaf.onelake_security_member`),
feeds the id -> name resolution used by `out_of_band()`, `who_can_access()`, `drift()`, and the
name -> id resolution in `effective_access()`. A caller pointing the other three arguments at
renamed tables should pass this one too, or those methods silently resolve names against the
default member table instead.

## Which method do I want?

| I want to know... | Call |
|---|---|
| What ran, filtered by mode/status/env/time | `runs(...)` |
| Everything that touched one role/member/scope, oldest first | `log_history(...)` |
| Every row written by one pipeline run (its `batch_id`) | `batch(batch_id)` |
| What went wrong recently | `failures(since=None)` |
| The last time a mode ran | `last_run(mode=None)` |
| The newest durably proven successful deployment | `last_successful_deployment(mode=None)` |
| What's deployed right now, with provenance | `current_generation()` |
| Is the deployed mapping out of date vs. the live config | `is_stale()` |
| Did the deployed generation actually get logged (chain intact) | `verify_chain()` |
| Every established grant, with both apply ends/who/config-version | `grants(...)` |
| When was this grant first and last pushed, and by whom | `provenance(role, scope, member=None)` |
| Who has access the framework didn't grant | `out_of_band()` (needs `client`) |
| Modeled effective access to a table for one member | `effective_access(member, table, member_type=None, *, engine)` (needs `client`) |
| Every member who can reach a table, and via which role | `who_can_access(table)` (needs `client`) |
| Full desired-vs-live comparison, categorized | `drift()` (needs `client`) |
| Which tables have no role reaching them at all | `coverage()` |
| Lifetime of every config generation ever run | `timeline()` |
| Which generation(s) deployed a given grant | `trace(...)` |
| Who authored (committed) a config version | `authored_by(version)` |
| Exact config rows behind a generation | `config_at(version=None, date=None)` |
| Field-level diff between two config versions | `config_diff(v1, v2)` |
| How one role/scope's config value evolved over every version | `value_history(subject, last=None)` |
| Delta version history of a control table (config/mapping/log) | `table_history(table)` |
| Snapshot of any control table at a version or date | `at(table, version=None, date=None)` |
| One-shot health snapshot -- the `mode=trace` payload | `report()` |

---

## Run history (audit-log queries)

Six methods that read `onelake_security_log` directly (no mapping/config join). Each takes optional
filters -- a filter left as `None` is not applied -- and returns rows as a DataFrame (all 27
`onelake_security_log` columns, see [data-model.md](../data-model.md#onelake_security_log---append-only-audit-trail)),
except `last_run()` and `last_successful_deployment()`, which each return a single dict or `None`.

### runs(mode=None, status=None, env=None, since=None, batch_id=None)

Every logged row matching the given filters, newest `run_at` first. `since` is an inclusive floor
compared as a string -- safe because `run_at` is ISO-8601, so lexical order matches chronological
order.

Returns: DataFrame.

```python
trail.runs(mode="apply", env="prod", since="2026-07-01T00:00:00+00:00").show()
```

### log_history(role=None, member=None, scope=None)

Every row touching a given subject (role/member/scope), oldest first -- the chronological story
of that one subject, as opposed to `runs()`'s newest-first operational view.

Returns: DataFrame.

```python
trail.log_history(role="SalesTH").show()
```

### batch(batch_id)

Every row written by one run, identified by its `batch_id` -- the full blast radius of a single
invocation (its `start`/`complete` pair plus, on a deploy, every per-grant `validate` row).

Returns: DataFrame.

```python
trail.batch("b7f2e1a0-0000-0000-0000-000000000000").show()
```

### failures(since=None)

Rows that are not a clean success: a non-`"success"` `status`, or a populated `error_category`.
Newest first, optionally floored by `since` (same rule as `runs()`).

Returns: DataFrame.

```python
trail.failures(since="2026-07-01").show()
```

### last_run(mode=None)

The single newest row for a mode (or overall, if `mode` is `None`), as a plain dict -- `None` when
nothing has been logged yet.

Returns: dict | None.

```python
trail.last_run("generate")
# {"mode": "generate", "status": "success", "run_at": "2026-07-11T11:58:00+00:00", ...}
```

### last_successful_deployment(mode=None)

The newest complete successful deployment with durable evidence. Without `mode`, considers a
successful `apply` or the apply leg of `rollback`; a supplied `mode` narrows the accepted log mode.
The completion record must carry config and mapping hashes plus a backup path and payload hash
(with a narrow legacy fallback for pre-schema completion records). It returns `None` when that
evidence is absent; a newer `last_run()` row is not a substitute.

Returns: dict | None.

```python
trail.last_successful_deployment()
```

---

## Freshness / integrity

Whether the deployed generation still matches the live config, and whether its provenance chain
actually made it into the log.

### current_generation()

The provenance of the single row in `onelake_security_mapping` (`generate` overwrites the table in
full each run, so there is only ever one generation live). `mapping_hash`/`mapping_version` are
not stored columns on the mapping table -- they are computed here with the exact same functions
`generate()` itself calls, so this agrees with what a real run would log. `None` when nothing has
been generated yet.

Returns: dict | None -- `config_hash`, `config_version`, `framework_version`, `generated_at`,
`mapping_hash`, `mapping_version`.

```python
trail.current_generation()
# {"config_hash": "284ae40f8b47a294", "config_version": 42, "framework_version": "1.0.0",
#  "generated_at": "2026-07-11T12:00:00+00:00", "mapping_hash": "9f8e7d6c00000000", "mapping_version": 7}
```

### is_stale()

`True` when the deployed mapping no longer matches the live active config -- the exact comparison
`generate`'s STALE guard makes (the active config rows' `config_hash` vs. the mapping's stamped
`config_hash`). The active rows go through the same autotrim (`Parse.trim_row`) `Deployment.short_rows`
applies before `generate` stamps that hash, so both sides hash identical values and incidental
leading/trailing whitespace never reads as a change. No mapping yet also counts as stale.

Returns: bool.

```python
trail.is_stale()  # False
```

### verify_chain()

`ok=True` only when the current generation has ordered successful completion records for generate
→ plan → apply that all carry its config and mapping hashes. The apply completion also needs durable
backup and payload evidence. A matching row alone is not a complete deployment chain.

Returns: `ChainStatus`, a frozen dataclass:

#### ChainStatus

```python
@dataclass(frozen=True)
class ChainStatus:
    ok: bool
    details: dict
```

`details` is `{"reason": "no mapping"}` when no generation exists. Otherwise it contains the
current `config_hash` and `mapping_hash`, boolean `generated`/`planned`/`applied` flags,
per-stage `stage_rows` counts, and a state: `missing_mapping`, `generated`, `planned`, `applied`,
or `incomplete`.

```python
status = trail.verify_chain()
status.ok        # True
status.details["state"]  # "applied"
```

---

## Grant provenance

Who has what, first applied and last re-applied -- read from the log, not the live API (`out_of_band()` is the one
exception: it needs the live DAR to know what's actually granted).

### grants(role=None, scope=None, member=None)

Establishing DAR grants read from the log: one row per `(role_name, scope_path, member_id)`,
deduped on the lowercased triple, keeping BOTH ends of the `run_at` range (each output row carries
the LATEST row's original-case display values and `config_version` -- the state in effect now;
`first_applied`/`first_granted_by` are the only backward-looking fields). Only counts `validate`+`success` rows from a
mode that recorded a successful push (`apply`, regardless of request-construction option --
including a rollback chain's apply, whose rows are stamped `mode=rollback` -- a
`plan` dry run never deploys, and a failed apply re-stamps its rows, so a broken push
establishes nothing). Aggregates across ALL envs -- unlike `Log.grant_provenance`,
which is scoped to one env.

Returns: DataFrame -- `role_name`, `scope_path`, `member_id`, `member_name`, `first_applied`,
`first_granted_by`, `last_applied`, `last_granted_by`, `config_version`. The two `*_granted_by`
fields name the principal at each END of the range -- routinely different people, which is why one
unqualified `granted_by` was dropped. Each is the log row's `run_by` verbatim: a UPN on an
interactive run, and `"<name> (<objectId>)"` -- or the bare object id when unlabelled -- on a
pipeline run. The parenthesised object id is the attested part; the name is only an
operator-supplied label from `onelake_security_member` (see
[`Log.resolve_principal`](Log.md#resolve_principalspark-member_table-value)), so match on the id.

```python
trail.grants(role="SalesTH").show()
```

### provenance(role, scope, member=None)

One establishing grant's provenance for a `(role, scope[, member])` -- the first row of `grants()`
-- as a plain dict, or `None` when no such grant exists. Named for what it returns rather than for
one end of it: the former name `since` claimed a continuity the log cannot establish (see
[data-model.md](../data-model.md#audit-query-recipes)).

Returns: dict | None.

```python
trail.provenance("SalesTH", "/Tables/sales/orders")
# {"role_name": "SalesTH", "scope_path": "/Tables/sales/orders", "member_id": "a1b2c3d4-0000-0000-0000-000000000000",
#  "member_name": "sg-sales",
#  "first_applied": "2026-07-11T12:00:00+00:00", "first_granted_by": "example.user@example.invalid",
#  "last_applied": "2026-08-24T05:00:00+00:00",  "last_granted_by": "other.user@example.invalid",
#  "config_version": 42}
```

### out_of_band()

Live DAR grants that have no framework provenance -- the same set `show`'s `out_of_band` count is
built from. Flattens every live `(role, scope, member)` grant and keeps those whose lowercased
triple is absent from `grants()`'s established set -- not merely absent from raw log rows: a grant
seen only in a `plan`/read row still counts as out-of-band, exactly as `show` treats it. `member_name`
is resolved id->name from `self.member_table` (the same lookup `who_can_access` uses); an id absent
from the table surfaces as the raw id rather than erroring, since an out-of-band member is usually
not in it, and an id the table gives **more than one distinct name** surfaces as the marker
`<ambiguous: N names in member table>` rather than whichever row sorted last. Needs a
`FabricClient`; raises `UsageError` if the instance was built with `client=None`.

Returns: DataFrame -- `role_name`, `scope_path`, `member_id`, `member_name`.

```python
trail = Audit(spark, config_table, mapping_table, log_table,
                    client=FabricClient(workspace_id, item_id))
trail.out_of_band().show()
```

---

## Live access & coverage

Four more read-only views: net effective access for one member, the reverse (who can reach a
table), a full desired-vs-live comparison, and which tables have no reaching role at all.

### effective_access(member, table, member_type=None, *, engine)

OLAF's modeled access summary for `table` and `member`: one detail row per reaching role plus a
synthesized row. Microsoft documents role/RLS combination generally, while SQL-endpoint CLS uses
intersection/deny semantics. Interpret the output for the specific engine/access mode; do not use
it as proof of universal effective access. See
[multiple-role evaluation](https://learn.microsoft.com/en-us/fabric/onelake/security/data-access-control-model#evaluating-multiple-onelake-security-roles)
and [table/column/RLS behavior](https://learn.microsoft.com/en-us/fabric/onelake/security/table-column-row-security).
`member` may be a display name/UPN or an objectId -- resolved against
`onelake_security_member` (No-Graph); pass `member_type` to disambiguate a display name shared by
two principals of different types. No reaching role returns an empty frame. Needs a `FabricClient`.

`engine` is required: `spark` and `direct_lake` model CLS as a union, while `sql_endpoint` models
the intersection of explicit CLS allow-lists. An unrestricted SQL-endpoint role does not remove
another explicit restriction. RLS remains the OR/most-permissive model across reaching roles. This
is a DAR reporting model, not proof of endpoint identity mode or enforcement for a request.

Returns: DataFrame -- `role_name`, `rls_condition`, `visible_columns`, `granting_role`, `effective`,
`engine`.

```python
trail.effective_access("sg-sales", "sales.orders", engine="spark").show()
```

### who_can_access(table)

The reverse of `effective_access()`: every member who can reach `table`, one row per
`(member, role)` pair -- a member reachable via two roles gets two rows, one per role. Needs a
`FabricClient`.

Returns: DataFrame -- `member_name`, `member_id`, `via_role`, `permission`, `rls_cls_summary`.

```python
trail.who_can_access("sales.orders").show()
```

### drift()

The full desired-vs-live comparison, categorized -- a read-only view, never a plan (it neither
records a plan row nor gates `apply`; that stays `plan()`'s job). One row per grant, categorized
`framework` (a live grant with framework provenance), `out_of_band` (a live grant with none),
`policy` (a live grant with provenance whose deployed `permission`, RLS predicate or CLS
allow-list differs from the mapping — `out_of_band` takes precedence over it), or
`missing` (a grant the mapping lock-file wants that isn't live). Needs a `FabricClient`.

Returns: DataFrame -- `role_name`, `scope_path`, `category`, `detail`, `member_id`, `member_name`.

```python
trail.drift().show()
```

### coverage()

Protected vs. unprotected table surface: every table in the live catalog, whether any role reaches
it, and how many roles/whether RLS or CLS applies. A table with zero mapping rows still gets a row
(`protected=False`) -- this is the gap finder for tables nobody configured at all, not a recap of
what's already configured. No `FabricClient` needed.

Returns: DataFrame -- `table`, `protected`, `roles_count`, `has_rls`, `has_cls`.

```python
trail.coverage().show()
```

---

## Generation lineage

Which config generation produced a grant, and who authored the config itself (a different
question from who *deployed* it -- see [data-model.md](../data-model.md#provenance--generation-tracing)
recipe 3).

### timeline()

Every logged config generation as one row: log rows grouped by `(config_version, config_hash)`,
reporting `first_seen`/`last_seen` (min/max `run_at`) and `runs` (a count) -- the lifetime of each
generation as the audit trail saw it. Ordered by `config_version`.

Returns: DataFrame -- `config_version`, `config_hash`, `first_seen`, `last_seen`, `runs`.

```python
trail.timeline().show()
```

### trace(member=None, role=None, scope=None)

Which deploying generation(s) stand behind a grant: `validate`+`success` rows from `apply` --
including a rollback chain's apply, stamped `mode=rollback` (the same rule `grants()`
applies -- a `plan` dry run never deployed), narrowed to the
given subject, newest first, projected down to the generation's coordinates.

Returns: DataFrame -- `config_version`, `config_hash`, `run_at`, `run_by`.

```python
trail.trace(member="sg-sales", scope="/Tables/sales/orders").show()
```

### authored_by(version)

Who committed a given `onelake_security_config` Delta version, read from `DESCRIBE HISTORY` -- not the
log. This answers "who wrote the rule", not "who deployed it" (that's `run_by` on the log rows;
see the data-model.md recipe above). `None` when no history row carries that version.

Returns: dict | None -- `version`, `timestamp`, `user`.

```python
trail.authored_by(42)
# {"version": 42, "timestamp": "2026-07-10T09:00:00Z", "user": "example.user@example.invalid"}
```

### config_at(version=None, date=None)

The exact config rows that produced a generation, via Delta time-travel. Give exactly one of
`version` (`VERSION AS OF`, int-coerced -- no string passthrough) or `date` (`TIMESTAMP AS OF`,
shape-validated). Delta time travel requires a compatible external environment. The repository's
smoke notebook is an unexecuted protocol, not evidence that this release commit ran on Fabric
(see [live-smoke-test.md](../live-smoke-test.md)).

Returns: DataFrame -- the config table's schema as of that version/date.

```python
trail.config_at(42).show()
```

### config_diff(v1, v2)

Role/scope/member changes between two config Delta versions, via `config_at` then diffed in
Python. `v1`/`v2` are two config versions to compare (whatever `config_at` itself accepts, most
commonly Delta version numbers). Reports: `added` (a role/scope pair present in `v2` only),
`removed` (present in `v1` only), and `changed` -- one row per differing field for a role/scope
pair present in both versions (permission, RLS/CLS columns, the member include/exclude columns,
`lakehouse_name`, `active`, `notes`).

Returns: DataFrame -- `change_type`, `role_name`, `scope_key`, `field`, `old`, `new`.

```python
trail.config_diff(41, 42).show()
```

### value_history(subject, last=None)

How one role/scope's config value evolved across config Delta versions: one row per version
where the subject is present, carrying each tracked field's value plus a `changed` flag against
the previous version the subject appeared in (the first appearance, or a reappearance after a gap,
always counts as changed). **Cost:** one sequential time-travel read per walked version — on a
long-lived config table that is one read per Delta commit ever made — so `last=N` (int or
int-valued only) bounds the walk to the N newest versions for interactive use. **Window
semantics:** the bounded walk's oldest row reads as a first appearance *in the window* (its
`changed=True` does not mean first in history); every row carries `window_truncated` (True when
`last` genuinely cut versions off the walk); and an **empty frame under `last=N` means "not
present in the last N versions", not "never existed"** — widen or drop `last` to tell those
apart.

Returns: DataFrame -- `config_version`, `role_name`, `scope_key`, the tracked config fields,
`changed`, and `window_truncated`.

```python
trail.value_history("SalesTH").show()
```

### table_history(table)

`DESCRIBE HISTORY` of a control table, made readable. `table` is `"config"`, `"mapping"`, or
`"log"`.

Returns: DataFrame -- `version`, `timestamp`, `user`, `operation`, `rows`.

```python
trail.table_history("config").show()
```

### at(table, version=None, date=None)

The snapshot of any control table (`"config"`, `"mapping"`, or `"log"`) at a Delta version or
date -- `config_at()` generalized to any control table. Give exactly one of `version` or `date`.

Returns: DataFrame -- the table's schema as of that version/date.

```python
trail.at("mapping", version=7).show()
```

---

## Report / rollup

### report()

The one-call operational snapshot behind `mode=trace`, answering the question an operator has
after an apply: what is deployed right now, and does it match the config? Composes the other
read-only `Audit` methods into a single dict -- no new queries, no writes.

Every count describes the CURRENT state, except `established_ever`, which is the log's cumulative
total across every environment and config version and is named so it cannot be misread as today's
figure. The live-state keys need to read the DAR, so they are present only when the instance has a
`client` set (the runtime always builds one for `trace`).

Returns: dict -- `current_generation`, `last_generate`, `last_apply`, `last_deployment`,
`last_deployment_mode`, `is_stale`, `established_ever`, and -- with a client --
`live_role_count`, `live_grant_count`, `desired_grant_count`, `missing`, `unexpected`,
`out_of_band`, `policy_checked`, `policy_mismatch`, `in_sync`.

`in_sync` is the conjunction of the two axes: the identity sets
match **and** `policy_mismatch` is 0. See [modes.md](../modes.md#where-each-number-comes-from).

```python
trail = Audit(spark, config_table, mapping_table, log_table, client=client)
trail.report()
# {"current_generation": {"config_hash": "284ae40f8b47a294", "config_version": 42, ...},
#  "last_generate": {"mode": "generate", "status": "success", ...},
#  "last_apply": {"mode": "apply", "status": "success", ...},
#  "last_deployment": {"mode": "apply", "status": "success", ...},
#  "last_deployment_mode": "apply",
#  "is_stale": False, "established_ever": 41,
#  "live_role_count": 2, "live_grant_count": 5, "desired_grant_count": 5,
#  "missing": 0, "unexpected": 0, "out_of_band": 0,
#  "policy_checked": 5, "policy_mismatch": 0, "in_sync": True}
```

This is exactly what the runtime runs for `mode=trace` (via the `run_mode` entrypoint; see
[modes.md](../modes.md#trace--read-only-operational-snapshot) for the full result shape merged into
the exit JSON, a table of what each number is computed from, and why a config of N roles is not a
deployment of N grants).
