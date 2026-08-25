# OLAF

Back to [API index](../api-reference.md) - [docs](../README.md).

> **Community Preview:** OLAF is independent and is not affiliated with or endorsed by Microsoft.
> Mutating operations use a Microsoft Fabric DAR endpoint documented as Preview for
> evaluation/development, not production use. Sensitive operations, including first setup, are
> disabled by default until the [control-data security contract](../control-data-security.md) is
> satisfied. See [platform-contract.md](../platform-contract.md) for canonical Microsoft sources.

**Audience:** primary interactive interface -- most users reach for `OLAF` first, and never touch
the domain classes ([Audit](Audit.md) / [Deployment](Deployment.md) / [FabricClient](FabricClient.md) /
[Log](Log.md)) it wraps.

`OLAF` is a singleton-static, flat facade over the same `run_mode` engine the pipeline
path uses. Never instantiate it -- every method is a `@staticmethod`/`@classmethod`
called directly on `OLAF` at first level (`OLAF.apply()`, `OLAF.grants()`, ...). Every
method lives in `notebooks/olaf.ipynb`'s **OLAF** section, loaded via `%run olaf`.

## The three entry paths

| Path | How | Outcome handling |
|---|---|---|
| **Pipeline** | A Fabric pipeline / `notebook.run` sets the `mode` parameter; the notebook's guarded ▶️ Run cell calls `run_and_exit(mode, allowed, params, spark)`. | **Raises** on `blocked`/`error` (the native Fabric Failure arrow); `notebook.exit(...)` on `success`/`skipped`. |
| **Interactive** | Inside the runtime notebook itself (`mode` left at its `""` default, so the Run cell's guard skips dispatch), call `OLAF.<action>()` directly in any cell. | **Never raises** on outcome -- every method returns a Spark DataFrame; a blocked/error outcome comes back as a DataFrame whose `status` column says so. |
| **Cookbook / driver notebook** | A separate notebook runs `%run olaf` -- Fabric's `import` -- to load the OLAF facade + every class into its own namespace, then calls `OLAF.<action>()`. Identical to the interactive path; the only difference is *which* notebook you're typing in. | Same as interactive: never raises. |

### The `mode=""` dispatch guard

The parameters cell defaults `mode = ""`. The Run cell only dispatches `if mode:` -- so an empty
`mode` (the default) makes `%run olaf` a **pure library load**: every class and the
`OLAF` facade are defined in the caller's namespace, and *nothing runs* -- crucially, `notebook.exit`
is never called, which would otherwise terminate the parent notebook that `%run`-ed this one. This
is what makes the interactive and cookbook paths safe: `%run olaf` behaves like
`from olaf import OLAF`, not like running the pipeline. A pipeline sets `mode` to a real
value (`setup` / `generate` / `validate` / `plan` / `apply` / `rollback` / `show` / `trace`) to
dispatch for real.

## Invariants

- **Never raises on outcome.** Deployment/maintenance ops route through `run_mode` (the
  non-raising engine); a blocked/error outcome comes back as a DataFrame whose `status` column says
  so, and the raw envelope dict is always available at `OLAF.last_result`. The exceptions are: the
  four live-DAR audit utilities (`out_of_band`, `effective_access`, `who_can_access`, `drift`),
  which raise `UsageError` (part of the `OLAFError` hierarchy defined in the runtime's Exceptions
  section -- **not** a plain `RuntimeError`) when no `FabricClient` resolved, exactly as calling the
  underlying client-less `Audit` method directly would; and `reset()`, which raises the same
  `UsageError` off-Fabric before it touches anything (it needs a live client and is not routed
  through `run_mode` -- see [`reset()`](#reset-) below).
- **Uniform return.** Every method returns **one** DataFrame type -- a Spark DataFrame, all-string
  schema (booleans render as the literal text `"True"`/`"False"`), the same shape
  [`Audit._df`](Audit.md) uses, so it builds under the CI fake-spark harness and displays natively
  on Fabric. A query/audit method returns its result table; an ops method (`generate`/`plan`/`apply`/
  `setup`/`rollback`) returns a compact DataFrame *view* of the outcome envelope.
  **Exception -- `config_at`/`at`:** these are Delta time-travel reads (raw `spark.sql`), so they
  return the control table's **own native schema** -- real column types, e.g. a BOOLEAN `active` --
  **not** the all-string projection. A point-in-time snapshot deliberately shows the table's true
  types; every other audit read still comes back all-string.

## `OLAF.configure(**kw)`

Sets base params (`env`, `tenant_id`, `lakehouse_name`, `config_table`, `mapping_table`, `log_table`,
`member_table`, `mapping_history_dir`, `role_backup_dir`, `batch_id`, ...) shared by every method. Chainable (returns
`OLAF`). `lakehouse_name` is where `setup` gets its required attachment assertion (see
[`setup()`](#setuprebuildfalse) below); every other mode reads it from the config table instead.

**Two parameters cell keys are NOT configurable: `keep_unmanaged` and `rebuild`.** Passing either
raises `UsageError` naming the key. They are **per-call** parameters on paths that mutate live
data -- pass them to `OLAF.apply(keep_unmanaged=...)` / `OLAF.generate(
rebuild=...)` on the call that should use them. A configured value could not be honoured anyway:
both methods carry a signature default that overrides `_base_params`, so the option must be
reviewed on each call. Nothing is stored
when the call is refused -- valid keys passed alongside are not applied either.

**`env` is validated, not free-form.** A value that is given must match `^[A-Za-z0-9_-]{1,64}$`
-- letters, digits, `_` or `-`, 1-64 characters. An invalid value raises `UsageError` before
anything is stored, the same rule `run_mode` enforces on the pipeline path (see
[modes.md](../modes.md#failure-catalog)). The constraint exists because `env` is stamped on every
audit row and read back as a SQL `WHERE` literal by the log reads -- a value that could close that
literal is refused, not repaired. `env` is **optional** and defaults to blank: it exists to tell one environment's log rows from another's when several deployments share a control-table shape, so a deployment that needs no such split leaves it unset. A blank env is stored as **NULL** (like every other blank string column), and the log reads scope on `env IS NULL` accordingly — an ad-hoc query for unlabelled rows must do the same, `env = ''` matches nothing. **`verbosity` is validated the same way**: a
value outside the five levels raises `UsageError` before anything is stored -- a sticky typo
would otherwise ride into every later run and be refused there anyway.

The public-preview control contract requires a fresh immutable DAR snapshot and its ETag for a
mutation. Do not use an unconditional-write escape hatch as a workaround for `412`; obtain a fresh
snapshot, repeat review, and keep the technical ETag evidence separate from the per-run workspace
isolation attestation.

Returns: a DataFrame of every parameter the next run will use — the same table as [`OLAF.show_params()`](#olafshow_params).

```python
%run olaf
OLAF.configure(env="qa", tenant_id="00000000-0000-0000-0000-000000000000")
OLAF.configure(keep_unmanaged=True)      # UsageError -- per-call only
OLAF.apply(keep_unmanaged=True)   # this is where it belongs
```

## `OLAF.params`

Every parameter the next run will **use** — what `OLAF.configure()` has stored, over
`PARAM_DEFAULTS` for everything it has not — as a plain `dict`. A property, like `last_result`: an
attribute, no parentheses, and no run needed.

```python
OLAF.configure(env="feature", lakehouse_name="LH_Gold")
OLAF.params["env"]            # 'feature'                     -- you set it
OLAF.params["config_table"]   # 'olaf.onelake_security_config' -- you did not, and this is what runs
OLAF.params["keep_unmanaged"] # request-construction option, not a platform deletion guarantee
```

It is a **copy**: `OLAF.params[...] = ...` changes nothing. `configure()` is the one way in, and it
is where the per-call parameters (`keep_unmanaged`, `rebuild`) are refused — a mutable view would
route around that.

The defaults are read from `PARAM_DEFAULTS`, the same map `run_mode` resolves against, so this
cannot drift from what actually happens. A static test reads `run_mode` back and fails if a
parameter is ever resolved against a literal instead.

`keep_unmanaged` and `rebuild` appear because a call passing neither gets exactly these values —
but they stay per-call, and nothing you do makes them sticky.

## `OLAF.show_params()`

The same content as a DataFrame, plus a `source` column — the display form of `OLAF.params`.
Reads only; sets nothing.

```python
display(OLAF.show_params())      # or .show() outside a notebook
```
```
✅ show_params · 6 set · 11 default
```

| parameter | value | source |
|---|---|---|
| `config_table` | `metadata.onelake_security_config` | `set` |
| `env` | `feature` | `set` |
| `keep_unmanaged` | `False` | `per-call` |
| `mapping_history_dir` | `Files/security/mapping-history` | `default` |
| `role_backup_dir` | `Files/security/role-backups` | `default` |

> `mapping_history_dir` / `role_backup_dir` must name folders **inside `Files/`** — `cleanup()`
> deletes every file under them. A leading `/` or a different letter case of `Files` is
> canonicalized (the same spellings the config's folder columns accept); an empty value, a
> backslash, a `..` escape, or a folder outside `Files/` is refused at the parameter boundary.
| `verbosity` | `info` | `default` |
| … | | |

Without `source`, a default and a deliberate choice render identically: an operator reading
`env  dev` cannot tell whether someone chose dev or nobody chose anything.

Like every other method here it **returns** the frame and does not render it — `display(...)` on
Fabric, `.show()` elsewhere — so the table stays usable as an input rather than only as output.

`OLAF.configure()` returns the same table, which is why calling it with no arguments works as a
reader; prefer `show_params()`, which does not pretend a setter ran.

## `OLAF.last_result`

The raw result `dict` — the **result envelope**, `{mode, status, changed, message, params, data,
error, batch_id, run_id, config_hash}` — from the most recent `OLAF` call, including
`OLAF.show()`/`OLAF.trace()` (both also route through `run_mode`). Read it when the compact
DataFrame view isn't enough.

**It is the same object the pipeline gets.** A run that ends `success` or `skipped` passes exactly
this dict, JSON-encoded, to `notebookutils.notebook.exit(...)`, so a pipeline reads it from the
activity's `exitValue`. A run that ends `blocked` or `error` never reaches `notebook.exit` — it
raises with the same envelope as the exception payload, so the activity FAILS (the native-failure
contract) instead of succeeding with a bad verdict.

**It is set on every outcome, on both paths.** `OLAF.<action>()` does not raise on a blocked run:
it returns the frame and stashes the envelope here — and the ▶️ Run cell sets the same variable
before it exits or raises, so the `OLAF.last_result:` line a run prints is true wherever it ran. So a notebook can branch on the last run
without a pipeline, and without parsing the printed output:

```python
OLAF.generate()
if OLAF.last_result["changed"]:          # nothing to deploy if generate skipped
    OLAF.plan()
    if OLAF.last_result["data"]["counts"]:
        OLAF.apply()
elif OLAF.last_result["status"] == "blocked":
    print(OLAF.last_result["error"])     # the whole collect-all fix-list
```

## Every call says whether it ran

The modes print their verdict through the result renderer — a status badge, the message, the
data, then a blank line and `OLAF.last_result: {…}`, the same envelope as one line of JSON, named
after the variable it is also sitting in. Everything else prints one line saying what it found:

```
✅ health · 9 check(s) · all pass

→  DataFrame[check, status, detail]
```

A call that ran but has nothing good to report says so with ⚠️ instead — `explain()` over a
config `generate` would hard-reject, for instance. It still returns a real frame and still never
raises; a ✅ there would read as a clean preview.

```
⚠️  explain · 4 blocking error(s) · this config would NOT generate

→  DataFrame[error]
```

**Every call ends by naming what it hands back, column by column, on a line of its own.** A printed
verdict says nothing about a returned value, and once you notice the value it is opaque: an
unassigned Spark frame echoes as `DataFrame[mode: string, status: string, …]`, which reads as noise
until you know it is the schema of the thing you just got. Naming the columns turns that echo into
the answer — and giving it its own blank-line-separated last line puts it in the same place
whatever you called, instead of trailing a sentence here and a wrapped JSON dump there.

A mode's verdict block ends the same way:

```
OLAF.last_result: {"mode": "setup", …}

→  DataFrame[mode, status, changed, message]
```

Long schemas stop early rather than wrapping — the log passthroughs return 27 columns, and a hint
that buries the verdict above it is worse than one that stops:

```
→  DataFrame[batch_id, run_id, run_at, env, mode, workspace_name, lakehouse_name, role_name, +19 more]
```

The column names come from `frame.columns`, which is metadata: **no query runs**, so it stays free
on the lazy frames (`config_at`, `at`, `table_history`) that a row count would have made expensive.
It names the **columns**, never a binding: the variable is yours, and guessing it is how an earlier
`→ display(result)` came to be wrong more often than right. Rendering stays your call
(`display(...)`, `.show()`).

The line also lands directly above the `DataFrame[mode: string, …]` a notebook echoes for an
unassigned frame, which is what it decodes. The ▶️ Run cell never prints it: that path exits or
raises out of the notebook with no caller left to hand a frame to.

Without the verdict line above it, a cell that succeeded and a cell that came back empty look
identical until you render the frame. It carries the answer where there is one (`health`,
`diagnose_member`, `status`, `explain`, `configure`) and stops at "it ran" where there is not —
deliberately **not** a row count, because `config_at`/`at`/`table_history` return a lazy Spark
frame and counting it would run a query you never asked for.

## Which method do I want?

| I want to... | Call |
|---|---|
| See what the next run will actually use | `OLAF.show_params()` |
| Create/migrate the control tables | `OLAF.configure(lakehouse_name=...)` then `OLAF.setup()` |
| Run a 9-check doctor pass | `OLAF.health()` |
| See a 1-row deployment snapshot | `OLAF.status()` |
| Find out why one member can't see data | `OLAF.diagnose_member(member)` |
| Preview the roles a config would produce, before generate | `OLAF.explain()` |
| Freeze the config into the mapping lock-file | `OLAF.generate(rebuild=False)` |
| Dry-run generate's validation with zero writes | `OLAF.validate()` |
| Diff desired vs. live | `OLAF.plan()` |
| Push the plan to the live DAR | `OLAF.apply(keep_unmanaged=False)` |
| Restore a prior config version | `OLAF.rollback(rollback_to_version="", rollback_reason="")` |
| Submit a sensitive reset/containment request | `OLAF.reset()` 🔥 |
| Contain configured OLAF control paths and tables | `OLAF.cleanup()` 🔥 |
| Pivot the live DAR by table/role/member | `OLAF.show(by, subject="")` |
| Get the operational snapshot behind `mode=trace` | `OLAF.trace()` |
| Filter the raw audit log | `OLAF.runs(...)` |
| Everything touching one role/member/scope, oldest first | `OLAF.log_history(...)` |
| Every row from one run (`batch_id`) | `OLAF.batch(batch_id)` |
| What went wrong recently | `OLAF.failures(since=None)` |
| The last time a mode ran | `OLAF.last_run(mode=None)` |
| What's deployed right now, with provenance | `OLAF.current_generation()` |
| Is the deployed mapping stale vs. the live config | `OLAF.is_stale()` |
| Did the deployed generation's chain make it into the log | `OLAF.verify_chain()` |
| Every established grant, with provenance | `OLAF.grants(...)` |
| When was this grant first and last pushed, and by whom | `OLAF.provenance(role, scope, member=None)` |
| Which live grants have no framework provenance | `OLAF.out_of_band()` (needs a client) |
| Modeled effective access to a table for one member | `OLAF.effective_access(member, table, member_type=None, *, engine)` (needs a client) |
| Every member who can reach a table | `OLAF.who_can_access(table)` (needs a client) |
| Protected vs unprotected table surface | `OLAF.coverage()` |
| Categorized desired-vs-live drift (framework/out_of_band/policy/missing) | `OLAF.drift()` (needs a client) |
| Lifetime of every config generation | `OLAF.timeline()` |
| Who authored (committed) a config version | `OLAF.authored_by(version)` |
| Exact config rows behind a generation | `OLAF.config_at(version=None, date=None)` |
| Delta history of any control table | `OLAF.table_history(table)` |
| A snapshot of any control table at a version/date | `OLAF.at(table, version=None, date=None)` |
| What changed between two config versions | `OLAF.config_diff(v1, v2)` |
| How one role/scope's config value evolved over time | `OLAF.value_history(subject, last=None)` |
| One-shot health snapshot (the `mode=trace` payload) | `OLAF.report()` |

---

## Maintenance -- control-table lifecycle & diagnostics

### `setup(rebuild=False)`

Create/migrate the four control tables (idempotent, additive schema-drift migration). Maps to
`mode=setup`.

`rebuild=True` **DROPS and recreates** any control table whose live column **types** disagree with
the framework's. `ALTER` cannot retype a Delta column, so a drifted table stays broken however many
times `setup` runs, and every write to it is refused by Delta -- this is the way out. It is
destructive: the config's authored rows and the log's audit history go with the table. It drops
nothing else: a table that merely misses a column is still migrated additively, and one that already
agrees is untouched, so leaving the flag on does not cost you a table each run. Every drop prints
the table, the columns that forced it and the row count going with it, and an `action='rebuild'` row
lands in the **new** log. Reload an author-owned table afterwards with
[`load_config()`](#load_configtable-path-sheetnone).

`lakehouse_name` is **required** for `setup` -- configure it first, or the call comes back `blocked`
with *"lakehouse_name is required for setup"*. It is an **assertion**, not a target: `setup` always
writes to the **attached** lakehouse (its DDL uses two-part `olaf.…` names), so the name exists to get
a wrong attachment refused -- naming a different lakehouse is refused, never honoured. See
[modes.md](../modes.md#setup--create--migrate-the-control-tables-runnable-anytime).

Returns: DataFrame -- `mode`, `status`, `changed`, `message` (1-row status summary).

```python
OLAF.configure(lakehouse_name="LH_Gold")   # the lakehouse this notebook is attached to
OLAF.setup()
OLAF.setup(rebuild=True)                   # ...after a type drift, accepting the data loss
```

### `load_config(table, path, sheet=None)`

Load an author-owned control table from a workbook on the lakehouse. Do not upload or load a real
workbook until the control store has passed an external-access review. Supplying a per-run
workspace-isolation attestation is optional -- it is recorded as `workspace_isolation=attested`
or `unknown` and gates nothing. The repository workbook is synthetic only.

`table` is the **logical** name -- `"config"` or `"member"` -- not the physical table, so the same
call works whichever names `OLAF.configure()` points at (the same convention as `OLAF.at("mapping")`).

`"mapping"` and `"log"` are **refused**, and that is the reason the argument is a logical name
rather than any table: the mapping is a lock-file `generate` derives, so loading one would deploy
grants nobody authored, and the log is append-only audit history, so loading one would forge it.

The sheet's columns must match the table's **exactly** -- missing and unexpected are both refused,
naming them, and nothing is written. A sheet short a column is a config that silently means
something other than the one the author edited, and this is the last point before it becomes
deployed access. An empty sheet is refused because replacing authored rows with an empty set would
delete the whole security config.

The authored-row replacement covers **OLAF's own columns only**. A control table may carry columns another
framework added (load provenance, for example): those are never dropped and never rewritten --
the write keeps the live table's schema (no `overwriteSchema`; `mergeSchema` only ever *adds* a
column OLAF's contract gained), carries each surviving row's foreign values over by row key
(the **whole authored row** for config -- it has no unique key, so editing any authored column
makes it a new row that starts with `NULL` there -- and `member_type` + `member_name` for member),
leaves them `NULL` on rows the workbook just added, and lets them go only when the row itself is
deleted.

Types come from `TableSchema`, so `active` lands as a real `BOOLEAN`. No audit row is written --
this is authoring, not deployment; the Delta commit is the record, readable via
`OLAF.table_history("config")`.

Returns: DataFrame -- `table`, `rows`, `source`, `sheet` (1-row summary).

```python
OLAF.load_config("config", "Files/security/onelake_security.xlsx", "config")
OLAF.load_config("member", "Files/security/onelake_security.xlsx", sheet="member")
```

### `health()`

One-call doctor: nine independent checks, each wrapped so one failing check never aborts the
others -- `health()` **never raises**. `control_tables` (all 4 present with expected schema),
`table_location` (the tables this session reads are the ones it is pointed at -- see below),
`mapping_staleness` (mapping vs. active config), `dar_reachable` (a bounded DAR snapshot),
`control_data_exposure` (separate snapshot and attestation facts), `identity_preflight` (a Fabric
token for the ambient identity), `runtime_prerequisites` (observed Spark baseline only),
`last_apply_age` (how long since the newest durably proven successful apply -- warns past 30 days),
and `out_of_band` (count of live DAR grants with no framework provenance). When no client resolves
(off-Fabric), the DAR-dependent rows report fail/warn with a clear detail while the
log/mapping-based rows are still evaluated.

`control_data_exposure` reports compact JSON facts: `dar_snapshot_safe`, `dar_etag`,
`reserved_paths`, `snapshot_error`, and `workspace_isolation`. A safe point-in-time DAR snapshot
and `workspace_isolation=attested` are separate facts; neither proves workspace isolation or
holds a lock for a later write.

**On `table_location`.** Every other check reads "the control tables" as though there were only one
set. There is not: they are whatever the **attached** lakehouse holds, so the answer to every other
check depends on the attachment being the one you think it is. Two ways it is not, and neither
surfaces anywhere else:

1. **The attachment is cross-workspace** (`fail`). Every mode refuses this pairing, but a `setup`
   run before that guard existed left four control tables plus an audit row in the other workspace
   with nothing to say so.
2. **The attachment moved under the tables** (`fail`). The mapping stamps the workspace and
   lakehouse it was generated against; when those no longer match the runtime context, this session
   is reading a different set of tables than the one it generated. The same mismatch is also
   **refused** at `plan`/`apply`/`rollback` by the target-identity guard -- `health()` is the
   read-only detector, not the only defense.

Before the first `generate` there is no stamped target to compare against, which is a `pass` with
that stated -- a second warning there would only duplicate `mapping_staleness`. Off Fabric there is
no context at all, which is a `warn`.

Returns: DataFrame -- `check`, `status` (`pass`/`warn`/`fail`), `detail`. Always 9 rows.

```python
OLAF.health()
```

→ Spark DataFrame (always 9 rows):

| check | status | detail |
|---|---|---|
| control_tables | pass | all 4 control tables present with the expected schema |
| table_location | pass | control tables are in the attached lakehouse (LH_Gold) |
| mapping_staleness | pass | mapping matches the active config (version 42) |
| dar_reachable | pass | bounded DAR read succeeded with a collection ETag |
| control_data_exposure | pass | JSON facts show a safe DAR snapshot and `workspace_isolation=attested` |
| identity_preflight | pass | Fabric token acquired for the ambient identity (synthetic example) |
| runtime_prerequisites | pass | observed Spark meets the baseline; verify the Fabric Runtime label |
| last_apply_age | pass | last apply was 2 day(s) ago |
| out_of_band | warn | 1 out-of-band grant(s) with no framework provenance |

### `status()`

One-call at-a-glance deployment snapshot, built purely from the log + mapping (no live client
needed, unlike `health()`'s DAR-dependent checks). `n_roles`/`n_members` are distinct counts off
the mapping lock-file; `last_generate` is the newest logged generate row, while `last_apply` is
the newest durably proven successful `apply`. `last_deployment` and `last_deployment_mode` include
a rollback apply leg only when its completion record carries durable backup and payload evidence.
`live_config_version` is the config version the *current* mapping was generated from.
`pending_change` is `False` without a mapping; otherwise it remains `True` until `verify_chain()`
finds an ordered, same-hash generate → plan → apply completion chain.

Returns: DataFrame -- `n_roles`, `n_members`, `last_generate`, `last_apply`,
`last_deployment`, `last_deployment_mode`, `live_config_version`, `pending_change`. Always 1 row.

```python
OLAF.status()
```

→ Spark DataFrame (1 row):

| n_roles | n_members | last_generate | last_apply | last_deployment | last_deployment_mode | live_config_version | pending_change |
|---|---|---|---|---|---|---|---|
| 3 | 5 | 2026-07-15T08:00:00+00:00 | 2026-07-15T08:05:00+00:00 | 2026-07-15T08:05:00+00:00 | apply | 42 | false |

### `diagnose_member(member)`

Why can't `member` see data? Walks the same chain a human troubleshooter would, **in order**:
`member_in_table` (present in the member cache?) → `id_resolved` (a valid GUID objectId?) →
`in_mapping` (does that objectId appear in any mapping row?) → `live_in_dar` (is it actually live
in the DAR? needs a client -- reports `ok=False, "no live client"` without one, never raises) →
`apply_in_sync` (is the deployed mapping not newer than the last successful apply?). `member` is a
display name **or** an objectId -- a GUID-shaped value passes through the same way
`effective_access` accepts it (step 1 reports the pass-through instead of a name lookup), so an id
copied out of `who_can_access()`'s `member_id` column works here too. The first
three steps are a dependency chain -- once one fails, every step after it is short-circuited with
`"skipped — prerequisite failed"` instead of reporting a misleading guess; `live_in_dar` and
`apply_in_sync` are independent of each other and of the chain's break point.

Returns: DataFrame -- `step`, `ok` (bool), `detail`. Always 5 rows, in order.

```python
OLAF.diagnose_member("example.user@example.invalid")
```

→ Spark DataFrame (always 5 rows, in order):

| step | ok | detail |
|---|---|---|
| member_in_table | true | found in olaf.onelake_security_member (type User) |
| id_resolved | true | resolved to objectId 8f4e2a…c1 |
| in_mapping | true | in role(s): SalesReaders |
| live_in_dar | true | live in role(s): SalesReaders |
| apply_in_sync | true | mapping is in sync with the last apply (2026-07-15T08:05:00+00:00) |

### `reset()` 🔥

**Sensitive containment operation, disabled by default.** OLAF can submit an empty bulk DAR
request, but Microsoft's Preview contract does not document deletion-by-omission. Verify the
post-state; do not claim that every role was deleted or that a platform default role cannot be
recreated. “Nobody can read” is also overbroad: workspace roles, default roles, engine/access mode,
and shortcut behavior remain relevant under the
[official access model](https://learn.microsoft.com/en-us/fabric/onelake/security/data-access-control-model#onelake-security-and-workspace-permissions).

The pre-write DAR snapshot and immutable ETag are recovery/concurrency inputs; they are separate
from the per-run workspace-isolation attestation. A backup does not make a Preview request atomic
or guarantee exact restore. OLAF creates/reads the sentinel before the first mutation and
revalidates it with the captured snapshot immediately before each sensitive write, then re-reads
and classifies the observed state. See [RUNBOOK §3c](../runbook.md#3c-recovery--break-glass-incident-procedure-no-public-replay-api).

Not reachable as a mode -- `run_mode` refuses `mode="reset"` by name, deliberately, so no pipeline
can select it by setting a string. Needs a live `FabricClient`; raises `UsageError` off-Fabric
(before anything is touched). See [RUNBOOK §3h](../runbook.md#3h-reset-and-cleanup--destructive-utilities).

Returns: a diagnostic DataFrame with one `prior_live_role_candidate` per role seen before
submission (or a synthetic `(none observed before submission)` row), `request="empty_payload"`,
`backup_path`, and `post_state_review_required=True`. These are pre-request candidates and a
review requirement, not observed platform deletions.

```python
OLAF.reset()
```

### `cleanup()` 🔥

**Sensitive containment operation, disabled by default.** It is restricted to explicitly
configured OLAF tables and paths after boundary validation. Preserve incident evidence and an
independently protected recovery copy before use.

Cleanup does not touch live DAR state, prove erasure, remove external copies, or create
cryptographic/transactional isolation. Same-lakehouse containment limits OLAF's delete scope; it
does not isolate the control plane from data-plane identities.

Not reachable as a mode -- `run_mode` refuses `mode="cleanup"` by name for the same reason as
`reset()`. Does not need a client. See [RUNBOOK
§3h](../runbook.md#3h-reset-and-cleanup--destructive-utilities).

Returns: DataFrame -- `kind` (`dropped table` / `deleted file` / `NOT REMOVED` / `LIVE ROLE LEFT
BEHIND` / `INCIDENT SENTINEL PRESERVED` / `EXPOSURE NOT REMEDIATED`), `name`. The last two rows are
emitted on every run: containment is not remediation, and the incident sentinel is deliberately kept.

```python
OLAF.cleanup()
```

---

## Deployment -- the generate → plan → apply → rollback chain

### `explain()`

Preview the roles/scopes/predicates a config **would** produce, **before** `generate` ever runs --
a dry projection over `generate`'s own resolution chain (`Catalog.canonical` → `Generate.rows` →
`Generate._build_grants` — static functions, but **not** call-free: `Catalog.canonical`'s folder
lister makes a real read-only OneLake call, see the next paragraph), reading the same config
`Deployment.short_rows` reads but stopping before the write step: no mapping row, no log row, no
live client. `members` is the RESOLVED list — a `glob:` pattern shows what it expanded to, and a
literal shows the member table's spelling rather than the config's. It flattens the four typed member-name
columns (group/user/sp/mi) into one `;`-joined column of config-authored **display names** (not
resolved objectIds).

**Same checks as `validate()` and `generate()`, different output.** All three call
`Deployment._run_validation()`: the config rules, the No-Graph member gate, and — where a client
exists — the lakehouse target guard. `explain()` reports and never gates; `validate()` returns an
envelope; `generate()` writes when clean and rejects when not.

The **one** exception is the lakehouse target guard. It resolves the declared lakehouse through the
Fabric API (`client.resolve_lakehouse`), so it needs a live client and `explain()` has none by
design; the clean-path message names that gap rather than leaving it implied. The member gate is a
plain Delta read and *is* run here — it used to be skipped, which meant a config could preview clean
and still be refused by `validate()` on an unseeded member, misleading exactly the person previewing
before a deploy.

A config whose **rules** are sound still gets its preview. Anything left after those pass is about
the environment, which says nothing about what the config would produce, so the grants are shown and
the problems reported alongside them.

Read-only is not call-free: for a config that declares **folder** scopes, `explain()` resolves the
attached workspace/lakehouse GUIDs (`Target.resolve`) and the production folder lister issues real
read-only `notebookutils.fs.ls` listings on Fabric — that listing *is* `generate`'s folder
resolution. **How many listings depends on the config** — the number of folder entries, how deep
each path runs, and any glob, which multiplies it. `Catalog.resolve_folders` is the authority on
that walk; its arithmetic is deliberately not restated here. A table/column-only config needs no
GUIDs and touches no filesystem. When the target cannot be resolved (off-Fabric, no lakehouse
pinned, or a lakehouse pinned from another workspace), a folder-scoped config gets an `error` row
naming the **target** as the cause — pointing at the notebook's lakehouse attachment rather than
prescribing one remedy, because those causes are indistinguishable at that point and do not share a
fix — reported alongside every other validation error the config carries. That row is appended by
`Generate._scope_pair`'s ordinary collect-all path, so a config can produce **more than one** of
them (`include_folders` and `exclude_folders` are resolved in separate loops; the repeat is left
undeduped on purpose, since that helper is shared machinery). A listing that fails (403, missing
path, throttle) likewise becomes an `error` row — `explain()` still never raises out of the
resolution chain.

The two are deliberately **asymmetric** about that aggregate, and the difference is one of **control
flow**, not of how much of the catalog is readable: `Catalog.canonical` builds `tables` eagerly and
merely *stores* the folders callable and a lazy per-table column view (`Catalog.LazyColumnMap`) —
neither folders nor columns are listed until `Generate.rows` first asks — so a failing listing
leaves the catalog exactly as readable as an unresolvable target does.
What differs is the exception **type**, and so whether the row loop survives. `Generate._scope_pair`
catches `ValidationError` and nothing else, so the injected lister's `ValidationError` is recorded
and every remaining config row is still validated. Anything that *escapes* `Generate.rows` — a
failing listing, but also any non-`ValidationError` defect in the pure rule checks the same guard
wraps — unwinds that loop, so the rows after it are never validated at all: their errors were never
computed and cannot be shown, and the preview collapses to a **single** `error` row. That guard is
broad by design, so its message names both possible sides rather than asserting a live-service cause
it cannot know.

**`explain()` never raises from the resolution chain, not from every step.** The config table is
read *before* the guard (it decides the empty-config early return), so a missing or unreadable
config table still raises. That gap pre-dates the folder-listing change and is unchanged by it.

**Contract:** when the config is valid **and** the resolution chain completed, returns a 6-column
DataFrame (`role_name`, `scope_path`, `permission`, `rls_condition`, `visible_columns`, `members`),
one row per role × scope grant (or empty but typed, 0 rows, for an all-inactive config). Otherwise
returns a **1-column `error` frame**, one row per error. That frame carries **two different
outcomes** and does *not* by itself mean the config would be rejected: a **blocking validation
error** (one `generate()` would hard-reject) yields it, and so does a **valid** config whose
folder listing could not be resolved — an unresolvable target, or a listing that failed. Read the
row text, not just `df.columns`, before reporting a config rejection: an infrastructure failure and
a config rejection reach the caller through the same shape. An empty/all-inactive config → an empty
but typed frame (same 6 columns, 0 rows).

Returns: DataFrame -- `role_name`, `scope_path`, `permission`, `rls_condition`, `visible_columns`,
`members` (a resolved preview); or `error` (blocking validation errors **or** a folder listing that
could not be resolved). **`df.columns` distinguishes preview from error** -- one is a 6-element
column list, the other is a 1-element list -- but only the row text distinguishes a rejected config
from an unreachable target.

```python
OLAF.explain()
```

→ Spark DataFrame (one row per role × scope grant; nothing written):

| role_name | scope_path | permission | rls_condition | visible_columns | members |
|---|---|---|---|---|---|
| SalesReaders | /Tables/sales/orders | Read | null | null | sg-sales |
| FinanceReaders | /Tables/fin/ledger | Read | region='APAC' | amount;region | example.user@example.invalid |

### `generate(rebuild=False)`

Build the mapping lock-file from the short config: resolve include/exclude, validate every rule
(collect-all), freeze the grants + write a versioned review CSV. `rebuild=False` is idempotent --
**except five cases** (the full list, with the reason behind each, is in
[modes.md](../modes.md)): never for a config carrying a `glob:` member pattern and never when the
mapping's stamped member objectIds no longer match what `onelake_security_member` resolves those
names to today (both because `config_hash` cannot see the member table it depends on); never when
the mapping's stamped workspace/lakehouse ids no longer name the attached target; never while the
member cache carries resolution errors; and never when the mapping's stamped `framework_version`
differs from the running one (one revalidating rebuild per mapping after an upgrade). Otherwise an
unchanged config is a no-op (`status="skipped"`). Maps to `mode=generate`.

Returns: DataFrame -- `mode`, `status`, `changed`, `message`, `grants`, `roles`, `warnings`, `csv`.

```python
OLAF.generate(rebuild=False)
```

### `validate()`

A zero-write dry-run of `generate()`'s full validation pipeline -- identical checks (every config
rule, the No-Graph member gate, the lakehouse target guard), **no** mapping write, **no** CSV
export, and **no** log row, not even a `rejected` one on a blocked run (`generate` always logs;
`validate` never does). A clean config returns success with the grant/role/warning counts; a
blocking error comes back with the identical collect-all error list `generate()` would produce for
that config -- never raises. Exempt from the `tenant_id`-required guard, since it stamps nothing.
Maps to `mode=validate`.

Returns: DataFrame -- `mode`, `status`, `changed` (always `False`), `message` (1-row status
summary; the fuller `grants`/`roles`/`warnings` detail is on `OLAF.last_result["data"]`).

```python
OLAF.validate()
```

### `plan()`

Diff the desired state (the mapping lock-file) against the live DAR -- read-only; this is what
unlocks `apply`. Maps to `mode=plan`.

Returns: DataFrame -- one row per changed role: `mode`, `status`, `changed`, `message`, `role`,
`action` (`create`/`update`/`omit`). `omit` means a prior-live role is absent from the configured
set; it is a request-construction candidate, not a deletion observation. No drift → one row with
`role`/`action` both `null`.

```python
OLAF.plan()
```

### `apply(keep_unmanaged=False)`

Submit the reviewed roles to the Preview bulk DAR endpoint. `keep_unmanaged` controls OLAF's
request construction; it does not create an atomic-replacement or deletion-by-omission platform
contract. Apply is disabled by default and requires an approved external-access review, an
immutable DAR snapshot and ETag, a sentinel before the first write, snapshot/sentinel
revalidation immediately before each sensitive write, and post-state verification. The per-run
workspace-isolation attestation is optional -- it is recorded as
`workspace_isolation=attested|unknown` and never enforced. Maps to `mode=apply`.

Returns: DataFrame -- `mode`, `status`, `changed`, `message`, `push_status` (the bulk PUT's HTTP
status -- *not* a count), `roles_written` (the count), `keep_unmanaged`, `request`, `backup_path`,
`omitted_role_candidates`, `drift_omission_candidates`, and `post_state_review_required`.
The candidate fields describe roles omitted from the request or the reviewed drift plan; they are
not observed platform deletions.

```python
OLAF.apply(keep_unmanaged=False)
```

### `rollback(rollback_to_version="", rollback_reason="")`

Restore `onelake_security_config` to a prior Delta version (blank = previous; a value = that exact
version) and re-run `generate` → `plan` → `apply`. `rollback_reason` is required. Maps to
`mode=rollback`.

`rollback_to_version` is forwarded to the engine **only when the call names one**, so a value set
via `OLAF.configure(rollback_to_version=...)` is honoured when the call leaves it blank; a value
passed to the call still beats the configured one. The
consequence is that a call cannot re-request "the previous version" over a configured pin -- but
that is the default, so expressing it means not configuring one. `rollback_reason` is deliberately
**not** treated the same way: it is passed unconditionally, blank included, so a configured reason
can never become sticky and stamp a stale justification onto a later, unrelated rollback -- a
reasonless call is refused instead.

Returns: DataFrame -- `mode`, `status`, `changed`, `message` (1-row status summary).

```python
OLAF.rollback(rollback_to_version="41", rollback_reason="bad RLS predicate on Orders")
```

---

## Audit -- read-only queries

`show()`/`trace()` are explicit staticmethods on `OLAF` (they route through `run_mode` because they
need the live target); every other method below is an `Audit` read method forwarded verbatim by a
metaclass `__getattr__` -- calling `OLAF.<name>(...)` is `OLAF._as_frame(getattr(trail, name)(...))`
against an `Audit` bound to the ambient spark + configured control tables, with a live
`FabricClient` lazily bound the same way the `trace` run-mode path does (`Target.resolve()` →
`FabricClient`). Off-Fabric (no client resolves), the log/mapping-only reads keep working and the
four live-DAR methods (`out_of_band`, `effective_access`, `who_can_access`, `drift`) raise
`UsageError` with a clear "needs a FabricClient" message.

`OLAF._as_frame` coerces **whatever** the underlying `Audit` method returns to the uniform
DataFrame: a method that already returns a DataFrame passes through unchanged; a `dict` is copied
into a 1-row frame; a dataclass (`verify_chain` → `ChainStatus`) is flattened via `dataclasses.asdict`;
`None` or a bare scalar (`is_stale` → `bool`) rides in a single `value` column.

### `show(by, subject="")`

Pivot the live DAR by `table`/`role`/`member`, enriched with log provenance
(`first_applied`/`first_granted_by`/`last_applied`/`last_granted_by`/`config_version`) and an
out-of-band flag. Maps to `mode=show`.

Returns: DataFrame -- the same eleven columns on every axis (`role_name`, `scope_path`,
`member`, `member_name`, `permission`, `first_applied`, `first_granted_by`, `last_applied`,
`last_granted_by`, `config_version`, `provenance`), one row per role × scope × member. Only the
ORDER is axis-dependent (`GRANT_LEAD_COLUMNS`): `by="table"` leads with `scope_path`, `by="role"`
with `role_name`, `by="member"` with `member`, `member_name` -- the remaining columns follow in
`GRANT_COLUMNS` order. No match → 1 row of `mode`, `status`, `by`, `subject`, `matches`.

```python
OLAF.show(by="table", subject="sales.orders")
```

### `trace()`

The operational snapshot (deployed generation, role/grant counts, staleness, out-of-band count
when a client resolved). Maps to `mode=trace`; the same computation as `report()` below, projected
down to scalars.

Returns: DataFrame -- `mode`, `status`, `live_role_count`, `live_grant_count`, `desired_grant_count`, `missing`, `unexpected`, `out_of_band`, `policy_checked`, `policy_mismatch`, `in_sync`, `is_stale`, `established_ever`
(1 row).

```python
OLAF.trace()
```

### Run history (audit-log queries)

Five methods reading `onelake_security_log` directly. A filter left `None` is not applied.

#### `runs(mode=None, status=None, env=None, since=None, batch_id=None)`

Every logged row matching the filters, newest `run_at` first.

Returns: DataFrame -- all 27 `onelake_security_log` columns (see
[data-model.md](../data-model.md#onelake_security_log---append-only-audit-trail)).

```python
OLAF.runs(mode="apply", env="prod", since="2026-07-01T00:00:00+00:00")
```

#### `log_history(role=None, member=None, scope=None)`

Every row touching a given subject, **oldest first** (the chronological story of that subject, as
opposed to `runs()`'s newest-first operational view).

Returns: DataFrame -- all 27 log columns.

```python
OLAF.log_history(role="SalesReaders")
```

#### `batch(batch_id)`

Every row written by one run (its `batch_id`) -- the full blast radius of a single invocation.

Returns: DataFrame -- all 27 log columns.

```python
OLAF.batch("b7f2e1a0-0000-0000-0000-000000000000")
```

#### `failures(since=None)`

Rows that are not a clean success: a non-`"success"` `status`, or a populated `error_category`.
Newest first.

Returns: DataFrame -- all 27 log columns.

```python
OLAF.failures(since="2026-07-01")
```

#### `last_run(mode=None)`

The single newest row for a mode (or overall), wrapped into a 1-row frame -- or a 1-row, 1-column
(`value=null`) frame when nothing has been logged yet.

Returns: DataFrame -- all 27 log columns (1 row), or `value` (1 row, `null`).

```python
OLAF.last_run("generate")
```

### Freshness / integrity

#### `current_generation()`

The provenance of the single row in `onelake_security_mapping`. `None` (→ a 1-row `value=null`
frame) when nothing has been generated yet.

Returns: DataFrame -- `config_hash`, `config_version`, `framework_version`, `generated_at`,
`mapping_hash`, `mapping_version` (1 row), or `value` (1 row, `null`).

```python
OLAF.current_generation()
```

#### `is_stale()`

`True` when the deployed mapping no longer matches the live active config. `Audit.is_stale()`
returns a bare `bool`; `OLAF.is_stale()` wraps it into a 1-row frame with a single column.

Returns: DataFrame -- `value` (bool, 1 row).

```python
OLAF.is_stale()
```

#### `verify_chain()`

Returns `ok=True` only when the current generation has ordered, successful, same-hash
generate → plan → apply completion records. The apply completion must carry durable backup and
payload evidence. `Audit.verify_chain()` returns a `ChainStatus` dataclass (`ok: bool`,
`details: dict`); `OLAF.verify_chain()` flattens it via `dataclasses.asdict`. `details` reports the
current hashes, per-stage row counts and state: `missing_mapping`, `generated`, `planned`,
`applied`, or `incomplete`.

Returns: DataFrame -- `ok`, `details` (the nested dict, stringified) (1 row).

```python
OLAF.verify_chain()
```

### Grant provenance

#### `grants(role=None, scope=None, member=None)`

Established DAR grants read from the log -- one row per `(role_name, scope_path, member_id)`,
deduped keeping BOTH ends of the `run_at` range and the principal who pushed each end.

Returns: DataFrame -- `role_name`, `scope_path`, `member_id`, `member_name`, `first_applied`,
`first_granted_by`, `last_applied`, `last_granted_by`, `config_version`. Display fields and
`config_version` come from the LATEST push -- the state in effect now.

```python
OLAF.grants(role="SalesReaders")
```

#### `provenance(role, scope, member=None)`

One established grant's provenance for a `(role, scope[, member])` -- the first row of `grants()`.
`Audit.provenance()` returns a `dict` or `None`; wrapped into a 1-row frame (or `value=null`).

Returns: DataFrame -- `role_name`, `scope_path`, `member_id`, `member_name`, `first_applied`,
`first_granted_by`, `last_applied`, `last_granted_by`, `config_version` (1 row), or `value`
(1 row, `null`).

```python
OLAF.provenance(role="SalesReaders", scope="/Tables/sales/orders")
```

### Compliance & live-DAR

Read-only queries that pivot the **live** DAR directly (all but `coverage` need a live
`FabricClient`, raising `UsageError` without one).

#### `out_of_band()`

Live DAR grants that have no framework provenance. `member_name` is resolved id->name from the
member cache table (`self.member_table`); an id absent from the cache surfaces as the raw id, and
an id carrying **more than one distinct name** surfaces as the marker
`<ambiguous: N names in member table>` rather than an arbitrarily-picked name.

Returns: DataFrame -- `role_name`, `scope_path`, `member_id`, `member_name`.

```python
OLAF.out_of_band()
```

#### `coverage()`

Protected vs. unprotected table surface -- the compliance gap finder. The table universe is
every real table in the lakehouse catalog (`Catalog.canonical`, the same lister `generate()` uses),
not merely the tables named in the mapping -- a table with zero mapping rows still gets a row here
(`protected=false`), surfacing what nobody configured at all. Only mapping rows whose `scope_path`
starts with `/Tables/` count (folder-scope rows are skipped). No live client needed.

Returns: DataFrame -- `table`, `protected` (bool), `roles_count`, `has_rls` (bool), `has_cls`
(bool). One row per catalog table.

```python
OLAF.coverage()
```

→ Spark DataFrame (one row per table in the catalog):

| table | protected | roles_count | has_rls | has_cls |
|---|---|---|---|---|
| sales.orders | true | 1 | true | false |
| fin.ledger | true | 1 | true | true |
| sales.staging_scratch | false | 0 | false | false |

#### `effective_access(member, table, member_type=None, *, engine)`

OLAF's modeled access summary for `table` and `member` -- one detail row per reaching role plus a
synthesized row. Microsoft documents role/RLS combination generally, while SQL-endpoint CLS uses
intersection/deny semantics. Interpret the output for the specific engine and access mode; it is
not universal authorization proof. See
[multiple-role evaluation](https://learn.microsoft.com/en-us/fabric/onelake/security/data-access-control-model#evaluating-multiple-onelake-security-roles)
and [table/column/RLS behavior](https://learn.microsoft.com/en-us/fabric/onelake/security/table-column-row-security).
No reaching role → an empty but typed frame. Needs a `FabricClient`. `engine` is required:
`spark` and `direct_lake` report CLS as a union; `sql_endpoint` reports the intersection of
explicit CLS allow-lists. An unrestricted SQL-endpoint role does not erase another explicit
restriction. The helper models DAR rules only; it does not prove endpoint identity mode or
enforcement for a request.

`member` accepts a **name/UPN** or an objectId (GUID) -- resolved via `Audit._resolve_member` before
matching the live DAR: a GUID-shaped value passes through **unchanged**; otherwise it's looked up
case-insensitively by `member_name` in the No-Graph member table (the same table `generate` resolves
from -- see [data-model.md](../data-model.md#onelake_security_member---the-no-graph-nameobjectid-table)
and the [preload guidance](../runbook.md#2b-member-resolution-table-no-graph)). A name absent from
the table raises `UsageError` naming the No-Graph limitation -- `effective_access` resolves
config-declared members, it does not expand group membership. A name matching more than one live-DAR
member is not possible here (resolution is 1:1 by objectId); [`who_can_access(table)`](#who_can_accesstable)
remains useful for discovering *which* members reach a table in the first place.

The member table's logical PK is `(member_type, lower(member_name))` -- a Group and a User named
`finance-team` are two different, legitimate principals. Pass the optional **`member_type`**
(`Group` / `User` / `ServicePrincipal` / `ManagedIdentity`) to scope resolution to one of them.
Omitted, an unambiguous name resolves exactly as before; a name matching rows of more than one type
raises `UsageError` naming the colliding types and asking for `member_type` -- it never silently
picks whichever row was read first.

```python
# a Group and a User both named "finance-team" -- say which principal you mean
OLAF.effective_access(member="finance-team", table="sales.orders", member_type="Group", engine="spark")
```

Returns: DataFrame -- `role_name`, `rls_condition`, `visible_columns`, `granting_role`,
`effective` (bool), `engine`.

```python
# member is a name/UPN (preloaded in onelake_security_member) — a GUID objectId also works unchanged
OLAF.effective_access(member="example.user@example.invalid", table="sales.orders", engine="spark")
```

→ Spark DataFrame (one row per reaching role, plus the union):

| role_name | rls_condition | visible_columns | granting_role | effective | engine |
|---|---|---|---|---|---|
| SalesReaders | region='APAC' | null | null | false | spark |
| FinanceReaders | null | null | null | false | spark |
| null | null | null | FinanceReaders;SalesReaders | true | spark |

#### `who_can_access(table)`

The reverse of `effective_access` -- every member who can reach `table`, one row per
`(member, role)` pair (a member reachable via two roles gets two rows). `member_name` is resolved
from the member cache (`self.member_table`); an objectId absent from the cache surfaces as the raw
id, and one carrying **more than one distinct name** surfaces as the marker
`<ambiguous: N names in member table>` instead of an arbitrary pick. `rls_cls_summary` is a
one-cell digest: `"rows: <condition>"` and/or `"cols: <col;col>"`, or `"unrestricted"`. No reaching role → an empty but typed frame. Needs a `FabricClient`.

Returns: DataFrame -- `member_name`, `member_id`, `via_role`, `permission`, `rls_cls_summary`.

```python
OLAF.who_can_access(table="sales.orders")
```

→ Spark DataFrame:

| member_name | member_id | via_role | permission | rls_cls_summary |
|---|---|---|---|---|
| example.user@example.invalid | 00000000…0001 | SalesReaders | Read | rows: region='APAC' |
| sg-finance-leads | c9d1e0…7f | FinanceReaders | Read | unrestricted |

#### `drift()`

The full desired-vs-live comparison, categorized and **read-only** -- a pure comparison
view: it neither records a plan row nor gates `apply` (that stays `plan()`'s job). One row per
live grant, tagged `framework` (in `grants()`'s established set), `out_of_band` (not), or
`policy` (provenanced, but the deployed `permission`/RLS/CLS differs from what the mapping
declares — `out_of_band` wins when a grant qualifies for both); plus one
row per **desired** grant (from the mapping lock-file) that is entirely absent from the live DAR,
tagged `missing`. `member_name` is resolved id->name from the member cache table (`self.member_table`,
the same lookup `out_of_band`/`who_can_access` use) for every row/category; an id absent from the
cache surfaces as the raw id, and an id carrying **more than one distinct name** surfaces as the
marker `<ambiguous: N names in member table>`. Needs a `FabricClient`.

Returns: DataFrame -- `role_name`, `scope_path`, `category` (`framework`/`out_of_band`/`policy`/`missing`),
`detail`, `member_id`, `member_name`. (`scope_path` is the live-DAR / mapping path -- the same column
name its siblings `out_of_band`/`grants`/mapping/log use.) `member_id`+`member_name` are **appended**
at the end, matching `out_of_band()`'s convention, and are kept as a **pair** for the same reason
`out_of_band`/`who_can_access`/`grants` keep it: when the two columns are equal, the id was absent
from the member cache and fell back to itself.

```python
OLAF.drift()
```

→ Spark DataFrame:

| role_name | scope_path | category | detail | member_id | member_name |
|---|---|---|---|---|---|
| SalesReaders | /Tables/sales/orders | framework | live grant matches framework provenance (...) | 00000000…0001 | example.user@example.invalid |
| FinanceReaders | /Tables/fin/ledger | out_of_band | live grant has no framework provenance (...) | c9d1e0…7f | sg-finance-leads |
| TempAuditors | /Tables/sales/orders | missing | desired grant absent from live DAR (...) | a2b3c4…5d | sg-temp-auditors |

### Generation lineage & config time-travel

#### `timeline()`

Every logged config generation as one row: `(config_version, config_hash)` grouped, with
`first_seen`/`last_seen` (min/max `run_at`) and `runs` (a count). Ordered by `config_version`.

Returns: DataFrame -- `config_version`, `config_hash`, `first_seen`, `last_seen`, `runs`.

```python
OLAF.timeline()
```

#### `authored_by(version)`

Who committed a given `onelake_security_config` Delta version, read from `DESCRIBE HISTORY` --
answers "who wrote the rule", not "who deployed it".

Returns: DataFrame -- `version`, `timestamp`, `user` (1 row), or `value` (1 row, `null`) when no
history row carries that version.

```python
OLAF.authored_by(42)
```

#### `config_at(version=None, date=None)`

The exact config rows behind a generation, via Delta time-travel. Give **exactly one** of
`version` (`VERSION AS OF`, int-coerced) or `date` (`TIMESTAMP AS OF`) -- a `ValueError` otherwise.
`version` stays positional-compatible with a plain `config_at(42)` call.

Returns: DataFrame -- the config table's own schema (`CONFIG_AUTHOR_COLUMNS`, 20 columns), as of
that version/date.

```python
OLAF.config_at(42)
OLAF.config_at(date="2026-07-01")
```

#### `table_history(table)`

Delta `DESCRIBE HISTORY` of a control table, made readable. `table` is one of
`config`/`mapping`/`log` (resolved via `_control_table` to the actual configured table name --
an unknown name raises `ValueError`). `rows` comes from the nested `operationMetrics.numOutputRows`;
`user` falls back to `operationParameters.userName` when `userName` is null.

Returns: DataFrame -- `version`, `timestamp`, `user`, `operation`, `rows`. Newest first
(`DESCRIBE HISTORY`'s own order).

```python
OLAF.table_history("config")
```

→ Spark DataFrame:

| version | timestamp | user | operation | rows |
|---|---|---|---|---|
| 42 | 2026-07-15T08:00:00+00:00 | example.user@example.invalid | UPDATE | 14 |
| 41 | 2026-07-10T09:00:00+00:00 | example.user@example.invalid | UPDATE | 13 |

#### `at(table, version=None, date=None)`

`config_at`, generalized to **any** control table -- `table` resolved via `_control_table`
first (so `at("bogus", version=1)` raises before the exactly-one guard runs), then exactly one of
`version`/`date`, same as `config_at`.

Returns: DataFrame -- the resolved table's own schema (`config` → `CONFIG_AUTHOR_COLUMNS`,
`mapping` → `MAPPING_COLUMNS` + `MAPPING_PROVENANCE_COLUMNS`, `log` → `LOG_COLUMNS`), as of that
version/date.

```python
OLAF.at("mapping", version=7)
```

→ Spark DataFrame (onelake_security_mapping's own schema, as of version 7 — abbreviated here):

| role_name | scope_path | permission | ... |
|---|---|---|---|
| SalesReaders | /Tables/sales/orders | Read | ... |

#### `config_diff(v1, v2)`

Role/scope/member changes between two config Delta versions, via two `config_at()` reads
diffed in Python. Rows are keyed by `(role_name, scope_key)`, where `scope_key` is the row's own
scope-defining columns (`include_tables`/`exclude_tables`/`include_folders`/`exclude_folders`,
joined with `|` -- a **synthesized identity key**, *not* a real path, so it is named `scope_key`
(distinct from the live `scope_path`): raw config has no literal `scope_path`, that only exists
post-`generate` on the mapping table). `added` = key in v2 only; `removed` = key in v1 only;
`changed` = key in both with at least one of the other authored columns differing -- **one row per
differing field**, so a role with 3 changed fields yields 3 rows. `added`/`removed` rows carry
`field`/`old`/`new` as `null`.

Returns: DataFrame -- `change_type` (`added`/`removed`/`changed`), `role_name`, `scope_key`,
`field`, `old`, `new`.

```python
OLAF.config_diff(41, 42)
```

→ Spark DataFrame (scope_key is the config's own include/exclude-column key, '|'-joined):

| change_type | role_name | scope_key | field | old | new |
|---|---|---|---|---|---|
| changed | SalesReaders | sales.orders | rls_condition | null | region='APAC' |
| added | TempAuditors | sales.orders | null | null | null |

#### `value_history(subject, last=None)`

How **one** role/scope's config value evolved across **every** config Delta version --
walks `table_history("config")`'s version list oldest-first, reading each via `config_at(version)`
and keeping only the row matching `subject` (via `Parse.subject_match`, against the same
`(role_name, scope)` key `config_diff` uses -- when more than one row matches in a version, the
lowest key wins deterministically). `changed` is `True` on the subject's first appearance (whether
that's the very first version or a reappearance after a gap), and on any later version where a
tracked field (the same field set `config_diff` compares) differs from the last version the
subject was present in.

Returns: DataFrame -- `config_version`, `role_name`, `scope_key` (the same synthesized
`config_diff` identity key, not a real path), plus the 15
`config_diff`-field columns (`lakehouse_name`, `permission`, `rls_condition`, `include_columns`,
`exclude_columns`, `include_group_names`, `exclude_group_names`, `include_user_names`,
`exclude_user_names`, `include_sp_names`, `exclude_sp_names`, `include_mi_names`,
`exclude_mi_names`, `active`, `notes`), plus `changed` (bool) and `window_truncated` (bool --
`True` when `last=N` genuinely cut versions off the walk, so a bounded result says so about
itself) -- 20 columns total.

```python
OLAF.value_history(subject="SalesReaders")
```

→ Spark DataFrame (20 columns total — every config_diff field column, abbreviated here):

| config_version | role_name | scope_key | permission | rls_condition | ... | changed |
|---|---|---|---|---|---|---|
| 41 | SalesReaders | sales.orders | Read | null | ... | true |
| 42 | SalesReaders | sales.orders | Read | region='APAC' | ... | true |

### Report / rollup

#### `report()`

The one-call operational snapshot behind `mode=trace`: composes every other read-only `Audit`
method into a single dict -- no new queries, no writes. Includes `out_of_band` only when a client
resolved.

Returns: DataFrame -- `current_generation`, `last_generate`, `last_apply`, `last_deployment`,
`last_deployment_mode` (nested dicts/string values), `is_stale`, `established_ever`, and the
live-state counts `live_role_count`, `live_grant_count`, `desired_grant_count`, `missing`,
`unexpected`, `out_of_band`, `policy_checked`, `policy_mismatch`, `in_sync` (1 row).

```python
OLAF.report()
```

---

## Related

- [api-reference.md](../api-reference.md) -- the OLAF-first overview, the 3 entry paths, and the
  lower-level domain classes (`Audit`/`Deployment`/`FabricClient`/`Log`) `OLAF` wraps.
- [Audit.md](Audit.md) -- the class every `OLAF` audit passthrough forwards to; construct it
  directly for use outside `OLAF` (a test, a custom script).
- [Deployment.md](Deployment.md) -- the class the `OLAF` deployment methods and `OLAF.setup()` route
  through via `run_mode`.
- [errors.md](errors.md) -- the full `OLAFError` exception hierarchy, including `UsageError`
  (raised by the four live-DAR audit utilities with no client, by `configure()` for the per-call
  `keep_unmanaged`/`rebuild`/invalid-`env` refusals, and by `reset()`/`cleanup()` off-Fabric) and
  the `TargetResolutionError` lakehouse-guard subtree.
- [`notebooks/olaf_cookbook.ipynb`](../../notebooks/olaf_cookbook.ipynb) --
  every method above as a runnable, copy-paste cell with mock output.
