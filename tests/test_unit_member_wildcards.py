"""Member.expand_wildcards — patterns in member columns expand from onelake_security_member.

A member column may hold `*`/`?`. Expansion draws ONLY from the member table (the No-Graph gate's
single directory) and only from rows of that column's own member_type, so `include_sp_names = "sg-*"`
matches nothing even when Groups named `sg-…` exist.

Two properties carry the whole feature and each has its own test below:

  • the function returns NEW dicts and never mutates its input. `config_hash` is a property
    recomputed on every access from `short_rows`, which hands back the same mutable list, and
    `generate` reads it once for the idempotency skip and again when stamping the mapping — with
    validation in between. A mutating expansion therefore stamps a hash that `plan`/`apply` (which
    re-read the raw one) reject as STALE, forever. Measured on main: 391f70c0… -> ed2cbbba….

  • a 0-match pattern is reported AND dropped from the cell. Errors are collected, not raised, so a
    pattern left in place flows on to `Member._check_known` and emits the message that was
    reported ("add it (with its objectId)") — advice that is nonsense for a glob.
"""

import pytest

from _olaf_runtime import Member

# {(member_type, name.lower()): name AS WRITTEN} — the third value _load_member_cache returns
SPELLINGS = {
    ("Group", "sg-analysts"): "sg-analysts",
    ("Group", "sg-analysts-ro"): "sg-analysts-ro",
    ("Group", "sg-finance"): "sg-finance",
    ("ServicePrincipal", "svc-etl"): "svc-etl",
}


def expand(**cols):
    """One row through expand_wildcards -> (that row's new dict, errors)."""
    row = {"role_name": "R", **cols}
    rows, errors = Member.expand_wildcards([row], SPELLINGS)
    return rows[0], errors


# ---------------------------------------------------------------- the two load-bearing properties


def test_input_rows_are_never_mutated():
    """The blocker the plan's red-team found. If this regresses, every wildcard config generates
    cleanly and can then NEVER be applied — and no no-wildcard differential can see it."""
    row = {"role_name": "R", "include_group_names": "glob:sg-*"}
    before = dict(row)

    rows, _errors = Member.expand_wildcards([row], SPELLINGS)

    assert row == before, "expand_wildcards mutated the caller's row"
    assert rows[0] is not row, "expand_wildcards returned the same object, not a copy"
    assert rows[0]["include_group_names"] != row["include_group_names"]


def test_zero_match_pattern_is_reported_and_dropped_not_left_to_the_member_gate():
    """The member gate's advice must be unreachable for a member column: the pattern never survives."""
    new, errors = expand(include_group_names="glob:sg-ghost-*")

    assert len(errors) == 1
    assert "rule C15" in errors[0]
    assert "sg-ghost-*" in errors[0]
    # dropped, so nothing downstream can try to resolve a glob as a principal
    assert new["include_group_names"] == ""
    assert not any("with its objectId" in e for e in errors)


# ---------------------------------------------------------------------------- expansion behaviour


def test_pattern_expands_from_the_member_table_in_sorted_order():
    new, errors = expand(include_group_names="glob:sg-analysts*")
    assert errors == []
    assert new["include_group_names"] == "sg-analysts;sg-analysts-ro"


def test_expansion_is_filtered_by_the_columns_own_member_type():
    """`sg-*` names Groups; asking for it in the ServicePrincipal column must match nothing rather
    than silently pulling a Group into an SP grant."""
    new, errors = expand(include_sp_names="glob:sg-*")
    assert len(errors) == 1
    assert "rule C15" in errors[0]
    assert "ServicePrincipal" in errors[0]
    assert new["include_sp_names"] == ""


@pytest.mark.parametrize(
    "pattern,expected",
    [
        ("glob:*", "sg-analysts;sg-analysts-ro;sg-finance"),
        ("glob:sg-analyst?", "sg-analysts"),
        ("glob:sg-f*e", "sg-finance"),
        ("glob:SG-ANALYSTS*", "sg-analysts;sg-analysts-ro"),  # matching is case-insensitive
    ],
)
def test_metacharacters_and_case_insensitive_matching(pattern, expected):
    new, errors = expand(include_group_names=pattern)
    assert errors == []
    assert new["include_group_names"] == expected


def test_a_cell_may_mix_a_literal_and_a_pattern():
    new, errors = expand(include_group_names="sg-finance;glob:sg-analysts*")
    assert errors == []
    assert new["include_group_names"] == "sg-finance;sg-analysts;sg-analysts-ro"


def test_a_literal_also_matched_by_the_pattern_is_not_listed_twice():
    new, errors = expand(include_group_names="sg-analysts;glob:sg-analysts*")
    assert errors == []
    assert new["include_group_names"] == "sg-analysts;sg-analysts-ro"


def test_exclude_columns_expand_too_and_a_dead_exclude_pattern_errors():
    """Deliberately unlike a dead LITERAL exclusion, which is accepted: `Catalog.resolve_tables`
    records why — a dead exclude leaves everything it meant to remove granted, so it fails OPEN."""
    new, errors = expand(include_group_names="glob:sg-*", exclude_group_names="glob:sg-analysts-*")
    assert errors == []
    assert new["exclude_group_names"] == "sg-analysts-ro"

    _new2, errors2 = expand(include_group_names="glob:sg-*", exclude_group_names="glob:sg-ghost-*")
    assert len(errors2) == 1
    assert "rule C15" in errors2[0]


# ------------------------------------------------------------------------------- canonicalisation


def test_a_literal_is_rewritten_to_the_member_tables_spelling():
    """Mapping spelling becomes a pure function of the member table, so a pattern-expanded row and
    a literal row can never disagree about case — the false 'different principals' hard error."""
    new, errors = expand(include_group_names="SG-Analysts")
    assert errors == []
    assert new["include_group_names"] == "sg-analysts"


def test_a_literal_absent_from_the_table_is_left_exactly_as_written():
    """Not this function's job to judge — the No-Graph gate names it, and it must name the spelling
    the author actually typed."""
    new, errors = expand(include_group_names="sg-nope")
    assert errors == []
    assert new["include_group_names"] == "sg-nope"


def test_a_cell_needing_no_change_is_left_byte_identical():
    row = {"role_name": "R", "include_group_names": "sg-analysts;"}  # trailing separator preserved
    rows, errors = Member.expand_wildcards([row], SPELLINGS)
    assert errors == []
    assert rows[0]["include_group_names"] == "sg-analysts;"


def test_a_cell_whose_author_wrote_two_spellings_is_left_alone_for_the_existing_guard():
    """Canonicalisation must never silently resolve a disagreement the author wrote.

    `Generate._members`' within-cell guard splits the RAW cell precisely because `Parse.list`
    dedupes case-insensitively and would drop a variant silently. Rewriting such a cell — with or
    without canonicalisation — collapses the two spellings and that guard stops firing, turning a
    hard error into a silent under-grant. So this function keeps its hands off and lets the guard
    report it.
    """
    new, errors = expand(include_group_names="SG-Analysts;sg-analysts")
    assert errors == []
    assert new["include_group_names"] == "SG-Analysts;sg-analysts"


# ------------------------------------------------------------------------------------ neutrality


def test_rows_without_any_member_column_pass_through_untouched():
    rows, errors = Member.expand_wildcards(
        [{"role_name": "R", "include_tables": "sales.*"}], SPELLINGS
    )
    assert errors == []
    assert rows[0] == {"role_name": "R", "include_tables": "sales.*"}


def test_a_table_glob_is_not_treated_as_a_member_pattern():
    """`include_tables` globs are legal and unrelated; only the eight member columns expand."""
    rows, errors = Member.expand_wildcards(
        [{"role_name": "R", "include_tables": "sales.*", "include_group_names": "sg-finance"}],
        SPELLINGS,
    )
    assert errors == []
    assert rows[0]["include_tables"] == "sales.*"


def test_empty_and_absent_member_cells_stay_empty_rather_than_becoming_a_string():
    rows, errors = Member.expand_wildcards(
        [{"role_name": "R", "include_group_names": None, "exclude_group_names": ""}],
        SPELLINGS,
    )
    assert errors == []
    assert rows[0]["include_group_names"] is None
    assert rows[0]["exclude_group_names"] == ""


def test_errors_name_the_row_and_survive_across_several_rows():
    rows, errors = Member.expand_wildcards(
        [
            {"role_name": "Alpha", "include_group_names": "sg-finance"},
            {"role_name": "Beta", "include_group_names": "glob:sg-ghost-*"},
        ],
        SPELLINGS,
    )
    assert len(errors) == 1
    assert "row 2 (Beta)" in errors[0]
    assert rows[0]["include_group_names"] == "sg-finance"


def test_an_empty_member_table_makes_every_pattern_a_zero_match_rather_than_a_silent_pass():
    """Fail closed: `_load_member_cache` tolerates an absent table by returning empty, so expansion
    must refuse rather than quietly produce an empty member list."""
    new, errors = Member.expand_wildcards(
        [{"role_name": "R", "include_group_names": "glob:sg-*"}], {}
    )
    assert len(errors) == 1
    assert "rule C15" in errors[0]
    assert new[0]["include_group_names"] == ""


# --------------------------------------------------------------- has_wildcard's column scope


def test_has_wildcard_sees_the_eight_member_columns():
    for col in (
        "include_group_names",
        "exclude_group_names",
        "include_user_names",
        "exclude_user_names",
        "include_sp_names",
        "exclude_sp_names",
        "include_mi_names",
        "exclude_mi_names",
    ):
        assert Member.has_wildcard([{"role_name": "R", col: "glob:sg-*"}]), col
        assert Member.has_wildcard([{"role_name": "R", col: "glob:sg-analyst?"}]), col


def test_has_wildcard_ignores_globs_outside_the_member_columns():
    """A table or folder glob is legal and ordinary — `include_tables: "sales.*"` is in the
    canonical fixture. A whole-row scan would match it and disable the generate idempotency skip
    for essentially every real config, silently, with both of its branches still covered."""
    assert not Member.has_wildcard(
        [{"role_name": "R", "include_tables": "sales.*", "exclude_folders": "/Files/raw/*"}]
    )
    assert not Member.has_wildcard([{"role_name": "R", "include_group_names": "sg-analysts"}])
    assert not Member.has_wildcard([])


# ------------------------------------------------- a pattern must DECLARE itself (privilege review)


def test_a_name_containing_a_metacharacter_is_a_literal_unless_it_declares_itself_a_pattern():
    """The privilege-boundary blocker. Entra permits `*` and `?` in a displayName, so sniffing for
    those characters cannot tell a glob from a real name — and guessing wrong grants a DIFFERENT
    principal with no error at all.

    Measured before this fix: config `Sales? Reporting`, member table holding the unrelated
    `Salesx Reporting`, expanded to `Salesx Reporting` with `errors == []` and generated a mapping.
    On base the same input was a hard block, so the feature had converted a fail-closed refusal
    into a silent wrong-principal grant.
    """
    spellings = {("Group", "salesx reporting"): "Salesx Reporting"}
    rows, errors = Member.expand_wildcards(
        [{"role_name": "R", "include_group_names": "Sales? Reporting"}], spellings
    )

    assert errors == []
    # left exactly as authored — it is a NAME, so the No-Graph gate decides its fate, not fnmatch
    assert rows[0]["include_group_names"] == "Sales? Reporting"
    assert "Salesx Reporting" not in rows[0]["include_group_names"]


def test_the_marker_is_case_insensitive_and_tolerates_space_after_the_colon():
    for spelling in ("glob:sg-analyst*", "GLOB:sg-analyst*", "Glob: sg-analyst*"):
        new, errors = expand(include_group_names=spelling)
        assert errors == [], spelling
        assert new["include_group_names"] == "sg-analysts;sg-analysts-ro", spelling


def test_a_declared_pattern_that_matches_nothing_still_errors_under_c15():
    new, errors = expand(include_group_names="glob:sg-ghost-*")
    assert len(errors) == 1
    assert "rule C15" in errors[0]
    assert "glob:sg-ghost-*" in errors[0]
    assert new["include_group_names"] == ""
