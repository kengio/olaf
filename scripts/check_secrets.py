#!/usr/bin/env python3
"""Run the pinned Gitleaks scanner without exposing source or findings in CI logs.

The scanner image is pulled by immutable digest before any repository directory is mounted. Every
scan then runs network-disabled, read-only, without Linux capabilities, and with Gitleaks redaction
enabled. Scanner stdout/stderr is captured in a temporary file and discarded; this command emits
only a generic pass/fail result.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


IMAGE = "ghcr.io/gitleaks/gitleaks:v8.30.0@sha256:691af3c7c5a48b16f187ce3446d5f194838f91238f27270ed36eef6359a574d9"

# gitleaks resolves a config before it scans anything, and v8.30.0 treats a missing one as FATAL:
# `unable to load gitleaks config, err: Config File ".gitleaks" Not Found in "[/scan]"`. It exits 1
# for that, which is the same status a real finding produces — so every mode here would read a
# configuration error as "leaks found" and fail closed forever, and the self-test could never pass.
#
# Passing --config explicitly removes the discovery step entirely. `useDefault` inherits the rule
# set built into the image, which is already pinned by the digest above, so this makes the LOADING
# deterministic without pinning a second copy of the rules or adding a file to the public tree.
CONFIG_TOML = "[extend]\nuseDefault = true\n"
CONFIG_ARGUMENT = "--config=/config/gitleaks.toml"


@contextlib.contextmanager
def mounted_config():
    """Materialize the scanner config in a throwaway directory for a read-only mount."""
    with tempfile.TemporaryDirectory(prefix="olaf-gitleaks-config-") as directory:
        config_dir = Path(directory)
        (config_dir / "gitleaks.toml").write_text(CONFIG_TOML, encoding="utf-8")
        yield config_dir


@dataclass(frozen=True)
class CapturedResult:
    """A scanner exit status and private, temporary output retained only for validation."""

    status: int
    output: bytes
    console: bytes = b""


def run_captured(command: list[str], *, stdin=None) -> CapturedResult:
    """Capture scanner output privately so callers can validate it without publishing it."""
    with tempfile.TemporaryDirectory(prefix="olaf-secret-gate-") as directory:
        output = Path(directory) / "scanner-output.txt"
        with output.open("wb") as handle:
            result = subprocess.run(
                command,
                stdin=stdin,
                stdout=handle,
                stderr=subprocess.STDOUT,
                check=False,
            )
        contents = output.read_bytes()
    return CapturedResult(result.returncode, contents)


def prepare_image(engine: str, image: str) -> bool:
    """Pulling by digest precedes every read-only repository mount."""
    return run_captured([engine, "pull", image]).status == 0


def container_user() -> list[str]:
    """Run the scanner as the invoking user rather than as root.

    The scan, config, and report directories are created by THIS process, and `mkdtemp` makes them
    0700. `--cap-drop ALL` removes CAP_DAC_OVERRIDE — the capability that lets root ignore file
    permissions — so a root container could not open its own scan target. gitleaks reported
    `Config File ".gitleaks" Not Found in "[/scan]"` for a directory it simply could not read, and
    `permission denied` once the config was mounted explicitly. One cause, three symptoms.

    Matching the owner fixes it without loosening a single permission bit, and drops the container
    from root to an unprivileged uid on the way. Omitted where the platform has no uid concept.
    """
    getuid, getgid = getattr(os, "getuid", None), getattr(os, "getgid", None)
    if getuid is None or getgid is None:
        return []
    return ["--user", f"{getuid()}:{getgid()}"]


def restricted_command(
    engine: str,
    image: str,
    source: Path,
    command: list[str],
    report_dir: Path | None = None,
    config_dir: Path | None = None,
) -> list[str]:
    """Build a container invocation with no network, write access, capabilities, or token mount.

    `report_dir`, when given, adds the ONE writable bind mount the scanner needs to emit a report
    file. Everything else is unchanged: no network, root filesystem still read-only, scan target
    still mounted read-only, all capabilities dropped, no privilege escalation, no token.
    """
    mounts = ["--volume", f"{source.resolve()}:/scan:ro"]
    if config_dir is not None:
        mounts += ["--volume", f"{config_dir.resolve()}:/config:ro"]
    if report_dir is not None:
        mounts += ["--volume", f"{report_dir.resolve()}:/report"]
    return [
        engine,
        "run",
        "--rm",
        "--pull",
        "never",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        *container_user(),
        "--env",
        "GITLEAKS_ENABLE_REDACTION=true",
        *mounts,
        image,
        *command,
    ]


def run_tree(engine: str, image: str, source: Path) -> int:
    with mounted_config() as config_dir:
        return run_captured(
            restricted_command(
                engine,
                image,
                source,
                ["dir", CONFIG_ARGUMENT, "--redact", "--exit-code", "1", "/scan"],
                config_dir=config_dir,
            )
        ).status


def run_history(engine: str, image: str, source: Path) -> int:
    with mounted_config() as config_dir:
        return run_captured(
            restricted_command(
                engine,
                image,
                source,
                [
                    "git",
                    CONFIG_ARGUMENT,
                    "--redact",
                    "--exit-code",
                    "1",
                    "--log-opts=--all --reflog",
                    "/scan",
                ],
                config_dir=config_dir,
            )
        ).status


def run_history_report(engine: str, image: str, source: Path) -> CapturedResult:
    """Request JSON only for the self-test; its bytes stay private to this process.

    gitleaks emits a report ONLY to `--report-path`. With no path it prints its banner and a couple
    of log lines and no JSON at all, so `json.loads` on the captured stream cannot succeed no matter
    what the scan found — the exit code says "leaks found: 1" while the parse fails. That is why the
    self-test could not pass even once the fixture token became detectable.

    So give the report a destination: one throwaway writable bind mount, read back on this side and
    discarded with the temporary directory. The container keeps every other restriction — no
    network, read-only root filesystem, read-only scan target, all capabilities dropped.
    """
    with (
        tempfile.TemporaryDirectory(prefix="olaf-gitleaks-report-") as directory,
        mounted_config() as config_dir,
    ):
        report_dir = Path(directory)
        result = run_captured(
            restricted_command(
                engine,
                image,
                source,
                [
                    "git",
                    CONFIG_ARGUMENT,
                    "--redact",
                    "--report-format=json",
                    "--report-path=/report/findings.json",
                    "--exit-code",
                    "1",
                    "--log-opts=--all --reflog",
                    "/scan",
                ],
                report_dir=report_dir,
                config_dir=config_dir,
            )
        )
        report = report_dir / "findings.json"
        try:
            contents = report.read_bytes()
        except OSError:
            # The container writes as its own user; an unreadable or absent report is a failed
            # self-test, not a crash, and must not be reported as a clean scan.
            contents = b""
    return CapturedResult(result.status, contents, result.output)


def run_all_objects(engine: str, image: str, source: Path) -> int:
    """Scan every local Git object without exposing blob content to the invoking process output."""
    with mounted_config() as config_dir:
        producer = subprocess.Popen(
            ["git", "-C", str(source), "cat-file", "--batch-all-objects", "--batch"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        assert producer.stdout is not None
        command = [
            engine,
            "run",
            "--rm",
            # -i, or the engine wires the container's stdin to /dev/null and gitleaks scans zero
            # bytes: on a large repository git blocks on the full pipe and dies, and on a small one
            # everything reports clean having read nothing at all.
            "-i",
            "--pull",
            "never",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            *container_user(),
            "--env",
            "GITLEAKS_ENABLE_REDACTION=true",
            "--volume",
            f"{config_dir.resolve()}:/config:ro",
            image,
            "stdin",
            CONFIG_ARGUMENT,
            "--redact",
            "--exit-code",
            "1",
        ]
        status = run_captured(command, stdin=producer.stdout).status
        producer.stdout.close()
        producer.wait()
        return status if producer.returncode == 0 else 2


def self_test_token() -> str:
    """A synthetic GitHub-PAT-shaped token for the scanner self-test.

    Assembled at runtime rather than written as a literal, under two constraints that pull in
    opposite directions:

    * check_public_release.py's SECRET_MARKERS would flag a literal GitHub-PAT prefix sitting in
      this file, so the prefix is built from fragments.
    * gitleaks applies an ENTROPY threshold to its github-pat rule, so the body cannot be filler.
      The body used to be thirty-six repeats of one letter — zero entropy — and gitleaks 8.30
      answers "no leaks found", exit 0, empty report for it. That made the self-test unpassable, so
      the one gate proving the scanner actually matches something had never passed, and nothing
      downstream could tell a working scan from a scan that silently matched nothing.

    A digest gives a stable, high-entropy body with no real credential anywhere in the chain.
    """
    return "g" + "hp_" + hashlib.sha256(b"olaf-gitleaks-self-test").hexdigest()[:36]


def self_test(engine: str, image: str) -> bool:
    """Require a parsed redacted finding for an ephemeral committed synthetic token."""
    with tempfile.TemporaryDirectory(prefix="olaf-gitleaks-self-test-") as directory:
        repository = Path(directory)
        for command in (
            ["git", "init", "-q", str(repository)],
            ["git", "-C", str(repository), "config", "user.name", "OLAF Test"],
            ["git", "-C", str(repository), "config", "user.email", "test@example.invalid"],
        ):
            if subprocess.run(
                command, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            ).returncode:
                return False
        token = self_test_token()
        (repository / "fixture.txt").write_text(token, encoding="utf-8")
        if subprocess.run(
            ["git", "-C", str(repository), "add", "fixture.txt"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode:
            return False
        if subprocess.run(
            ["git", "-C", str(repository), "commit", "-qm", "synthetic scanner fixture"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode:
            return False
        result = run_history_report(engine, image, repository)
        reason = refusal_reason(result, token)
        if reason is not None:
            # The REASON only. The scanner's own output stays private — a real scan's console can
            # carry the matched context, and this gate must never be what publishes it.
            print(f"  self-test refused: {reason}")
            return False
        return True


def refusal_reason(result: CapturedResult, token: str) -> str | None:
    """None when the result is an acceptable redacted finding, else why it was refused.

    A self-test that only answers "FAIL" is nearly useless: the same word covered a fixture the
    scanner discarded for low entropy, a report the scanner was never asked to write, and a config
    it could not load — three unrelated causes, none of them visible in CI. Name the reason instead.
    """
    if token.encode("utf-8") in result.output or token.encode("utf-8") in result.console:
        return "the scanner echoed the synthetic token instead of redacting it"
    if result.status != 1:
        return f"scanner exit status {result.status}, expected 1 for a detected leak"
    if not result.output:
        return (
            f"the scanner exited {result.status} but wrote no report "
            f"({len(result.console)} byte(s) of console) — it may have failed before scanning, "
            "for example on an unloadable config"
        )
    try:
        report = json.loads(result.output.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "the scanner report was not parseable JSON"
    if not isinstance(report, list):
        return "the scanner report was not a list of findings"
    if not any(
        isinstance(finding, dict)
        and isinstance(finding.get("RuleID"), str)
        and finding["RuleID"]
        and isinstance(finding.get("Secret"), str)
        and finding["Secret"]
        for finding in report
    ):
        return "the scanner report carried no finding with a rule and a secret"
    return None


def has_redacted_finding(result: CapturedResult, token: str) -> bool:
    """Accept only a JSON finding that does not echo the self-test token."""
    return refusal_reason(result, token) is None


def print_result(label: str, status: int) -> int:
    if status == 0:
        print(f"secret {label}: PASS")
        return 0
    print(f"secret {label}: FAIL")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", default="docker", help="container engine executable")
    parser.add_argument("--image", default=IMAGE, help="immutable Gitleaks image reference")
    subparsers = parser.add_subparsers(dest="mode", required=True)
    subparsers.add_parser("self-test", help="prove the scanner returns a finding")
    for mode in ("tree", "history", "all-objects"):
        mode_parser = subparsers.add_parser(mode)
        mode_parser.add_argument("path", type=Path)
    args = parser.parse_args(argv)
    if "@sha256:" not in args.image:
        parser.error("the scanner image must be pinned by digest")
    if not prepare_image(args.engine, args.image):
        print("secret scanner: FAIL")
        return 1
    if args.mode == "self-test":
        return print_result("self-test", 0 if self_test(args.engine, args.image) else 1)
    if not args.path.is_dir():
        parser.error("scan path must be a directory")
    if args.mode == "tree":
        return print_result("tree", run_tree(args.engine, args.image, args.path))
    if args.mode == "history":
        return print_result("history", run_history(args.engine, args.image, args.path))
    return print_result("all-objects", run_all_objects(args.engine, args.image, args.path))


if __name__ == "__main__":
    raise SystemExit(main())
