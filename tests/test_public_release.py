"""Black-box release-hygiene checks using controlled, disposable fixtures.

These tests exercise the public-release scanner as a command-line gate. Fixtures contain no real
identifiers and build the few intentionally bad values at runtime, so the test source is safe to
ship and a scanner cannot pass merely by exempting its own source file.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SCANNER = REPO_ROOT / "scripts" / "check_public_release.py"
SECRET_GATE = REPO_ROOT / "scripts" / "check_secrets.py"


def run_gate(*args: str | Path, cwd: Path = REPO_ROOT) -> subprocess.CompletedProcess[str]:
    """Run the actual scanner and return its deliberately redacted output."""
    return subprocess.run(
        [sys.executable, str(SCANNER), *(str(arg) for arg in args)],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def codes(result: subprocess.CompletedProcess[str]) -> set[str]:
    """Issue codes are the stable public contract; raw discovered values must never be printed."""
    found = set()
    for line in result.stdout.splitlines():
        if line.startswith("[") and "]" in line:
            found.add(line[1 : line.index("]")])
    return found


def write_notebook(path: Path, *, execution_count: int | None, outputs: list[object]) -> None:
    path.write_text(
        json.dumps(
            {
                "nbformat": 4,
                "nbformat_minor": 5,
                "metadata": {},
                "cells": [
                    {
                        "cell_type": "code",
                        "id": "release-fixture",
                        "metadata": {},
                        "source": "print('fixture')\n",
                        "execution_count": execution_count,
                        "outputs": outputs,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def write_zip(path: Path, members: dict[str, str | bytes]) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for name, value in members.items():
            archive.writestr(name, value)


def png_chunk(kind: bytes, data: bytes) -> bytes:
    """A PNG scanner needs only the chunk framing; no image decoder is involved in this test."""
    return len(data).to_bytes(4, "big") + kind + data + b"\0\0\0\0"


def test_tree_rejects_non_latin_and_private_identifier_fixtures(tmp_path: Path):
    """Removing a language/privacy classifier must make this controlled tree pass incorrectly."""
    private_email = "fixture" + "@gma" + "il.com"
    private_path = "/" + "Users" + "/fixture"
    tenant_host = "tenant" + ".onmicrosoft.com"
    public_ip = "8" + ".8.8.8"
    uuid = "12345678" + "-1234-4234-8234-1234567890ab"
    thai = chr(0x0E01)
    cyrillic = chr(0x0416)
    (tmp_path / "notes.md").write_text(
        "\n".join((thai, cyrillic, private_email, private_path, tenant_host, public_ip, uuid)),
        encoding="utf-8",
    )

    result = run_gate("tree", tmp_path)

    assert result.returncode == 1, result.stdout + result.stderr
    assert {
        "NON_LATIN_TEXT",
        "PERSONAL_EMAIL",
        "HOME_PATH",
        "TENANT_HOST",
        "PUBLIC_IP",
        "UUID",
    } <= codes(result)
    assert private_email not in result.stdout
    assert private_path not in result.stdout


def test_tree_rejects_uuid_in_test_sources(tmp_path: Path):
    """Test fixtures are release content too and cannot carry a UUID-shaped identifier."""
    tests_directory = tmp_path / "tests"
    tests_directory.mkdir()
    uuid = "12345678" + "-1234-4234-8234-1234567890ab"
    (tests_directory / "fixture.py").write_text(uuid, encoding="utf-8")

    result = run_gate("tree", tmp_path)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "UUID" in codes(result)
    assert uuid not in result.stdout


def test_tree_keeps_examples_safe_and_allowlists_context_scoped(tmp_path: Path):
    """A broad exception must not turn the owner/noreply policy into a wildcard."""
    (tmp_path / "examples.md").write_text(
        "user@example.invalid\n192.0.2.7\nhttps://github.com/" + "kengio/olaf\n",
        encoding="utf-8",
    )
    (tmp_path / "CODEOWNERS").write_text("* @" + "kengio\n", encoding="utf-8")
    result = run_gate("tree", tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr

    noreply = "10076938+" + "kengio" + "@" + "users.noreply.github.com"
    (tmp_path / "ordinary.md").write_text(noreply, encoding="utf-8")
    result = run_gate("tree", tmp_path)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "APPROVED_IDENTITY_CONTEXT" in codes(result)


def test_tree_rejects_notebook_execution_state_and_outputs_separately(tmp_path: Path):
    """Dropping either half of the output-free notebook contract is a release regression."""
    write_notebook(tmp_path / "count.ipynb", execution_count=1, outputs=[])
    write_notebook(
        tmp_path / "output.ipynb", execution_count=None, outputs=[{"output_type": "stream"}]
    )

    result = run_gate("tree", tmp_path)

    assert result.returncode == 1, result.stdout + result.stderr
    assert {"NOTEBOOK_EXECUTION_COUNT", "NOTEBOOK_OUTPUT"} <= codes(result)


def test_tree_rejects_hidden_or_active_workbook_content_and_person_metadata(tmp_path: Path):
    """A filename-only workbook check would miss hidden and executable OOXML payloads."""
    write_zip(
        tmp_path / "unsafe.xlsx",
        {
            "xl/workbook.xml": '<sheet name="hidden" state="hidden"/>',
            "xl/worksheets/sheet1.xml": "<worksheet><f>1+1</f></worksheet>",
            "xl/externalLinks/externalLink1.xml": "<externalLink/>",
            "docProps/core.xml": "<dc:creator>Fixture Person</dc:creator>",
        },
    )

    result = run_gate("tree", tmp_path)

    assert result.returncode == 1, result.stdout + result.stderr
    assert {"XLSX_HIDDEN_SHEET", "XLSX_FORMULA", "XLSX_EXTERNAL", "XLSX_PERSON_METADATA"} <= codes(
        result
    )


def test_tree_rejects_png_text_metadata(tmp_path: Path):
    """Renaming an image cannot hide an embedded author/profile text chunk."""
    payload = b"\x89PNG\r\n\x1a\n" + png_chunk(b"tEXt", b"Author\0Fixture")
    (tmp_path / "preview.png").write_bytes(payload)

    result = run_gate("tree", tmp_path)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "PNG_TEXT_METADATA" in codes(result)


def test_tree_rejects_mutable_or_privileged_workflow_fixture(tmp_path: Path):
    """A tag action, write token, or fork-privileged trigger must each be independently blocked."""
    (tmp_path / "workflow.yml").write_text(
        "\n".join(
            (
                "on:",
                "  pull_request_target:",
                "permissions:",
                "  contents: write",
                "jobs:",
                "  test:",
                "    runs-on: ubuntu-latest",
                "    steps:",
                "      - uses: actions/checkout@v4",
            )
        ),
        encoding="utf-8",
    )

    result = run_gate("tree", tmp_path)

    assert result.returncode == 1, result.stdout + result.stderr
    assert {"WORKFLOW_PRIVILEGED_TRIGGER", "WORKFLOW_PERMISSION", "WORKFLOW_ACTION_PIN"} <= codes(
        result
    )


def test_tree_rejects_non_closed_dependency_lock_fixture(tmp_path: Path):
    """A range, extra index, or missing hash reopens the network dependency-resolution gap."""
    (tmp_path / "requirements.txt").write_text(
        "--extra-index-url https://packages.invalid/simple\nexample-package>=1.0\n",
        encoding="utf-8",
    )

    result = run_gate("tree", tmp_path)

    assert result.returncode == 1, result.stdout + result.stderr
    assert {"LOCK_EXTRA_INDEX", "LOCK_RANGE_PIN", "LOCK_MISSING_HASH"} <= codes(result)


def test_tree_requires_dependabot_to_watch_the_ci_lock_directory(tmp_path: Path):
    """Removing pip update coverage leaves a reviewed lock file stale indefinitely."""
    dependabot = tmp_path / ".github" / "dependabot.yml"
    dependabot.parent.mkdir()
    dependabot.write_text(
        "version: 2\nupdates:\n  - package-ecosystem: github-actions\n    directory: /\n",
        encoding="utf-8",
    )

    result = run_gate("tree", tmp_path)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "DEPENDABOT_PIP" in codes(result)


def test_tree_rejects_missing_html_asset_and_unapproved_evidence_host(tmp_path: Path):
    """Markdown-only link checks leave public HTML assets and factual sources unchecked."""
    (tmp_path / "page.html").write_text(
        '<img src="missing.svg"><a href="https://untrusted.invalid/evidence">evidence</a>',
        encoding="utf-8",
    )

    result = run_gate("tree", tmp_path)

    assert result.returncode == 1, result.stdout + result.stderr
    assert {"HTML_LOCAL_LINK", "UNAPPROVED_EVIDENCE_HOST"} <= codes(result)


def test_tree_rejects_an_asset_host_when_used_as_non_asset_evidence(tmp_path: Path):
    """A badge host is approved only for assets, not for factual documentation links."""
    (tmp_path / "page.html").write_text(
        '<a href="https://img.shields.io/only-an-asset">evidence</a>', encoding="utf-8"
    )

    result = run_gate("tree", tmp_path)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "UNAPPROVED_EVIDENCE_HOST" in codes(result)


def test_tree_allows_an_asset_host_when_used_as_an_image_source(tmp_path: Path):
    """The asset exception must survive the generic URL pass for a valid image source."""
    (tmp_path / "page.html").write_text(
        '<img src="https://img.shields.io/only-an-asset">', encoding="utf-8"
    )

    result = run_gate("tree", tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr


def test_archive_rejects_internal_path_and_symlink(tmp_path: Path):
    """A release archive must not inherit coordination files or a link-based escape hatch."""
    archive_path = tmp_path / "unsafe.tar"
    with tarfile.open(archive_path, "w") as archive:
        payload = b"internal"
        member = tarfile.TarInfo("olaf-1.0.0/.superpowers/report.md")
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))
        link = tarfile.TarInfo("olaf-1.0.0/link")
        link.type = tarfile.SYMTYPE
        link.linkname = "../outside"
        archive.addfile(link)

    result = run_gate("archive", archive_path, "--prefix", "olaf-1.0.0/")

    assert result.returncode == 1, result.stdout + result.stderr
    assert {"ARCHIVE_FORBIDDEN_PATH", "ARCHIVE_LINK"} <= codes(result)


def test_candidate_archive_matches_head_from_a_temporary_archive_directory(tmp_path: Path):
    """Archive membership must be read from the caller's checkout, not the tar's directory."""
    source = tmp_path / "source"
    source.mkdir()
    (source / "page.html").write_text('<img src="asset.svg">', encoding="utf-8")
    (source / "asset.svg").write_text("<svg/>", encoding="utf-8")
    for command in (
        ["git", "init", "-q", str(source)],
        ["git", "-C", str(source), "config", "user.name", "OLAF Test"],
        ["git", "-C", str(source), "config", "user.email", "test@example.invalid"],
        ["git", "-C", str(source), "add", "page.html", "asset.svg"],
        ["git", "-C", str(source), "commit", "-qm", "archive fixture"],
    ):
        subprocess.run(command, check=True)
    archive_path = tmp_path / "candidate.tar"
    subprocess.run(
        [
            "git",
            "archive",
            "--format=tar",
            "--prefix=olaf-1.0.0/",
            "--output",
            archive_path,
            "HEAD",
        ],
        cwd=source,
        check=True,
    )

    result = run_gate(
        "archive", archive_path, "--tree", "HEAD", "--prefix", "olaf-1.0.0/", cwd=source
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_candidate_tree_passes_the_release_gate():
    """Fixtures alone are insufficient: the actual tracked candidate must pass the same CLI."""
    result = run_gate("tree", REPO_ROOT)
    assert result.returncode == 0, result.stdout + result.stderr


def test_release_workflow_fails_closed_on_all_secret_scan_surfaces():
    """History reachability does not prove every object is scanned unless this command is present."""
    workflow = (REPO_ROOT / ".github" / "workflows" / "test.yml").read_text(encoding="utf-8")

    assert "set -euo pipefail" in workflow
    assert "python scripts/check_secrets.py all-objects ." in workflow


def test_secret_orchestrator_uses_a_restricted_container_and_redacts_output(tmp_path: Path):
    """A scanner failure must not leak its raw finding while the container remains unprivileged."""
    engine = tmp_path / "fake-engine.py"
    arguments = tmp_path / "arguments.json"
    engine.write_text(
        "\n".join(
            (
                f"#!{sys.executable}",
                "import json, os, sys",
                "from pathlib import Path",
                "Path(os.environ['OLAF_FAKE_ARGS']).write_text(json.dumps(sys.argv[1:]))",
                "if sys.argv[1] == 'pull':",
                "    raise SystemExit(0)",
                "print('scanner fixture finding must stay private')",
                "raise SystemExit(1)",
            )
        ),
        encoding="utf-8",
    )
    engine.chmod(0o755)
    source = tmp_path / "source"
    source.mkdir()
    (source / "safe.txt").write_text("safe fixture", encoding="utf-8")
    environment = {**os.environ, "OLAF_FAKE_ARGS": str(arguments)}
    result = subprocess.run(
        [sys.executable, str(SECRET_GATE), "--engine", str(engine), "tree", source],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert "secret tree: FAIL" in result.stdout
    assert "scanner fixture finding" not in result.stdout + result.stderr
    command = json.loads(arguments.read_text(encoding="utf-8"))
    assert "--network" in command and command[command.index("--network") + 1] == "none"
    assert "--pull" in command and command[command.index("--pull") + 1] == "never"
    assert "--read-only" in command
    assert "--cap-drop" in command and command[command.index("--cap-drop") + 1] == "ALL"
    assert "no-new-privileges" in command
    assert any(argument.endswith(":ro") for argument in command)
    # The scanner runs as the invoking user, not root. --cap-drop ALL removes CAP_DAC_OVERRIDE,
    # so a root container cannot read the 0700 directories this process creates for it — the scan
    # target included. Matching the owner fixes that without loosening a permission bit, and leaves
    # the container unprivileged rather than root.
    assert (
        "--user" in command
        and command[command.index("--user") + 1] == f"{os.getuid()}:{os.getgid()}"
    )


def test_secret_orchestrator_self_test_accepts_only_a_scanner_finding(tmp_path: Path):
    """The self-test is green only when the scanner reports its ephemeral synthetic fixture.

    This stub used to print the JSON report to stdout, and the orchestrator used to read it from
    there — so the test passed while the real tool did something else entirely. gitleaks writes a
    report ONLY to `--report-path`; given none it emits its banner and two log lines and no JSON at
    all. The fake matched the code instead of the tool, which is why a self-test that could never
    pass against real gitleaks looked fully covered.

    The stub now honours the tool's contract: resolve the `/report` bind mount, write JSON to
    `--report-path`, exit 1. The assertions also bound the one relaxation that requires — the report
    mount is writable, the scan target is not, and nothing else is mounted at all.
    """
    engine = tmp_path / "finding-engine.py"
    arguments = tmp_path / "arguments.json"
    engine.write_text(
        "\n".join(
            (
                f"#!{sys.executable}",
                "import json, os, sys",
                "from pathlib import Path",
                "Path(os.environ['OLAF_FAKE_ARGS']).write_text(json.dumps(sys.argv[1:]))",
                "if sys.argv[1] == 'pull':",
                "    raise SystemExit(0)",
                "args = sys.argv[1:]",
                "if '--report-format=json' not in args:",
                "    raise SystemExit(1)",
                "mounts = {}",
                "for argument in args:",
                "    if argument.startswith('/') and ':' in argument:",
                "        host, container = argument.split(':')[:2]",
                "        mounts[host] = container",
                "report = next(a.split('=', 1)[1] for a in args if a.startswith('--report-path='))",
                "host_dir = next(",
                "    h for h, c in mounts.items() if report.startswith(c.rstrip('/') + '/')",
                ")",
                "target = host_dir.rstrip('/') + report[len(mounts[host_dir].rstrip('/')) :]",
                "Path(target).write_text(",
                "    json.dumps([{'RuleID': 'synthetic', 'Secret': 'REDACTED'}])",
                ")",
                "raise SystemExit(1)",
            )
        ),
        encoding="utf-8",
    )
    engine.chmod(0o755)

    result = subprocess.run(
        [sys.executable, str(SECRET_GATE), "--engine", str(engine), "self-test"],
        cwd=REPO_ROOT,
        env={**os.environ, "OLAF_FAKE_ARGS": str(arguments)},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "secret self-test: PASS\n"

    command = json.loads(arguments.read_text(encoding="utf-8"))
    volumes = [command[i + 1] for i, a in enumerate(command) if a == "--volume"]
    assert sum(v.endswith(":/scan:ro") for v in volumes) == 1, (
        f"the scan target must stay read-only, got {volumes}"
    )
    assert sum(v.endswith(":/config:ro") for v in volumes) == 1, (
        f"the scanner config must be mounted read-only, got {volumes}"
    )
    assert sum(v.endswith(":/report") for v in volumes) == 1, (
        f"the report needs exactly one writable mount, got {volumes}"
    )
    assert len(volumes) == 3, f"no other mount may be added, got {volumes}"
    assert sum(not v.endswith(":ro") for v in volumes) == 1, (
        f"the report mount must be the ONLY writable one, got {volumes}"
    )
    assert any(a.startswith("--report-path=/report/") for a in command), (
        "the report must be written into the mounted report directory"
    )
    assert "--config=/config/gitleaks.toml" in command, (
        "the scanner must be given an explicit config; v8.30.0 treats a missing one as fatal and "
        "exits 1, which is indistinguishable from a real finding"
    )


def test_secret_orchestrator_self_test_rejects_an_error_status_one(tmp_path: Path):
    """A nonzero scanner error is not evidence that the synthetic secret was detected."""
    engine = tmp_path / "error-engine.py"
    engine.write_text(
        "\n".join(
            (
                f"#!{sys.executable}",
                "import sys",
                "if sys.argv[1] == 'pull':",
                "    raise SystemExit(0)",
                "print('scanner fixture error')",
                "raise SystemExit(1)",
            )
        ),
        encoding="utf-8",
    )
    engine.chmod(0o755)

    result = subprocess.run(
        [sys.executable, str(SECRET_GATE), "--engine", str(engine), "self-test"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert result.stdout.endswith("secret self-test: FAIL\n")
    # A refusal now names its reason, because "FAIL" alone covered three unrelated causes and none
    # of them was visible in CI. The scanner's own output is still never published: a real scan's
    # console can carry the matched context, so only facts ABOUT the run may be printed.
    assert "self-test refused:" in result.stdout
    assert "scanner fixture error" not in result.stdout + result.stderr


def test_workflow_inline_scalars_cannot_contain_a_colon_space():
    """A plain YAML scalar containing ": " is read as the start of a nested mapping, and GitHub
    rejects the ENTIRE workflow for it: zero jobs, no annotations, just "This run likely failed
    because of a workflow file issue."

    `pip install --require-hashes --only-binary=:all: -r requirements/ci-test.txt` is exactly that
    shape — `:all: -r` — and it made this workflow unparseable from the day it was written. CI had
    never run a single time, on any commit.

    Every local gate passed anyway. check_public_release.py inspects workflows with a regex
    (WORKFLOW_ACTION) and never with a YAML parser, and the sibling secret-scan test above asserts
    substrings, so a file that GitHub cannot even load looked green everywhere.

    pyyaml is deliberately NOT a test dependency — requirements/ci-test.txt is a hash-locked
    three-package file — so this guards the footgun directly rather than parsing the document.
    """
    import re

    inline_key = re.compile(r"^\s*-?\s*(run|name|if|shell|working-directory):[ \t]+(?![|>])(.*)$")
    offenders = []
    for path in sorted((REPO_ROOT / ".github").rglob("*.yml")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            match = inline_key.match(line)
            if not match:
                continue
            value = match.group(2).strip()
            if not value or value[0] in "'\"":
                continue
            if ": " in value.split(" #")[0]:
                offenders.append(f"{path.relative_to(REPO_ROOT).as_posix()}:{lineno}")

    assert not offenders, (
        "unquoted YAML scalar contains ': ', which GitHub reads as a nested mapping and refuses to "
        f"load — quote the value at {', '.join(offenders)}"
    )


def test_the_secret_scanner_self_test_token_can_actually_be_detected():
    """The self-test is the gate that proves the secret scanner matches anything at all. Its
    fixture token used to be a GitHub-PAT prefix followed by thirty-six repeats of one letter, and
    gitleaks applies an entropy threshold to that rule: it answers "no leaks found", exit 0, empty
    report. `has_redacted_finding` requires exit 1 and a parsed finding, so the self-test could
    never pass — and it had never been run, because the workflow that runs it was itself
    unparseable YAML.

    A scanner whose self-test cannot pass is indistinguishable from a scanner that silently matches
    nothing, which is the whole failure mode the self-test exists to rule out. Docker is not
    available in this job, so pin the property that made it undetectable rather than the scan.
    """
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location(
        "_olaf_check_secrets", REPO_ROOT / "scripts" / "check_secrets.py"
    )
    module = importlib.util.module_from_spec(spec)
    # dataclass() resolves annotations through sys.modules, so register before executing.
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        token = module.self_test_token()
    finally:
        sys.modules.pop(spec.name, None)

    assert token.startswith("g" + "hp_"), "the fixture must keep the shape gitleaks matches on"
    body = token[4:]
    assert len(body) == 36, f"github-pat bodies are 36 characters, got {len(body)}"
    assert len(set(body)) >= 10, (
        f"fixture body has only {len(set(body))} distinct character(s) — gitleaks' entropy "
        "threshold will discard it and the self-test can never pass"
    )


def test_the_python_floor_is_consistent_and_matches_the_lock_interpreter():
    """Five places declare the supported Python floor. A sixth — the pip-compile header inside each
    lock — decides whether that floor can actually install anything.

    A lock compiled on a NEWER interpreter than the floor silently omits every
    `python_version < "X"` conditional dependency, and `--require-hashes` then refuses the install
    outright rather than resolving them. pytest 9.1.1 needs `exceptiongroup` and `tomli` below
    3.11; the 3.11-compiled lock carries neither; the 3.10 matrix leg died on "In --require-hashes
    mode, all requirements must have their versions pinned with ==". Nobody saw it because the
    workflow was unparseable YAML and had never run.

    So pin the floor across the workflow matrix, ruff's target-version, both prose declarations,
    and the interpreter each lock was compiled on. Raising or lowering the floor is then a single
    coherent edit instead of five that can disagree.
    """
    import re

    def sole(pattern, text, label, flags=0):
        found = re.findall(pattern, text, flags)
        assert found, f"could not read the Python floor from {label}"
        return found

    workflow = (REPO_ROOT / ".github" / "workflows" / "test.yml").read_text(encoding="utf-8")
    matrix_raw = sole(r"python-version:\s*\[([^\]]+)\]", workflow, "the CI matrix")[0]
    matrix = sorted(
        tuple(int(part) for part in v.split(".")) for v in re.findall(r"\d+\.\d+", matrix_raw)
    )
    assert len(matrix) >= 2, f"the matrix should span several interpreters, got {matrix}"

    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    major, minor = sole(r'target-version\s*=\s*"py(\d)(\d+)"', pyproject, "pyproject.toml")[0]
    ruff_target = (int(major), int(minor))

    declared = {"pyproject.toml target-version": ruff_target, "CI matrix minimum": matrix[0]}
    for name in ("CONTRIBUTING.md", "docs/testing.md"):
        text = (REPO_ROOT / name).read_text(encoding="utf-8")
        floor = sole(r"Python\s*(?:≥|>=)\s*(\d+)\.(\d+)", text, name)[0]
        declared[name] = (int(floor[0]), int(floor[1]))
    for name in ("requirements/ci-test.txt", "requirements/ci-lint.txt"):
        text = (REPO_ROOT / name).read_text(encoding="utf-8")
        compiled = sole(r"pip-compile with Python (\d+)\.(\d+)", text, name)[0]
        declared[f"{name} (pip-compile interpreter)"] = (int(compiled[0]), int(compiled[1]))

    distinct = set(declared.values())
    assert len(distinct) == 1, (
        "the declared Python floor disagrees across the repository: "
        + ", ".join(f"{k}={'.'.join(str(n) for n in v)}" for k, v in sorted(declared.items()))
    )


def _load_check_secrets():
    """Import scripts/check_secrets.py by path. dataclass() resolves annotations through
    sys.modules, so the module must be registered before it executes."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_olaf_check_secrets", REPO_ROOT / "scripts" / "check_secrets.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(spec.name, None)
        raise
    return module


ALL_OBJECTS_IGNORES_STDIN = '#!/usr/bin/env python3\n# A consumer that never reads stdin - what `docker run` without -i looks like from this side.\nimport json\nimport os\nimport sys\n\nopen(os.environ["OLAF_FAKE_ARGS"], "w").write(json.dumps(sys.argv[1:]))\nsys.exit(0)\n'


def test_all_objects_hands_the_object_stream_to_an_interactive_container(tmp_path: Path):
    """`all-objects` pipes `git cat-file --batch-all-objects --batch` into the scanner's stdin. A
    `docker run` WITHOUT -i wires the container's stdin to /dev/null, so gitleaks reads EOF at once
    and scans zero bytes.

    Both outcomes of that are bad, and the worse one is the quiet one:

    * On a repository large enough to fill the 64 KB pipe, git blocks, dies when the pipe closes,
      and the mode returns 2 — a FAIL nobody could explain, which is how this was found.
    * On a small repository git finishes, nothing signals a problem, and the mode returns the
      container's 0. **PASS, having scanned nothing**, with a real secret sitting in the objects.

    This is the deepest scan in the chain — it is the one that reaches unreachable and dangling
    objects — so a silent zero-byte pass is the exact failure it exists to prevent.

    A stub cannot reproduce docker's stdin wiring (subprocess hands it the pipe either way), so the
    flag itself is pinned, alongside the behaviour that a consumer which does not drain the stream
    is never reported as clean.
    """
    module = _load_check_secrets()
    arguments = tmp_path / "arguments.json"
    engine = tmp_path / "ignore-stdin.py"
    engine.write_text(ALL_OBJECTS_IGNORES_STDIN, encoding="utf-8")
    engine.chmod(0o755)

    os.environ["OLAF_FAKE_ARGS"] = str(arguments)
    try:
        status = module.run_all_objects(str(engine), "stub:latest", REPO_ROOT)
    finally:
        os.environ.pop("OLAF_FAKE_ARGS", None)
    assert status != 0, (
        "a consumer that never drained the object stream was reported as a clean scan"
    )

    command = json.loads(arguments.read_text(encoding="utf-8"))
    assert "-i" in command or "--interactive" in command, (
        "the stdin-mode container must be interactive, or docker replaces its stdin with "
        f"/dev/null and the scan reads nothing: {command}"
    )


def test_workflow_block_scalars_never_dedent_below_their_own_block():
    """The other half of the same footgun, and it has now bitten this repo twice.

    A `run: |` block ends at the first non-blank line indented LESS than the block's own content
    indent. A heredoc written inside one looks natural at column 0 --

        run: |
          version="$(python - <<'PY'
    import json
    PY
    )"

    -- and YAML stops reading the block at `import json`, then tries that line as a mapping key.
    GitHub rejects the whole file: zero jobs, no annotations, and `gh pr checks` says "no checks
    reported on the branch", which reads like CI has not started rather than like CI is broken.
    Branch protection then waits forever for checks that can never appear.

    The sibling test above guards the `": "` inline-scalar shape, and its docstring records that
    the same class of break once meant CI "had never run a single time, on any commit". It cannot
    see this one: nothing on those lines is an inline scalar. Same outcome, different syntax.

    pyyaml is deliberately not a test dependency (see the sibling), so this measures indentation
    rather than parsing the document.
    """
    import re

    block_open = re.compile(r"^(\s*)-?\s*(?:run|if|shell|env|with):\s*[|>][+-]?\s*$")
    # a mapping key, or a list item — anything else at this indent is not YAML
    yaml_key = re.compile(r"^\s*(?:-\s+)?(?:[A-Za-z_][\w.-]*|'[^']*'|\"[^\"]*\"):(?:\s|$)|^\s*-\s")
    offenders = []
    for path in sorted((REPO_ROOT / ".github").rglob("*.yml")):
        lines = path.read_text(encoding="utf-8").splitlines()
        relative = path.relative_to(REPO_ROOT).as_posix()
        index = 0
        while index < len(lines):
            opened = block_open.match(lines[index])
            if not opened:
                index += 1
                continue
            key_indent = len(opened.group(1))
            index += 1
            # the block's content indent is set by its first non-blank line
            while index < len(lines) and not lines[index].strip():
                index += 1
            if index >= len(lines):
                break
            content_indent = len(lines[index]) - len(lines[index].lstrip())
            if content_indent <= key_indent:
                continue  # an empty block; the next key follows
            while index < len(lines):
                line = lines[index]
                if not line.strip():
                    index += 1
                    continue
                indent = len(line) - len(line.lstrip())
                if indent >= content_indent:
                    index += 1
                    continue
                # Dedented, so YAML has ended the block here. That is only legal if this line is
                # the next mapping key or list item. Testing the indent instead is what a first
                # draft of this guard did, and it passed on the very break it was written for:
                # a heredoc body at column 0 is BELOW the block's key, which read as "closing an
                # outer mapping" rather than as the killer it is.
                if not yaml_key.match(line):
                    offenders.append(f"{relative}:{index + 1}: {line.strip()[:60]!r}")
                break

    assert not offenders, (
        "a line inside a `run: |` block is indented below the block, which ends the block and "
        "makes the workflow unparseable — GitHub then runs zero jobs and reports no checks:\n  "
        + "\n  ".join(offenders)
    )
