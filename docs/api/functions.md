# Helper methods

Back to [API index](../api-reference.md) - [docs](../README.md).

**Audience:** mixed -- these are the pure, reusable helpers worth calling directly (in a notebook
cell, a script, or a test) rather than going through `Deployment`/`OLAF`. They have no side effects
beyond what's noted.

They are **static methods on classes**, not bare module-level functions -- call them as written
here. Most of those classes are pure namespaces (`Hash`, `Parse`, `ScopePath`, `Target`, `DAR`,
`Catalog`). `OLAFError` is not: it is the framework's **exception base class**
(`class OLAFError(Exception)`, the parent of `ValidationError`, `DARHTTPError` and `UsageError` --
see [errors.md](errors.md)) that additionally hosts the `classify` static method below.

| Helper | Purpose |
|---|---|
| `Hash.config(rows)` | Stable content fingerprint of the short config rows -- the staleness-guard comparison key. Same content -> same hash. |
| `Hash.mapping_content(rows)` | Order-independent content fingerprint of the mapping lock-file (its `MAPPING_COLUMNS` only, excluding per-generation provenance). Logged as `mapping_hash`. |
| `Catalog.config_version(spark, config_table)` | Delta commit version of the config table, captured at `generate` time via `DESCRIBE HISTORY`. `None` if unavailable. |
| `Parse.list(cell)` | Split a `;`-separated config cell: trim, drop empties, dedupe case-insensitively, tolerate a trailing `;`. |
| `Parse.trim_row(row)` | Strip leading/trailing whitespace from every `STRING` value in a config/member row dict, at the read seam -- before validation/resolution runs. Only the field's own outer whitespace is stripped; content inside an `rls_condition` string literal is untouched. Non-string values (e.g. the `active` boolean) pass through unchanged. |
| `Parse.bool_param(name, value)` | Strictly parse a boolean notebook parameter -> `(parsed, error)`: a real `bool` or the int `0`/`1` passes through; a string is matched case-insensitively against `BOOL_PARAM_SPELLINGS` (`true`/`false`, `1`/`0`, `yes`/`no`); a missing key, a blank string, or `None` -- three spellings of "no value was given" -- all parse to `False`; anything else returns an error instead of falling into the `bool()` coercion trap (`bool("false")` is `True`). |
| `Parse.env_param(value)` | Validate the OPTIONAL `env` label -> `(value, error)`: blank (or a missing Base parameter) is valid and means "no environment label"; a value that IS given must match `^[A-Za-z0-9_-]{1,64}$` (1-64 chars of letters/digits/`_`/`-`); anything else is refused, never repaired -- `env` is stamped on every audit row and read back as a SQL `WHERE` literal, so a value that could close that literal must be rejected outright. The one rule shared by `run_mode`'s parameter parse (`SystemExit`) and `OLAF.configure()` (`UsageError`). |
| `DAR.paths_and_members(role)` | A DAR role -> `(paths, member objectIds)` -- the two axes `show` pivots on. |
| `DAR.path_permissions(role)` | A DAR role -> `{scope_path: permission}`, the effective Read/ReadWrite per path. |
| `DAR.row_predicate(role, path)` | A DAR role -> the `rls_condition` it puts on one table path, or `None` when the role carries no RLS there (open). Returns the FIRST matching constraint. |
| `DAR.column_allowlist(role, path)` | A DAR role -> the CLS visible-column allow-list on one table path, or `None` for no CLS (every column). |
| `DAR.strip_predicate(path, value)` | A stored RLS constraint value -> the bare WHERE-clause condition inside it, reversing `RLS.to_predicate`'s wrapping; returns the value unchanged when the prefix is absent. The one formula `row_predicate` and `Audit._live_policies` share. |
| `Parse.subject_match(subject, *candidates)` | `show`'s matcher: glob (`fnmatch`) when `subject` has wildcards, else case-insensitive **equality** — these identifiers nest, so a substring match answered about principals and tables the caller had not named. |
| `ScopePath.table(table)` | `'schema.table'` -> `'/Tables/schema/table'` (the path form the DAR API uses). |
| `ScopePath.to_table(path)` | `'/Tables/schema/table'` -> `'schema.table'` (inverse of `ScopePath.table`). |
| `ScopePath.folder(folder)` | Normalize one folder entry to a canonical `/Files/...` path; rejects a table-shaped entry. |
| `Target.resolve()` | The attached workspace id + default lakehouse item id, from `notebookutils.runtime.context`. No params -- the framework always deploys to the attached lakehouse; raises a friendly "no lakehouse attached" `SystemExit` when none is pinned. |
| `Target.tenant(tenant_id=None)` | Explicit `tenant_id` wins; else best-effort auto-resolve from the runtime context; `None` if neither is available. |
| `Target.run_by(spark=None)` | Who is running this: runtime-context `userName` (an interactive user's UPN), else runtime-context `userId` (the running principal's Entra **object id** -- the layer that makes a service-principal / workspace-identity pipeline run attributable at all), else Spark `current_user()`, else `None`. Exception-safe. `Log` then labels a GUID-shaped result via [`Log.resolve_principal`](Log.md#resolve_principalspark-member_table-value) -- the id is never replaced. |
| `OLAFError.classify(exc)` | Map an exception to the audit `error_category` vocabulary: `http` \| `validation` \| `guard` \| `unexpected`. |

The rest of `notebooks/olaf.ipynb` -- the `generate`/`plan`/`apply` pipeline steps
(`Generate.rows`, `Catalog.canonical`, `DAR.diff`, `DAR.merge_upsert`/`DAR.merge_replace`,
`Member.resolve_ids`, `DAR.build_desired`, `DAR.to_role`, `Generate.to_log_grants`,
`TableSchema.definitions`/`TableSchema.ddl_type`, `Parse.table_entry`,
`Catalog.resolve_tables`/`Catalog.resolve_folders`, `Target._single_named`, the
`RLS.to_predicate`/`RLS.referenced_columns` pair) plus the runtime helper `Target.run_id` -- is
internal, wired together by
[Deployment](Deployment.md) and the runtime entrypoint (`run_mode`/
`run_and_exit`, the ▶️ Run dispatch cell), not meant to be
called standalone. See [architecture.md](../architecture.md) for how they fit together.

Two internal resolution helpers worth naming:

- `Member.resolve_ids(grants, cache, rows)` -- the **No-Graph** member gate: resolves each config
  member (`member_type`, display name) to an objectId from the `onelake_security_member` table only.
  `rows` is required (not defaulted): the gate checks every name a config row DECLARES across all
  eight member columns, not just the effective set that survives include/exclude subtraction --
  otherwise an exclude value, or an include value its own exclude cancels, would never be checked.
  A name absent from the cache, or a value that is already a GUID, produces a collect-all
  error in either pass. The **case-collision** error (two names of the same type differing only by
  case are different principals) comes from the effective pass alone -- the declared-name set is
  keyed on the lowered name, so it collapses case variants by construction and cannot report them.
  Returns `errors` -- a plain list, not a tuple; the resolved ids are never returned. When it is
  non-empty the caller must NOT write the mapping (all-or-nothing: no partial or stale ids ever land).
  On a clean name gate the ids are written IN PLACE onto each grant's four `member_*_ids` columns,
  positionally aligned with the matching `member_*_names` column -- the resolved-id cross-mix check
  runs after that in-place write, so it can still append errors that block the caller's write.
- `Target._single_named(items, name, kind, workspace_id)` -- resolves a display name to exactly one
  workspace item **case-insensitively**, returning its canonical `(displayName, id)`; 0 matches raise
  `TargetNotFound`, >1 case-variant spelling raises `TargetAmbiguous`. Backs
  `FabricClient.resolve_lakehouse` (the lakehouse target guard).
