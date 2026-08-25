# Testing guide

Everything that can run without a workspace is **pytest**, in plain `.py` under `tests/`.
The optional `tests/olaf_test_smoke.ipynb` is an authorization-only protocol for an isolated
Fabric lab. The repository does not claim that the current release commit has been executed
against a live tenant.

| What | Where | Needs a workspace? |
|---|---|---|
| unit + mock + runtime black-box | `tests/test_*.py` (pytest) | **No** — this is CI |
| optional external smoke protocol | `tests/olaf_test_smoke.ipynb` | Yes — authorized isolated lab only; never CI evidence |

That split is the whole design. CI cannot execute a Fabric notebook, so the suite that gates every
PR has to be importable Python; and a suite kept in two forms drifts the first time someone edits
one of them. So the assertions exist once, in pytest, and the notebook holds only what a live
service can answer that a fake cannot. A future result is release evidence only when it records
the exact commit SHA, runtime/API versions, access mode, authorization, restoration, and cleanup
without publishing environment identifiers. See [live-smoke-test.md](live-smoke-test.md).

## What is under test, and how it is reached

The deliverable is **one self-contained notebook**, `notebooks/olaf.ipynb` (ADR-007). pytest cannot
import a notebook, so [`tests/conftest.py`](../tests/conftest.py) extracts its library/entrypoint
cells — everything except the `parameters` cell, the ▶️ Run dispatch cell, and `%`-magics — into
`tests/_olaf_runtime.py` at import time, on **every run**. Tests import that module; coverage
measures that module.

Two consequences worth knowing:

* `tests/_olaf_runtime.py` is a **build artifact**. It is regenerated whenever the notebook
  changes, gitignored, and excluded from lint. Never edit it; edit the notebook.
* The extraction is why a notebook edit is reflected immediately — change a cell, run `pytest`,
  and you are testing the change.

The one cell the extraction drops on purpose is the ▶️ Run dispatch cell, because the runtime's
entrypoints have to be exercised the way Fabric calls them. `run_runtime_blackbox` (in
[`tests/_fakes.py`](../tests/_fakes.py)) execs that cell in a namespace seeded with the traced
module's globals, so the `run_mode` / `run_and_exit` it drives are the **same traced objects** —
that is what puts the entrypoints and their guard branches inside the coverage number.

## Running

```bash
python -m pip install --require-hashes --only-binary=:all: -r requirements/ci-test.txt
python -m pip check

pytest                  # the suite
pytest --cov            # ...plus the 100% branch-coverage gate (what CI runs)
pytest tests/test_unit_validation.py -k c11    # one module, one rule family
```

Requires Python ≥ 3.11 and nothing else at runtime — no Fabric or Spark. Installing the reviewed,
hash-locked local test wheels may require the package index; the tests themselves make no network
calls. `pyspark` is optional: `tests/_fakes.py` installs a minimal `pyspark.sql.types` stand-in
only when the real one is absent, so the suite runs identically on a bare interpreter and on a
Fabric image.

[`.github/workflows/test.yml`](../.github/workflows/test.yml) runs `pytest --cov` on 3.11 / 3.12 /
3.13 for every pull request and push to `main`, plus `scripts/lint.sh` as a separate job.
The install above consumes the same fully transitive, SHA-256-locked dependency set as CI. See the
[requirements instructions](../requirements/README.md) before regenerating it.

## Coverage

`notebooks/olaf.ipynb`'s library cells are held at **100% line + branch coverage**.

The gate is `fail_under = 100` in [`pyproject.toml`](../pyproject.toml)
(`[tool.coverage.report]`), so **`pytest --cov` IS the gate** — there is no separate coverage
script and no separate CI job. Below 100% the run fails and prints every missing line and partial
branch.

**What is measured.** One file: `tests/_olaf_runtime.py`, the extracted runtime.

| Surface | How it is covered |
|---|---|
| pure functions + classes | **white-box** — tests call them directly and drive the Fabric/OS seams (`FabricClient`, `Target.resolve` / `run_by` / `run_id`, `Catalog.fs_folder_lister`) through in-memory fakes |
| `run_mode` / `run_and_exit` | **black-box** — `run_runtime_blackbox(mode, ...)` seeds params + fakes and execs the ▶️ Run dispatch cell for every mode, covering the guard branches (missing `tenant_id`, unresolvable target, invalid `show` axis, the `notebookutils`-absent path); the two gates the black-box cannot reach are called directly in `tests/test_runtime_blackbox.py` |

The **test modules are the harness — never measured.** Deployment-specific policy rules are
outside the public fixture set; adopters test their own synthetic policy separately.

**Pragma policy — syscall lines only, never whole-file.** Coverage is closed by *running* the
logic, not by excluding it. Fakes (`requests`, `notebookutils`, `pyspark.sql.types`) let
header-building, payload-building, response-parsing and every branch execute for real. A
`# pragma: no cover` is reserved **only** for a single line that genuinely cannot run in CI — an
actual network/OS syscall — each with a one-clause reason. **Never** exclude a whole file or a
whole method, or any line containing logic: a gate that hides logic is worse than no gate.

The framework code currently carries **no** `# pragma: no cover` — every line runs under the fakes.

## Writing tests

Plain pytest functions. No base class, no `scope` marker, no registry — a file named `test_*.py`
holding functions named `test_*` is collected.

```python
import pytest

from _olaf_runtime import ScopePath, ValidationError


def test_folder_path_strips_slashes():
    assert ScopePath.folder("Files/exports/") == "/Files/exports"


@pytest.mark.parametrize("bad", ["Tables/nope", "randomdir/x"])
def test_folder_path_rejects_paths_outside_files(bad):
    with pytest.raises(ValidationError):
        ScopePath.folder(bad)
```

**Rules that keep the suite healthy**

1. Import the runtime from `_olaf_runtime`, the fakes and shared data from `_fakes`. Both are
   importable because `tests/` is pytest's rootdir — no path juggling.
2. One behaviour per test, named for the behaviour: `test_merge_upsert_never_deletes`, not
   `test_case_7`. A table of inputs is `@pytest.mark.parametrize`, not a loop — a loop reports one
   failure and hides the rest.
3. Shared setup is a **fixture**; shared data is a helper in `_fakes.py`. Do not copy a builder
   into a second module.
4. Fixtures are **mock data only** — invented schemas/roles/groups and reserved example domains,
   never real organization, customer, tenant, workspace, item, or principal values.
5. Keep a test in the module that matches its subject; each module's docstring names the notebook
   class it was converted from, so history stays traceable.
6. Anything needing a live workspace does not belong here — it belongs in the smoke notebook.

`tests/test_collection_integrity.py` fails the run if a module defines `test_` functions but is
named so pytest would never collect it — the one silent-loss shape that survives the move off the
old scope-based discovery.

## The live smoke notebook

`tests/olaf_test_smoke.ipynb` is self-contained (it reads nothing else under `tests/`). Do not
upload it until an authorized lab, external-access review, and rollback/cleanup plan exist. If
those controls exist, upload it alongside `notebooks/olaf.ipynb`, fill in only synthetic
parameters, and *Run all*.
It drives the runtime two ways — `notebookutils.notebook.run` per mode (the pipeline path) and
`%run` + `OLAF.<action>()` (the interactive path) — and prints one `PASS`/`FAIL` line per check.

CI never executes it. Its cells are procedures, not results, and they do not prove live behavior.

## Linting & formatting

```bash
python -m pip install --require-hashes --only-binary=:all: -r requirements/ci-lint.txt
python -m pip check

scripts/lint.sh          # check only (what CI runs): fails on lint findings or format drift
scripts/lint.sh --fix    # apply autofixes + reformat in place
```

**Style inside a notebook is not the same as the notebook being valid.** `lint.sh` reads the Python
in the cells; a separate structural check is still required. `tests/test_notebook_contracts.py`
checks each shipped notebook
against the nbformat v4 required properties **and their types**, written out by hand rather than
deferred to `nbformat.validate`: nbformat is not a test dependency, so an `importorskip` would skip
in exactly the environment that shipped the bug. The same file holds `NOTEBOOKS` (a glob, so a new
notebook is covered the moment it lands) and a check that `scripts/lint.sh`'s own hardcoded list
matches it — the two lists drifting apart is what let the invalid notebook through CI.

Config is in [`pyproject.toml`](../pyproject.toml) (`[tool.ruff]`): line length 100, and the
conservative default rule set (pyflakes `F` + pycodestyle `E4`/`E7`/`E9`) — no import-sorting or
opinionated refactor rules that would force behaviour-touching rewrites.

[`scripts/lint.sh`](../scripts/lint.sh) makes **two passes**, and the difference between them is
the point:

* **The five notebooks** (`olaf.ipynb`, `olaf_master_workflow.ipynb`, `olaf_runner.ipynb`,
  `olaf_cookbook.ipynb`, `olaf_test_smoke.ipynb`) go through
  `nbqa` with `--extend-ignore=F821,E402`. Each references names ruff cannot resolve in isolation —
  the Fabric-injected `spark`, papermill parameters, the runtime's public API pulled in by `%run` —
  and each has imports after a cell boundary. Every **other** rule still applies.
* **`tests/`** is linted **strictly**, no exemptions: plain `.py` with no injected globals and no
  magics, so neither false positive exists. The one carve-out is a per-file ignore for
  `tests/_fakes.py` (`F403`/`F405`), which star-imports the runtime on purpose — that is the
  namespace the fixtures notebook had after `%run olaf`, and enumerating it would be a second copy
  of the runtime's public surface, drifting the first time the runtime grows a name.

The per-notebook exemptions cannot be expressed as ruff `per-file-ignores`: nbqa lints a temp copy
whose name is the notebook stem plus a random suffix, and the stems here are prefixes of one
another (`olaf`, `olaf_cookbook`, `olaf_master_workflow`, `olaf_runner`, `olaf_test_smoke`), so no glob isolates
one from another.

## Reading the results

pytest's own output. A failure prints the assertion with both sides expanded:

```
FAILED tests/test_unit_apply.py::test_merge_upsert_never_deletes - AssertionError: assert [...] == [...]
```

For a parametrized case the id names the exact input: `test_falsy_spellings_submit_config_payload_with_an_omission_candidate[false]`.

A coverage failure looks like this — and the missing lines are line numbers **in the generated
module**, so open `tests/_olaf_runtime.py` to read them in context, then fix the notebook:

```
tests/_olaf_runtime.py   3606   3   1314   1   99%   1712-1714, 1720->1723
Coverage failure: total of 99.92 is less than fail-under=100.00
```
