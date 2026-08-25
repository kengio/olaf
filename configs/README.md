# configs/

The starter template. **Every data row is synthetic** — shaded and meant to be
replaced. The file contains no real tenant, workspace, item, customer, or principal
data. Its `example.invalid` user label uses an IETF-reserved special-use domain:
[RFC 6761](https://www.rfc-editor.org/rfc/rfc6761.html#section-6.4).

> **Do not upload a workbook containing real principal data until the external
> workspace/item sharing and elevated-access review is complete.** OLAF cannot
> protect a workbook before upload. Follow
> [Protecting OLAF control data](../docs/control-data-security.md).

`onelake_security.xlsx` holds both authored tables as sheets in one workbook, so a config and the
member list it depends on cannot drift apart or be uploaded out of step.

| Sheet | What it is |
|---|---|
| `config` | All 20 `onelake_security_config` columns in their fixed order, with three sample rows showing the patterns the framework supports — table scopes with a glob · folder scope + RLS · CLS blacklist. Author your rows here, then load them into the `olaf.onelake_security_config` control table. |
| `member` | OLAF's directory-free preload: `member_type` · `member_name` · `member_id` (Entra objectId). Synthetic rows cover the four supported types. Fill in every principal your config references, then load into `olaf.onelake_security_member`. OLAF deliberately resolves from that table and does not call Microsoft Graph; this is a project design choice, not a platform impossibility claim. |
| `config-guide` | Per-column reference for the `config` sheet. |
| `member-guide` | Per-column reference for the `member` sheet. |

Load each sheet by name:

```python
OLAF.load_config("config", "Files/security/onelake_security.xlsx", "config")
OLAF.load_config("member", "Files/security/onelake_security.xlsx", "member")
```

Runtime parameters are deployment-specific — they live in your own deployment workbook, not in this
template, and are documented in [docs/runbook.md](../docs/runbook.md).

**The column set is a contract.** All 20 config columns and their order are fixed by the framework —
do not add, remove or reorder them. The guide sheets document every column's type, whether it is
required, and its accepted format.

**Verify every `member_id` against Entra before loading.** A syntactically valid
object ID can still identify the wrong principal; OLAF cannot infer the operator's
intent from that ID. Microsoft documents NotebookUtils token audiences separately;
OLAF's decision not to call Graph must not be read as a universal Fabric limitation:
[NotebookUtils credentials](https://learn.microsoft.com/en-us/fabric/data-engineering/notebookutils/notebookutils-credentials#get-token).

Platform guidance in the workbook is versioned for OLAF v1.0.0 and points to the
maintained [platform contract](../docs/platform-contract.md). Microsoft's service
rules and limits may change; the official links remain authoritative.
