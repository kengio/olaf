#!/usr/bin/env bash
# Lint + format for every repository Python surface: shipped notebooks (via nbqa), the pytest suite,
# and repository scripts (ruff directly).
#
# Config lives in pyproject.toml ([tool.ruff]). This is the same check CI runs.
#
# Requires the reviewed lint lock: `python -m pip install --require-hashes -r requirements/ci-lint.txt`.
#
# Usage:
#   scripts/lint.sh          # check only (CI mode): non-zero exit on lint or format drift
#   scripts/lint.sh --fix    # apply autofixes + reformat in place (local dev)
set -euo pipefail

cd "$(dirname "$0")/.."

# ---------------------------------------------------------------------------------------------
# Notebooks — linted with two rules suppressed, and the reason is per-notebook, not a blanket
# allowance:
#
# olaf.ipynb is ONE self-contained notebook holding the library + entrypoints. Its `parameters`
# cell + ▶️ Run dispatch cell reference the Fabric-injected `spark` global and sit before the module
# imports, so F821 (undefined name) and E402 (import not at top) are false positives on it.
#
# olaf_master_workflow.ipynb drives the runtime end to end. Its `%run olaf` sits mid-notebook (the
# stages above it are notebook.run calls that need no library), so every import in the cells after
# it trips E402; `spark`, `display` and the `OLAF` facade all arrive from Fabric or from that %run,
# so ruff reads them as undefined (F821). Same two false positives, same reason.
#
# olaf_cookbook.ipynb is `%run olaf` + bare `OLAF.<action>()` demo calls — `OLAF` is defined by the
# %run'd runtime, which ruff cannot resolve in isolation (F821), and the %run precedes any code
# (E402). It is a live-Fabric example, NOT CI-executed.
#
# olaf_runner.ipynb is the slim pipeline wrapper: a `%%configure` JSON cell (a dict literal, as
# far as ruff can see), then parameters + one notebook.run dispatch. Its import sits after the
# configure and parameters cells (E402); the exempted pass costs it nothing else.
#
# olaf_test_smoke.ipynb is the live-only authorization protocol, and it is the one notebook here
# WITHOUT the %run shape: a parameters cell, then a single `import notebookutils` + guard cell. No
# %run and no Fabric-injected globals means F821 never fires on it — only E402 does, because that
# import necessarily follows the parameters cell. Exempting both costs nothing and keeps one list.
# Likewise never executed by CI.
#
# Every OTHER ruff rule (unused imports/vars, redefinitions, statement style, syntax) still applies
# to all of them. Deterministic; a per-file-ignore glob would flake because nbqa's temp-copy name
# makes the notebook stems unglobbable prefixes of one another (see pyproject.toml
# [tool.ruff.lint]).
notebooks=(
    notebooks/olaf.ipynb
    notebooks/olaf_cookbook.ipynb
    notebooks/olaf_master_workflow.ipynb
    notebooks/olaf_runner.ipynb
    tests/olaf_test_smoke.ipynb
)
notebook_ignore=(--extend-ignore=F821,E402)

# ---------------------------------------------------------------------------------------------
# Plain Python — tests and scripts are linted STRICTLY (no --extend-ignore): neither has
# Fabric-injected globals or magics. tests/_olaf_runtime.py is excluded in pyproject.toml
# ([tool.ruff] extend-exclude): conftest.py regenerates it from the notebook on every run, so it is
# a build artifact (and gitignored).
#
# `--exclude '*.ipynb'` because ruff >= 0.13 lints notebooks natively, so a bare `ruff check tests/`
# would ALSO pick up olaf_test_smoke.ipynb — under the strict rules, without the F821/E402
# exemptions its `%run` shape needs. Each notebook has exactly one linter: the nbqa pass above.
python_pass=(tests/ scripts/ --exclude '*.ipynb')

if [[ "${1:-}" == "--fix" ]]; then
    nbqa ruff --fix "${notebook_ignore[@]}" "${notebooks[@]}"
    nbqa "ruff format" "${notebooks[@]}"
    ruff check --fix "${python_pass[@]}"
    ruff format "${python_pass[@]}"
    bash -n scripts/*.sh
    echo "lint: autofixed + reformatted ${#notebooks[@]} notebooks + tests/ + scripts/"
else
    nbqa ruff "${notebook_ignore[@]}" "${notebooks[@]}"  # notebooks: F821/E402 suppressed
    nbqa "ruff format" --check "${notebooks[@]}"         # format drift check
    ruff check "${python_pass[@]}"                        # plain Python: strict, no exemptions
    ruff format --check "${python_pass[@]}"
    bash -n scripts/*.sh
    echo "lint: ${#notebooks[@]} notebooks + tests/ + scripts/ pass ruff check + format"
fi
