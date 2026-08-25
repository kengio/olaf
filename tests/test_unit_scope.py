"""Scope resolution + the documented worked examples, as pytest functions.

Ported from `olaf_test_unit.ipynb` classes `ListAndGlob` and `GenerateHappyPaths`.
The runtime under test is the traced `_olaf_runtime` module (see conftest.py).
"""

import pytest

from _olaf_runtime import Catalog, Parse, ScopePath, ValidationError
from _fakes import CANON, generate_errors, generate_ok, index_grants, make_row


# ---------------------------------------------------------------------------------------------
# Parse.list / Catalog.resolve_* — list hygiene, name vs path form, globbing
# ---------------------------------------------------------------------------------------------


def test_parse_list_trims_dedupes_and_tolerates_trailing_semicolon():
    assert Parse.list(" a; b ;;a; ") == ["a", "b"]


def test_parse_list_none_is_empty():
    assert Parse.list(None) == []


def test_name_form_case_insensitive():
    assert Catalog.resolve_tables("hr.PAYROLL", CANON) == ["hr.payroll"]


def test_path_form_resolves():
    assert Catalog.resolve_tables("/Tables/sales/orders", CANON) == ["sales.orders"]


def test_table_part_glob():
    assert Catalog.resolve_tables("sales.*", CANON) == [
        "sales.leads",
        "sales.orders",
        "sales.returns",
    ]


@pytest.mark.parametrize(
    "pattern", ["*.orders", "sa*.orders"], ids=["bare-star schema", "partial-wildcard schema"]
)
def test_schema_wildcard_rejected(pattern):
    with pytest.raises(ValidationError):
        Catalog.resolve_tables(pattern, CANON)


@pytest.mark.parametrize("bad", ["sales.nope*", "orders"], ids=["zero-match glob", "no schema"])
def test_zero_match_and_noschema_raise(bad):
    with pytest.raises(ValidationError):
        Catalog.resolve_tables(bad, CANON)


def test_column_redirect_message():
    with pytest.raises(ValidationError) as excinfo:
        Catalog.resolve_tables("/Files/raw", CANON)
    assert "include_folders" in str(excinfo.value)


# ---------------------------------------------------------------------------------------------
# Parse.table_entry's rule-A3 messages. Before this, only a SUBSTRING of the shared message
# was pinned (test_unit_lib.py's other-absolute-path test); the shorter third message was pinned by
# nothing at all, which is how two of them came to sit byte-identical in separate branches unseen.
#
# Two facts need holding:
#   * the SHARED sentence is reached by BOTH "leading slash, not /Tables" AND "no dot at all".
#     '/sales.orders' is the case that proves it: it has a dot, so ONLY the leading-slash operand
#     can reject it. Without that input, deleting `e.startswith("/") or` from the merged condition
#     leaves the whole suite green at 100% while '/sales.orders' is silently accepted as
#     ('/sales', 'orders') -- a scope that can never match a real schema. Measured: across all
#     1186 tests the slash operand was decisive 0 times out of 762 evaluations before it was added.
#   * the third message is DELIBERATELY shorter -- see the comment beside it in Parse.table_entry.
#     A later tidy-up that reads the difference as an oversight and folds all three together goes
#     red on the negative assertion below.
# ---------------------------------------------------------------------------------------------

BOTH_FORMS = "table entry must be schema.table or /Tables/schema/table"
NAME_FORM_ONLY = "table entry must be schema.table:"


@pytest.mark.parametrize(
    "entry",
    ["/foo/bar", "/x", "/", "orders", "sales orders", "", "/sales.orders"],
    ids=[
        "slash-not-tables",
        "slash-short",
        "bare-slash",
        "no-dot",
        "space-not-dot",
        "empty",
        "slash-AND-dot",
    ],
)
def test_table_entry_both_forms_message_is_shared(entry):
    # One sentence reached from two conditions. 'slash-AND-dot' is the load-bearing case: it is the
    # only input here that the no-dot operand cannot reject, so it is what makes this a merge pin
    # rather than only a message pin.
    with pytest.raises(ValidationError) as excinfo:
        Parse.table_entry(entry)
    msg = str(excinfo.value)
    assert BOTH_FORMS in msg, msg
    assert f"'{entry}'" in msg and "(rule A3)" in msg, msg


@pytest.mark.parametrize("entry", [".orders", "sales.", ".", ".."])
def test_table_entry_dotted_but_empty_side_keeps_the_shorter_message(entry):
    # The distinction the merge must NOT erase: these mention ONLY the name form.
    with pytest.raises(ValidationError) as excinfo:
        Parse.table_entry(entry)
    msg = str(excinfo.value)
    assert NAME_FORM_ONLY in msg, msg
    assert "/Tables/schema/table" not in msg, f"the shorter A3 message lost its distinction: {msg}"


def test_table_entry_path_form_has_its_own_message():
    with pytest.raises(ValidationError) as excinfo:
        Parse.table_entry("/Tables/a")
    msg = str(excinfo.value)
    assert "table path must be /Tables/schema/table" in msg, msg
    assert BOTH_FORMS not in msg, msg


def test_folder_per_segment_glob():
    assert Catalog.resolve_folders("/Files/raw/region_*", CANON["folders"]) == [
        "/Files/raw/region_a",
        "/Files/raw/region_b",
    ]


def test_folder_literal_resolves_when_it_exists():
    assert Catalog.resolve_folders("/Files/raw/region_b", CANON["folders"]) == [
        "/Files/raw/region_b"
    ]


def test_folder_literal_must_exist():
    with pytest.raises(ValidationError):
        Catalog.resolve_folders("/Files/raw/nope", CANON["folders"])


def test_folder_without_leading_slash_canonicalizes():
    assert ScopePath.folder("Files/exports/x") == "/Files/exports/x"


def test_table_path_in_a_folder_column_redirects():
    with pytest.raises(ValidationError) as excinfo:
        Catalog.resolve_folders("/Tables/sales/orders", CANON["folders"])
    assert "include_tables" in str(excinfo.value)


def test_path_derivation_round_trips():
    assert ScopePath.table("hr.payroll") == "/Tables/hr/payroll"
    assert ScopePath.to_table("/Tables/hr/payroll") == "hr.payroll"


# ---------------------------------------------------------------------------------------------
# Generate.rows — the documented worked examples 1-7b
# ---------------------------------------------------------------------------------------------


def test_1_explicit_include_list():
    grants, _, _ = generate_ok(
        [
            make_row(
                role_name="RefRead",
                include_tables="ref.calendar;sales.leads",
                include_group_names="sg-analysts",
            )
        ]
    )
    idx = index_grants(grants)
    assert set(idx) == {("RefRead", "/Tables/ref/calendar"), ("RefRead", "/Tables/sales/leads")}
    assert idx[("RefRead", "/Tables/ref/calendar")]["member_group_names"] == "sg-analysts"


def test_2_table_part_wildcard():
    grants, _, _ = generate_ok(
        [
            make_row(
                role_name="SalesRead", include_tables="sales.*", include_group_names="sg-analysts"
            )
        ]
    )
    assert sorted(a["scope_path"] for a in grants) == [
        "/Tables/sales/leads",
        "/Tables/sales/orders",
        "/Tables/sales/returns",
    ]


def test_3_all_except():
    grants, _, summary = generate_ok(
        [
            make_row(
                role_name="SalesRead",
                include_tables="sales.*",
                exclude_tables="sales.returns",
                include_group_names="sg-analysts",
            )
        ]
    )
    assert sorted(a["scope_path"] for a in grants) == [
        "/Tables/sales/leads",
        "/Tables/sales/orders",
    ]
    assert summary["SalesRead"]["excluded"] == 1


def test_4_carveout_two_rows_per_table_rls():
    rows = [
        make_row(
            role_name="SalesTH",
            include_tables="sales.*",
            exclude_tables="sales.orders",
            rls_condition="region = 'TH'",
            include_group_names="sg-analysts",
        ),
        make_row(
            role_name="SalesTH",
            include_tables="sales.orders",
            rls_condition="region = 'TH' AND type = 'B2B'",
            include_group_names="sg-analysts",
        ),
    ]
    grants, warnings, _ = generate_ok(rows)
    idx = index_grants(grants)
    assert idx[("SalesTH", "/Tables/sales/leads")]["rls_condition"] == "region = 'TH'"
    assert idx[("SalesTH", "/Tables/sales/returns")]["rls_condition"] == "region = 'TH'"
    assert (
        idx[("SalesTH", "/Tables/sales/orders")]["rls_condition"]
        == "region = 'TH' AND type = 'B2B'"
    )
    assert any("carve-out or accident" in w and "sales/orders" in w for w in warnings)


def test_5_folder_wildcard_and_exclude():
    grants, _, _ = generate_ok(
        [
            make_row(
                role_name="RawRead",
                include_folders="/Files/raw/region_*",
                exclude_folders="/Files/raw/region_b",
                include_group_names="sg-analysts",
            )
        ]
    )
    assert [(a["scope_path"], a["scope_type"]) for a in grants] == [
        ("/Files/raw/region_a", "Folder")
    ]


def test_6_member_exclude():
    grants, _, _ = generate_ok(
        [
            make_row(
                role_name="RefRead",
                include_tables="ref.*",
                include_group_names="sg-analysts;sg-contractors",
                exclude_group_names="sg-contractors",
                include_sp_names="svc-etl",
            )
        ]
    )
    assert grants[0]["member_group_names"] == "sg-analysts"  # include - exclude
    assert grants[0]["member_sp_names"] == "svc-etl"  # service principals typed separately
    assert grants[0]["member_user_names"] is None  # unused member type stays empty


def test_7a_cls_blacklist():
    grants, _, _ = generate_ok(
        [
            make_row(
                role_name="HrRead",
                include_tables="hr.payroll",
                exclude_columns="salary;bank_account",
                include_group_names="sg-analysts",
            )
        ]
    )
    assert grants[0]["visible_columns"] == "employee_id;name;department"


def test_7b_cls_whitelist():
    grants, _, _ = generate_ok(
        [
            make_row(
                role_name="HrRead",
                include_tables="hr.payroll",
                include_columns="employee_id;name;department",
                include_group_names="sg-analysts",
            )
        ]
    )
    assert grants[0]["visible_columns"] == "employee_id;name;department"


# ---------------------------------------------------------------------------------------------
# Parse.table_entry's /Tables PATH-form validation. Four conditions share one `or` chain and
# one message, so branch coverage sees them as a single branch. THREE inputs reached the guard on
# main -- '/Tables/a', '/Tables/onlyone', and the valid '/Tables/sales/orders' -- measured by
# instrumenting it and running the suite. (This comment first said one, which was wrong.) But both
# REJECTED ones are length-2, so only `len(parts) != 3` was ever distinguished, and THREE
# independent weakenings left all 1234 tests green at 100 %:
#   * `len(parts) != 3` -> `< 3`      accepted '/Tables/a/b/c/d', silently truncated to ('a','b')
#   * dropping `parts[0] != "Tables"` accepted '/TablesX/a/b'   as ('a','b')
#   * dropping `not parts[1]`         accepted '/Tables//b'     as ('', 'b')
# Each turns a rejection into a silently WRONG scope, the direction this repo treats as dangerous.
#
# 🔴 The fourth condition, `not parts[2]`, is UNREACHABLE -- measured, not assumed. `e.strip("/")`
# removes trailing slashes, so `parts[-1]` is never empty; when len(parts) == 3, parts[2] IS
# parts[-1]. Every candidate that would empty it ('/Tables/a/', '/Tables//') collapses to a
# different length and trips `len != 3` first. '/Tables/a/' was proposed as the empty-segment
# case; it is not one, and a test written that way would claim to pin an operand while actually
# pinning `len != 3` -- the exact "prove the covered operand, leave the uncovered one" trap this
# block exists to close. The condition is KEPT as defence in depth (it costs nothing and becomes
# live the moment that strip changes to an lstrip), and it is named here so the next reader does
# not go looking for the input that exercises it.
# ---------------------------------------------------------------------------------------------

PATH_FORM = "table path must be /Tables/schema/table"


@pytest.mark.parametrize(
    "entry",
    ["/Tables/a", "/Tables", "/Tables/a/b/c", "/Tables/a/b/c/d", "/TablesX/a/b", "/Tables//b"],
    ids=[
        "too-few-segments",
        "no-segments",
        "too-many-segments",
        "far-too-many-segments",
        "prefix-is-not-exactly-Tables",
        "empty-schema-segment",
    ],
)
def test_table_entry_path_form_rejects_each_malformed_shape(entry):
    """One input per REACHABLE condition. TWO of the six are uniquely load-bearing; the rest are
    deliberate redundancy, and the difference is measured rather than asserted.

    Uniquely load-bearing -- nothing else in the suite kills these mutations:
      * 'prefix-is-not-exactly-Tables' is the only input a length check cannot reject, so it is
        what keeps the `parts[0]` operand alive.
      * 'empty-schema-segment' is the only one reaching `not parts[1]`.

    Redundant, kept for readability and both-sides symmetry: 'too-many'/'far-too-many' each kill
    `< 3` on their own, and 'too-few'/'no-segments' were already covered on main by
    test_parse_table_entry_bad_tables_path and test_table_entry_path_form_has_its_own_message.
    Review measured this after the first version of this docstring over-claimed all six.
    """
    with pytest.raises(ValidationError) as excinfo:
        Parse.table_entry(entry)
    msg = str(excinfo.value)
    assert PATH_FORM in msg, msg
    assert f"'{entry}'" in msg and "(rule A3)" in msg, msg


def test_table_entry_path_form_accepts_the_one_valid_shape():
    # The control. Without it the parametrize above is satisfied by a function that rejects
    # everything, which pins the message rather than the validation.
    assert Parse.table_entry("/Tables/sales/orders") == ("sales", "orders")


def test_a_row_that_already_errored_is_not_also_blamed_for_an_empty_effective_set():
    """`_scope_pair`'s `not had_err` operand — the last sibling found by the unheld-operand sweep.

    When one include entry is malformed and another resolves, the malformed one raises (had_err) and
    the exclusion then empties what did resolve. Without this operand the row collects a SECOND
    error blaming the exclusion — 'row grants no tables after exclusion' — when the real problem is
    the entry it already reported. The row is blocked either way, so this is about the fix-list an
    author reads, not about correctness; nothing asserted it, and dropping the operand was green.
    """
    errors = generate_errors(
        [
            make_row(
                role_name="R",
                include_tables="bad-entry;sales.orders",
                exclude_tables="sales.orders",
                include_group_names="sg-analysts",
            )
        ]
    )

    assert any("'bad-entry'" in e and "(rule A3)" in e for e in errors), errors
    assert not any("empty effective set" in e for e in errors), errors
