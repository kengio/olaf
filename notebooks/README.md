# notebooks/

**One self-contained runtime, plus three optional notebooks.** OLAF v1.0.0 is
an independent community Preview for evaluation and development. `olaf` is the only file you *need* —
it's the whole tool a user or pipeline runs. `olaf_master_workflow` is the recommended starting
point: the runtime driven end to end, one stage per cell. `olaf_runner` is the pipeline wrapper —
one activity, one mode. The cookbook is copy-paste examples (import any of them only if you want
them). The **pytest** suite lives in [`../tests/`](../tests).

| Notebook | Role | Modes |
|---|---|---|
| `olaf.ipynb` | **The runtime — everything, self-contained.** All pure functions + the four classes (`FabricClient` · `Log` · `Deployment` · `Audit`) + the `run_mode`/`run_and_exit` entrypoints + the `OLAF` interactive facade + a `parameters` cell + the ▶️ Run dispatch cell. No `%run`, no separate lib. Import + invoke this one notebook; every mode is selected by the `mode` parameter. | `setup` · `generate` · `validate` · `plan` · `apply` · `rollback` · `show` · `trace` |
| `olaf_master_workflow.ipynb` | **The end-to-end workflow — start here.** Drives the runtime through setup → load_config → validate → generate → plan → gate → apply → verify, one stage per cell, each with its own failure report. Modes go through `notebookutils.notebook.run` so a blocked stage raises and the run stops; `configure`/`load_config` and the read-only tail go through the `OLAF` facade. Tagged `parameters` cell, so a pipeline can drive it. | drives all eight |
| `olaf_runner.ipynb` | **Pipeline wrapper — one activity, one mode.** Four cells: a `%%configure` cell that binds the default lakehouse from a pipeline base parameter (the binding `olaf` deliberately does not carry), a `parameters` cell mirroring the runtime's, a dispatch that hands the mode to `olaf` through `notebookutils.notebook.run`, and the exit that returns the envelope. Point a Data Factory Notebook activity at this, one activity per mode. | one per activity (any mode) |
| `olaf_cookbook.ipynb` | **Example cookbook (not run in CI).** `%run olaf` to load the `OLAF` facade, then one copy-paste cell per `OLAF.<action>(...)` example. Example output is illustrative and not live-release evidence. | — (examples only) |

The runtime dispatches **whatever `mode` is passed** — there is no per-notebook mode gate anymore.
Fabric permissions and OLAF intent are separate. Microsoft documents that a workspace
**Admin or Member** may edit OneLake data access roles and that the REST caller needs
`OneLake.ReadWrite.All`; Contributor is not sufficient to edit DAR definitions. See
[workspace permissions](https://learn.microsoft.com/en-us/fabric/onelake/security/data-access-control-model#onelake-security-and-workspace-permissions),
the [bulk DAR endpoint](https://learn.microsoft.com/en-us/rest/api/fabric/core/onelake-data-access-security/create-or-update-data-access-roles),
and the [runbook](../docs/runbook.md).

Before uploading real principal data or running any sensitive mode, complete the
external sharing/elevated-access review and supply a per-run isolation-attestation
reference. A DAR snapshot/ETag does not prove workspace isolation. See
[Protecting OLAF control data](../docs/control-data-security.md).

`%run olaf` is Fabric's `import`, pulling the `OLAF` facade into a driver notebook — the runtime's
▶️ Run cell is guarded, so `%run olaf` loads the definitions without dispatching anything. The
runtime itself never `%run`s: the production pipeline path passes `mode` and drives it through
`notebookutils.notebook.run` — which is also how `olaf_runner` calls it, so that one never `%run`s
either (its child gets its own session, inheriting the lakehouse its `%%configure` cell bound).
Two of the remaining notebooks `%run olaf`: the cookbook, to load the facade for its copy-paste
examples; and `olaf_master_workflow.ipynb` mid-notebook, for the `configure`/`load_config` and
read-only-tail stages that go through the `OLAF` facade rather than
`notebookutils.notebook.run`. `tests/olaf_test_smoke.ipynb` does **not** — it holds no runtime code
at all: a parameters cell of authorization flags, then one guard cell that stops the run unless
every flag is set. No `%run`, no facade call, no Spark.

Tests: `../tests/test_*.py` — **pytest**, run by CI as `pytest --cov` (see
[../docs/testing.md](../docs/testing.md)). They need no Fabric and are not importable notebooks.
The one exception is `../tests/olaf_test_smoke.ipynb`, an optional authorized live-validation
protocol. It is not release evidence unless the exact SHA, runtime, authorized target scope,
access effect, restore point, and cleanup are recorded (see
[../docs/live-smoke-test.md](../docs/live-smoke-test.md)).

Import steps: [../docs/fabric-import.md](../docs/fabric-import.md).
