# CI dependency locks

The CI jobs install only the hash-locked `ci-test.txt` and `ci-lint.txt` files. They are generated
from the small direct-dependency `.in` files so a review can distinguish the chosen tools from
their resolved transitive dependencies.

Regenerate a lock in an isolated maintenance environment, one command at a time. The committed
locks record the interpreter used in their headers; regenerate both current sets with Python 3.11.

Always compile on the **floor** interpreter of the CI matrix, never a newer one. A lock compiled on
a newer interpreter silently omits every `python_version < "X"` conditional dependency, and
`--require-hashes` then refuses the install rather than resolving them: a 3.11-compiled lock has no
`exceptiongroup` or `tomli`, so a 3.10 job fails with "all requirements must have their versions
pinned with ==". `tests/test_public_release.py` pins the floor against these headers for that
reason. When changing supported interpreter compatibility, resolve and validate every CI
interpreter before committing the replacement locks.

```bash
python3.11 -m pip install pip-tools==7.5.3
python3.11 -m piptools compile --generate-hashes --strip-extras \
  --output-file requirements/ci-test.txt requirements/ci-test.in

python3.11 -m pip install pip-tools==7.5.3
python3.11 -m piptools compile --generate-hashes --strip-extras \
  --output-file requirements/ci-lint.txt requirements/ci-lint.in
```

Before committing regenerated locks, run the intended job's install command and `python -m pip
check`. The locks must contain exact package pins and SHA-256 hashes only: no editable installs,
local paths, VCS/URL requirements, alternate indexes, or trusted hosts. CI also sets
`--only-binary=:all:` to avoid source-distribution build execution.

The checks follow pip's [secure-install guidance](https://pip.pypa.io/en/stable/topics/secure-installs/)
and [repeatable-install guidance](https://pip.pypa.io/en/stable/topics/repeatable-installs/).
