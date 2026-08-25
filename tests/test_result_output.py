"""What a run PRINTS — the operator-facing half of every mode.

The envelope is the contract, but nobody reads a 10-key JSON object at a glance, and in a notebook
that dump is the whole output of the cell. `_print_result` renders the verdict first and keeps the
machine line after it, so the two audiences are served without either one losing anything.
"""

import json

import pytest

from _olaf_runtime import _print_result

# The label on the machine line — the variable the same envelope is also sitting in.
MACHINE_LINE = "OLAF.last_result: "


def envelope(**overrides):
    base = {
        "mode": "generate",
        "status": "success",
        "changed": True,
        "message": "generated 3 grants",
        "params": {},
        "data": {},
        "error": None,
        "batch_id": "B",
        "run_id": "R",
        "config_hash": "894df536a602615a",
    }
    base.update(overrides)
    return base


def printed(capsys, **overrides):
    _print_result(envelope(**overrides))
    return capsys.readouterr().out


@pytest.mark.parametrize(
    ("status", "badge"),
    [("success", "✅ success"), ("skipped", "⏭️"), ("blocked", "🚫 blocked"), ("error", "❌ error")],
)
def test_each_status_leads_with_its_own_badge(capsys, status, badge):
    """The verdict is the first thing on the line, so a scrolled-past cell still reads at a glance."""
    assert badge in printed(capsys, status=status).splitlines()[1]


def test_an_unknown_status_falls_back_to_its_own_name(capsys):
    """A status the badge map has not been taught must still print — never a KeyError, and never a
    blank where the verdict goes."""
    assert "experimental" in printed(capsys, status="experimental")


def test_a_changed_run_says_so_and_an_unchanged_one_does_not(capsys):
    assert "· changed" in printed(capsys, changed=True)
    assert "· changed" not in printed(capsys, changed=False)


def test_the_mode_is_always_named(capsys):
    assert "— apply" in printed(capsys, mode="apply")


def test_a_blank_message_prints_no_empty_line(capsys):
    """setup's envelope can carry an empty message; an indented blank line reads as a rendering
    bug."""
    out = printed(capsys, message="")
    assert "   \n" not in out


def test_a_small_dict_renders_as_key_values(capsys):
    out = printed(capsys, data={"counts": {"create": 2, "delete": 1}})
    assert "· counts: create=2, delete=1" in out


def test_the_machine_line_is_named_after_the_variable_it_is_also_in(capsys):
    """A reader who wants one field should not have to re-run the mode or hand-parse a wrapped
    JSON dump: the label says where the same envelope is already sitting."""
    out = printed(capsys, data={"grants": 3})
    assert MACHINE_LINE == "OLAF.last_result: "
    assert "RESULT:" not in out


def test_a_blank_line_separates_the_verdict_from_the_machine_line(capsys):
    """Butted straight against the last `· key: value` bullet it reads as one more of them, and
    setup's `unchanged:` line is long enough to wrap and hide the boundary entirely."""
    out = printed(capsys, data={"grants": 3})
    lines = out.splitlines()
    assert lines[lines.index(next(ln for ln in lines if ln.startswith(MACHINE_LINE))) - 1] == ""


def test_a_big_dict_renders_as_its_size(capsys):
    """A per-role summary can hold hundreds of roles — the human block states the size and leaves
    the contents to the JSON line below it."""
    out = printed(capsys, data={"summary": {f"Role{i}": i for i in range(9)}})
    assert "· summary: 9 entries" in out
    assert "Role7" not in out.split(MACHINE_LINE)[0]  # ...but the JSON still carries it
    assert "Role7" in out.split(MACHINE_LINE)[1]


def test_an_empty_dict_says_none_rather_than_nothing(capsys):
    """setup's `migrated` is {} on a clean re-run. Rendered as a join of nothing it printed
    `· migrated:` with a blank after the colon, which reads as a rendering fault."""
    assert "· migrated: (none)" in printed(capsys, data={"migrated": {}})


def test_a_nested_dict_renders_without_python_repr(capsys):
    """generate's per-role summary is a dict of dicts. Left to str() it prints
    `{'included': 2, 'excluded': 1}` — quotes, braces and all — in the middle of a human block."""
    out = printed(capsys, data={"summary": {"SalesReaders": {"included": 2, "excluded": 1}}})
    assert "· summary: SalesReaders=included=2 excluded=1" in out
    assert "{'included'" not in out.split(MACHINE_LINE)[0]


def test_a_nested_list_renders_joined(capsys):
    out = printed(capsys, data={"targets": {"roles": ["A", "B"], "folders": []}})
    assert "roles=A, B" in out
    assert "folders=(none)" in out


def test_a_list_renders_joined_and_an_empty_one_says_none(capsys):
    out = printed(
        capsys,
        data={
            "omitted_role_candidates": ["LegacyManual", "DefaultReader"],
            "drift_omission_candidates": [],
        },
    )
    assert "· omitted_role_candidates: LegacyManual, DefaultReader" in out
    assert "· drift_omission_candidates: (none)" in out


def test_a_scalar_renders_as_itself(capsys):
    assert "· grants: 3" in printed(capsys, data={"grants": 3})


def test_an_error_is_shown_under_the_verdict(capsys):
    out = printed(capsys, status="blocked", error="generate blocked: 2 error(s): ...")
    assert "↳ generate blocked: 2 error(s)" in out


def test_the_machine_line_survives_and_still_parses(capsys):
    """The pretty block is additive. The machine line stays exactly one line of valid JSON — it is
    what a pipeline log or a support ticket is read back from."""
    out = printed(capsys, data={"grants": 3, "roles": 2})
    result_lines = [ln for ln in out.splitlines() if ln.startswith(MACHINE_LINE)]
    assert len(result_lines) == 1
    parsed = json.loads(result_lines[0][len(MACHINE_LINE) :])
    assert parsed["mode"] == "generate"
    assert parsed["data"] == {"grants": 3, "roles": 2}
    assert parsed["config_hash"] == "894df536a602615a"
