# Getting the notebooks into Fabric

> **Community Preview:** OLAF — OneLake Access Framework is an independent community project. It
> is not affiliated with, endorsed by, sponsored by, or certified by Microsoft. Its mutating DAR
> dependency is Preview and documented for evaluation/development, not production use. Start with
> [the platform contract](platform-contract.md) and
> [control-data security](control-data-security.md).

The required runtime is `notebooks/olaf.ipynb`. The runner, master workflow, cookbook, and smoke
notebook are optional. Import only the files needed for an authorized evaluation.

| Path | Best for | Format |
|---|---|---|
| [Portal import](#1-portal-import-manual-quickest) | an isolated evaluation | `.ipynb` |
| [REST bootstrap](#2-rest-api-scripted-bootstrap) | an approved scripted import | `.ipynb` definition |
| [Git integration](#3-git-integration--cicd) | source control after evaluation | Fabric item format |

## Prerequisites (all paths)

- A non-production workspace and lakehouse on Fabric capacity.
- For DAR updates, the calling identity must be a workspace **Admin or Member** and the API
  authorization must include `OneLake.ReadWrite.All`. Read and write permissions are separate
  from OLAF's own mode labels. See Microsoft's
  [bulk DAR endpoint](https://learn.microsoft.com/en-us/rest/api/fabric/core/onelake-data-access-security/create-or-update-data-access-roles)
  and [workspace permission model](https://learn.microsoft.com/en-us/fabric/onelake/security/data-access-control-model#onelake-security-and-workspace-permissions).
- Microsoft Fabric Runtime 1.3 or a later runtime that meets the documented Spark 3.5+ requirement.
  Verify the selected runtime against the current
  [Fabric runtime lifecycle](https://learn.microsoft.com/en-us/fabric/data-engineering/runtime).
- A separate control-data location whose external access has been reviewed. A same-lakehouse
  layout is convenient but is not cryptographic or transactional isolation.
- No real workbook, principal, tenant, workspace, item, or policy data until that review is
  complete. Use only the synthetic template during evaluation.

## 1. Portal import (manual, quickest)

1. Open an authorized non-production Fabric workspace.
2. Use **Import → Notebook → From this computer** and select `notebooks/olaf.ipynb`.
3. Import an optional wrapper only when needed:
   `notebooks/olaf_runner.ipynb`, `notebooks/olaf_master_workflow.ipynb`, or
   `notebooks/olaf_cookbook.ipynb`. The smoke notebook is an unexecuted protocol, not release
   evidence; see [live-smoke-test.md](live-smoke-test.md).
4. Attach the intended non-production lakehouse in the notebook Explorer, or use an approved
   wrapper binding as described below.
5. Confirm the runtime parameters cell remains tagged as a parameters cell.
6. Before any setup or mutation, record the external-access review, obtain a per-run isolation
   attestation, capture the DAR snapshot and immutable ETag, and create/read the sentinel before
   the first sensitive write. The runtime revalidates the sentinel and captured snapshot before
   every sensitive write. Sensitive modes, including first setup, are disabled by default.

Microsoft's notebook documentation describes
[how to explore and use a lakehouse in a notebook](https://learn.microsoft.com/en-us/fabric/data-engineering/lakehouse-notebook-explore)
and [how notebook parameters work in pipelines](https://learn.microsoft.com/en-us/fabric/data-factory/notebook-activity#parameters-settings).

## 1b. Bind the default lakehouse — portal attachment or a wrapper's `%%configure`

For interactive evaluation, attach a default lakehouse in the Fabric portal. For a pipeline,
place the session binding in a wrapper notebook so it runs before the child notebook session:

```text
%%configure -f
{
  "defaultLakehouse": {
    "name": {
      "parameterName": "<pipeline parameter>",
      "defaultValue": "<non-production lakehouse>"
    }
  }
}
```

The binding selects a runtime attachment; it is not an authorization boundary. The per-run
workspace-isolation attestation is separate, optional, and recorded per run rather than enforced.
Microsoft documents `%%configure` and the default-lakehouse settings in
[Spark session configuration magic command](https://learn.microsoft.com/en-us/fabric/data-engineering/author-execute-notebook#spark-session-configuration-magic-command).

### Where the lakehouse comes from, per entry path

| Entry path | Binding source | Public-preview requirement |
|---|---|---|
| `%run olaf` from a driver | caller session | verify the caller attachment and attestation |
| `notebookutils.notebook.run("olaf", ...)` | parent session | verify the parent attachment and attestation |
| pipeline activity targeting `olaf` | configured activity/notebook | use a reviewed wrapper; do not infer isolation from attachment |
| interactive run | portal attachment | verify the attachment before every sensitive run |

### The shipped wrapper: `olaf_runner.ipynb`

[`notebooks/olaf_runner.ipynb`](../notebooks/olaf_runner.ipynb) is a one-mode wrapper. Treat it as
an evaluation scaffold: inspect its parameters, target binding, and result handling before use.
The wrapper does not remove the control-data, authorization, or sentinel requirements.

## 2. REST API (scripted bootstrap)

The official Fabric REST API can create a notebook item from an `ipynb` definition. Follow the
current [Create Notebook API](https://learn.microsoft.com/en-us/rest/api/fabric/notebook/items/create-notebook)
for request shape, permissions, long-running-operation handling, and definition parts. Do not
copy a token, workspace ID, notebook definition, or API response into this repository.

A safe bootstrap process must:

1. resolve the authorized target outside the repository;
2. verify the target is non-production and externally reviewed;
3. import the exact release commit's notebook bytes;
4. verify the item definition and default lakehouse after import; and
5. record only redacted evidence, never real identifiers or secrets.

## 3. Git integration + CI/CD

Fabric Git integration stores notebooks in Fabric's item representation. Microsoft documents the
supported repository providers, branch model, and item serialization in
[Git integration overview](https://learn.microsoft.com/en-us/fabric/cicd/git-integration/intro-to-git-integration).
Deployment pipelines are documented separately in
[Fabric deployment pipelines](https://learn.microsoft.com/en-us/fabric/cicd/deployment-pipelines/intro-to-deployment-pipelines).

Use local CI for deterministic tests. Treat any external smoke run as separately authorized,
redacted evidence bound to the exact commit SHA. Do not promote OLAF v1.1.0 Preview as a
production-ready security control.

## Which path when

- **Local review first:** inspect the notebook, workbook, controls, and tests without Fabric.
- **Authorized evaluation:** use portal import in an isolated non-production workspace.
- **Repeatable evaluation:** use REST or Git integration only after the same controls are in place.

No path removes the need for an external-access review, fresh DAR
snapshot/ETag, pre-write sentinel, post-state verification, and containment-oriented cleanup.
