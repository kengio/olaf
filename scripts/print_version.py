#!/usr/bin/env python3
"""Print the runtime's `__version__`, read from the notebook that defines it.

The release-evidence archive is named after the version, and naming it in the workflow froze it:
the v1.1.0 evidence would have been filed under `olaf-1.0.0`. Reading it here keeps one source of
truth and keeps the workflow free of an inline script -- an earlier attempt embedded a heredoc in
the `run:` block, whose body sat at column 0 and silently terminated the YAML literal, so GitHub
could not parse the workflow and ran zero jobs while reporting only "no checks reported".
"""

import json
import pathlib
import re
import sys

NOTEBOOK = pathlib.Path(__file__).resolve().parent.parent / "notebooks" / "olaf.ipynb"


def main() -> int:
    cells = json.loads(NOTEBOOK.read_text(encoding="utf-8"))["cells"]
    source = "\n".join("".join(cell.get("source", [])) for cell in cells)
    found = re.search(r'__version__\s*=\s*"([^"]+)"', source)
    if not found:
        print(f"no __version__ in {NOTEBOOK}", file=sys.stderr)
        return 1
    print(found.group(1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
