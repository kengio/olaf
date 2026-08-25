<!-- Thanks for contributing. CONTRIBUTING.md explains the unusual shape of this repo
     (the deliverable is a notebook; the test suite is regenerated from it on every run) —
     the five minutes it takes to read will save you an afternoon. -->

## What changed, and why

<!-- One concern per PR. For anything touching validation, member resolution, the target
     guard, or apply: say what the security consequence is (see SECURITY.md's trust model —
     a change that weakens a gate described there needs to argue for itself explicitly). -->

## Checklist — the same gates CI runs

- [ ] `pytest --cov` passes locally — the full suite, with **100% line AND branch coverage**
      held (new logic ships with its tests in the same PR)
- [ ] `scripts/lint.sh` passes (ruff check + format, notebooks via nbqa and `tests/` strict)
- [ ] Notebook edits were made **byte-safely**: cell sources edited programmatically, JSON
      structure preserved, no wholesale formatter run over the `.ipynb` (CONTRIBUTING.md →
      "Editing the notebook"); `tests/_olaf_runtime.py` was never edited by hand (it is a
      build artifact)
- [ ] Docs updated in the same PR wherever a claim this change touches appears — the runbook,
      the mode manual, the data model, and the architecture doc overlap by design; zero stale
      claims left behind (`pytest tests/test_doc_links.py` covers links and anchors)
- [ ] No real identifiers anywhere — code, tests, fixtures, docs, commit messages, this PR:
      no tenant/workspace/lakehouse/group/user names, no objectIds
- [ ] Notable changes have a `CHANGELOG.md` entry (Keep a Changelog format); unsure whether
      yours qualifies — propose one and let review decide
