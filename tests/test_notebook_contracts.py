"""Static contracts on the shipped notebooks that CI can check without a Fabric kernel.

CI never executes a notebook, so a cell that Fabric would refuse fails for the first time on a
tenant — in front of whoever is running it. These are the refusals cheap enough to catch here.
"""

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
# GLOBBED, not listed. This was three hardcoded paths, and `scripts/lint.sh` carries a second
# hardcoded list of the same notebooks — so adding olaf_master_workflow.ipynb to lint.sh alone left
# it covered by NO contract test, and it shipped invalid (seven code cells missing execution_count)
# with every gate green. A glob cannot go stale that way; test_lint_covers_every_shipped_notebook
# below holds the other list to the same set.
NOTEBOOKS = sorted(REPO_ROOT.glob("notebooks/*.ipynb")) + sorted(REPO_ROOT.glob("tests/*.ipynb"))


def _cells(path, kind=None):
    """(index, source-text) per cell, optionally filtered to one cell_type. nbformat allows
    `source` to be a string or a list of lines; this repo's notebooks all use list, uniformly —
    normalize anyway, since nothing here depends on staying that way."""
    nb = json.loads(path.read_text(encoding="utf-8"))
    return [
        (i, c["source"] if isinstance(c["source"], str) else "".join(c["source"]))
        for i, c in enumerate(nb["cells"])
        if kind is None or c["cell_type"] == kind
    ]


def code_cells(path):
    """(index, source-text) for each code cell."""
    return _cells(path, "code")


def markdown_cells(path):
    """(index, source-text) for each MARKDOWN cell — the half code_cells() drops by design, and
    exactly where a prose platform-status claim hides: the bulk endpoint's stale "GA" assertion
    shipped in a markdown class header and survived every code-cell-only scan."""
    return _cells(path, "markdown")


def all_cell_text(path):
    """Every cell's source concatenated, markdown included."""
    return "\n".join(text for _, text in _cells(path))


@pytest.mark.parametrize("path", NOTEBOOKS, ids=lambda p: p.name)
def test_every_run_magic_is_alone_in_its_cell(path):
    """`%run` must be the ONLY thing in its cell. Fabric refuses a cell that mixes it with anything
    else — including a comment — with:

        MagicUsageError: %run cannot run with other code or magic commands.

    A comment that merely mentions `%run` is fine; what matters is whether a LINE dispatches it.
    Explanations belong in a markdown cell above the magic, which is where both callers keep them.
    """
    offenders = []
    for index, text in code_cells(path):
        lines = text.split("\n")
        magic = [n for n, line in enumerate(lines) if line.lstrip().startswith("%run")]
        if not magic:
            continue
        other = [n for n, line in enumerate(lines) if line.strip() and n not in magic]
        if other:
            offenders.append(
                f"cell {index}: %run on line(s) {[n + 1 for n in magic]} shares the cell with "
                f"{len(other)} other line(s), first at line {other[0] + 1}: {lines[other[0]][:60]!r}"
            )
    assert not offenders, f"{path.name} — " + " | ".join(offenders)


# ---------------------------------------------------------------------------------------------
# PARAM_DEFAULTS is the single source — proven by reading run_mode back, not by restating it


def test_param_defaults_is_complete_and_is_the_only_source():
    """PARAM_DEFAULTS exists so OLAF.params can answer "what will this run use" without keeping a
    second copy of every default. That only holds while run_mode resolves against it: the moment
    someone adds `params.get("new_thing", "some-literal")`, OLAF.params silently omits `new_thing`
    and the operator is back to guessing. This reads the source back and refuses both halves —
    a parameter missing from the map, and a default written as a literal beside it.
    """
    import re

    from _olaf_runtime import PARAM_DEFAULTS

    source = (REPO_ROOT / "tests" / "_olaf_runtime.py").read_text(encoding="utf-8")
    start = source.index("\ndef run_mode(")
    body = source[start : source.index("\ndef ", start + 1)]

    resolved = re.findall(r'params\.get\(\s*"([a-z_]+)",\s*([^)\n]+?)\s*[,)]', body)
    assert resolved, "run_mode stopped resolving parameters the way this test reads them"

    missing = sorted({key for key, _ in resolved} - set(PARAM_DEFAULTS))
    assert not missing, f"run_mode resolves {missing}, which OLAF.params would never show"

    literals = sorted(
        {f"{key}={default}" for key, default in resolved if default != f'PARAM_DEFAULTS["{key}"]'}
    )
    assert not literals, (
        f"default written beside the .get() instead of read from the map: {literals}"
    )


# ---------------------------------------------------------------------------------------------
# Every public OLAF method announces the frame it returns — checked structurally, not case by case


def test_no_public_olaf_method_returns_a_frame_silently():
    """`load_config` returned `OLAF._frame(...)` directly, and `explain` did it on three of its
    four exits — an empty config, an unresolvable one, and a config that would not generate. All
    four printed nothing at all, which is the exact failure the announcement was added to fix: a
    cell that came back empty and a cell that succeeded look identical until you render the frame.
    Nothing caught it, because each was a separate return statement nobody thought to test.

    So this reads the class rather than the behaviour: every `return` in a public method must hand
    its frame to `_announce` or `_returned`. A new method, or a new early exit in an old one, is
    caught the moment it is written.
    """
    import ast

    source = (REPO_ROOT / "tests" / "_olaf_runtime.py").read_text(encoding="utf-8")
    olaf = next(
        node
        for node in ast.parse(source).body
        if isinstance(node, ast.ClassDef) and node.name == "OLAF"
    )

    def returns_a_frame(node):
        """`return OLAF._frame(...)` / `._view(...)` — a frame handed back with nothing printed."""
        call = node.value
        return (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr in {"_frame", "_view", "_show_view", "_trace_view"}
        )

    silent = [
        (method.name, node.lineno)
        for method in olaf.body
        if isinstance(method, ast.FunctionDef) and not method.name.startswith("_")
        for node in ast.walk(method)
        if isinstance(node, ast.Return) and returns_a_frame(node)
    ]
    assert not silent, f"frame returned without an announcement: {silent}"


def test_the_parameters_cell_stays_aligned_per_section():
    """`=` and `#` line up within each `# -- section --` of the parameters cell.

    The cell is hand-aligned under `# fmt: off` (ruff format would collapse the inline-comment
    spacing), which means nothing but a reader catches drift -- and it has now drifted twice, both
    times from appending a parameter to a section without re-padding its neighbours:
    `role_backup_dir` widened the control-tables block, then `verbosity` landed one column short of
    it. This is the check that stops a third time.

    Per SECTION, not per file: a section's widths come from its own longest name and value, so
    `tenant_id` is not padded out to `mapping_history_dir`'s column.
    """
    import re

    runtime = next(p for p in NOTEBOOKS if p.name == "olaf.ipynb")
    src = next(s for _, s in code_cells(runtime) if "# fmt: off" in s and "mode " in s)
    assign = re.compile(r"^(\w+)( *)= (.*?)( +)# ")

    sections, current = [], []
    for line in src.split("\n"):
        if line.startswith("# -- "):
            if current:
                sections.append(current)
            current = []
        elif assign.match(line):
            current.append(line)
    if current:
        sections.append(current)
    assert len(sections) >= 6, sections  # the cell still has its sections

    for rows in sections:
        eq = {line.index(" = ") for line in rows}
        hash_ = {line.index("#") for line in rows}
        head = rows[0].split(" = ")[0].strip()
        assert len(eq) == 1, f"section at {head!r}: `=` at columns {sorted(eq)}"
        assert len(hash_) == 1, f"section at {head!r}: `#` at columns {sorted(hash_)}"
        # and the padding is MINIMAL — a section indented past its own longest row is drift too
        names = [line.split(" = ")[0].rstrip() for line in rows]
        values = [line.split(" = ", 1)[1].split("  #")[0].rstrip() for line in rows]
        assert eq.pop() == max(len(n) for n in names), f"section at {head!r}: over-padded names"
        assert max(len(v) for v in values) + 2 == len(rows[0].split(" = ", 1)[1].split("#")[0]), (
            f"section at {head!r}: over-padded values"
        )


@pytest.mark.parametrize("path", NOTEBOOKS, ids=lambda p: p.name)
def test_every_shipped_notebook_is_valid_nbformat(path):
    """Nothing in this repo checked that a notebook is a valid NOTEBOOK.

    `lint.sh` checks Python style inside the cells; the contracts above check `%run` isolation and
    parameter alignment. All of them pass on a file GitHub and Jupyter both refuse to open. Not
    hypothetical: `olaf_master_workflow.ipynb` shipped with seven code cells missing
    `execution_count`, every gate green, and the first thing to notice was GitHub's renderer showing
    "Invalid Notebook". A notebook nobody can open is a broken deliverable however clean its Python.

    Written out rather than deferred to `nbformat.validate` on purpose: nbformat is not a dependency
    here, so an importorskip would make this guard SKIP in exactly the environment that shipped the
    bug — a guard that cannot fail is not a guard.
    """
    nb = json.loads(path.read_text(encoding="utf-8"))

    assert nb.get("nbformat") == 4, f"{path.name}: nbformat must be 4, got {nb.get('nbformat')!r}"
    assert isinstance(nb.get("cells"), list) and nb["cells"], f"{path.name}: no cells"
    # top-level required properties. Omitted from the first version of this test, which was written
    # to catch a missing `execution_count` and reached only as far as that one bug.
    assert isinstance(nb.get("metadata"), dict), f"{path.name}: 'metadata' is a required property"
    minor = nb.get("nbformat_minor")
    assert isinstance(minor, int), f"{path.name}: 'nbformat_minor' is a required property"

    seen_ids = set()
    for i, cell in enumerate(nb["cells"]):
        kind = cell.get("cell_type")
        where = f"{path.name} cell {i} ({kind})"
        assert kind in ("code", "markdown", "raw"), f"{where}: bad cell_type"
        assert isinstance(cell.get("metadata"), dict), f"{where}: 'metadata' is a required property"

        # PRESENCE IS NOT ENOUGH -- the schema types these, and a hand-patched cell gets the type
        # wrong as easily as it drops the key. `source` as a list of non-strings and
        # `execution_count` as a string both survived the presence-only version of this test.
        src = cell.get("source")
        assert isinstance(src, str) or (
            isinstance(src, list) and all(isinstance(x, str) for x in src)
        ), f"{where}: 'source' must be a string or a list of strings"

        # `id` is REQUIRED from nbformat_minor 5 on. Every notebook here declares 5 and none of them
        # carried it: `nbformat.read` quietly repairs the file in memory and warns, so the only
        # symptom was a deprecation notice nobody sees in CI -- "will become a hard error" is the
        # whole reason to hold the line here rather than after it breaks.
        if minor >= 5:
            cid = cell.get("id")
            assert isinstance(cid, str) and 1 <= len(cid) <= 64, f"{where}: bad or missing 'id'"
            assert cid not in seen_ids, f"{where}: duplicate cell id {cid!r}"
            seen_ids.add(cid)

        if kind == "code":
            assert "execution_count" in cell, f"{where}: 'execution_count' is a required property"
            assert cell["execution_count"] is None or isinstance(cell["execution_count"], int), (
                f"{where}: 'execution_count' must be an integer or null"
            )
            assert isinstance(cell.get("outputs"), list), f"{where}: 'outputs' must be an array"


def test_lint_covers_every_shipped_notebook():
    """`scripts/lint.sh` keeps its own hardcoded notebook list, and nothing tied the two together.

    Adding a notebook to one and not the other is silent in both directions: miss lint.sh and the
    file is never style-checked; miss NOTEBOOKS (which is now a glob) and it is never contract-checked.
    """
    lint = (REPO_ROOT / "scripts" / "lint.sh").read_text(encoding="utf-8")
    listed = set(re.findall(r"^\s+((?:notebooks|tests)/[\w.]+\.ipynb)\s*$", lint, re.M))
    shipped = {str(p.relative_to(REPO_ROOT)) for p in NOTEBOOKS}

    # without this the test is vacuous the moment the glob stops matching: two empty sets compare
    # equal and it passes green on a repo with no notebooks at all.
    assert shipped, "the notebook glob matched nothing — NOTEBOOKS is broken, not lint.sh"
    assert listed == shipped, (
        f"scripts/lint.sh and the shipped notebooks disagree — "
        f"only in lint.sh: {sorted(listed - shipped)} · only on disk: {sorted(shipped - listed)}"
    )


def test_master_workflow_parameters_are_tagged_and_captured():
    """Issue #6's contract: the workflow's inputs live in the ONE cell Fabric treats as Base
    parameters (tagged 'parameters'), and every declared parameter is consumed downstream —
    the lakehouse by the constants cell's capture-by-value (`LAKEHOUSE = lakehouse_name`,
    the pattern every pre-%run input follows), the rest by reference — so a workflow input
    can neither be invisible to a pipeline nor silently dead."""
    import ast

    path = REPO_ROOT / "notebooks" / "olaf_master_workflow.ipynb"
    nb = json.loads(path.read_text(encoding="utf-8"))
    tagged = [c for c in nb["cells"] if "parameters" in c.get("metadata", {}).get("tags", [])]
    assert len(tagged) == 1  # exactly one Base-parameters surface
    assert tagged[0]["metadata"]["tags"] == ["parameters"]
    declared = {
        t.id
        for node in ast.parse("".join(tagged[0]["source"])).body
        if isinstance(node, ast.Assign)
        for t in node.targets
        if isinstance(t, ast.Name)
    }
    assert "lakehouse_name" in declared  # the issue-#6 input is a parameter, not a constant
    rest = "\n".join(
        "".join(c["source"])
        for c in nb["cells"]
        if c["cell_type"] == "code" and "parameters" not in c.get("metadata", {}).get("tags", [])
    )
    assert "LAKEHOUSE = lakehouse_name" in rest  # captured by value, like its siblings
    for name in sorted(declared):
        assert re.search(rf"\b{name}\b", rest), f"declared parameter {name!r} is never consumed"


def test_olaf_ships_with_no_session_binding_of_its_own():
    """`olaf.ipynb` must carry NO lakehouse binding — neither a `%%configure` cell nor a
    portal attachment (which Fabric persists under `metadata.dependencies`). The binding
    belongs to the wrapper notebook that %run-s or notebook.run-s olaf: a `%%configure`
    cell here would still execute inside a %run child, and its practical `-f` form would
    force-restart the session and wipe the caller's variables. See
    docs/fabric-import.md ("Bind the default lakehouse")."""
    path = REPO_ROOT / "notebooks" / "olaf.ipynb"
    for i, source in code_cells(path):
        assert not source.lstrip().startswith("%%configure"), f"cell {i} is a %%configure cell"
        for line in source.splitlines():
            assert not line.lstrip().startswith("%%configure"), f"cell {i} smuggles %%configure"
    nb = json.loads(path.read_text(encoding="utf-8"))
    deps = nb.get("metadata", {}).get("dependencies")
    assert not deps or "lakehouse" not in deps, "olaf.ipynb pins an attached lakehouse"


@pytest.mark.parametrize("name", ["olaf_master_workflow.ipynb", "olaf_runner.ipynb"])
def test_wrapper_binding_cell_names_the_parameters_cell_lakehouse(name):
    """Both pipeline wrappers OPEN with the session-binding cell, and its `parameterName` is
    locked to the parameters-cell lakehouse parameter: Fabric routes a pipeline's base
    parameters by name into BOTH channels (`%%configure` before the session exists, the
    parameters cell after), so the one `lakehouse_name` base parameter drives the session
    binding and setup's assertion together — a drift between the two names would silently
    leave one channel on its default."""
    import ast

    path = REPO_ROOT / "notebooks" / name
    index, first = code_cells(path)[0]
    lines = first.splitlines()
    assert lines[0].strip() == "%%configure -f", f"{name}: first code cell is not %%configure -f"
    body = json.loads("\n".join(lines[1:]))
    binding = body["defaultLakehouse"]["name"]
    assert binding["defaultValue"], f"{name}: defaultValue must be a visible placeholder"

    nb = json.loads(path.read_text(encoding="utf-8"))
    tagged = [c for c in nb["cells"] if "parameters" in c.get("metadata", {}).get("tags", [])]
    assert len(tagged) == 1, f"{name}: exactly one Base-parameters surface"
    declared = {
        t.id
        for node in ast.parse("".join(tagged[0]["source"])).body
        if isinstance(node, ast.Assign)
        for t in node.targets
        if isinstance(t, ast.Name)
    }
    assert binding["parameterName"] == "lakehouse_name", f"{name}: binding parameter renamed"
    assert binding["parameterName"] in declared, f"{name}: binding names no declared parameter"


def test_runner_parameters_mirror_the_runtime_surface():
    """`olaf_runner`'s parameters cell is a MIRROR of olaf's own, because its dispatch dict
    replaces the child's parameters cell wholesale: a runtime parameter missing from the runner
    could never be set from a pipeline through it, and a parameter olaf does not accept would be
    dead weight. Locked as set equality (plus the runner-only `timeout_seconds`), and every
    runtime parameter must be passed to notebook.run EXPLICITLY — an omitted key would silently
    fall back to olaf's default rather than the runner's."""
    import ast

    def declared_in(path):
        nb = json.loads(path.read_text(encoding="utf-8"))
        tagged = [c for c in nb["cells"] if "parameters" in c.get("metadata", {}).get("tags", [])]
        assert len(tagged) == 1 and tagged[0]["metadata"]["tags"] == ["parameters"]
        return {
            t.id
            for node in ast.parse("".join(tagged[0]["source"])).body
            if isinstance(node, ast.Assign)
            for t in node.targets
            if isinstance(t, ast.Name)
        }

    runner_path = REPO_ROOT / "notebooks" / "olaf_runner.ipynb"
    runner = declared_in(runner_path)
    runtime = declared_in(REPO_ROOT / "notebooks" / "olaf.ipynb")
    assert runtime, "olaf's parameters cell declared nothing — the mirror check is broken"
    assert runner == runtime | {"timeout_seconds"}, (
        f"runner is not an honest mirror — missing: {sorted(runtime - runner)} · "
        f"extra: {sorted(runner - runtime - {'timeout_seconds'})}"
    )
    dispatch = "\n".join(
        src for c, src in code_cells(runner_path) if "notebookutils.notebook.run" in src
    )
    assert dispatch, "the runner lost its dispatch cell"
    for name in sorted(runtime):
        assert f'"{name}": {name}' in dispatch, f"{name!r} is not passed explicitly"
    assert re.search(r"\btimeout_seconds\b", dispatch), "the child run's timeout is not used"


def test_the_runtime_import_surface_matches_the_documented_fabric_packages():
    """README's badge and SECURITY/CONTRIBUTING say the runtime needs no pip installs
    because every non-stdlib import is preinstalled in the Fabric Spark runtime — and they
    NAME the set. Hold the notebook to it: a new third-party import must update those docs
    (and the Fabric runtime must actually provide the package) before it ships."""
    import ast
    import sys

    documented = {"notebookutils", "pandas", "pyspark", "requests"}
    src = "\n\n".join(text for _, text in code_cells(REPO_ROOT / "notebooks" / "olaf.ipynb"))
    roots = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            roots |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    third_party = roots - set(sys.stdlib_module_names)
    assert third_party == documented, (
        f"non-stdlib imports {sorted(third_party)} != documented {sorted(documented)} — "
        "update README.md / SECURITY.md / CONTRIBUTING.md (and confirm the Fabric runtime "
        "preinstalls the package) before shipping a new import"
    )


def test_public_dar_omission_contract_uses_candidates_not_deletion_fields():
    """The Preview bulk request can omit a prior-live role, but OLAF must not describe that
    candidate as a platform deletion. Keep the public API, data model, audit API, and source
    docstring synchronized with the runtime's deliberately qualified record fields."""
    notebook = "\n".join(source for _, source in code_cells(REPO_ROOT / "notebooks" / "olaf.ipynb"))
    public_contract = "\n".join(
        (REPO_ROOT / path).read_text(encoding="utf-8")
        for path in (
            "docs/api/OLAF.md",
            "docs/api-reference.md",
            "docs/data-model.md",
            "docs/api/Log.md",
            "docs/api/Deployment.md",
        )
    )

    assert "create | update | no_change | omit" in notebook
    for label in (
        "omission_candidate",
        "omitted_role_candidates",
        "drift_omission_candidates",
        "post_state_review_required",
        "prior_live_role_candidates",
        "submitted",
        "unknown",
    ):
        assert label in public_contract, f"public contract omits runtime label {label!r}"

    for legacy in (
        "`deleted`",
        "drift_deletes",
        "delete_message",
        "delete_status",
    ):
        assert legacy not in public_contract, f"legacy deletion field remains: {legacy!r}"

    assert "apply DELETES it by default" not in notebook

    # The suite SHIPS. A stale envelope key or a deletion outcome asserted in the fakes and
    # renderer fixtures reads as the runtime contract to anyone browsing tests/ — and
    # `drift_deletes` outlived the runtime field it named by exactly that route.
    support = "\n".join(
        (REPO_ROOT / path).read_text(encoding="utf-8")
        for path in ("tests/_fakes.py", "tests/test_result_output.py")
    )
    assert "drift_deletes" not in support, (
        "the shipped suite still names the retired drift_deletes envelope key"
    )
    assert "keep_unmanaged=False) DELETES it" not in support, (
        "the shipped suite still calls an omitted role a confirmed platform deletion"
    )

    # reset() submits an EMPTY DAR payload and leaves the control tables in place; cleanup() is
    # the mode that deletes. A guard described as firing "before it deletes anything" asserts a
    # deletion reset does not perform.
    for doc in ("docs/api-reference.md", "docs/api/errors.md"):
        # Collapse whitespace first: errors.md wrapped this very phrase across a line break, so a
        # raw substring pin read as green while the claim was still on the page.
        prose = re.sub(r"\s+", " ", (REPO_ROOT / doc).read_text(encoding="utf-8"))
        assert "before it deletes anything" not in prose, (
            f"{doc} describes reset() as deleting; it submits an empty payload"
        )


def test_public_claims_do_not_invent_live_probe_or_github_configuration_evidence():
    """Public text may cite official behaviour, but local tests cannot prove a Fabric probe or
    a remote repository setting. Keep those claims out of the shipped notebook and policy."""
    notebook = "\n".join(source for _, source in code_cells(REPO_ROOT / "notebooks" / "olaf.ipynb"))
    security = (REPO_ROOT / "SECURITY.md").read_text(encoding="utf-8")

    assert "live-probed" not in notebook
    assert "per-role ETag honored" not in notebook
    assert "public-release gate verifies that private reporting is enabled" not in security
    assert "owner must manually confirm" in security
    assert "docs.github.com/en/code-security" in security


def test_no_shipped_file_calls_the_preview_bulk_endpoint_a_ga_write():
    """OLAF's mutating dependency is the bulk DAR create/update endpoint, which Microsoft labels
    **Preview** — and this repository's own public contract says so in SECURITY.md,
    docs/api/FabricClient.md, and docs/platform-contract.md, the last of which turns it into the
    release-status claim ("community Preview", not production-ready). A shipped file that calls
    the SAME endpoint a GA write asserts the opposite of the documented posture, and an evaluator
    reading the runtime rather than the docs would draw the opposite conclusion. So pin the stale
    claim absent everywhere it shipped, and require its replacement to actually carry the status
    rather than merely drop the sentence.

    Capital-P "Preview" is the load-bearing token: lowercase "preview" is this repo's OTHER,
    unrelated concept — the zero-write dry run that explain()/generate return — so a
    case-insensitive check here would be satisfied by a sentence about a dry run.
    """
    notebook = all_cell_text(REPO_ROOT / "notebooks" / "olaf.ipynb")
    push_failure_suite = (REPO_ROOT / "tests" / "test_apply_push_failure.py").read_text(
        encoding="utf-8"
    )
    shipped = {
        "notebooks/olaf.ipynb": notebook,
        "tests/test_apply_push_failure.py": push_failure_suite,
    }

    for name, source in shipped.items():
        assert not re.search(r"\bGA\b", source), (
            f"{name} still asserts a GA release status for the Preview bulk endpoint"
        )
        assert not re.search(r"generally available", source, re.IGNORECASE), (
            f"{name} still asserts a GA release status for the Preview bulk endpoint"
        )

    header = next(
        text
        for _, text in markdown_cells(REPO_ROOT / "notebooks" / "olaf.ipynb")
        if "### `FabricClient`" in text
    )
    assert re.search(r"bulk full-set PUT[^.]*\bPreview\b", header), (
        "the FabricClient header must state the BULK PUT's Preview status, not only the granular "
        "surface's"
    )

    put_roles_docstring = notebook.split("Bulk full-set PUT", 1)[1].split('"""', 1)[0]
    assert "Preview" in put_roles_docstring, (
        "put_roles' opening docstring must name the endpoint's Preview status"
    )
    assert "Preview" in push_failure_suite.split('"""', 2)[1], (
        "the push-failure suite's module docstring must name the endpoint's Preview status"
    )


def test_the_run_cell_allows_exactly_the_runtimes_known_modes():
    """The ▶️ Run cell hands `run_and_exit` a hardcoded `allowed=` set; `run_mode` holds its own
    `KNOWN_MODES`. Two copies of one list — and the Run cell is the single code cell the coverage
    gate cannot see, because tests/conftest.py drops it by its `run` tag before extracting the
    runtime. So a drift between them is invisible to every local gate and fails for the first time
    on a real Fabric pipeline: a mode added to KNOWN_MODES but not to the Run cell simply cannot be
    selected by `notebook.run`. This is the same shape as the second hardcoded notebook list in
    scripts/lint.sh that once left a shipped notebook covered by no contract test at all."""
    import ast

    nb = json.loads((REPO_ROOT / "notebooks" / "olaf.ipynb").read_text(encoding="utf-8"))
    run_cells = [
        "".join(c["source"])
        for c in nb["cells"]
        if c["cell_type"] == "code" and "run" in c.get("metadata", {}).get("tags", [])
    ]
    assert len(run_cells) == 1, f"expected exactly one `run`-tagged cell, found {len(run_cells)}"

    def set_literal(node):
        assert isinstance(node, ast.Set), f"expected a set literal, got {type(node).__name__}"
        return {e.value for e in node.elts}

    allowed = None
    for node in ast.walk(ast.parse(run_cells[0])):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "run_and_exit":
            allowed = next(set_literal(k.value) for k in node.keywords if k.arg == "allowed")
    assert allowed, "the Run cell no longer calls run_and_exit(allowed={...})"

    library = "\n\n".join(text for _, text in code_cells(REPO_ROOT / "notebooks" / "olaf.ipynb"))
    known = None
    for node in ast.walk(ast.parse(library)):
        if isinstance(node, ast.Assign) and any(
            getattr(t, "id", None) == "KNOWN_MODES" for t in node.targets
        ):
            known = set_literal(node.value)
    assert known, "run_mode no longer defines KNOWN_MODES as a set literal"

    assert allowed == known, (
        f"the Run cell's allowed set and run_mode's KNOWN_MODES have drifted — "
        f"only in Run: {sorted(allowed - known)}; only in KNOWN_MODES: {sorted(known - allowed)}"
    )


def test_the_smoke_protocol_holds_no_runtime_code():
    """`tests/olaf_test_smoke.ipynb` is an authorization gate and a written protocol, not a suite:
    a parameters cell of flags, then one guard cell that stops the run unless every flag is set.

    Two places described it as something else — notebooks/README.md counted it among the notebooks
    that `%run olaf`, "for its own facade-driven checklist (§12)", and scripts/lint.sh justified its
    F821 exemption with "the same %run shape". Neither was true: the notebook has no `%run`, no
    facade call, no Spark, and no §12, and ruff's strict pass raises only E402 on it.

    Pin the property rather than the prose. If this notebook ever grows real runtime code, the
    exemption rationale and both descriptions need revisiting, and this is what will say so.
    """
    smoke = REPO_ROOT / "tests" / "olaf_test_smoke.ipynb"
    everything = all_cell_text(smoke)
    code = "\n".join(text for _, text in code_cells(smoke))

    # `spark.` not `spark`: the notebook legitimately RECORDS a spark_version, it just must never
    # touch a Spark session.
    for absent in ("%run", "OLAF.", "spark."):
        assert absent not in code, (
            f"the smoke protocol now contains {absent!r} — it holds runtime code, so revisit "
            "notebooks/README.md and the F821 exemption rationale in scripts/lint.sh"
        )
    assert "§12" not in everything, "the retired '§12' cross-reference is back"
    assert "notebookutils.notebook.exit" in code, (
        "the guard that stops an unauthorized run is the notebook's whole purpose"
    )


def test_the_smoke_guard_covers_every_documented_authorization_item():
    """docs/live-smoke-test.md lists what must be recorded BEFORE any live action, and the smoke
    notebook's guard is what mechanically enforces it. They were badly out of step: the guard
    checked ten flags, but they mapped to under half the page's requirements — no notebook
    checksum, no Spark version, no API test date, no target class, no operator identity, no
    access-effect scope, no time window, no recovery pointer, no cleanup owner.

    Passing the guard therefore did not mean the gate had been satisfied, which is the worst thing
    a safety check can do: it reads as authorization while covering half the ground. Hold the guard
    to the page it claims to enforce.
    """
    import ast

    code = "\n".join(text for _, text in code_cells(REPO_ROOT / "tests" / "olaf_test_smoke.ipynb"))
    checked = set()
    for node in ast.walk(ast.parse(code)):
        if isinstance(node, ast.Assign) and any(
            getattr(t, "id", None) == "required" for t in node.targets
        ):
            checked = {k.value for k in node.value.keys}
    assert checked, "the guard no longer builds a `required` mapping"

    for item in (
        "exact_commit_sha",
        "notebook_checksum",
        "runtime_version",
        "spark_version",
        "api_test_date",
        "target_class",
        "operator_identity_class",
        "access_effect_scope",
        "window_and_max_duration",
        "dar_snapshot_and_etag_captured",
        "recovery_pointer",
        "cleanup_owner_and_success_criteria",
        "attestation_evidence_reference",
    ):
        assert item in checked, (
            f"the guard stopped requiring {item!r}, which docs/live-smoke-test.md's authorization "
            "gate demands before any live action"
        )

    # The gate is about a record, not a mood: every flag must default to empty/False in the shipped
    # file, so an operator cannot inherit someone else's authorization by opening the notebook.
    for node in ast.walk(ast.parse(code)):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            value = node.value.value
            if isinstance(value, (str, bool)):
                assert not value, (
                    f"{getattr(node.targets[0], 'id', '?')} ships pre-filled; the authorization "
                    "record must be entered per run, never inherited"
                )


def test_no_shipped_file_still_names_the_retired_since_accessor():
    """`Audit.since()` became `Audit.provenance()`, and the rename escaped THREE sweeps in the
    same disguise every time: a bare slash-separated list (`runs / grants / since / timeline`),
    which matches neither the backticked spelling nor the call-site one that each sweep grepped
    for. This pin exists because a fourth instance turned up after the third fix, so the shape
    itself -- not any one occurrence -- is what needs holding down.

    `since=` stays legal: it is an unrelated inclusive time floor on runs() and failures()."""
    offenders = []
    for path in sorted(REPO_ROOT.rglob("*")):
        if not path.is_file() or path.suffix not in {".md", ".py", ".ipynb"}:
            continue
        rel = path.relative_to(REPO_ROOT).as_posix()
        # _olaf_runtime.py is gitignored and regenerated from the notebook by conftest, so it
        # cannot drift on its own -- pinning it would just report the notebook defect twice.
        if (
            rel.startswith((".venv/", ".superpowers/", ".git/", ".worktree/"))
            or rel == "tests/_olaf_runtime.py"
        ):
            continue
        if rel == "tests/test_notebook_contracts.py":  # this file names the shape on purpose
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for pattern in (
            r"/\s*since\s*/",  # the bare slash-separated list form
            r"\bAudit\.since\b",
            r"\bOLAF\.since\b",
            r"\.since\(",
            r'"since":',  # a grant-provenance column key
        ):
            for m in re.finditer(pattern, text):
                line = text[: m.start()].count("\n") + 1
                offenders.append(f"{rel}:{line}: {m.group(0)!r}")
    assert not offenders, "retired `since` accessor/column still named in:\n  " + "\n  ".join(
        offenders
    )
