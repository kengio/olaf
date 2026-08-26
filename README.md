<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/brand/olaf-banner-dark.png">
    <img src="assets/brand/olaf-banner-light.png" alt="OLAF — OneLake Access Framework. Arctic owl shield mark and wordmark." width="620">
  </picture>
</p>

<p align="center">
  Review every access change. Apply only what you approved.
</p>

<p align="center">
  <a href="https://github.com/kengio/olaf/actions/workflows/test.yml"><img src="https://github.com/kengio/olaf/actions/workflows/test.yml/badge.svg" alt="tests"></a>
  <a href="docs/testing.md"><img src="https://img.shields.io/badge/coverage-100%25%20(fixtures)-1E3A8A" alt="Coverage: 100% against in-memory fixtures"></a>
  <a href="https://github.com/kengio/olaf/tags"><img src="https://img.shields.io/github/v/tag/kengio/olaf?label=release&color=1E3A8A" alt="Latest release tag"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-1E3A8A" alt="License: MIT"></a>
  <img src="https://img.shields.io/badge/status-community%20Preview-FB923C" alt="Status: community Preview">
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-0EA5E9" alt="Python 3.11, 3.12 and 3.13">
  <img src="https://img.shields.io/badge/Fabric-Runtime%201.3%20%2F%20Spark%203.5%2B-0EA5E9" alt="Microsoft Fabric Runtime 1.3 / Spark 3.5 or newer">
  <img src="https://img.shields.io/badge/dependencies-hash--pinned-0EA5E9" alt="Dependencies installed with --require-hashes">
  <img src="https://img.shields.io/badge/lint-ruff-0EA5E9" alt="Linted and formatted with ruff">
</p>

> [!IMPORTANT]
> **An independent community Preview for evaluation and development, not a production-ready
> security product.** Its mutating path uses Microsoft's bulk Data Access Roles `PUT`, which
> Microsoft documents as [Preview and not recommended for production](https://learn.microsoft.com/en-us/rest/api/fabric/core/onelake-data-access-security/create-or-update-data-access-roles).

**OLAF — OneLake Access Framework** is a plan → review → apply workflow for
Microsoft Fabric OneLake data access roles. It is one self-contained Fabric
notebook with an Excel-authored configuration, a generated mapping, a saved plan,
and an audit trail.

OLAF is not affiliated with, endorsed by, sponsored by, or supported by Microsoft.
The product names above are used only to describe interoperability. See
[Microsoft's Trademark and Brand Guidelines](https://www.microsoft.com/en-us/legal/intellectualproperty/trademarks)
and the project's [platform contract](docs/platform-contract.md).

## What is OneLake security?

[OneLake security](https://learn.microsoft.com/en-us/fabric/onelake/security/data-access-control-model)
is Microsoft Fabric's native, data-plane access control: you define **security roles on the data
itself** (a lakehouse's tables, folders, rows, columns) and OneLake enforces them everywhere — instead
of re-implementing security per engine with T-SQL views, workspace boundaries, or report-side filters.

- **Role-based access** — grant Read per table (`/Tables/schema/table`) or folder (`/Files/...`);
  users with no role see nothing (**deny by default**).
  [Create and manage roles →](https://learn.microsoft.com/en-us/fabric/onelake/security/create-manage-roles)
- **Row-level security (RLS)** — SQL-like predicates per table per role (static values; the
  OneLake-supported operator subset — `= <> > >= < <= IN NOT AND OR IS BLANK NULL TRUE FALSE`,
  rule C9). A member of several roles gets the **union** of what they allow.
  [Row-level security →](https://learn.microsoft.com/en-us/fabric/onelake/security/row-level-security)
- **Column-level security (CLS)** — hide specific columns per role.
  [Column-level security →](https://learn.microsoft.com/en-us/fabric/onelake/security/column-level-security)
- **Entra-native membership** — role members are Entra users, **security groups**, or service
  principals; membership changes happen in Entra, no sync tables to maintain.
- **One definition, every engine** — enforced across the SQL analytics endpoint, Spark, and Direct
  Lake semantic models (Power BI), so reports and notebooks see the same filtered data.
- **Granular write** — separate ReadWrite roles for scoped write access (RLS/CLS don't apply there).

Worth knowing: roles carrying RLS/CLS are read-only; workspace Admins/Members/Contributors bypass
OneLake security (it governs consumers); role definitions are per lakehouse.

## Why OLAF?

OneLake security provides the enforcement layer. OLAF makes operating that layer **reviewable,
repeatable, and easier to govern** when deployments grow beyond a handful of roles and tables.

| What you need | What OLAF does |
|---|---|
| **Know what will change** | `plan` shows every role and grant that will be created, updated, or omitted from the submitted payload. |
| **Prevent an unreviewed write** | `apply` requires a saved plan and refuses stale or drifted state. |
| **Catch risky configuration early** | Validation blocks common RLS/CLS, predicate, casing, and multi-role exposure mistakes before deployment. |
| **Keep the runtime simple** | One unchanged notebook serves every project; configuration and runtime parameters carry project intent. |
| **Explain what happened** | Plan and apply are linked in the audit log with who, what, when, and the relevant configuration state. |

*Full rule-by-rule detail → [docs/architecture.md](docs/architecture.md).*

## What OLAF does

| Need | OLAF behavior |
|---|---|
| Review intended access | `generate` resolves authored scopes and members; `plan` presents the desired-versus-live diff. |
| Refuse stale approval | `apply` requires a saved plan for the same config and mapping generation and rechecks observable drift. |
| Fail closed around sensitive control data | Every sensitive write requires the DAR snapshot gate and an operation sentinel; the per-run operator attestation is optional and only recorded, as `attested` or `unknown`. |
| Preserve recovery evidence | Destructive paths record prepared intent and recovery pointers before a real DAR write. |
| Explain current state | Read-only audit methods expose lineage, drift, coverage, and engine-explicit policy calculations. |

OLAF validates configuration and request construction. Microsoft Fabric remains the
enforcement system, and enforcement depends on the engine, access mode, workspace
role, item permissions, and shortcuts. See the official
[engine and user access model](https://learn.microsoft.com/en-us/fabric/onelake/security/data-access-control-model#engine-and-user-access-to-data)
and [SQL endpoint enforcement guidance](https://learn.microsoft.com/en-us/fabric/onelake/security/troubleshoot-onelake-security-for-sql-analytics-endpoints#access-modes-and-enforcement).

## Before you start

> [!CAUTION]
> OLAF stores principal identifiers and authorization/recovery state in control
> tables and under `Files/security`. Sensitive modes are disabled by default and
> require a clean DAR snapshot with an ETag. The per-run workspace isolation
> attestation is optional — OLAF records it as `attested` or `unknown` and
> never gates on it. Complete the external access review **before uploading a
> real workbook**. Start with [Protecting OLAF control data](docs/control-data-security.md).

- Use Microsoft Fabric Runtime 1.3 / Spark 3.5 or newer, then verify the selected
  runtime and bundled package versions in your Fabric environment. Microsoft
  publishes the [runtime lifecycle](https://learn.microsoft.com/en-us/fabric/data-engineering/lifecycle)
  and [OneLake security limitations](https://learn.microsoft.com/en-us/fabric/onelake/security/data-access-control-model#onelake-security-limitations).
- Use a workspace Admin or Member identity for DAR edits and ensure the REST caller
  has `OneLake.ReadWrite.All`. Contributor does not edit DAR definitions. See
  [workspace permissions](https://learn.microsoft.com/en-us/fabric/onelake/security/data-access-control-model#onelake-security-and-workspace-permissions)
  and the [bulk DAR authorization contract](https://learn.microsoft.com/en-us/rest/api/fabric/core/onelake-data-access-security/create-or-update-data-access-roles).
- Treat the same-lakehouse layout as a trusted-administrator boundary, not
  cryptographic or transactional isolation. If that threat model is unacceptable,
  do not import real principal data or run sensitive modes in this Preview.

## Quick start

OLAF runs inside a Fabric notebook; it is not a CLI.

1. Import [`notebooks/olaf.ipynb`](notebooks/olaf.ipynb) and optionally the
   [`olaf_master_workflow.ipynb`](notebooks/olaf_master_workflow.ipynb) driver.
   See [Getting the notebooks into Fabric](docs/fabric-import.md).
2. Attach the intended lakehouse and run the read-only health diagnostic before
   setup:

   ```python
   OLAF.configure(lakehouse_name="SampleLakehouse")
   OLAF.health()
   ```

3. Review and remove reserved-path DAR overlap, broad `ReadAll` access, dynamic
   default-reader access, unauthorized elevated workspace roles, shares, shortcuts,
   and automation that can reopen them. Record the review in your change system.
   Microsoft documents these access paths in the
   [OneLake access model](https://learn.microsoft.com/en-us/fabric/onelake/security/data-access-control-model).
4. Supply the review reference for this run, then create the control tables:

   ```python
   OLAF.configure(
       lakehouse_name="SampleLakehouse",
       control_data_isolation_attestation="change-review-123",
   )
   OLAF.setup()
   ```

5. Only now upload a sanitized copy of
   [`configs/onelake_security.xlsx`](configs/onelake_security.xlsx) under
   `Files/security`, replace every synthetic row, verify every Entra object ID, and
   import both sheets:

   ```python
   OLAF.load_config("member", "Files/security/onelake_security.xlsx", "member")
   OLAF.load_config("config", "Files/security/onelake_security.xlsx", "config")
   ```

6. Use a new attestation reference for each sensitive operation and review every
   result:

   ```python
   OLAF.generate()
   OLAF.plan()
   OLAF.apply()
   ```

Do not treat a successful request or dry run as proof of propagation or enforcement.
The release has fixture-based CI evidence only; it does not claim live verification
for the release SHA. See [Evidence status](docs/platform-contract.md#evidence-status).

## Modes at a glance

| Mode | Purpose | Writes |
|---|---|---|
| `setup` | Create or migrate the four control tables after the privacy gate. | tables and audit |
| `validate` | Run configuration validation without a mapping, log, file, backup, or DAR write. | none |
| `generate` | Resolve config and member data into a versioned mapping and review artifact. | sensitive artifacts and audit |
| `plan` | Compare desired and live DAR state and save the reviewed plan. | sensitive audit |
| `apply` | Submit the reviewed payload through the Preview bulk DAR endpoint. | backup, audit, and DAR |
| `rollback` | Restore a selected config version, then run the guarded deployment chain. | config, artifacts, audit, and DAR |
| `show` | Read-only live pivot. | none |
| `trace` | Read-only operational and drift summary. | none |

`reset()` and `cleanup()` are interactive destructive utilities. Reset is subject to
the full sensitive-write gate. Cleanup is an emergency containment path that may run
without attestation, preserves an incident sentinel, and cannot prove that prior
disclosure was erased. See the [mode manual](docs/modes.md) and
[runbook](docs/runbook.md).

Every mode returns one result envelope:

```text
mode · status · changed · message · params · data · error
batch_id · run_id · config_hash
```

`changed` may be `null` when a real write outcome is ambiguous. A confirmed or
ambiguous write is never reported as `changed=false`. See
[Error handling](docs/error-handling.md).

## Configuration model

The workbook has two authored sheets:

- `config`: roles, table/folder scopes, permission, RLS/CLS, and member names;
- `member`: the synthetic-name-to-Entra-object-ID preload used by OLAF.

The workbook is an untrusted input. Verify every object ID against Entra before
loading. OLAF deliberately does not call Microsoft Graph for directory resolution;
that is a project design choice, not a claim that Graph tokens are universally
impossible in Fabric notebooks. NotebookUtils documents an evolving audience list:
[Get a token](https://learn.microsoft.com/en-us/fabric/data-engineering/notebookutils/notebookutils-credentials#get-token).

The generated mapping carries explicit scopes and provenance. It is a review artifact,
not an authorization boundary by itself. Full references:

- [Config examples](docs/config-examples.md)
- [Data model](docs/data-model.md)
- [Rule catalog](docs/architecture.md#rule-catalog)
- [Workbook instructions](configs/README.md)

OLAF's predicate parser is intentionally conservative and is not an exhaustive
statement of what the platform accepts. Use Microsoft's current
[RLS syntax reference](https://learn.microsoft.com/en-us/fabric/onelake/security/row-level-security-syntax).
Role, member, path, and latency values are volatile platform limits; consult the
[current limitations](https://learn.microsoft.com/en-us/fabric/onelake/security/data-access-control-model#onelake-security-limitations)
and [latencies](https://learn.microsoft.com/en-us/fabric/onelake/security/data-access-control-model#latencies-in-onelake-security)
pages at deployment time.

## Concurrency and recovery

OLAF requires an ETag-bearing DAR snapshot for sensitive writes. Conditional DAR
requests use the exact captured ETag; a missing or changed snapshot blocks instead
of silently refreshing the approved state. Microsoft documents the collection ETag,
optional `If-Match`, and `412` response in the official
[list](https://learn.microsoft.com/en-us/rest/api/fabric/core/onelake-data-access-security/list-data-access-roles)
and [bulk PUT](https://learn.microsoft.com/en-us/rest/api/fabric/core/onelake-data-access-security/create-or-update-data-access-roles)
references.

That ETag covers only the DAR collection. It does not lock workspace sharing,
privileged access, shortcuts, or local Delta/file writes. A REST response, control
table change, audit append, backup write, and Delta `RESTORE` are not one transaction.
Preserve the incident sentinel, prepared record, and recovery pointers after a
partial or ambiguous operation. Follow
[Protecting OLAF control data](docs/control-data-security.md) and the
[recovery runbook](docs/runbook.md#3c-recovery--break-glass-incident-procedure-no-public-replay-api).

## Documentation

- [Documentation map](docs/README.md)
- [Platform contract](docs/platform-contract.md)
- [Control-data security](docs/control-data-security.md)
- [Runbook](docs/runbook.md)
- [Mode manual](docs/modes.md)
- [API reference](docs/api-reference.md)
- [Testing guide](docs/testing.md)
- [Roadmap](docs/roadmap.md)

## Roadmap

Options, not promises or release dates. Each is blocked on a specific, checkable condition, and none
may weaken the control-data boundary or turn an observed service behavior into a platform contract.
**[docs/roadmap.md](docs/roadmap.md)** carries the detail and the official sources behind each one.

- [ ] **Differential apply** — per-role writes instead of one bulk `PUT`, so an apply touches only
      what changed. *Blocked on a stable, concurrency-safe official contract: the single-role
      endpoints are themselves Preview, and the design needs documented precondition behavior plus a
      recovery model for a partially completed role set.*
- [ ] **RLS + CLS cross-grant detection at `generate`** — closing rule C5's blind spot, where a user
      arrives through a group on one side and directly on the other. *The conservative rule stays
      until evidence justifies otherwise; OLAF deliberately does not call Microsoft Graph.*
- [ ] **Scheduled drift detection** — a gate an adopter schedules rather than wires themselves.
      *Must stay read-only, separate identity drift from policy drift, and keep real principal values
      out of CI logs.*
- [ ] **Local authoring tool** — validate and build a config from a developer machine against a
      real workspace, so table names, member resolution and rule violations surface while the
      config is still being written instead of at `generate`. *Blocked on where real identifiers
      and a Fabric token are allowed to live once they leave the Fabric boundary; validation only,
      never a second write path.*
- [ ] **Separate control store** — a separately secured store or independently managed keys, for
      organizations that cannot accept v1's trusted-administrator boundary. *Not a minimal v1 change.*

**Not planned:** replacing Fabric enforcement with an OLAF query layer · workspace and item RBAC ·
calling Microsoft Graph in v1 · claiming production readiness while the required endpoint is Preview
— [the full list and why](docs/roadmap.md#not-planned).

## Repository layout

```text
notebooks/   runtime and optional workflow/example notebooks
configs/     synthetic starter workbook
tests/       fixture-based pytest suite
scripts/     repository checks
docs/        operator and API documentation
assets/      OLAF-owned artwork
```

## Security and support

Read [SECURITY.md](SECURITY.md) before using real principal data. GitHub private
vulnerability reporting is the confidential route when maintainers have enabled it;
the release gate verifies that setting before publication. GitHub documents the
[reporting flow and configuration requirement](https://docs.github.com/en/code-security/how-tos/report-and-fix-vulnerabilities/report-privately).

General questions and sanitized bug reports belong in
[GitHub issues](https://github.com/kengio/olaf/issues). See [SUPPORT.md](SUPPORT.md)
and [CONTRIBUTING.md](CONTRIBUTING.md).

<p align="center">
  <img src="assets/brand/olaf-mascot.png" alt="The OLAF Arctic Owl Sentinel — a crystalline owl holding a notebook that carries the OLAF shield crest and its padlock" width="260">
</p>

<p align="center">
  <em>The Arctic Owl keeps watch over your lake — every change is seen, reviewed,<br>
  and approved before it is submitted.</em>
</p>

## License and identity

Code, documentation, and project artwork are licensed under [MIT](LICENSE), with
`OLAF contributors` as the collective notice. Brand guidance is a request, not an
additional license restriction: [OLAF brand guidelines](docs/brand-guidelines.md).

---

<sub><b>Independent-project disclaimer.</b> OLAF is an independent community
project and is not affiliated with, endorsed by, sponsored by, or supported by
Microsoft. Microsoft, Microsoft Fabric, and OneLake are trademarks of the Microsoft
group of companies. No Microsoft logo or product icon is used by this project.</sub>
