# Glossary

The words this framework uses in a specific sense. If a doc reads oddly, the term is probably here.

Back to the [documentation map](README.md).

## The unit everything counts

### grant

**One role granting one permission on one scope to one member set.** It is the thing `generate`
produces, `plan` diffs, `apply` pushes, and `show` reads back — and it is exactly **one row of
`olaf.onelake_security_mapping`**.

```
role SalesReaders  ·  Read  ·  /Tables/sales/orders  ·  rows where Region='TH'  ·  sg-sales
└───────────────────────────── one grant ─────────────────────────────────────────────────┘
```

So `generate: 12 grants across 3 role(s)` means: three roles, expanded into twelve role×scope rows.
A role covering four tables contributes four grants — the count is **not** a role count, and that is
the point of counting it.

A grant is one idea in **three states**, which is why the word shows up on all three sides:

| state | where it lives | who reads it |
|---|---|---|
| **desired** | `onelake_security_mapping` | `plan`, `apply` |
| **recorded** | `onelake_security_log` | `Audit.grants()`, `provenance()`, `authored_by()` |
| **live** | the Fabric DAR | `show`, `Audit.out_of_band()`, `OLAF.drift()` |

> **`grants` is a count in some envelopes and a list in others** — a count from `generate` and
> `validate`, the live records from `show`. `data` keys are per mode; `roles` behaves the same way.

## The pieces of a grant

| term | means |
|---|---|
| **role** | a named bundle of grants. The unit Fabric actually stores in the DAR — OLAF authors it, the platform enforces it. |
| **scope** | *what* is being granted: a table (`/Tables/schema/name`) or a folder (`/Files/...`). Authored as `include_tables` / `include_folders`, resolved to a `scope_path`. |
| **member** | *who* the grant is for — an Entra group, user, service principal or managed identity. Authored as a **display name**, resolved to an objectId. |
| **permission** | *what they may do*: `Read` or `ReadWrite` (a `ReadWrite` row may not carry RLS/CLS — rule B3). |
| **RLS** | row-level security — a SQL predicate that hides rows (`Region='TH'`). |
| **CLS** | column-level security — the visible-column allowlist that hides columns. |

## The tables

| term | means |
|---|---|
| **config** | `onelake_security_config` — what a **human authors**. Short, pattern-based (`sales.*`), names not ids. The intended source of truth ([RUNBOOK §3a](runbook.md#3a-operating-policy--config-is-the-intended-source-of-truth)). |
| **mapping** | `onelake_security_mapping` — what `generate` **produces** from the config: every pattern expanded, every name resolved to an objectId, the target frozen. A lock-file, not an edit surface. |
| **log** | `onelake_security_log` — append-only audit trail. One row per step, plus run-level rows. |
| **member table** | `onelake_security_member` — the name→objectId table `generate` resolves from. Preloaded by you; the **only** resolution source. |

## How a run behaves

| term | means |
|---|---|
| **generation** | one `generate` run's output — the whole mapping at a point in time, identified by `config_hash` + `config_version`. |
| **No-Graph** | OLAF deliberately resolves member names from the member table and does not call Microsoft Graph. This is an OLAF design choice, not a claim that Graph tokens are unavailable in Fabric. |
| **DAR** | OneLake data access roles — the Microsoft Fabric REST surface OLAF reads and, when explicitly enabled, updates. The bulk update endpoint is Preview and is documented for evaluation/development, not production use. |
| **drift** | live ≠ desired. `plan` reports it; `apply` resolves it. |
| **out-of-band** | a live grant with no framework provenance behind it — someone edited the portal. |
| **envelope** | the single result object every mode returns: `{mode, status, changed, message, params, data, error, batch_id, run_id, config_hash}`. |
| **step** | one `(role × scope × member × action)` log row — single-valued, so lists never reach the log. |
| **apply request** | a reviewed bulk PUT request assembled from the mapping. Microsoft documents which roles are created or updated, but does not publish atomic-replacement or deletion-by-omission semantics. Inspect the post-state. |
| **reset / cleanup** | sensitive containment utilities. They are disabled by default and require the same external-access review, immutable DAR snapshot/ETag, and sentinel gate as other mutating operations; the per-run isolation attestation is optional — recorded, never enforced, and reported as `unknown` when absent. Cleanup contains only the configured OLAF paths; it is not proof of erasure or isolation. [RUNBOOK §3h](runbook.md#3h-reset-and-cleanup--destructive-utilities). |

Platform semantics and canonical Microsoft sources are maintained in
[platform-contract.md](platform-contract.md).
| **provenance** | the chain that answers *who deployed this, when, from which config*: `config_hash` → `mapping_hash` → `framework_version` → `run_by` / `run_at`. |
