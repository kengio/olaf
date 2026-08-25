"""Every internal markdown link resolves — the file AND the anchor.

CI never renders the docs, so a link that 404s is found by whoever clicks it. Two failure modes,
and the second is the one that keeps happening: the target FILE moving (the kebab-case rename
touched 164 references) and the target ANCHOR going stale (a renamed heading leaves every link to
it silently dead — `#c13` existed only as a heading slug for weeks, and one review found three
more).

External `http(s)` links are deliberately NOT fetched: a rate-limited or briefly-down third party
would fail a build that has nothing wrong with it. What is checked here is everything the repo
controls.
"""

import json
import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
LINK = re.compile(r"\[[^\]]*\]\(\s*(<?)([^)\s>]+)\1\s*(?:\"[^\"]*\")?\)")
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$", re.M)
EXPLICIT_ANCHOR = re.compile(r"<a\s+(?:id|name)\s*=\s*[\"']([^\"']+)[\"']", re.I)
SKIP_SCHEME = ("http://", "https://", "mailto:", "tel:", "#!", "data:")


def slug(heading: str) -> str:
    """GitHub's heading -> fragment rule, for our own headings.

    Lowercase, drop everything that is not alphanumeric / space / hyphen / underscore, then ONE
    hyphen per remaining space. Three details that all produced false "dead anchor" reports before
    they were right:

    * `_` SURVIVES -- it is a word character, and half these headings are snake_case identifiers.
      (Do not pre-strip markdown emphasis: `*`/`_` handling eats `set_config_provenance`.)
    * whitespace runs are NOT collapsed -- an em-dash between two spaces vanishes and leaves both
      spaces, which is where `…drifted--repair…` comes from.
    * no strip AFTER the punctuation pass -- a heading opening with an emoji keeps the space that
      followed it, so its anchor legitimately starts with a hyphen.

    Backticks, `*`, brackets and emoji all fall out of the punctuation pass on their own; only the
    link form needs unwrapping first, since GitHub slugs the visible text.
    """
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading.strip())
    text = re.sub(r"[^\w\s-]", "", text.lower(), flags=re.UNICODE)
    return re.sub(r"\s", "-", text)


def anchors_of(path: Path) -> set:
    text = path.read_text(encoding="utf-8", errors="replace")
    found = {slug(m.group(2)) for m in HEADING.finditer(text)}
    found |= {a.lower() for a in EXPLICIT_ANCHOR.findall(text)}
    return {a for a in found if a}


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


def _ignored(path):
    return bool(IGNORE_DIRS.intersection(path.relative_to(REPO_ROOT).parts))


def md_sources():
    """(label, containing-dir, text) for every markdown surface in the repo — the .md files and
    the markdown cells of the three notebooks, since those carry links too."""
    for p in sorted(REPO_ROOT.rglob("*.md")):
        # relative_to(REPO_ROOT): the repo itself may sit UNDER a .worktree/ path during
        # development, so filtering on absolute parts excludes everything.
        if _ignored(p):
            continue
        yield str(p.relative_to(REPO_ROOT)), p.parent, p.read_text(encoding="utf-8")
    for nb_path in sorted(REPO_ROOT.rglob("*.ipynb")):
        if _ignored(nb_path):
            continue
        nb = json.loads(nb_path.read_text(encoding="utf-8"))
        for i, c in enumerate(nb.get("cells", [])):
            if c.get("cell_type") != "markdown":
                continue
            src = c["source"]
            yield (
                f"{nb_path.relative_to(REPO_ROOT)} cell {i}",
                nb_path.parent,
                src if isinstance(src, str) else "".join(src),
            )


def collect(check_anchor):
    broken, checked = [], 0
    for label, base, text in md_sources():
        for m in LINK.finditer(text):
            target = m.group(2)
            if target.startswith(SKIP_SCHEME):
                continue
            file_part, _, anchor = target.partition("#")
            resolved = (base / file_part).resolve() if file_part else None
            checked += 1
            if file_part and not resolved.exists():
                broken.append(f"{label}: {target}  -> no such file")
                continue
            if anchor and check_anchor:
                doc = (
                    resolved if file_part else Path(base / label.split(" cell ")[0].split("/")[-1])
                )
                doc = resolved if file_part else REPO_ROOT / label
                if doc.suffix != ".md" or not doc.exists():
                    continue  # anchors inside a notebook are not addressable — nothing to verify
                if anchor.lower() not in anchors_of(doc):
                    broken.append(f"{label}: {target}  -> no such anchor in {doc.name}")
    return broken, checked


def test_every_internal_link_points_at_a_file_that_exists():
    broken, checked = collect(check_anchor=False)
    assert checked > 200, f"only {checked} links found — the collector is broken, not the docs"
    assert not broken, "broken link target(s):\n  " + "\n  ".join(broken)


def test_every_internal_anchor_exists_in_its_target():
    broken, _ = collect(check_anchor=True)
    anchor_only = [b for b in broken if "no such anchor" in b]
    assert not anchor_only, "dead anchor(s):\n  " + "\n  ".join(anchor_only)
