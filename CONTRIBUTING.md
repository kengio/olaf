# Contributing to OLAF

Thanks for taking an interest. This repository has an **unusual shape** — the deliverable is a
single Fabric notebook, and the test suite is generated from it on every run — so the five minutes
it takes to read this page will save you an afternoon of guessing.

> **Found a security vulnerability?** Do **not** open an issue or a pull request. Report it
> privately — see [SECURITY.md](SECURITY.md).

## The shape of this repo, in one table

| Path | What it is |
|---|---|
| `notebooks/olaf.ipynb` | **The deliverable.** One self-contained notebook holding the whole library plus its entrypoints. This is what you edit. |
| `notebooks/olaf_master_workflow.ipynb` | End-to-end driver notebook, one stage per cell. |
| `notebooks/olaf_cookbook.ipynb` | Usage examples. Never executed by CI. |
| `tests/` | The pytest suite. Plain `.py`, no Fabric, no Spark, no network. |
| `tests/olaf_test_smoke.ipynb` | Optional, sanitized live-validation protocol. Never executed by CI and never release evidence by itself. |
| `docs/` | The reference docs — start at [`docs/README.md`](docs/README.md). |
| `configs/` | The starter config workbook template. |
| `scripts/lint.sh` | The lint entry point, and exactly what CI runs. |

There is no `src/`, no package, and no build step. The notebook *is* the source.

## Dev setup

**Python ≥ 3.11** and nothing else at runtime — no Fabric or Spark. Installing the reviewed
hash-locked wheels may require package-index access; the local suite itself makes no network calls.
`pyspark` is optional; the fakes install a minimal stand-in when the real one is absent, so the
suite behaves identically on a bare interpreter and on a Fabric image.

```bash
git clone https://github.com/kengio/olaf.git
cd olaf

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

python -m pip install --require-hashes --only-binary=:all: -r requirements/ci-test.txt
python -m pip install --require-hashes --only-binary=:all: -r requirements/ci-lint.txt
python -m pip check
```

These are exactly the hash-locked CI dependency sets consumed by
[`.github/workflows/test.yml`](.github/workflows/test.yml). Regenerate them only through the
[requirements instructions](requirements/README.md), then verify the intended Python versions.

Runtime package availability and versions vary with the selected Microsoft Fabric runtime. OLAF's
supported baseline is Fabric Runtime 1.3 / Spark 3.5 or newer; operators must verify the selected
runtime and required imports at startup. Do not describe the bundled package set as permanent.
Microsoft publishes the [runtime lifecycle](https://learn.microsoft.com/en-us/fabric/data-engineering/lifecycle)
and [OneLake security limitations](https://learn.microsoft.com/en-us/fabric/onelake/security/data-access-control-model#onelake-security-limitations).
Everything installed above is for local testing and linting.

## Tests

```bash
pytest                  # the suite
pytest --cov            # ...plus the 100% coverage gate — this is what CI runs
pytest tests/test_unit_validation.py -k c11    # one module, one rule family
```

**The one thing that surprises everyone:** pytest cannot import a notebook, so
[`tests/conftest.py`](tests/conftest.py) **regenerates `tests/_olaf_runtime.py` from
`notebooks/olaf.ipynb` on every run**. Tests import that module; coverage measures it. Two
consequences you need on day one:

- **`tests/_olaf_runtime.py` is a build artifact** — gitignored, excluded from lint, overwritten on
  every run. **Never edit it; edit the notebook.** If you are patching the generated file, you are
  editing the wrong thing.
- **A notebook edit is reflected immediately** — change a cell, run `pytest`, and you are testing
  the change.

**`pytest --cov` IS the gate:** `fail_under = 100` with `branch = true` in
[`pyproject.toml`](pyproject.toml), so the run fails below 100% line and branch coverage. **Your PR
must keep it at 100%** — new logic in the notebook needs new tests in the same PR. A coverage
failure reports line numbers in the *generated* module, so read them in `tests/_olaf_runtime.py`,
then fix the **notebook**.

[`docs/testing.md`](docs/testing.md) owns the rest and is worth reading before you add a test: how
the extraction works, how the black-box path reaches the entrypoints, the `# pragma: no cover`
policy, the rules that keep the suite healthy, and what belongs in the live smoke notebook instead.

## Lint and formatting

```bash
scripts/lint.sh          # check only — exactly what CI runs
scripts/lint.sh --fix    # apply autofixes + reformat in place
```

Two passes, because notebooks and plain Python cannot be held to the same rules: the **notebooks**
go through `nbqa` with `F821`/`E402` suppressed (they reference Fabric-injected globals and the
`%run`-imported runtime, which ruff cannot resolve in isolation), while **`tests/`** and
**`scripts/`** are linted strictly with no exemptions. Every other rule applies to both. Config is in
[`pyproject.toml`](pyproject.toml); the reasoning, and why those exemptions cannot be ruff
`per-file-ignores`, is in [`docs/testing.md`](docs/testing.md#linting--formatting).

One rule is on you: **if you add a notebook, add it to the `notebooks` array in
[`scripts/lint.sh`](scripts/lint.sh)** — a test fails the build if that list and the shipped set
drift apart.

## Editing the notebook

`notebooks/olaf.ipynb` is a JSON document that has to stay a **valid notebook**, not just valid
Python. Style checks pass happily on a file Jupyter and GitHub both refuse to open. That is not
hypothetical — `olaf_master_workflow.ipynb` shipped with seven code cells missing `execution_count`
and every gate green.

Rules, all of them enforced by `tests/test_notebook_contracts.py`:

- **Preserve the JSON structure.** nbformat 4, a non-empty `cells` list, top-level `metadata`, and
  every required per-cell property present and correctly typed — `execution_count` included.
- **Never run a wholesale formatter over the notebook.** No `ruff format` on the file directly, no
  "format document" from an editor, no `jq`-style rewrite of the JSON. Use `scripts/lint.sh --fix`,
  which routes formatting through `nbqa` so it touches cell *source* and leaves the notebook
  envelope alone. A bulk reformat is also an unreviewable diff.
- **`%run` must be alone in its cell** — Fabric refuses a cell that mixes it with anything else,
  including a comment. Explanations go in a markdown cell above.
- **The `parameters` cell is hand-aligned under `# fmt: off`.** `=` and `#` line up *within each
  section*. Adding a parameter means re-padding its section's neighbours.
- **Every new parameter goes in `PARAM_DEFAULTS`**, not as a literal default beside a
  `params.get(...)` call — otherwise `OLAF.params` silently stops reporting it.
- **Every `return` in a public `OLAF` method must go through the announce/return helpers**, so a
  cell that came back empty never looks like a cell that succeeded.

Keep diffs small: one behaviour per cell edit where you can, so the JSON diff stays readable.

## Documentation

Docs live in `docs/` and are indexed by [`docs/README.md`](docs/README.md). If you change behaviour,
change the doc that describes it in the **same PR** — and check whether the same statement appears
elsewhere (the runbook, the mode manual, the data model and the architecture doc overlap by design).

**Internal links are gated by CI.** `tests/test_doc_links.py` resolves every internal markdown link
in every `.md` file *and* every markdown cell of every notebook — both the target **file** and the
target **anchor**. A renamed heading silently kills every link to it, so run the suite after touching
a heading:

```bash
pytest tests/test_doc_links.py
```

External `http(s)` links are **advisory only** — `scripts/check_external_links.py` runs in its own
`continue-on-error` CI job, because a rate-limited or briefly-down third party should not fail a
build that has nothing wrong with it. Read its report; it will not block you.

## Versioning

`__version__` in `notebooks/olaf.ipynb` is stamped into `framework_version` on audit rows. Release
tags use `v{__version__}`, and the annotated tag object must be created with the **sanitized
maintainer identity**, never a personal one. A tag object is public and immutable, and
`scripts/check_public_release.py` has `tree` and `archive` modes only — nothing catches a bad
tagger afterwards. The identity is deliberately absent from every tracked file (the gate reports
`APPROVED_IDENTITY_CONTEXT` if it appears in one), so read it off the previous release tag rather
than typing it:

```
git -c user.name="$(git for-each-ref --format='%(taggername)' refs/tags/vPREV)" \
    -c user.email="$(git for-each-ref --format='%(taggeremail:trim)' refs/tags/vPREV)" \
    tag -a vX.Y.Z -m 'release: OLAF vX.Y.Z'
```

Put user-visible
changes under `Unreleased` in `CHANGELOG.md`; release maintainers move them into a dated version
section as part of the release review. Do not bump a version or create a tag in an ordinary pull
request unless the pull request is explicitly the release change.

## Pull requests

- **CI must be green.** Every PR runs the suite with the coverage gate across the Python matrix, plus
  the lint job. The external-links job is advisory and may show red without blocking. Config:
  [`.github/workflows/test.yml`](.github/workflows/test.yml).
- **Keep PRs small and focused.** One concern per PR. A behaviour change, its tests, and its doc
  update belong together; unrelated cleanups belong in their own PR. Reviewing a notebook diff is
  harder than reviewing a `.py` diff, and a large one is effectively unreviewable.
- **Say what changed and why**, and — for anything touching validation, member resolution, the target
  guard, or `apply` — say what the security consequence is. Read the
  [trust model](SECURITY.md#trust-model) first; a change that weakens a gate described there needs to
  argue for itself explicitly.
- **No real identifiers anywhere** — not in code, tests, fixtures, docs, commit messages, or PR
  descriptions. No tenant, workspace, lakehouse, group or user names, and no objectIds.
- **Run it locally before pushing:**

  ```bash
  pytest --cov && scripts/lint.sh
  ```

- Notable changes get a `CHANGELOG.md` entry ([Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
  format). If you are unsure whether yours qualifies, propose one and let the review decide.

Anything that needs a live Fabric workspace is **not verified by CI**. Say so in the PR and do not
turn code review or an unrecorded smoke run into a public platform claim. A retained live result
must identify the exact release SHA, Fabric Runtime, API date, authorized target class, access
effect, restore point, and cleanup outcome; otherwise describe the behavior as unverified. See
[the platform evidence contract](docs/platform-contract.md#evidence-status).
