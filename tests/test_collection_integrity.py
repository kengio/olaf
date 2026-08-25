"""The false-green guard, ported to pytest.

The retired `scripts/run_tests.py` recomputed the CI-scope TestCase set independently of what
`run_suite` discovered, so a class present but never run (a `scope` typo, a SCOPES_FOR_MODE drift)
failed the job instead of passing quietly. That whole failure mode belonged to the bespoke
scope-based discovery and cannot recur under pytest, which collects by file/function name and
errors loudly on an unimportable module.

One residual shape survives the move: a module that DEFINES tests but is named so pytest never
collects it (a rename to `unit_scope.py`, a helper that grew a `test_` function). Nothing would
fail — the tests would simply stop running, and only the coverage gate might notice. These two
cases close that, and they are the only part of the old guard worth carrying over.
"""

from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
# `python_files = ["test_*.py"]` in pyproject.toml — the pattern pytest actually collects by.
COLLECTED_GLOB = "test_*.py"
# Generated on every run by conftest.py; it is the measured surface, never a test module.
GENERATED = {"_olaf_runtime.py"}


def python_modules():
    return [p for p in sorted(TESTS_DIR.glob("*.py")) if p.name not in GENERATED]


def defines_tests(path):
    return any(
        line.startswith("def test_") for line in path.read_text(encoding="utf-8").splitlines()
    )


@pytest.mark.parametrize("path", python_modules(), ids=lambda p: p.name)
def test_every_module_defining_tests_is_collectable(path):
    """A module that defines `test_` functions must match `python_files`, or pytest silently
    never runs it."""
    if not defines_tests(path):
        return
    assert path.match(COLLECTED_GLOB), (
        f"{path.name} defines test functions but does not match {COLLECTED_GLOB!r}, "
        "so pytest will not collect it"
    )


def test_the_harness_modules_define_no_tests():
    """conftest.py and _fakes.py are the harness. A `test_` function landing in either would be
    collected from conftest (never) or from a non-matching name (also never) — in both cases it
    would look written and never run."""
    for name in ("conftest.py", "_fakes.py"):
        assert not defines_tests(TESTS_DIR / name), f"{name} defines a test function"
