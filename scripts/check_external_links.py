#!/usr/bin/env python3
"""Report unreachable http(s) links in the repo's markdown. ADVISORY, never a merge gate.

The internal links -- files and anchors -- are gated in tests/test_doc_links.py, because those are
ours and a break is always our bug. These are not: a rate-limited Microsoft Learn page or a briefly
down third party would fail a build with nothing wrong in it. So this runs in its own
`continue-on-error` job and exists to be READ, not to block.

It also reports links that MOVED — where the page now declares a different canonical, or a redirect
landed somewhere else. Both cases keep answering 200, so a status-code check calls them healthy
forever: that is how two Microsoft Learn URLs cited here as independent sources both came to serve
one consolidated article without anything noticing. Moved is not broken; it is a prompt to re-read
the destination and check the sentence being quoted survived the merge.

Usage: python3 scripts/check_external_links.py [--timeout 20]
Exits 0 always; prints a report and a non-zero-looking summary line when something is unreachable.
"""

from __future__ import annotations

import concurrent.futures
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
# Every working directory .gitignore keeps out of the repo, plus .git itself. These tests walk
# the filesystem rather than the index, so an ignored folder that happens to be present —
# .superpowers/ coordination notes are the usual one — otherwise gets link-checked as if it
# shipped, and fails on paths that were never meant to resolve from the repo root.
IGNORE_DIRS = {
    ".git",
    ".worktree",
    ".superpowers",
    ".venv",
    ".ruff_cache",
    ".pytest_cache",
    "__pycache__",
    "node_modules",
    "out",
    "reports",
    "brand-drafts",
}
LINK = re.compile(r"\[[^\]]*\]\(\s*<?(https?://[^)\s>]+)>?\s*(?:\"[^\"]*\")?\)")
UA = "olaf-docs-linkcheck (+https://github.com/kengio/olaf)"
TIMEOUT = int(sys.argv[sys.argv.index("--timeout") + 1]) if "--timeout" in sys.argv else 20


def sources():
    for p in sorted(REPO_ROOT.rglob("*.md")):
        if IGNORE_DIRS.intersection(p.relative_to(REPO_ROOT).parts):
            continue
        yield p.relative_to(REPO_ROOT), p.read_text(encoding="utf-8")
    for p in sorted(REPO_ROOT.rglob("*.ipynb")):
        if IGNORE_DIRS.intersection(p.relative_to(REPO_ROOT).parts):
            continue
        nb = json.loads(p.read_text(encoding="utf-8"))
        for c in nb.get("cells", []):
            if c.get("cell_type") == "markdown":
                src = c["source"]
                yield p.relative_to(REPO_ROOT), src if isinstance(src, str) else "".join(src)


CANONICAL = re.compile(rb"""<link[^>]+rel=["']canonical["'][^>]+href=["']([^"']+)["']""", re.I)


def check(url):
    """(url, status, reason, where_it_really_is)

    GET, never HEAD: the interesting answer is in the BODY. A page can keep answering 200 at the
    address we cite while declaring a different canonical -- which is what an article
    consolidation looks like from out here, and no status code or redirect ever reveals it.
    """
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            head = r.read(65536)  # canonical lives in <head>; do not pull whole articles
            m = CANONICAL.search(head)
            # r.url covers an ordinary redirect; the canonical tag covers the quiet case.
            return url, r.status, "", (m.group(1).decode(errors="replace") if m else r.url)
    except urllib.error.HTTPError as e:
        return url, e.code, e.reason, url
    except Exception as e:  # DNS, TLS, timeout, redirect loop
        return url, 0, f"{type(e).__name__}: {e}", url


def _target(url):
    """URL without its fragment — fragments never reach the server, so they cannot be compared."""
    return url.split("#", 1)[0].rstrip("/")


def main():
    where = {}
    for path, text in sources():
        for m in LINK.finditer(text):
            where.setdefault(m.group(1).rstrip(".,"), set()).add(str(path))
    urls = sorted(where)
    print(f"checking {len(urls)} unique external link(s)…\n")
    bad, moved = [], []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        for url, status, reason, final in pool.map(check, urls):
            if status and 200 <= status < 400:
                # Reachable, but is it still the page we cited? Two Microsoft Learn URLs cited
                # here as independent sources both serve one consolidated article today — no
                # redirect, no error, just a canonical pointing somewhere else. Nothing that
                # looks only at status codes can see that. Advisory: not a break, a prompt to
                # re-read the destination and confirm the quoted sentence survived the merge.
                if _target(final) != _target(url):
                    moved.append((url, final))
                continue
            bad.append((url, status, reason))
    blocked = [b for b in bad if b[1] in (401, 403, 429)]
    dead = [b for b in bad if b not in blocked]
    if moved:
        print("-- moved · re-read the destination and confirm the quote is still there --")
        for url, final in sorted(moved):
            print(f"  {url}\n       -> {final}")
            for f in sorted(where[url]):
                print(f"        in {f}")
        print()
    for label, group in (("unreachable", dead), ("blocked, not necessarily broken", blocked)):
        if not group:
            continue
        print(f"-- {label} --")
        for url, status, reason in sorted(group):
            print(f"  [{status or 'ERR'}] {url}\n        {reason}")
            for f in sorted(where[url]):
                print(f"        in {f}")
        print()
    print(
        f"{len(urls) - len(bad)}/{len(urls)} reachable"
        + (f" · {len(moved)} moved" if moved else "")
        + (f" · {len(dead)} unreachable" if dead else "")
        + (f" · {len(blocked)} blocked" if blocked else "")
    )
    if blocked:
        print(
            "\nA 401/403/429 here can mean that the host refuses this caller rather than that a\n"
            "link is broken. Re-check the official destination manually before treating it as rot."
        )
    return 0  # advisory by design


if __name__ == "__main__":
    raise SystemExit(main())
