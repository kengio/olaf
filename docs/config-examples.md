# Config cookbook — worked config → mapping examples

All values here are synthetic. OLAF is an independent community Preview;
these examples describe OLAF's authored/mapping behavior, not proof of service
enforcement. Platform rules and limits remain governed by the
[official platform contract](platform-contract.md).

Config is authored as **include/exclude pairs**: one row in `onelake_security_config` states a
role's scope (tables, folders), its policy (permission, RLS, CLS), and its members, using
`include_*` patterns to grant and `exclude_*` patterns to subtract. `mode=generate` resolves every
row against the live catalog and writes one grant per (role × scope) into `onelake_security_mapping`
— the lock-file that `plan`/`apply` read from.

This page is a cookbook, not a spec: each recipe below shows one authored config table and the
`onelake_security_mapping` rows it generates to. For the full column-by-column reference (types,
defaults, nullability, written-by/read-by) see **[data-model.md](data-model.md)**; for the rule
catalog behind every guard referenced here (A1, A2, C1, C3, …) see
**[architecture.md](architecture.md#rule-catalog)**.

**Width note:** config tables below show only the columns relevant to that recipe (every column
not shown is blank — **except `lakehouse_name`, which is required on every row**: these recipes omit
it for width, but each row must name the target lakehouse, and it must be the lakehouse the notebook
is attached to). Mapping tables omit `permission` (always `Read` in these examples), the
`workspace_name`/`lakehouse_name` provenance labels, and the other provenance columns every row
carries (`config_hash`, `config_version`, `generated_at`, `framework_version`) — see data-model.md for
those. Every member name used below (`sg-analysts`, `svc-etl`, …) must also be present in
`onelake_security_member` with its objectId — OLAF deliberately resolves from that
table and does not call Microsoft Graph. This is a project design choice, not a
platform impossibility claim. NotebookUtils documents its current audience surface:
[Get a token](https://learn.microsoft.com/en-us/fabric/data-engineering/notebookutils/notebookutils-credentials#get-token).
`role_name` must also be alphanumeric and start with a letter (rule C12, on top of the ≤ 124-char
ceiling, rule C6) — every `role_name` used below already fits; see
[architecture.md](architecture.md#c12) for the full format rules.

**`NOT IN` and NULL — nothing checks this for you.** A deny-list predicate silently drops rows whose
column is NULL: three-valued logic makes `col NOT IN (...)` UNKNOWN for a NULL, and `WHERE` keeps
only TRUE. The direction is over-rejection — a role sees fewer rows than intended, never more — so
it is a data-completeness bug, not an exposure one, and no rule catches it. Either write
`col NOT IN (...) OR col IS NULL`, or satisfy yourself the column is never NULL. Decide which;
do not leave it to chance. See [architecture.md](architecture.md#key-invariants).

**Autotrim:** every cell shown below is stripped of leading/trailing whitespace before `generate`/
`validate` reads it (`Parse.trim_row`), so a stray space from a copy-paste never trips a validation
error. Whitespace *inside* an `rls_condition` string literal (e.g. `Region = 'TH '`) is preserved —
only the field's own outer whitespace is stripped.

## Shared mock catalog

Every recipe below draws from the same catalog:

- **Tables:** `sales.orders`, `sales.leads`, `sales.returns`, `hr.employees`, `hr.payroll`, `ref.calendar`
- **Folders:** `/Files/raw/region_a`, `/Files/raw/region_b`, `/Files/raw/temp`
- **Members:** `sg-analysts`, `sg-contractors` (Entra security groups), `svc-etl` (service principal)

None of these examples grants a configured OLAF control table or `/Files/security`.
Any desired or live read overlap with those reserved paths blocks sensitive modes.
Do not upload a real workbook until the external access review is complete; see
[Protecting OLAF control data](control-data-security.md).

---

## Happy-path recipes

### 1. Explicit include list

*When to use:* a role needs a small, fixed set of tables — just name them.

**`onelake_security_config`** (authored)

| role_name | include_tables | include_group_names |
|---|---|---|
| RefRead | `ref.calendar;sales.leads` | `sg-analysts` |

**`onelake_security_mapping`** (after `generate`)

| role_name | scope_path | scope_type | member_group_names |
|---|---|---|---|
| RefRead | /Tables/ref/calendar | Table | sg-analysts |
| RefRead | /Tables/sales/leads | Table | sg-analysts |

**Takeaway:** one row in, one row out per table — no wildcard resolution involved, just literal
name lookups against the catalog.

### 2. Table-part wildcard (`sales.*`)

*When to use:* grant a whole schema without enumerating every table in it.

**`onelake_security_config`**

| role_name | include_tables | include_group_names |
|---|---|---|
| SalesRead | `sales.*` | `sg-analysts` |

**`onelake_security_mapping`**

| role_name | scope_path | scope_type | member_group_names |
|---|---|---|---|
| SalesRead | /Tables/sales/leads | Table | sg-analysts |
| SalesRead | /Tables/sales/orders | Table | sg-analysts |
| SalesRead | /Tables/sales/returns | Table | sg-analysts |

**Takeaway:** the wildcard is resolved against the live catalog at generate time — a new
`sales.*` table shows up in the mapping on the next generate with no config change needed. The
schema part (`sales`) stays literal; only the table part accepts `*`/`?` (rule A2).

### 3. All-except (`sales.*` minus `sales.returns`)

*When to use:* grant a schema except one table, without listing the rest.

**`onelake_security_config`**

| role_name | include_tables | exclude_tables | include_group_names |
|---|---|---|---|
| SalesRead | `sales.*` | `sales.returns` | `sg-analysts` |

**`onelake_security_mapping`**

| role_name | scope_path | scope_type | member_group_names |
|---|---|---|---|
| SalesRead | /Tables/sales/leads | Table | sg-analysts |
| SalesRead | /Tables/sales/orders | Table | sg-analysts |

**Takeaway:** the subtraction is never silent — `generate`'s per-role summary records
`SalesRead: included=2 excluded=1 warnings=0`, so the omission is visible as a count in the review
output, not just implied by absence from the mapping.

### 4. Carve-out — one role, per-table RLS via two rows

*When to use:* one role needs a stricter (or different) row-level policy on a single table,
without splitting the role in two.

Member columns must be declared identically on both rows (rule C1 — a role is one member set,
regardless of how many rows define its scope).

**`onelake_security_config`**

| role_name | include_tables | exclude_tables | rls_condition | include_group_names |
|---|---|---|---|---|
| SalesTH | `sales.*` | `sales.orders` | `Region = 'TH'` | `sg-analysts` |
| SalesTH | `sales.orders` | — | `Region = 'TH' AND Type = 'B2B'` | `sg-analysts` |

**`onelake_security_mapping`**

| role_name | scope_path | scope_type | rls_condition | member_group_names |
|---|---|---|---|---|
| SalesTH | /Tables/sales/leads | Table | Region = 'TH' | sg-analysts |
| SalesTH | /Tables/sales/returns | Table | Region = 'TH' | sg-analysts |
| SalesTH | /Tables/sales/orders | Table | Region = 'TH' AND Type = 'B2B' | sg-analysts |

`generate` prints a cross-row warning here: *`role 'SalesTH': /Tables/sales/orders excluded in row 1
but included in row 2 — carve-out or accident?`* — this is the intentional case, so it's a warning the
author reads and moves past, not a block.

**Takeaway:** exclude is **row-scoped** — row 1's exclude only pulls `sales.orders` out of *that*
row's grant, leaving row 2 free to re-grant it under a different policy. This is what makes the
carve-out lossless without ever tripping rule C3 (one policy per table per role): each table
still ends up with exactly one policy.

### 5. Folder per-segment wildcard + exclude

*When to use:* grant a family of sibling folders, holding one back.

**`onelake_security_config`**

| role_name | include_folders | exclude_folders | include_group_names |
|---|---|---|---|
| RawRead | `/Files/raw/region_*` | `/Files/raw/region_b` | `sg-analysts` |

**`onelake_security_mapping`**

| role_name | scope_path | scope_type | member_group_names |
|---|---|---|---|
| RawRead | /Files/raw/region_a | Folder | sg-analysts |

**Takeaway:** `/Files/raw/temp` is never in play — `region_*` only matches folders whose name
starts with `region_`, and folder wildcards match **within one path segment only** (no `**`).
`region_b` is resolved and then subtracted, leaving `region_a` as the sole grant.

### 6. Member exclude (include groups minus one)

*When to use:* grant access to a broad membership set, then carve one member back out.

**`onelake_security_config`**

| role_name | include_tables | include_group_names | exclude_group_names | include_sp_names |
|---|---|---|---|---|
| RefRead | `ref.*` | `sg-analysts;sg-contractors` | `sg-contractors` | `svc-etl` |

**`onelake_security_mapping`**

| role_name | scope_path | scope_type | member_group_names | member_sp_names |
|---|---|---|---|---|
| RefRead | /Tables/ref/calendar | Table | sg-analysts | svc-etl |

**Takeaway:** member exclude subtracts within its own typed column — `sg-contractors` is
subtracted from `include_group_names` only, and `svc-etl` (a different member type entirely) is
unaffected and lands in its own `member_sp_names` column. Both `sg-analysts` and `sg-contractors`
must also exist in `onelake_security_member` — the No-Graph gate checks every name a row
declares (include and exclude alike), not only the survivors that reach the mapping.

### 7a. CLS blacklist — hide the sensitive columns

*When to use:* most columns on a table are fine to expose; a few need hiding.

`hr.payroll` catalog columns: `employee_id, name, department, salary, bank_account`.

**`onelake_security_config`**

| role_name | include_tables | exclude_columns | include_group_names |
|---|---|---|---|
| HrRead | `hr.payroll` | `salary;bank_account` | `sg-analysts` |

**`onelake_security_mapping`**

| role_name | scope_path | scope_type | visible_columns | member_group_names |
|---|---|---|---|---|
| HrRead | /Tables/hr/payroll | Table | employee_id;name;department | sg-analysts |

**Takeaway:** `visible_columns` holds the allow-list OLAF places in the DAR request,
not the authored exclusions. In blacklist mode, a newly discovered column enters the
allow-list on the next generate unless explicitly excluded; until regeneration, it is
absent from OLAF's saved payload. Actual engine enforcement is outside this mapping
example and differs by access path. See Microsoft's
[CLS semantics](https://learn.microsoft.com/en-us/fabric/onelake/security/data-access-control-model#column-level-security).

### 7b. CLS whitelist — show only what's listed

*When to use:* the opposite default — new columns should stay hidden until someone explicitly
lists them.

**`onelake_security_config`**

| role_name | include_tables | include_columns | include_group_names |
|---|---|---|---|
| HrRead | `hr.payroll` | `employee_id;name;department` | `sg-analysts` |

**`onelake_security_mapping`**

| role_name | scope_path | scope_type | visible_columns | member_group_names |
|---|---|---|---|---|
| HrRead | /Tables/hr/payroll | Table | employee_id;name;department | sg-analysts |

**Takeaway:** `generate` computes `visible_columns` as `include_columns` directly (the columns that
exist on the table), so today's result matches 7a's resolved allow-list exactly. The difference
only shows up on a later generation: whitelist mode does not add a new column until
the author explicitly includes it (unlike 7a's blacklist, where regeneration adds a
new non-excluded column to OLAF's allow-list). Setting both `include_columns` and
`exclude_columns` on the same row is an error (E12 below) — pick one CLS mode per row.

### 7c. RLS + CLS for one member — one role, never two (rule C5)

*When to use:* a member needs both row filtering **and** column hiding on the data they can reach.

OneLake security fails a user's queries when they reach data through one role carrying RLS and a
*different* role carrying CLS. Microsoft's
[combine section](https://learn.microsoft.com/en-us/fabric/onelake/security/table-column-row-security#combine-row-level-and-column-level-security) says
"the two policies have to be applied using a single OneLake security role", and that users hitting
"an unsupported role combination receive query errors".

> [!NOTE]
> **OLAF is deliberately stricter than Microsoft documents here.**
> The [access-control model](https://learn.microsoft.com/en-us/fabric/onelake/security/data-access-control-model#evaluating-multiple-onelake-security-roles)
> states the unsupported case more narrowly — *"The same user in two or more roles with **different
> allowed columns** isn't supported if any of the roles also has an RLS statement"* — and its example
> (`Role1` allows `c1`,`c2` with RLS; `Role2` allows `c2`,`c3`) is **one table**. C5 fires across
> tables too. Nothing in Microsoft's text says a cross-table pairing breaks, and nothing says it is
> safe; treating it as unsafe is OLAF's fail-closed design choice, not a quotation or
> a live-release result. Expect C5 to reject some configurations that the platform
> might accept. See [roadmap.md](roadmap.md#catching-the-rls--cls-cross-grant-collision-at-generate).

**Fails (E15 below)** — the reserved synthetic user
`example.user@example.invalid` is in an RLS role and a separate CLS role:
[`.invalid` is reserved for examples](https://www.rfc-editor.org/rfc/rfc6761.html#section-6.4).

| role_name | include_tables | rls_condition | exclude_columns | include_user_names |
|---|---|---|---|---|
| SalesAnalysts | `sales.orders` | `region = 'TH'` | — | `example.user@example.invalid` |
| HRViewers | `hr.payroll` | — | `salary` | `example.user@example.invalid` |

`generate` blocks: *`member 'example.user@example.invalid' is in role(s) ['SalesAnalysts'] carrying RLS and
role(s) ['HRViewers'] carrying CLS — OneLake does not support mixing RLS and CLS across roles for
one user (queries fail); combine both policies into a single role or remove the member from one side
(rule C5)`*.

**The preferred authoring fix — one role carries both policies:**

| role_name | include_tables | rls_condition | exclude_columns | include_user_names |
|---|---|---|---|---|
| SalesHrView | `sales.orders` | `region = 'TH'` | — | `example.user@example.invalid` |
| SalesHrView | `hr.payroll` | — | `salary` | `example.user@example.invalid` |

**Takeaway:** RLS and CLS combine freely **within** one role — a single role holding both never
trips C5. The rule only fires when the two policies are split across *different* roles the same
member is in; the remedy is always to fold them into one role (or drop the member from one side).

**What C5 catches — and what it can't.** The check compares **config members, including two spellings
that resolve to one objectId via the member table** (e.g. a user's UPN in the RLS role and that user's
mail alias in the CLS role — same person, one objectId, still blocked). It does **not** expand group
membership: OLAF does not call Graph, so a user who is inside a *group* on one side and
named directly (or via another group) on the other side is **not** caught at generate — the two sides
share no config member and no resolved objectId. OLAF does not infer the effective
group-mediated result; Microsoft's cited text does not define this exact case.
Keep RLS + CLS in one role per persona, and avoid mixing group-granted and user-granted RLS/CLS roles
for the same people.

### 7d. RLS/CLS column case must match the Delta schema exactly (rule C11)

*When to use:* always — especially for a mixed-case column such as `RecordTypeId`.

OLAF requires exact Delta-schema spelling for both RLS and CLS references. This is a
fail-closed authoring guard, not a live service claim. Current Microsoft guidance says
invalid or case-mismatched RLS can return no rows or SQL query errors; it does not
support a public fail-open assertion:
[RLS syntax](https://learn.microsoft.com/en-us/fabric/onelake/security/row-level-security-syntax).

**Fails (E16 below)** — the schema column is `RecordTypeId`, config writes `recordtypeid`:

| role_name | include_tables | rls_condition | include_group_names |
|---|---|---|---|
| SalesReaders | `sales.orders` | `recordtypeid = 'Invoice'` | `sg-analysts` |

`generate` blocks with a C11 exact-case error that names the expected column spelling.
A wrong-case `include_columns`/`exclude_columns` entry blocks through the corresponding
CLS authoring check.

**The fix — spell the column exactly as the schema does:**

| role_name | include_tables | rls_condition | include_group_names |
|---|---|---|---|
| SalesReaders | `sales.orders` | `RecordTypeId = 'Invoice'` | `sg-analysts` |

**Takeaway:** a case-insensitive existence check only confirms that a near-match is
present. C11 requires exact spelling before request construction. Actual query behavior
remains engine/access-mode specific and must not be inferred from this guard.

---

## Validation catch table

`generate` validates every row against the catalog before writing `onelake_security_mapping` — it
blocks on any error (nothing is written) and prints every warning for the author to review. These
are the guardrails behind the recipes above.

| # | Offending config | Rule | Outcome |
|---|---|---|---|
| E1 | `exclude_tables: sales.returns` with blank `include_tables` | pairing rule | ❌ error — exclude without include has nothing to subtract from (there is no `ALL` keyword) |
| E2 | a row declares members but no `include_tables`/`include_folders` | A1 | ❌ error — a row must grant at least one table or folder; an exclude alone is not a grant |
| E3 | `include_tables: *.orders` | A2 | ❌ error — schema part must be literal; wildcards are table-part only |
| E4 | `include_tables: /Files/raw` | entry format | ❌ error — looks like a folder path, did you mean `include_folders`? (column-redirect message; symmetric for a `/Tables/` entry in a folders column) |
| E5 | row 1 `include_group_names: sg-analysts`, row 2 `sg-analysts;sg-contractors` (same role) | C1 | ❌ error — member columns must be identical on every row of a role; unify the lists or split the roles |
| E6 | row 1 `sales.*` with RLS `Region='TH'` (no exclude), row 2 `sales.orders` with a different RLS | C3 | ❌ error — same table receives two different policies from one role (contrast with recipe 4, where row 1 excludes `sales.orders` first) |
| E7 | `include_tables: sales.orders` + `exclude_tables: sales.orders` | empty-after-subtract | ❌ error — row grants nothing after exclusion |
| E8 | `include_tables: sales.*` + `rls_condition: Region='TH'`, but `sales.returns` has no `Region` column | column-existence | ❌ error — RLS/CLS column missing in one of the resolved tables |
| E9 | `exclude_tables: sales.tmp_load` (not in the catalog) | exclude 0-match | 🔴 error — the exclusion removes nothing, so everything it was meant to take out stays granted; the message names the near miss (rule A2) |
| E10 | `include_folders: /Files/raw` + `exclude_folders: /Files/raw/temp` | subtree carve | ⚠️ warning — OneLake folder grants inherit subtree-wide with no DENY roles; the parent grant still exposes `temp`, exclude cannot carve out a subtree |
| E11 | includes resolving to 600 tables for one role | platform limit | ❌ error — paths exceed the 500-per-role platform limit; fails at generate, not at apply (shard the role) |
| E12 | `include_columns` and `exclude_columns` both set on one row | CLS mode | ❌ error — ambiguous; pick one CLS mode per row (whitelist or blacklist) |
| E13 | `include_group_names: sg-analysts` **or** `exclude_group_names: sg-contractors` naming a principal absent from `onelake_security_member` (the gate checks every name a row declares, include and exclude alike) | member preload gate | ❌ error — member '(name)' (Group) not found in `onelake_security_member`; add and verify it before generate. OLAF deliberately has no Graph lookup path. |
| E14 | `lakehouse_name` blank, or naming a lakehouse the notebook is **not** attached to | C2 (lakehouse target guard) | ❌ error — `lakehouse_name` is required and must name the attached lakehouse (also: not-found / ambiguous / more-than-one-lakehouse-per-config) |
| E15 | one member in an RLS-bearing role **and** a different CLS-bearing role (recipe 7c) | C5 | ❌ error — OneLake fails queries for a member mixing RLS and CLS across roles; combine both policies into one role, or remove the member from one side (a single role carrying both is fine) |
| E16 | `rls_condition: recordtypeid='Invoice'`, but the schema column is `RecordTypeId` (recipe 7d) | C11 | ❌ error — the column has only a case-insensitive near-match; write the exact Delta-schema spelling. Current Microsoft guidance says invalid/mismatched RLS can return no rows or query errors; OLAF makes no fail-open claim. |

| E17 | `include_group_names: glob:sg-ghost-*` (or the same in an `exclude_*` column) matching no `Group` in `onelake_security_member` | C15 (member pattern) | ❌ error — the pattern matched 0 members of its own `member_type`; it is named and dropped from the cell. Both directions error, unlike a dead *literal* exclusion, which is accepted — for a literal the must-exist-in-the-member-table requirement already IS the 0-match check |

E1–E9 and E12–E17 block `generate` outright (nothing is written to `onelake_security_mapping`);
**E10 alone** is a warning printed alongside a successful generate — a 0-match exclusion (E9) is
always an error, never a warning. E3–E4 share one family of entry-format
checks (literal-schema + column-redirect); E11's platform ceiling and its remedies (sharding,
consolidating members into groups) are covered in runbook.md. `generate` is **collect-all** — it
aggregates every error above (catalog validation **plus** the No-Graph member gate E13, the
lakehouse target guard E14, the cross-role RLS×CLS guard E15, and the column-case guard E16) and
rejects once with the full list, so you fix them in a single pass.

The numeric platform-limit example is an OLAF compatibility snapshot reviewed
on 2026-08-22, not a permanent service guarantee. Verify Microsoft's current
[OneLake security limitations](https://learn.microsoft.com/en-us/fabric/onelake/security/data-access-control-model#onelake-security-limitations)
before operation. Folder and permission behavior likewise follows the current
[permissions and supported items](https://learn.microsoft.com/en-us/fabric/onelake/security/data-access-control-model#permissions-and-supported-items)
contract.
