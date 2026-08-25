"""pytest harness for OLAF.

The deliverable stays ONE self-contained Fabric notebook (`notebooks/olaf.ipynb`) by
design — that is the shipping constraint. For CI we extract its library/entrypoint cells — everything except the
`parameters` cell, the ▶️ Run dispatch cell, and IPython %-magics — into a real
`tests/_olaf_runtime.py` so pytest-cov can trace it by path and enforce the 100 %
branch gate the bespoke `coverage_run.py` used to.

`run_runtime_blackbox` execs the runtime's ▶️ Run dispatch cell in a namespace
seeded with the traced module's globals, so `run_mode`/`run_and_exit` reached that
way are the SAME traced objects — the mock_integration coverage path.
"""

from __future__ import annotations

import pytest

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RUNTIME_NOTEBOOK = REPO_ROOT / "notebooks" / "olaf.ipynb"
GENERATED_RUNTIME = Path(__file__).resolve().parent / "_olaf_runtime.py"


def _code_cells(path):
    nb = json.loads(path.read_text(encoding="utf-8"))
    return [
        ("".join(c["source"]), c.get("metadata", {}).get("tags", []))
        for c in nb["cells"]
        if c["cell_type"] == "code"
    ]


def _is_library_cell(src, tags):
    """Drop the parameters cell, the ▶️ Run dispatch cell, and %-magics; keep the rest."""
    if src.startswith("%"):
        return False
    if "parameters" in tags or src.lstrip().startswith("# PARAMETERS"):
        return False
    if "run" in tags:
        return False
    return True


def _write_runtime_module():
    """Extract the runtime's library surface to tests/_olaf_runtime.py (idempotent)."""
    parts = [src for src, tags in _code_cells(RUNTIME_NOTEBOOK) if _is_library_cell(src, tags)]
    body = (
        '"""AUTO-GENERATED from notebooks/olaf.ipynb by tests/conftest.py — do not edit.\n'
        "The runtime deliverable is the notebook; this is the extracted library surface\n"
        'under test (pytest-cov traces THIS file). Regenerated on every test run."""\n\n'
        + "\n\n".join(parts)
        + "\n"
    )
    if not GENERATED_RUNTIME.exists() or GENERATED_RUNTIME.read_text(encoding="utf-8") != body:
        GENERATED_RUNTIME.write_text(body, encoding="utf-8")


# Regenerate at import time so a notebook edit is always reflected before collection/coverage.
_write_runtime_module()


@pytest.fixture(autouse=True)
def _pin_verbosity(tmp_path, monkeypatch):
    """The suite asserts the FULL printed output, which is now the `verbose` level rather than the
    only level. Pin it for every test so those assertions keep testing what they were written to
    test -- what a line SAYS -- and let tests/test_verbosity.py override the pin to test the other
    thing: WHICH lines appear at each level, including the real default (`info`).

    Restored after every test: `Say.override` and `OLAF._base_params` are module/class state in a
    single-namespace notebook, and a leak between tests is a false pass waiting to happen.
    """
    import _olaf_runtime as rt

    saved_override, saved_base = rt.Say.override, dict(rt.OLAF._base_params)
    # Fabric mounts /lakehouse/default; local pytest does not. Keep the production fixed path
    # intact in the notebook while giving every test an isolated operation-sentinel filesystem.
    monkeypatch.setattr(
        rt.ControlBoundary,
        "SENTINEL_FULL_PATH",
        str(tmp_path / ".olaf-sensitive-write.sentinel"),
    )
    rt.Say.override = "verbose"
    try:
        yield
    finally:
        rt.Say.override, rt.OLAF._base_params = saved_override, saved_base
