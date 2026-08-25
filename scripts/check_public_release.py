#!/usr/bin/env python3
"""Deterministic, redacted hygiene checks for an OLAF public-release tree or archive.

The scanner intentionally reports only a stable category and relative path. It never echoes a
matched value: a release check must not turn a local CI log into a second disclosure channel.
Network reachability and semantic source review are separate release gates.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import posixpath
import re
import subprocess
import tarfile
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse


TEXT_SUFFIXES = {
    ".cfg",
    ".html",
    ".ini",
    ".ipynb",
    ".json",
    ".md",
    ".py",
    ".rst",
    ".sh",
    ".svg",
    ".toml",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}
IGNORED_PARTS = {".git", ".pytest_cache", ".ruff_cache", ".venv", "__pycache__", "node_modules"}
ALLOWED_EMAIL_DOMAINS = {"contoso.com", "example.com", "example.invalid", "example.org", "x.com"}
PERSONAL_EMAIL_DOMAINS = {
    "gmail.com",
    "hotmail.com",
    "icloud.com",
    "live.com",
    "outlook.com",
    "proton.me",
    "protonmail.com",
    "yahoo.com",
}
ALLOWED_EVIDENCE_HOSTS = {
    "api.fabric.microsoft.com",
    "datatracker.ietf.org",
    "docs.github.com",
    "github.com",
    "git-scm.com",
    "gitleaks.io",
    "keepachangelog.com",
    "learn.microsoft.com",
    "nbformat.readthedocs.io",
    "pip.pypa.io",
    "www.contributor-covenant.org",
    "www.microsoft.com",
    "www.rfc-editor.org",
    "www.w3.org",
}
ALLOWED_ASSET_HOSTS = ALLOWED_EVIDENCE_HOSTS | {"img.shields.io"}
EMAIL = re.compile(
    r"(?<![\w.+-])([A-Za-z0-9][A-Za-z0-9._%+-]*@[A-Za-z0-9.-]+\.[A-Za-z]{2,})(?![\w.-])"
)
OWNER_HANDLE = re.compile(r"(?<![\w-])@" + "kengio" + r"\b")
APPROVED_NOREPLY = "10076938+" + "kengio" + "@" + "users.noreply.github.com"
HOME_PATH = re.compile(r"(?<![\w:])/(?:Users|home)/[^\s/'\"]+")
TENANT_HOST = re.compile(r"\b[a-z0-9-]+\.onmicrosoft\.com\b", re.I)
IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
UUID = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b", re.I
)
SECRET_MARKERS = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|\bgh[pousr]_[A-Za-z0-9_]{20,}\b"
)
HTML_ATTR = re.compile(r"\b(?:href|src)\s*=\s*[\"']([^\"']+)[\"']", re.I)
URL = re.compile(r"https?://[^\s<>()\[\]\"']+")
WORKFLOW_ACTION = re.compile(r"^\s*-?\s*uses:\s*([^\s#]+)", re.M)
FORMULA = re.compile(r"<(?:[A-Za-z_][\w.-]*:)?f(?:\s|>)")
DEFINED_NAME = re.compile(r"<(?:[A-Za-z_][\w.-]*:)?definedName(?:\s|>)")
HIDDEN_SHEET = re.compile(r"\bstate\s*=\s*[\"'](?:hidden|veryHidden)[\"']", re.I)


@dataclass(frozen=True, order=True)
class Finding:
    """A redacted release-gate result."""

    code: str
    path: str


class ReleaseScanner:
    """Scan a finite set of release files without following symlinks or printing content."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.findings: set[Finding] = set()

    def add(self, code: str, path: str) -> None:
        self.findings.add(Finding(code, path))

    def scan_tree(self) -> list[Finding]:
        for relative in self.tree_members():
            path = self.root / relative
            if path.is_symlink():
                self.add("TREE_SYMLINK", relative.as_posix())
                continue
            if path.is_file():
                self.scan_path(path, relative.as_posix())
        return sorted(self.findings)

    def tree_members(self) -> list[Path]:
        """Use Git's tracked candidate when available; fixtures use a regular recursive tree."""
        try:
            result = subprocess.run(
                ["git", "-C", str(self.root), "ls-files", "-z"],
                capture_output=True,
                check=True,
            )
        except (OSError, subprocess.CalledProcessError):
            return sorted(
                path.relative_to(self.root)
                for path in self.root.rglob("*")
                if path.is_file()
                and not IGNORED_PARTS.intersection(path.relative_to(self.root).parts)
            )
        return [Path(item.decode("utf-8")) for item in result.stdout.split(b"\0") if item]

    def scan_path(self, path: Path, relative: str) -> None:
        try:
            data = path.read_bytes()
        except OSError:
            self.add("UNREADABLE_FILE", relative)
            return
        suffix = path.suffix.lower()
        if suffix == ".xlsx":
            self.scan_xlsx(data, relative)
        elif suffix == ".png":
            self.scan_png(data, relative)
        else:
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                self.add("UNRECOGNIZED_BINARY", relative)
                return
            self.scan_text(text, relative)
            if suffix == ".ipynb":
                self.scan_notebook(text, relative)
            if suffix in {".html", ".md", ".rst", ".ipynb"}:
                self.scan_links(text, relative)
            if suffix in {".yaml", ".yml"}:
                self.scan_workflow(text, relative)
            if relative.endswith(".github/dependabot.yml"):
                self.scan_dependabot(text, relative)
            if path.name.endswith(".txt") and path.parent.name == "requirements":
                self.scan_lock(text, relative)
            elif path.name.startswith("requirements") and path.suffix == ".txt":
                self.scan_lock(text, relative)

    def scan_text(self, text: str, relative: str) -> None:
        if any(self.is_non_latin_letter(character) for character in text):
            self.add("NON_LATIN_TEXT", relative)
        if SECRET_MARKERS.search(text):
            self.add("SECRET_MARKER", relative)
        if HOME_PATH.search(text):
            self.add("HOME_PATH", relative)
        if TENANT_HOST.search(text):
            self.add("TENANT_HOST", relative)
        if UUID.search(text):
            self.add("UUID", relative)
        for match in IPV4.finditer(text):
            if not self.is_documentation_ip(match.group()):
                self.add("PUBLIC_IP", relative)
                break
        for match in EMAIL.finditer(text):
            address = match.group(1).lower()
            if self.is_uri_userinfo(text, match.start(1), match.end(1)):
                continue
            if address == APPROVED_NOREPLY:
                self.add("APPROVED_IDENTITY_CONTEXT", relative)
                continue
            domain = address.rsplit("@", 1)[1]
            if domain in PERSONAL_EMAIL_DOMAINS:
                self.add("PERSONAL_EMAIL", relative)
            elif domain not in ALLOWED_EMAIL_DOMAINS:
                self.add("NON_RESERVED_EMAIL", relative)
        if OWNER_HANDLE.search(text) and not relative.endswith("CODEOWNERS"):
            self.add("OWNER_HANDLE_CONTEXT", relative)

    @staticmethod
    def is_non_latin_letter(character: str) -> bool:
        if not unicodedata.category(character).startswith("L"):
            return False
        name = unicodedata.name(character, "")
        return name != "INFORMATION SOURCE" and "LATIN" not in name

    @staticmethod
    def is_documentation_ip(value: str) -> bool:
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            return True
        return any(
            address in ipaddress.ip_network(network)
            for network in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")
        )

    @staticmethod
    def is_uri_userinfo(text: str, start: int, end: int) -> bool:
        scheme = text.rfind("://", 0, start)
        if scheme < 0:
            return False
        authority_start = scheme + 3
        authority_end = len(text)
        for marker in "/?#\"'\n\r \t":
            candidate = text.find(marker, authority_start)
            if candidate >= 0:
                authority_end = min(authority_end, candidate)
        return authority_start <= start and end <= authority_end

    def scan_notebook(self, text: str, relative: str) -> None:
        try:
            notebook = json.loads(text)
        except json.JSONDecodeError:
            self.add("NOTEBOOK_INVALID_JSON", relative)
            return
        for cell in notebook.get("cells", []):
            if cell.get("cell_type") != "code":
                continue
            if cell.get("execution_count") is not None:
                self.add("NOTEBOOK_EXECUTION_COUNT", relative)
            if cell.get("outputs"):
                self.add("NOTEBOOK_OUTPUT", relative)

    def scan_xlsx(self, data: bytes, relative: str) -> None:
        try:
            with zipfile.ZipFile(io_bytes(data)) as workbook:
                members = set(workbook.namelist())
                if any(
                    name.startswith(("xl/externalLinks/", "xl/connections")) for name in members
                ):
                    self.add("XLSX_EXTERNAL", relative)
                if any(name.endswith("vbaProject.bin") for name in members):
                    self.add("XLSX_MACRO", relative)
                if any("comments" in name.lower() or "persons" in name.lower() for name in members):
                    self.add("XLSX_COMMENT_OR_PERSON", relative)
                if any(name.startswith("customXml/") for name in members):
                    self.add("XLSX_CUSTOM_XML", relative)
                for name in sorted(members):
                    if not name.endswith((".xml", ".rels")):
                        continue
                    xml = workbook.read(name).decode("utf-8", errors="replace")
                    if name == "xl/workbook.xml" and HIDDEN_SHEET.search(xml):
                        self.add("XLSX_HIDDEN_SHEET", relative)
                    if FORMULA.search(xml):
                        self.add("XLSX_FORMULA", relative)
                    if DEFINED_NAME.search(xml):
                        self.add("XLSX_DEFINED_NAME", relative)
                    if name == "docProps/core.xml" and re.search(
                        r"<(?:[\w.-]+:)?(?:creator|lastModifiedBy)(?:\s[^>]*)?>\s*[^<\s]",
                        xml,
                        re.I,
                    ):
                        self.add("XLSX_PERSON_METADATA", relative)
                    if name.endswith(".rels") and re.search(
                        r"TargetMode\s*=\s*[\"']External[\"']", xml, re.I
                    ):
                        self.add("XLSX_EXTERNAL", relative)
                    if name.startswith("xl/worksheets/") or name in {
                        "xl/sharedStrings.xml",
                        "docProps/core.xml",
                    }:
                        self.scan_text(xml, relative)
        except (OSError, zipfile.BadZipFile):
            self.add("XLSX_INVALID", relative)

    def scan_png(self, data: bytes, relative: str) -> None:
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            self.add("PNG_INVALID", relative)
            return
        position = 8
        while position + 12 <= len(data):
            length = int.from_bytes(data[position : position + 4], "big")
            kind = data[position + 4 : position + 8]
            position += 12 + length
            if position > len(data):
                self.add("PNG_INVALID", relative)
                return
            if kind in {b"tEXt", b"zTXt", b"iTXt", b"eXIf"}:
                self.add("PNG_TEXT_METADATA", relative)
                return

    def scan_links(self, text: str, relative: str, members: set[str] | None = None) -> None:
        attributes = list(HTML_ATTR.finditer(text))
        attribute_url_spans = [match.span(1) for match in attributes]
        for match in attributes:
            target = match.group(1).strip()
            parsed = urlparse(target)
            if parsed.scheme in {"http", "https"}:
                self.check_evidence_host(
                    parsed.hostname, relative, is_asset=match.group(0).lower().startswith("src")
                )
            elif not parsed.scheme and not target.startswith(("#", "data:")):
                local_target = target.split("#", 1)[0]
                if members is None:
                    target_exists = (
                        ((self.root / relative).parent / local_target).resolve().exists()
                    )
                else:
                    target_path = posixpath.normpath(
                        posixpath.join(posixpath.dirname(relative), local_target)
                    )
                    target_exists = target_path in members
                if local_target and not target_exists:
                    self.add("HTML_LOCAL_LINK", relative)
        for match in URL.finditer(text):
            if any(
                start <= match.start() and match.end() <= end for start, end in attribute_url_spans
            ):
                continue
            parsed = urlparse(match.group().rstrip(".,;:\\"))
            self.check_evidence_host(parsed.hostname, relative, is_asset=False)

    def check_evidence_host(self, host: str | None, relative: str, *, is_asset: bool) -> None:
        allowed = ALLOWED_ASSET_HOSTS if is_asset else ALLOWED_EVIDENCE_HOSTS
        if host and host.lower() not in allowed:
            self.add("UNAPPROVED_EVIDENCE_HOST", relative)

    def scan_workflow(self, text: str, relative: str) -> None:
        if "uses:" not in text and "jobs:" not in text:
            return
        if re.search(r"^\s*(?:pull_request_target|workflow_run)\s*:", text, re.M):
            self.add("WORKFLOW_PRIVILEGED_TRIGGER", relative)
        if re.search(r"^\s*permissions:\s*(?:\n\s*)?[^#\n]*\bwrite\b", text, re.M):
            self.add("WORKFLOW_PERMISSION", relative)
        for match in WORKFLOW_ACTION.finditer(text):
            action = match.group(1)
            if action.startswith("./"):
                continue
            if not re.fullmatch(r"[a-z0-9_.-]+/[a-z0-9_.-]+@[0-9a-f]{40}", action):
                self.add("WORKFLOW_ACTION_PIN", relative)
            if action.startswith("actions/checkout@"):
                next_step = text[match.end() :].split("\n      - ", 1)[0]
                if not re.search(r"^\s*persist-credentials:\s*false\s*$", next_step, re.M):
                    self.add("WORKFLOW_CHECKOUT_CREDENTIALS", relative)
        if re.search(r"^\s*(?:release|security|history)\w*:\s*$", text, re.M) and not re.search(
            r"^\s*fetch-depth:\s*0\s*$", text, re.M
        ):
            self.add("WORKFLOW_HISTORY_DEPTH", relative)

    def scan_lock(self, text: str, relative: str) -> None:
        if re.search(r"^\s*--(?:extra-)?index-url\b", text, re.M):
            self.add("LOCK_EXTRA_INDEX", relative)
        if re.search(r"^\s*--trusted-host\b", text, re.M):
            self.add("LOCK_TRUSTED_HOST", relative)
        blocks = re.split(r"\n(?=\S)", text)
        for block in blocks:
            stripped = block.strip()
            if not stripped or stripped.startswith("#") or stripped.startswith("--"):
                continue
            first = stripped.splitlines()[0]
            if "git+" in block or "://" in first or " @ " in first or first.startswith("-e "):
                self.add("LOCK_URL_OR_VCS", relative)
            if not re.match(r"^[A-Za-z0-9_.-]+==[A-Za-z0-9_.!+-]+(?:\s|\\|$)", first):
                self.add("LOCK_RANGE_PIN", relative)
            if not re.search(r"--hash=sha256:[0-9a-f]{64}\b", block, re.I):
                self.add("LOCK_MISSING_HASH", relative)

    def scan_dependabot(self, text: str, relative: str) -> None:
        pip_entry = re.compile(
            r"package-ecosystem:\s*[\"']?pip[\"']?[\s\S]{0,240}?directory:\s*[\"']?/requirements[\"']?",
            re.I,
        )
        if not pip_entry.search(text):
            self.add("DEPENDABOT_PIP", relative)


def io_bytes(data: bytes):
    """Avoid a filesystem extraction when inspecting archive members."""
    from io import BytesIO

    return BytesIO(data)


def scan_archive(path: Path, prefix: str, tree: str | None) -> list[Finding]:
    scanner = ReleaseScanner(path.parent)
    expected = expected_tree(tree) if tree else None
    seen: set[str] = set()
    contents: list[tuple[str, bytes]] = []
    archive_root = prefix.rstrip("/")
    try:
        with tarfile.open(path, "r:*") as archive:
            for member in archive.getmembers():
                name = member.name
                if name == archive_root and member.isdir():
                    continue
                if not name.startswith(prefix):
                    scanner.add("ARCHIVE_PREFIX", name or "archive")
                    continue
                relative = name[len(prefix) :]
                if not relative:
                    continue
                pure = PurePosixPath(relative)
                if pure.is_absolute() or ".." in pure.parts:
                    scanner.add("ARCHIVE_UNSAFE_PATH", relative)
                    continue
                if relative.startswith((".git/", ".superpowers/", ".worktree/")):
                    scanner.add("ARCHIVE_FORBIDDEN_PATH", relative)
                    continue
                if member.isdir():
                    continue
                if member.issym() or member.islnk():
                    scanner.add("ARCHIVE_LINK", relative)
                    continue
                if not member.isfile():
                    scanner.add("ARCHIVE_NON_REGULAR", relative)
                    continue
                if relative in seen:
                    scanner.add("ARCHIVE_DUPLICATE", relative)
                    continue
                seen.add(relative)
                content = archive.extractfile(member)
                if content is None:
                    scanner.add("ARCHIVE_UNREADABLE", relative)
                    continue
                data = content.read()
                contents.append((relative, data))
    except (OSError, tarfile.TarError):
        scanner.add("ARCHIVE_INVALID", path.name)
    for relative, data in contents:
        scan_archive_member(scanner, data, relative, seen)
    if expected is not None:
        for missing in expected - seen:
            scanner.add("ARCHIVE_MISSING_PATH", missing)
        for extra in seen - expected:
            scanner.add("ARCHIVE_EXTRA_PATH", extra)
    return sorted(scanner.findings)


def expected_tree(tree: str | None) -> set[str]:
    if tree is None:
        return set()
    result = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", tree],
        capture_output=True,
        text=True,
        check=True,
    )
    return {line for line in result.stdout.splitlines() if line}


def scan_archive_member(
    scanner: ReleaseScanner, data: bytes, relative: str, members: set[str]
) -> None:
    suffix = Path(relative).suffix.lower()
    if suffix == ".xlsx":
        scanner.scan_xlsx(data, relative)
    elif suffix == ".png":
        scanner.scan_png(data, relative)
    else:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            scanner.add("UNRECOGNIZED_BINARY", relative)
            return
        scanner.scan_text(text, relative)
        if suffix == ".ipynb":
            scanner.scan_notebook(text, relative)
        if suffix in {".html", ".md", ".rst", ".ipynb"}:
            scanner.scan_links(text, relative, members)


def print_findings(findings: list[Finding]) -> int:
    for finding in findings:
        print(f"[{finding.code}] {finding.path}")
    print(f"release gate: {'PASS' if not findings else 'FAIL'} ({len(findings)} finding(s))")
    return 0 if not findings else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    tree_parser = subparsers.add_parser("tree", help="scan a tracked candidate tree or fixture")
    tree_parser.add_argument("path", type=Path)
    archive_parser = subparsers.add_parser(
        "archive", help="scan a release archive without extraction"
    )
    archive_parser.add_argument("path", type=Path)
    archive_parser.add_argument("--prefix", required=True)
    archive_parser.add_argument("--tree", help="Git tree-ish whose members must exactly match")
    args = parser.parse_args(argv)
    if args.command == "tree":
        return print_findings(ReleaseScanner(args.path).scan_tree())
    return print_findings(scan_archive(args.path, args.prefix, args.tree))


if __name__ == "__main__":
    raise SystemExit(main())
