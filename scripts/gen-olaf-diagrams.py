#!/usr/bin/env python3
"""Emit the light/dark SVG pair for two OLAF diagrams, in the repo's house style.

    scripts/gen-olaf-diagrams.py docs/assets

Regenerates apply-semantics-{light,dark}.svg and destructive-utilities-{light,dark}.svg.
One generator emits both themes of both diagrams so a pair cannot drift apart — the light
and dark files differ only by the colour map below, and editing one by hand is how they
stop matching. Running it against docs/assets reproduces the committed files byte for byte.

House style, copied from docs/assets/pipeline-branching-*.svg:
  viewBox only (no width/height) · system-ui font stack · role="img" + a full aria-label
  classes .box .t .s .ln .lbl · rx=11 boxes · stroke-width 2 · markers get a `d` suffix in dark
"""

import sys

FONT = "system-ui,-apple-system,Segoe UI,Roboto,sans-serif"

# light -> dark, taken from the existing pairs
DARK = {
    "#eef1fc": "#1e2540",
    "#3a4a8c": "#aeb9f0",
    "#7c8fd6": "#5b68b0",
    "#e6f7ee": "#16281f",
    "#237a5c": "#7fe0a8",
    "#2a9d6c": "#3fbf7a",
    "#eeeef2": "#262b36",
    "#4a5262": "#d0d6e0",
    "#9aa4b2": "#8a94a6",
    "#fdeaea": "#3a1f22",
    "#9c3535": "#f0a8a8",
    "#d97070": "#d97070",
    "#fcefd6": "#3a2c10",
    "#9c6a2a": "#efb35e",
    "#d99a45": "#e0a052",
    "#f0ebfc": "#2a1f40",
    "#4a3f8c": "#c3b8f5",
    "#8e44ad": "#a56fd6",
    "#5b6472": "#9aa4b2",
    "#f6f7f9": "#1a1d24",
    "#e2e5ec": "#2b303c",
}


def head(vb, aria, dark, extra_css=""):
    sub = DARK["#5b6472"] if dark else "#5b6472"
    sfx = "d" if dark else ""
    ln = "#8a94a6"
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="{vb}" font-family="{FONT}" role="img" aria-label="{aria}"><title>{aria}</title>
  <defs>
    <marker id="a{sfx}" viewBox="0 0 10 10" refX="8.5" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0 L10 5 L0 10 z" fill="{ln}"/></marker>
    <marker id="ar{sfx}" viewBox="0 0 10 10" refX="8.5" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0 L10 5 L0 10 z" fill="#d97070"/></marker>
  </defs>
  <style>
    .box{{stroke-width:2}}
    .t{{font-size:14px;font-weight:700}}
    .s{{font-size:10.5px;fill:{sub}}}
    .ln{{stroke:{ln};stroke-width:2;fill:none}}
    .lbl{{font-size:10.5px;fill:{sub};font-weight:600}}
    .chip{{font-size:11px;font-weight:600}}
    .del{{font-size:11px;font-weight:700;fill:#9c3535}}
    .kept{{font-size:11px;font-weight:700;fill:#237a5c}}
    .note{{font-size:10.5px;fill:{sub};font-style:italic}}{extra_css}
  </style>
'''


def c(col, dark):
    return DARK.get(col, col) if dark else col


def box(x, y, w, h, fill, stroke, dark, rx=11):
    return f'  <rect class="box" x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{c(fill, dark)}" stroke="{c(stroke, dark)}"/>\n'


def txt(x, y, s, cls="t", fill=None, anchor="middle", dark=False, size=None):
    f = f' fill="{c(fill, dark)}"' if fill else ""
    z = f' font-size="{size}"' if size else ""
    return f'  <text class="{cls}" x="{x}" y="{y}" text-anchor="{anchor}"{f}{z}>{s}</text>\n'


# ══════════════════════════════════════════════════════════════════════════════
# A · apply-semantics
# ══════════════════════════════════════════════════════════════════════════════
ARIA_A = (
    "What apply does to the live role set. Config declares two roles, Sales and Finance, while the "
    "live item also holds DefaultReader and a hand-made LegacyRole that config does not declare. "
    "A bare apply is the default and replaces the whole set: the two declared roles remain and both "
    "undeclared roles, including DefaultReader, are deleted. Passing keep_unmanaged equals true "
    "upserts instead: the two declared roles are created or updated and both undeclared roles stay. "
    "Fabric does not recreate a deleted DefaultReader, so a default apply against a lakehouse people "
    "already read from is an access cutover, not a deploy."
)


def diagram_a(dark):
    W, H = 748, 410
    m = "d" if dark else ""
    o = head(f"0 0 {W} {H}", ARIA_A, dark)

    # inputs — both feed both outcomes, so they join before the split
    o += box(24, 20, 320, 76, "#eef1fc", "#7c8fd6", dark)
    o += txt(184, 44, "config declares", "t", "#3a4a8c", dark=dark, size=13)
    o += txt(184, 65, "Sales · Finance", "chip", "#3a4a8c", dark=dark)
    o += txt(184, 84, "the complete intent", "s", dark=dark)

    o += box(404, 20, 320, 76, "#eeeef2", "#9aa4b2", dark)
    o += txt(564, 44, "live roles on the item", "t", "#4a5262", dark=dark, size=13)
    o += txt(564, 65, "Sales · Finance · DefaultReader · LegacyRole", "chip", "#4a5262", dark=dark)
    o += txt(564, 84, "two of these config never declared", "s", dark=dark)

    # join, then split — the call is what differs, not the inputs
    o += '  <path class="ln" d="M184 96 L184 126 L564 126 L564 96"/>\n'
    o += f'  <path class="ln" d="M184 126 L184 176" marker-end="url(#a{m})"/>\n'
    o += f'  <path class="ln" d="M564 126 L564 176" marker-end="url(#a{m})"/>\n'
    o += txt(374, 120, "apply reads both, every run", "s", dark=dark)
    o += txt(192, 150, "apply()", "lbl", anchor="start", dark=dark)
    o += txt(192, 165, "the default", "s", anchor="start", dark=dark)
    o += txt(572, 150, "apply(keep_unmanaged=true)", "lbl", anchor="start", dark=dark)
    o += txt(572, 165, "the deliberate override", "s", anchor="start", dark=dark)

    # outcomes
    o += box(24, 178, 320, 128, "#fdeaea", "#d97070", dark)
    o += txt(184, 202, "REPLACE", "t", "#9c3535", dark=dark, size=13)
    o += txt(184, 226, "kept: Sales · Finance", "kept", dark=dark)
    o += txt(184, 250, "deleted: LegacyRole", "del", dark=dark)
    o += txt(184, 268, "deleted: DefaultReader", "del", dark=dark)
    o += txt(184, 292, "live now mirrors config exactly", "s", dark=dark)

    o += box(404, 178, 320, 128, "#e6f7ee", "#2a9d6c", dark)
    o += txt(564, 202, "INCREMENTAL UPSERT", "t", "#237a5c", dark=dark, size=13)
    o += txt(564, 226, "created / updated: Sales · Finance", "kept", dark=dark)
    o += txt(564, 254, "untouched: DefaultReader · LegacyRole", "kept", dark=dark)
    o += txt(564, 284, "nothing is ever deleted", "s", dark=dark)

    # the consequence band
    o += box(24, 322, 700, 66, "#fcefd6", "#d99a45", dark)
    o += txt(
        374,
        346,
        "Fabric does not recreate a deleted DefaultReader",
        "t",
        "#9c6a2a",
        dark=dark,
        size=13,
    )
    o += txt(
        374,
        368,
        "so a default apply on a lakehouse people already read from is an access cutover, not a deploy",
        "s",
        dark=dark,
    )
    return o + "</svg>\n"


# ══════════════════════════════════════════════════════════════════════════════
# B · destructive-utilities
# ══════════════════════════════════════════════════════════════════════════════
ARIA_B = (
    "The blast radius of the two irreversible utilities. reset deletes every data access role on the "
    "item, including roles config never declared and DefaultReader, so nobody reads the data through "
    "OneLake security afterwards; it writes a role backup first and aborts without deleting if that "
    "write fails, and it leaves the control tables alone. cleanup drops all four control tables, "
    "including the entire audit history, and deletes every file under the security folder, including "
    "every pre-apply role backup; it leaves the live roles alone. The two interlock: cleanup destroys "
    "the backups reset depends on, so running cleanup first makes reset unrecoverable. Neither is a "
    "deployment mode, both are refused from the pipeline entrypoints and are available only "
    "interactively, by name."
)


def diagram_b(dark):
    W, H = 748, 404
    o = head(f"0 0 {W} {H}", ARIA_B, dark)

    # reset()
    o += box(24, 22, 330, 176, "#fdeaea", "#d97070", dark)
    o += txt(189, 46, "reset()", "t", "#9c3535", dark=dark)
    o += txt(189, 66, "destroys ACCESS", "s", dark=dark)
    o += txt(40, 92, "✕  every data access role on the item", "del", anchor="start", dark=dark)
    o += txt(52, 110, "config's own, anyone else's, Default*", "s", anchor="start", dark=dark)
    o += txt(40, 134, "✓  control tables untouched", "kept", anchor="start", dark=dark)
    o += txt(40, 158, "✓  writes a role backup first", "kept", anchor="start", dark=dark)
    o += txt(52, 176, "backup write fails → deletes nothing", "s", anchor="start", dark=dark)

    # cleanup()
    o += box(394, 22, 330, 176, "#f0ebfc", "#8e44ad", dark)
    o += txt(559, 46, "cleanup()", "t", "#4a3f8c", dark=dark)
    o += txt(559, 66, "destroys EVIDENCE", "s", dark=dark)
    o += txt(410, 92, "✕  all four control tables", "del", anchor="start", dark=dark)
    o += txt(422, 110, "config, mapping, member, the audit history", "s", anchor="start", dark=dark)
    o += txt(410, 134, "✕  every file under the security folder", "del", anchor="start", dark=dark)
    o += txt(422, 152, "including every pre-apply role backup", "s", anchor="start", dark=dark)
    o += txt(410, 176, "✓  live roles untouched", "kept", anchor="start", dark=dark)

    # the interlock — the label sits ABOVE the dashed run, never on it
    m = "d" if dark else ""
    o += txt(374, 220, "cleanup() deletes the backups reset() depends on", "del", dark=dark)
    o += f'  <path class="ln" d="M559 198 L559 236 L189 236 L189 264" stroke="#d97070" stroke-dasharray="5 4" marker-end="url(#ar{m})"/>\n'

    o += box(24, 266, 700, 54, "#fdeaea", "#d97070", dark)
    o += txt(
        374,
        290,
        "run cleanup() first and reset() becomes unrecoverable",
        "t",
        "#9c3535",
        dark=dark,
        size=13,
    )
    o += txt(
        374,
        310,
        "there is no third copy — the backup folder was the recovery path for both",
        "s",
        dark=dark,
    )

    # not a mode
    o += box(24, 336, 700, 52, "#eeeef2", "#9aa4b2", dark)
    o += txt(374, 360, "neither is a deployment mode", "t", "#4a5262", dark=dark, size=13)
    o += txt(
        374,
        378,
        "both are refused from the pipeline entrypoints — interactive only, by name",
        "s",
        dark=dark,
    )
    return o + "</svg>\n"


if __name__ == "__main__":
    out = sys.argv[1].rstrip("/")
    for name, fn in (("apply-semantics", diagram_a), ("destructive-utilities", diagram_b)):
        for dark in (False, True):
            p = f"{out}/{name}-{'dark' if dark else 'light'}.svg"
            open(p, "w", encoding="utf-8").write(fn(dark))
            print("wrote", p)
