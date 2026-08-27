# Microsoft Fabric platform contract used by OLAF

OLAF is an independent community project. It is not affiliated with, endorsed by,
sponsored by, or supported by Microsoft. Microsoft, Microsoft Fabric, and OneLake
are trademarks of the Microsoft group of companies. Their use here describes the
platform with which OLAF is intended to interoperate; see Microsoft's
[Trademark and Brand Guidelines](https://www.microsoft.com/en-us/legal/intellectualproperty/trademarks).

## Release status

OLAF is a **community Preview for evaluation and development**, not a
production-ready security product. Its mutating path uses the bulk Data Access
Roles (DAR) `PUT`, which Microsoft labels **Preview** and says is not recommended
for production use. Review that endpoint's current status before every deployment:
[Create or update data access roles](https://learn.microsoft.com/en-us/rest/api/fabric/core/onelake-data-access-security/create-or-update-data-access-roles).

The public endpoint contract describes creating or updating the roles supplied in
the request. It does not promise atomic replacement, deletion of omitted roles,
stable role identifiers, or exact restoration from a prior response. OLAF therefore
records intended and observed state but does not present those properties as
Microsoft platform guarantees.

## Permissions and identity

Microsoft documents that workspace **Admin or Member** roles may create or modify
OneLake data access roles. Contributor is not sufficient to edit DAR definitions.
The REST request also requires the `OneLake.ReadWrite.All` permission; the endpoint
supports user, service-principal, and managed-identity callers. These are separate
requirements from OLAF's own read-only or write intent:

- [OneLake security and workspace permissions](https://learn.microsoft.com/en-us/fabric/onelake/security/data-access-control-model#onelake-security-and-workspace-permissions)
- [Bulk DAR `PUT` authorization](https://learn.microsoft.com/en-us/rest/api/fabric/core/onelake-data-access-security/create-or-update-data-access-roles)
- [NotebookUtils token audiences and constraints](https://learn.microsoft.com/en-us/fabric/data-engineering/notebookutils/notebookutils-credentials#get-token)

OLAF acquires a Fabric REST token using the documented `pbi` audience key. Token
acquisition does not by itself prove that the current identity has the permissions
or workspace role required for a particular operation.

## Enforcement depends on the access path

OneLake security is not a universal enforcement statement. Microsoft documents the
supported engines and access modes, including restrictions for shortcuts and SQL
analytics endpoints. SQL endpoint enforcement requires user-identity mode. Workspace
roles and item permissions can also provide access outside a DAR rule:

- [Engine and user access to data](https://learn.microsoft.com/en-us/fabric/onelake/security/data-access-control-model#engine-and-user-access-to-data)
- [SQL analytics endpoint access modes and enforcement](https://learn.microsoft.com/en-us/fabric/onelake/security/troubleshoot-onelake-security-for-sql-analytics-endpoints#access-modes-and-enforcement)
- [SQL analytics endpoint and OneLake security](https://learn.microsoft.com/en-us/fabric/onelake/security/sql-analytics-endpoint-onelake-security)

Multiple roles generally combine access, but column-level security differs by
engine: non-SQL access uses union semantics while the SQL analytics endpoint applies
deny/intersection behavior. Any OLAF effective-access result must therefore name the
engine, and it remains a policy calculation rather than proof of propagated runtime
enforcement:
[Column-level security](https://learn.microsoft.com/en-us/fabric/onelake/security/data-access-control-model#column-level-security).

Microsoft's current RLS guidance says an invalid or case-mismatched predicate can
return no rows or produce a SQL query error. OLAF treats exact column spelling as an
authoring guard and does not claim a fail-open platform behavior:
[RLS syntax](https://learn.microsoft.com/en-us/fabric/onelake/security/row-level-security-syntax).

## Permissions, RLS, CLS, and current limits

`ReadWrite` includes `Read`, and RLS/CLS restrictions apply only in the supported
read paths described by Microsoft. The effective outcome also depends on workspace
and item permissions; absence of a DAR role is not proof that nobody can read data:
[Permissions and supported items](https://learn.microsoft.com/en-us/fabric/onelake/security/data-access-control-model#permissions-and-supported-items).

OLAF validates a conservative subset of authored predicates. That parser is an OLAF
guard, not an exhaustive declaration of every expression the service may accept.
Use Microsoft's current syntax reference as the platform authority:
[Row-level security syntax](https://learn.microsoft.com/en-us/fabric/onelake/security/row-level-security-syntax).

Role, member, path, predicate, and propagation limits are service properties that
may change. Consult the current pages instead of treating copied values as permanent
contracts:

- [OneLake security limitations](https://learn.microsoft.com/en-us/fabric/onelake/security/data-access-control-model#onelake-security-limitations)
- [OneLake security latencies](https://learn.microsoft.com/en-us/fabric/onelake/security/data-access-control-model#latencies-in-onelake-security)
- [SQL analytics endpoint troubleshooting](https://learn.microsoft.com/en-us/fabric/onelake/security/troubleshoot-onelake-security-for-sql-analytics-endpoints)

Microsoft documents restrictions when RLS and CLS reach the same identity through
multiple roles. OLAF uses a conservative validation rule and does not extrapolate
untested behavior across engines, tables, or membership paths:

- [Combine table, column, and row security](https://learn.microsoft.com/en-us/fabric/onelake/security/table-column-row-security#combine-row-level-and-column-level-security)
- [Evaluate multiple OneLake security roles](https://learn.microsoft.com/en-us/fabric/onelake/security/data-access-control-model#evaluating-multiple-onelake-security-roles)

## Concurrency and recovery boundary

Microsoft documents a collection ETag on DAR list responses, an optional quoted
`If-Match` header on bulk `PUT`, and `412 Precondition Failed`. OLAF uses that surface
to detect a changed DAR collection, but does not infer that a failed request proves
anything about unrelated workspace sharing, prior reads, or external copies:

- [List data access roles](https://learn.microsoft.com/en-us/rest/api/fabric/core/onelake-data-access-security/list-data-access-roles)
- [Create or update data access roles](https://learn.microsoft.com/en-us/rest/api/fabric/core/onelake-data-access-security/create-or-update-data-access-roles)

An OLAF backup is recovery evidence, not a guaranteed exact restore point. REST
writes, Delta table changes, audit rows, file writes, and Delta `RESTORE` are not one
transaction. After an ambiguous or partial operation, stop, preserve the prepared
record and backup, obtain a fresh bounded snapshot, and decide on recovery without
overwriting a concurrent change.

## Supported runtime baseline

OLAF supports Microsoft Fabric Runtime 1.3 / Spark 3.5 or newer. The runtime checks
the observable Spark version; operators must verify the selected Fabric Runtime in
the workspace settings. Bundled package versions change with supported runtime
images, so OLAF does not promise that a fixed package set is permanently preinstalled
or remediated:

- [Fabric runtime lifecycle](https://learn.microsoft.com/en-us/fabric/data-engineering/lifecycle)
- [OneLake security limitations](https://learn.microsoft.com/en-us/fabric/onelake/security/data-access-control-model#onelake-security-limitations)

## Evidence status

The public release has automated fixture-based CI evidence. It does **not**
claim exact-release-SHA verification against a live Fabric tenant. Any future live
result must state the release SHA, Fabric Runtime, API date, target class, test scope,
and cleanup result, and must be described as a dated observation rather than a
platform contract.
