# Log

Back to [API index](../api-reference.md) - [docs](../README.md).

**Audience:** internal -- one instance is constructed per run by
[Deployment](Deployment.md) and threaded through every mode. Reach
for this class directly only for a custom pipeline step that needs a hand-written audit row; for
querying what's already been logged, use [Audit](Audit.md) instead.

Writes/reads `onelake_security_log`. Remembers the run context (batch/run/env/target labels +
`run_by`, member-labelled via `resolve_principal` + config/mapping provenance) once at construction, so every row carries it without re-passing
arguments per call. Stamps `run_duration` on the `complete` row and `error_category` on failure
rows.

## resolve_principal(spark, member_table, value)

A `@staticmethod` -- call it on the class. Label a GUID-shaped `run_by` with the display name `onelake_security_member` gives that object id:
returns `"<member_name> (<objectId>)"` on a **unique** match, and `value` unchanged in every other
case. Read-only and exception-safe -- a display detail on a log row must never be able to fail the
run that writes it.

The name is **appended, never substituted**. The object id is the only part of `run_by` the runtime
attests, while `member_table` is a pipeline-overridable parameter over a schema anyone with write
access can add rows to; substituting would let one planted member row make every subsequent log row
read as an arbitrary UPN, indistinguishable from a genuinely authenticated interactive run. Listing
a deploy identity is therefore **optional enrichment** -- a member row can add a label, it can never
replace an identity. (A member row grants nothing on its own: grants come from the config's eight
`include_*`/`exclude_*_names` columns — unless a `glob:` pattern matches the row, in which case
adding it grants it with no config change.)

The value is returned unchanged when it is empty, when `member_table` is `None`, when it is not
GUID-shaped (a UPN is already a name), when no row carries that id, when the matching row's
`member_name` is null/blank, when the id resolves to **more than one** name (picking either would
make `run_by` depend on row order), or when the member table cannot be read at all (`setup` runs
before it exists). Matching is on the objectId **alone** -- it is unique across principal types in
Entra, and the runtime could not supply a `member_type` anyway.

Returns: str -- `"<member_name> (<objectId>)"`, or `value` unchanged.

## set_config_provenance(config_hash_value, config_version_value)

Stamp the config generation identity onto every subsequent row -- `config_hash`/`config_version`
are first-class log columns, not message JSON. Call once per run before any row is written.

Returns: `self` (chainable).

## set_mapping_provenance(mapping_hash_value, mapping_version_value)

Stamp the mapping-generation identity onto every subsequent row (mirrors
`set_config_provenance`) -- the config -> mapping -> run provenance chain. Call once per run,
after the mapping is written (`generate`) or read (`plan`/`apply`).

Returns: `self` (chainable).

## row(action, status, **fields)

Build one log row: every `COLUMNS` key defaulted to `None`, then the run context, then `run_at`
(UTC ISO-8601), `action`, `status`, then any extra `fields` override the defaults. Every other
method on this class is a thin wrapper over `row()`.

Returns: dict.

## fail_row(action, exc, **fields)

A `row()` with `status="failed"` and `error_category` set via `OLAFError.classify(exc)` (see
[functions.md](functions.md)).

Returns: dict.

## run_header_rows(grants)

One `start` row plus one `validate` row per single-valued role x scope x member grain. `config_hash` /
`config_version` ride as first-class columns (set via `set_config_provenance`), so the `start` row
carries no message.

Returns: list[dict].

## action_rows(plan, executed_message, omit_message, omit_status)

One row per planned role action from a `DAR.diff` plan dict. For an `omit` plan entry, the row
uses `action="omission_candidate"` and the supplied `omit_status`; it records a role omitted from
OLAF's request construction, not a platform deletion. Confirm the post-state from a fresh DAR read.

Returns: list[dict].

## complete_row(plan, **extra)

The `complete` row for `plan`/`apply`: `run_duration` plus the plan dict and operation `extra` as
message JSON. Apply records `omitted_role_candidates`, `drift_omission_candidates`, and
`post_state_review_required` where applicable; those are candidate/review fields, not deletion
observations. `find_plan_record` reads the saved plan back from this row.

Returns: dict.

## run_complete(message, **extra)

The `complete` row for `setup`/`generate`: same `run_duration` stamp as `complete_row`, but a
versioned JSON message carrying a human `summary` string
(`{"schema":1,"operation":...,"summary":...}`) in place of `complete_row`'s `plan` key. Unlike
`complete_row`, the `extra` kwargs are also stamped as first-class row fields.

Returns: dict.

## batch_token (property)

This run's `batch_id` reduced to a filename-safe token -- the per-invocation component of `apply`'s
role-backup filename (see `Deployment._backup_live_roles`). `run_mode` mints a fresh `uuid4` batch
per call unless a pipeline passes its own run id, so two applies landing in the same clock second
still name different files. Truncated for legibility, **not** for uniqueness -- uniqueness is the
exclusive-create claim's job, never this token's.

Returns: str.

## write(rows)

Append rows to the log table with an explicit schema derived from `TableSchema.frame_schema` --
`run_at` a real `TIMESTAMP`, `config_version`/`mapping_version` real `BIGINT`, everything else
`STRING` (Spark cannot infer a type for a column that is `None` in every row, e.g. `member_name` on
a `start` row) -- so an appended frame whose types disagreed with the table's would be refused by
Delta. No-op on an empty list.

## find_plan_record(config_hash_value, mapping_hash_value)

The latest successful `plan` `complete` row for this `config_hash` **and** `mapping_hash` -- the
saved-plan gate `apply` checks before writing. Matches on the first-class `config_hash`
and `mapping_hash` columns, not by parsing message JSON. The `mapping_hash` match binds the plan to
the exact mapping generation it reviewed: `config_hash` fingerprints the config rows only, so a
mapping regenerated from the same rows with different member/scope resolution (an edited objectId in
`onelake_security_member`) carries the same `config_hash` but a new `mapping_hash`, and the old plan
must not unlock an apply of content nobody reviewed. A row with no stamped `mapping_hash` never
matches -- SQL `NULL` compares unknown -- so an unbindable plan fails closed to a re-plan (every
framework release stamps `mapping_hash` on plan rows, so such a row is externally written or
hand-edited, not a past release's). Accepts a `plan` **or** `rollback` mode row (`mode IN ('plan',
'rollback')`): a rollback stamps its whole chain `mode=rollback`, so the inline plan it writes must
still satisfy this gate for the `apply()` step of that same chain.

Returns: dict | None -- the parsed message (carries the plan for the drift check); `None` when no
such plan exists (including when the log table doesn't exist yet).

## has_run_complete(mapping_hash_value)

True when a successful generate-side `complete` row for this mapping generation exists --
the completion record generate's self-healing skip verifies (a prior run may have committed
the mapping, then died before its audit rows landed). Matches the first-class `mapping_hash`
column; `mode IN ('generate', 'rollback')`, because a rollback chain's generate stamps its rows
`mode=rollback`. `False` on any read failure -- the repair row is then written, the safe
direction for an audit trail (a duplicate completion record is noise; a missing one is a hole).

Returns: bool.

## grant_provenance()

Establishing-grant provenance per `(role_name, scope_path, member_id)`, read from the log, scoped
to this instance's `env` -- powers `show`'s audit enrichment. Restricted to rows whose mode
actually pushed the grant -- `apply`, including the apply leg of a rollback chain (stamped
`mode=rollback`, matching the plan-record loader's precedent; a `plan` dry run never deploys, and
a failed apply re-stamps its rows, so a broken push establishes nothing) -- BOTH ends of the
`run_at` range are kept per key, each with the principal who pushed it.

Returns: dict -- `{(role_name, scope_path, member_id): {first_applied, first_granted_by,
last_applied, last_granted_by, config_version, member_name}}`, lowercased keys. `config_version`
and `member_name` follow the LATEST push -- the state in effect now. Each `*_granted_by` is the
log row's `run_by` verbatim, so on a pipeline
run it reads `name (objectId)` (or the bare object id when unlabelled); the parenthesised id is the
attested part, the name is an operator-supplied label. `{}` when the log is unavailable or empty (everything then reads
as out-of-band -- an honest signal, not an error).
