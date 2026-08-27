"""The documented error/warning cases E1-E12 plus the retained duplicate invariants and
the C4-C13 rule family, as pytest functions.

Ported from `olaf_test_unit.ipynb` class `ValidationRules`.
"""

import itertools

import pytest

from _olaf_runtime import RLS, RLS_UNSUPPORTED_KEYWORDS, Generate, Member
from _fakes import CANON, generate_errors, generate_warnings, make_row


def canon_with_order_columns(*extra):
    """CANON with extra columns appended to sales.orders — the fixture the C11/C9 keyword-column
    rules need (the rule is about the CASE of a reference, so the column must exist)."""
    return {
        "tables": CANON["tables"],
        "folders": CANON["folders"],
        "columns": {
            **CANON["columns"],
            "sales.orders": [*CANON["columns"]["sales.orders"], *extra],
        },
    }


def c12_errors(role_name):
    """rule C12 errors from a single make_row(role_name=..., include_tables=...,
    include_group_names=...) row -- the shared filter the three blocking C12 tests need,
    each varying only role_name."""
    return [
        e
        for e in generate_errors(
            [
                make_row(
                    role_name=role_name,
                    include_tables="sales.orders",
                    include_group_names="sg-analysts",
                )
            ]
        )
        if "rule C12" in e
    ]


# ---------------------------------------------------------------------------------------------
# E1-E12 — the documented error/warning cases
# ---------------------------------------------------------------------------------------------


def test_e1_exclude_without_include():
    errors = generate_errors(
        [make_row(role_name="R", exclude_tables="sales.returns", include_group_names="sg-analysts")]
    )
    assert any("nothing to subtract from" in e for e in errors)


def test_e2_no_scope_grant():
    errors = generate_errors([make_row(role_name="R", include_group_names="sg-analysts")])
    assert any("rule A1" in e for e in errors)


def test_e3_schema_wildcard():
    errors = generate_errors(
        [make_row(role_name="R", include_tables="*.orders", include_group_names="sg-analysts")]
    )
    assert any("schema part must be literal" in e for e in errors)


def test_e4_column_redirect():
    errors = generate_errors(
        [make_row(role_name="R", include_tables="/Files/raw", include_group_names="sg-analysts")]
    )
    assert any("include_folders" in e for e in errors)


def test_e5_member_mismatch():
    rows = [
        make_row(
            role_name="SalesRead", include_tables="sales.orders", include_group_names="sg-analysts"
        ),
        make_row(
            role_name="SalesRead",
            include_tables="hr.employees",
            include_group_names="sg-analysts;sg-contractors",
        ),
    ]
    assert any("rule C1" in e for e in generate_errors(rows))


def test_e6_two_policies_one_table():
    rows = [
        make_row(
            role_name="SalesReaders",
            include_tables="sales.*",
            rls_condition="region = 'TH'",
            include_group_names="sg-analysts",
        ),
        make_row(
            role_name="SalesReaders",
            include_tables="sales.orders",
            rls_condition="region = 'US'",
            include_group_names="sg-analysts",
        ),
    ]
    assert any("rule C3" in e for e in generate_errors(rows))


def test_e7_empty_after_subtract():
    errors = generate_errors(
        [
            make_row(
                role_name="R",
                include_tables="sales.orders",
                exclude_tables="sales.orders",
                include_group_names="sg-analysts",
            )
        ]
    )
    assert any("empty effective set" in e for e in errors)


def test_e8_column_existence():
    canon8 = {
        "tables": CANON["tables"],
        "folders": CANON["folders"],
        "columns": {**CANON["columns"], "sales.returns": ["return_id", "reason", "amount"]},
    }
    errors = generate_errors(
        [
            make_row(
                role_name="R",
                include_tables="sales.*",
                rls_condition="region = 'TH'",
                include_group_names="sg-analysts",
            )
        ],
        canon=canon8,
    )
    assert any("region" in e and "missing" in e and "sales.returns" in e for e in errors)


def test_e9_exclude_zero_match_errors():
    """An exclude that matches nothing is an ERROR, not a warning.

    It used to warn, on the reasoning that pre-emptively excluding a not-yet-created table is
    normal. But include and exclude fail in opposite directions: a dead include grants less than
    intended and fails closed, while a dead exclude leaves everything it was meant to remove
    still granted -- it fails open. The lenient treatment was on the dangerous side.
    """
    errors, warnings = generate_warnings(
        [
            make_row(
                role_name="R",
                include_tables="sales.orders",
                exclude_tables="sales.tmp_load",
                include_group_names="sg-analysts",
            )
        ]
    )
    assert any("matched 0" in e for e in errors)
    # the message has to say the exclusion did nothing, or the reader has no way to tell this
    # apart from the include-side miss, which is the harmless direction
    assert any("still granted" in e for e in errors)
    assert not any("matched 0" in w for w in warnings)


def test_e9b_zero_match_names_the_near_miss():
    """A refusal should carry the fix. A misspelt schema is reported as such, separately from a
    real schema holding an unknown table -- schemas are created once up front, so naming one that
    does not exist can only be a typo."""
    errors, _ = generate_warnings(
        [
            make_row(
                role_name="R",
                include_tables="saels.orders",
                include_group_names="sg-analysts",
            )
        ]
    )
    assert any("unknown schema 'saels'" in e and "did you mean 'sales'?" in e for e in errors)


def test_e10_subtree_carve_warns():
    errors, warnings = generate_warnings(
        [
            make_row(
                role_name="R",
                include_folders="/Files/raw",
                exclude_folders="/Files/raw/temp",
                include_group_names="sg-analysts",
            )
        ]
    )
    assert errors == []
    assert any("subtree" in w for w in warnings)


def test_e11_paths_limit():
    big = {
        "tables": {f"big.t{i:03}": f"big.t{i:03}" for i in range(600)},
        "columns": {},
        "folders": {},
    }
    errors = generate_errors(
        [make_row(role_name="Big", include_tables="big.*", include_group_names="sg-analysts")],
        canon=big,
    )
    assert any("500" in e and "exceed" in e for e in errors)


def test_e12_cls_both_modes():
    errors = generate_errors(
        [
            make_row(
                role_name="R",
                include_tables="hr.payroll",
                include_columns="name",
                exclude_columns="salary",
                include_group_names="sg-analysts",
            )
        ]
    )
    assert any("pick one CLS mode" in e for e in errors)


# ---------------------------------------------------------------------------------------------
# Rule C11 — RLS/CLS column references must match the Delta column's CASE
# ---------------------------------------------------------------------------------------------


def test_c11_rls_column_case_mismatch_blocks():
    # The null-safety predicate references the column twice, once with an incorrect case.
    # C11 scans every raw occurrence and blocks this OLAF authoring mismatch, naming both
    # spellings and the table. It deliberately makes no service-enforcement claim.
    errors = generate_errors(
        [
            make_row(
                role_name="R",
                include_tables="sales.orders",
                rls_condition="RecordTypeId NOT IN ('x') OR recordtypeid IS NULL",
                include_group_names="sg-analysts",
            )
        ],
        canon=canon_with_order_columns("RecordTypeId"),
    )
    c11 = [e for e in errors if "rule C11" in e]
    assert len(c11) == 1, c11
    assert "recordtypeid" in c11[0]
    assert "RecordTypeId" in c11[0]
    assert "sales.orders" in c11[0]
    assert "OLAF blocks this authoring mismatch" in c11[0]
    assert "do not infer service enforcement" in c11[0]
    assert "fail-open" not in c11[0]


def test_c11_exact_case_reference_ok():
    # Same schema, but the RLS predicate is spelled exactly like the Delta column -- no C11
    # error (the whole point of the rule is the CASE, not the column's existence).
    errors = generate_errors(
        [
            make_row(
                role_name="R",
                include_tables="sales.orders",
                rls_condition="RecordTypeId = 'X'",
                include_group_names="sg-analysts",
            )
        ],
        canon=canon_with_order_columns("RecordTypeId"),
    )
    assert not any("rule C11" in e for e in errors), errors


def test_c11_repeated_exact_case_reference_ok():
    # The same null-safety idiom, but BOTH occurrences spelled exactly like the Delta column
    # -- no C11 error. Proves the full-occurrence scan still dedups identically-cased repeats
    # and fires only on a genuine case difference (guards a mutation that flags any repeat).
    errors = generate_errors(
        [
            make_row(
                role_name="R",
                include_tables="sales.orders",
                rls_condition="RecordTypeId NOT IN ('x') OR RecordTypeId IS NULL",
                include_group_names="sg-analysts",
            )
        ],
        canon=canon_with_order_columns("RecordTypeId"),
    )
    assert not any("rule C11" in e for e in errors), errors


def test_c11_cls_include_columns_case_mismatch_blocks():
    # C11 also blocks an incorrect-case include_columns reference. This is an OLAF authoring
    # validation; the result does not characterize service-side behavior.
    errors = generate_errors(
        [
            make_row(
                role_name="R",
                include_tables="sales.orders",
                include_columns="order_id;recordtypeid",
                include_group_names="sg-analysts",
            )
        ],
        canon=canon_with_order_columns("RecordTypeId"),
    )
    c11 = [e for e in errors if "rule C11" in e]
    assert len(c11) == 1, c11
    assert "recordtypeid" in c11[0]
    assert "RecordTypeId" in c11[0]
    assert "CLS column" in c11[0]
    assert "fail-open" not in c11[0]
    assert "silently" not in c11[0]


def test_c11_rls_and_cls_same_column_rls_message_wins():
    # A wrong-case column referenced by both RLS and CLS collapses to one C11 error using the
    # RLS-specific authoring guidance.
    errors = generate_errors(
        [
            make_row(
                role_name="R",
                include_tables="sales.orders",
                rls_condition="recordtypeid = 'X'",
                include_columns="order_id;recordtypeid",
                include_group_names="sg-analysts",
            )
        ],
        canon=canon_with_order_columns("RecordTypeId"),
    )
    c11 = [e for e in errors if "rule C11" in e]
    assert len(c11) == 1, c11
    assert "RLS column" in c11[0]
    assert "OLAF blocks this authoring mismatch" in c11[0]
    assert "fail-open" not in c11[0]


def test_c11_keyword_named_rls_column_case_mismatch_blocks():
    # T1 SECURITY REGRESSION. Delta column `Between`, predicate spelled `between`. Before T1
    # the column lexer dropped the keyword-named bareword, `referenced` came back empty, and
    # the `if referenced and eff_tables:` block -- BOTH the existence check and C11 -- never
    # ran. The lexer must keep the identifier visible so OLAF can block the authoring error.
    errors = generate_errors(
        [
            make_row(
                role_name="R",
                include_tables="sales.orders",
                rls_condition="between = 'x'",
                include_group_names="sg-analysts",
            )
        ],
        canon=canon_with_order_columns("Between"),
    )
    c11 = [e for e in errors if "rule C11" in e]
    assert len(c11) == 1, c11
    assert "RLS column 'between'" in c11[0]
    assert "Between" in c11[0]
    assert "sales.orders" in c11[0]
    assert "OLAF blocks this authoring mismatch" in c11[0]
    assert "fail-open" not in c11[0]


def test_e8_keyword_named_rls_column_missing_blocks():
    # The same T1 unblocking, seen by the column-existence check: `Between` is not a column
    # of sales.orders in the default CANON, so a predicate naming it must be reported missing
    # rather than silently reading as "this predicate references no column at all".
    errors = generate_errors(
        [
            make_row(
                role_name="R",
                include_tables="sales.orders",
                rls_condition="Between = 'x'",
                include_group_names="sg-analysts",
            )
        ]
    )
    e8 = [e for e in errors if "column 'Between'" in e and "missing in: sales.orders" in e]
    # Count-pinned, like the sibling C11 test above: a bare `any(...)` would hide a SECOND
    # spurious error raised on the same predicate by the same lexer change.
    assert len(e8) == 1, errors


def test_column_check_skips_when_column_metadata_unknown():
    # A table absent from the canonical columns map (metadata unavailable) skips BOTH the
    # missing-column check and C11's case-match check rather than false-flagging -- both
    # need column metadata to reason about columns at all.
    canon_nocols = {"tables": {"sales.orders": "sales.orders"}, "folders": {}, "columns": {}}
    errors = generate_errors(
        [
            make_row(
                role_name="R",
                include_tables="sales.orders",
                rls_condition="region = 'TH'",
                include_group_names="sg-analysts",
            )
        ],
        canon=canon_nocols,
    )
    assert errors == []


# ---------------------------------------------------------------------------------------------
# The RLS lexers — T1's keyword-in-operand-position guard and the shared literal stripper
# ---------------------------------------------------------------------------------------------
# T1: a bareword matching an UNSUPPORTED SQL keyword but sitting in OPERAND position
# (`Between = 'x'`) is a COLUMN, and the column lexer must emit it -- otherwise `referenced` comes
# back empty and the whole `if referenced and eff_tables:` block (the column-existence check AND
# rule C11) never runs for that predicate. The four tests below are the four outcomes of the guard.


def test_unsupported_keyword_in_operand_position_is_a_column():
    assert RLS.referenced_columns_all("Between = 'x'") == ["Between"]


def test_unsupported_keyword_in_operator_position_is_not_a_column():
    assert RLS.referenced_columns_all("qty BETWEEN 1 AND 2") == ["qty"]
    # ... and C9 still rejects it as an unsupported operator (own lexer, unaffected)
    assert "BETWEEN" in RLS.unsupported_tokens("qty BETWEEN 1 AND 2")
    assert "LIKE" in RLS.unsupported_tokens("region LIKE 'th%'")


def test_supported_alphabetic_keywords_stay_non_columns():
    # NOT / IN / OR / IS / NULL are in RLS_KEYWORDS but NOT in RLS_UNSUPPORTED_KEYWORDS,
    # so the guard cannot readmit them -- gating on RLS_KEYWORDS instead would report
    # `NOT` as a missing column on the happy path.
    assert RLS.referenced_columns_all("region NOT IN ('north') OR region IS NULL") == [
        "region",
        "region",
    ]


def test_plain_non_keyword_barewords_are_unchanged():
    assert RLS.referenced_columns_all("1=0 AND 1=1") == []
    assert RLS.referenced_columns_all("UPPER(region) = 'TH'") == ["region"]


@pytest.mark.parametrize(
    ("keyword", "follow"),
    list(
        itertools.product(
            sorted(RLS_UNSUPPORTED_KEYWORDS),
            (
                "= 'x'",
                "<> 'x'",
                "!= 'x'",
                "<= 1",
                ">= 1",
                "< 1",
                "> 1",
                "IN ('x')",
                "IS NULL",
                "'x'",
                "1 AND 2",
                "",
            ),
        )
    ),
)
def test_keyword_lexers_agree_on_operand_position(keyword, follow):
    # DRIFT GUARD. RLS.referenced_columns_all and RLS.unsupported_tokens are the two directions
    # of ONE decision, which is why they share a single compiled follow-set
    # (RLS_OPERAND_FOLLOW_RE): a keyword-named bareword is readmitted as a COLUMN by the first
    # exactly when the second declines to flag it as an unsupported OPERATOR. Nothing else in
    # this suite pins the two against each other on the SAME input, so a widened copy on one
    # side only would pass everywhere else -- and in the "not flagged, still not a column"
    # direction that recreates T1's validation gap: C9 stays silent while the column lexer
    # drops the column, so the column-existence check and rule C11 never run on it.
    # Function-call position ("(" next) is deliberately out of scope: the follow-set never sees
    # it -- both functions branch on "(" first, and C9 flags "NAME(" rather than "NAME".
    predicate = f"{keyword} {follow}".strip()
    is_column = keyword in RLS.referenced_columns_all(predicate)
    is_flagged = keyword in RLS.unsupported_tokens(predicate)
    assert is_column != is_flagged, (
        f"lexers disagree on {predicate!r}: column={is_column} flagged={is_flagged}"
    )


# DRIFT PIN. `re.sub(r"'[^']*'", " ", str(condition))` used to be written out four times,
# byte-identical, in referenced_columns_all / names_any_identifier / unsupported_tokens /
# connective_count. That expression IS the definition of "outside a string literal" -- the reading
# rules C9, C11 and C13 must share -- so four copies with nothing forcing agreement is a latent
# split. It now lives in RLS._strip_literals; the tests below pin the four against ONE
# literal-bearing input so a divergent copy cannot come back.


def test_strip_literals_content_is_invisible_to_all_four_lexers():
    # Every bareword here sits INSIDE the quoted literal, so all four must read the predicate
    # as naming nothing, flagging nothing and connecting nothing. Change the literal pattern in
    # any ONE lexer (e.g. ' -> ") and that lexer starts seeing `region`/`AND`/`LIKE` and this
    # test fails on exactly that lexer.
    inside = "1 = 'region AND type LIKE x'"
    assert RLS.referenced_columns_all(inside) == []
    assert not RLS.names_any_identifier(inside)
    assert RLS.unsupported_tokens(inside) == []
    assert RLS.connective_count(inside) == 0


def test_strip_literals_non_literal_content_stays_visible_to_all_four_lexers():
    # ... and the same four still see everything OUTSIDE the literal, so the assertions above
    # are a stripping check and not a "these lexers see nothing" tautology. TWO literals, not
    # one: with a single literal `'[^']*'` and `'.*'` are indistinguishable, so a lexer drifting
    # to the greedy pattern would pass. With two, greedy swallows the ` AND type = ` between
    # them and `type`/one connective disappear -- caught here.
    outside = "region = 'AND x' AND type = 'OR y' AND qty > 1"
    assert RLS.referenced_columns_all(outside) == ["region", "type", "qty"]
    assert RLS.names_any_identifier(outside)
    assert RLS.unsupported_tokens(outside) == []
    assert RLS.connective_count(outside) == 2


def test_strip_literals_each_guard_returns_its_own_empty_value():
    # The four guards stay at their call sites, so pin each one's own empty value here. They
    # cannot be hoisted INTO RLS._strip_literals as they stand: the four callers return four
    # DIFFERENT empty values and only the caller knows which. Folding a strip-neutral
    # `if not condition: return ""` into the helper is a DIFFERENT proposal, and it works --
    # behaviour-preserving, and it makes these four guards redundant rather than merely
    # survivable (measured out-of-tree, fold in + all four guards deleted: 457/457 and 0
    # differences over 60 comparisons). The guards are kept as a readability judgement, not a
    # correctness requirement: each function states its own empty-value contract at its own top
    # instead of letting it emerge from "" flowing through the regexes.
    # Honest caveat, corrected after an earlier over-claim: only the first two assertions BITE today. Deleting
    # referenced_columns_all's guard with nothing in its place fails 16 tests and deleting
    # names_any_identifier's fails 2; THIS test is in both sets. The other two are weaker, but
    # they are NOT equally weak, and the earlier wording ("behaviourally inert") overstated it:
    #   * connective_count's guard is genuinely inert (measured: 0 diffs over 60 comparisons).
    #   * unsupported_tokens' guard is TEST-inert, not behaviour-inert. Remove it and a falsy
    #     CONTAINER makes C9 report a spurious token -- unsupported_tokens([]) comes back ['[]']
    #     and unsupported_tokens({}) comes back ['{}'], because str([]) is "[]".
    # Reachability, ranked honestly: for every falsy value a Spark or spreadsheet read can
    # realistically deliver -- None, "", 0, 0.0, False -- both guards ARE inert (all five
    # verified). So this is precision, not a defect. Pinned anyway: the invariant is load-bearing
    # for the next editor, and it starts enforcing the moment either function grows a case where
    # "None" is not inert.
    assert RLS.referenced_columns_all(None) == []
    assert not RLS.names_any_identifier(None)
    assert RLS.unsupported_tokens(None) == []
    assert RLS.connective_count(None) == 0
    # the container case the corrected wording rests on -- WITH the guard, both stay empty
    assert RLS.unsupported_tokens([]) == []
    assert RLS.connective_count({}) == 0


def test_strip_literals_str_coercion_and_the_unhandled_escape():
    # CHARACTERIZATION of two docstring claims that nothing pinned. (a) the str() coercion in
    # RLS._strip_literals: a non-str condition (a spreadsheet cell read as a number) must not
    # raise -- drop the str() and re.sub raises TypeError on this line. (b) the SQL '' escape is
    # NOT handled: `'it''s'` is read as TWO adjacent literals rather than one. Recorded, not
    # enforced: swapping the pattern for an escape-aware `'(?:[^']|'')*'` leaves the suite green
    # (verified), because both readings consume the SAME span and differ only in how many blanks
    # they leave behind, which no lexer can see. These two lines therefore pin what the reading
    # produces, so an escape "fix" that changes the RESULT -- leaving a stray `s` visible as a
    # column -- fails here instead of silently widening C9/C11/C13.
    assert RLS.referenced_columns_all(123) == []
    escaped = "name = 'it''s' AND qty > 1"
    assert RLS.referenced_columns_all(escaped) == ["name", "qty"]
    assert RLS.connective_count(escaped) == 1


# ---------------------------------------------------------------------------------------------
# Rules C4-C13 — cross-role exposure, platform ceilings, and the RLS syntax subset
# ---------------------------------------------------------------------------------------------


def test_rls_function_call_is_not_a_column():
    # LOWER(...) is a function, not a column. Two guarantees asserted together:
    #  (1) the column-existence lexer reads it as a call (only arg `region` is validated), so
    #      there is NO "column 'LOWER' missing" error; and
    #  (2) C9 POSITIVELY fires — a function call is unOneLake-supported RLS syntax that fails at
    #      query time, so LOWER(region)='th' must raise a rule C9 error.
    # Reconciliation (T1 review): this test predates C9 and was weakened from
    # `errors == []` to the missing-column-only check once the function call began
    # (correctly) tripping C9; the C9 positive is now asserted explicitly, not left silent.
    grants, errors, _warnings, _summary = Generate.rows(
        [
            make_row(
                role_name="R",
                include_tables="sales.orders",
                rls_condition="LOWER(region) = 'th'",
                include_group_names="sg-analysts",
            )
        ],
        CANON,
    )
    assert not any("LOWER" in e and "missing" in e for e in errors)  # not a missing col
    assert any("rule C9" in e for e in errors), errors  # but IS an unsupported call
    assert grants[0]["rls_condition"] == "LOWER(region) = 'th'"


@pytest.mark.parametrize(
    "predicate", ["Between = 'x'", "Case = 'x'", "End = 'x'", "As = 'x'", "End IS NULL"]
)
def test_c9_keyword_named_columns_in_operand_position_not_flagged(predicate):
    # Regression (T1 review): a bareword that happens to match an unsupported SQL keyword is
    # an OPERATOR only in operator position. As the LEFT OPERAND of a comparison
    # (`Between = 'x'`) or an IS test (`End IS NULL`) it is a COLUMN NAME, so C9 must NOT flag
    # it — otherwise a valid config with a keyword-named column is wrongly blocked. The keyword
    # columns are added to the catalog in the exact predicate case, so since T1 — which made the
    # column lexer emit these barewords at all — column-existence (E8) and case-match (C11)
    # genuinely RUN over them and genuinely pass. (Before T1 they did not run at all: the lexer
    # returned no references, so `errors == []` below held for the wrong reason.) C9 is therefore
    # still the only rule that could block here.
    errors = generate_errors(
        [
            make_row(
                role_name="R",
                include_tables="sales.orders",
                rls_condition=predicate,
                include_group_names="sg-analysts",
            )
        ],
        canon=canon_with_order_columns("Between", "Case", "End", "As"),
    )
    assert not any("rule C9" in e for e in errors), errors
    assert errors == [], errors  # keyword-named column is otherwise fully valid


def test_c4_overexposure_warns_without_blocking():
    # one member reaches one table through two roles -> union over-exposure warning, not an error
    rows = [
        make_row(
            role_name="RoleOne", include_tables="sales.orders", include_group_names="sg-analysts"
        ),
        make_row(
            role_name="RoleTwo", include_tables="sales.orders", include_group_names="sg-analysts"
        ),
    ]
    errors, warnings = generate_warnings(rows)
    assert errors == []
    assert any("rule C4" in w and "sg-analysts" in w for w in warnings)


def test_c5_cross_role_rls_cls_mix_blocks():
    # A member in an RLS-bearing role AND a DIFFERENT CLS-bearing role breaks OneLake queries;
    # generate must error (rule C5). The member+both-role assertions also prove non-vacuity:
    # the rule fires precisely on the offending member and names the RLS and CLS roles.
    rows = [
        make_row(
            role_name="SalesAnalysts",
            include_tables="sales.orders",
            rls_condition="region = 'th'",
            include_user_names="user1@example.com",
        ),
        make_row(
            role_name="HRViewers",
            include_tables="hr.payroll",
            exclude_columns="salary",
            include_user_names="user1@example.com",
        ),
    ]
    c5 = [e for e in generate_errors(rows) if "rule C5" in e]
    assert len(c5) == 1
    assert "user1@example.com" in c5[0]
    assert "SalesAnalysts" in c5[0]
    assert "HRViewers" in c5[0]


def test_c5_single_role_rls_and_cls_ok():
    # RLS and CLS combined in ONE role is doc-supported -> no C5 error.
    rows = [
        make_row(
            role_name="Combined",
            include_tables="hr.payroll",
            rls_condition="department = 'sales'",
            exclude_columns="salary",
            include_user_names="user1@example.com",
        )
    ]
    assert not any("rule C5" in e for e in generate_errors(rows))


def test_c5_two_rls_roles_no_cls_ok():
    # A member spanning two roles is fine when neither role carries CLS -> no C5 error.
    rows = [
        make_row(
            role_name="RlsOne",
            include_tables="sales.orders",
            rls_condition="region = 'th'",
            include_user_names="user1@example.com",
        ),
        make_row(
            role_name="RlsTwo",
            include_tables="sales.leads",
            rls_condition="region = 'us'",
            include_user_names="user1@example.com",
        ),
    ]
    assert not any("rule C5" in e for e in generate_errors(rows))


def test_c5_distinct_members_no_cross_mix_ok():
    # An RLS role and a CLS role coexist, but NO single member is in both -> no C5 error.
    rows = [
        make_row(
            role_name="RlsRole",
            include_tables="sales.orders",
            rls_condition="region = 'th'",
            include_user_names="user1@example.com",
        ),
        make_row(
            role_name="ClsRole",
            include_tables="hr.payroll",
            exclude_columns="salary",
            include_user_names="user2@example.com",
        ),
    ]
    assert not any("rule C5" in e for e in generate_errors(rows))


def cross_role_alias_rows():
    """An RLS role naming a user by UPN and a CLS role naming the same user by a mail alias —
    two different config spellings of ONE principal."""
    return [
        make_row(
            role_name="SalesAnalysts",
            include_tables="sales.orders",
            rls_condition="region = 'th'",
            include_user_names="Alice@Example.com",
        ),
        make_row(
            role_name="HRViewers",
            include_tables="hr.payroll",
            exclude_columns="salary",
            include_user_names="alice.smith@example.com",
        ),
    ]


def test_c5_same_objectid_alias_cross_role_blocks():
    # Two DIFFERENT config spellings of ONE user (a UPN + its mail alias) resolve to the SAME
    # objectId in the member table: the UPN sits in an RLS role, the alias in a different CLS
    # role. The value-level C5 keys on the raw spelling and misses the alias, but OneLake still
    # fails the user's queries — the resolved-id pass must fire exactly one C5 error naming BOTH
    # spellings and the shared objectId.
    rows = cross_role_alias_rows()
    grants, gen_errors, _w, _s = Generate.rows(rows, CANON)
    # value-level pass groups on the raw spelling -> two different keys -> no C5 yet
    assert not any("rule C5" in e for e in gen_errors), gen_errors
    oid = "b0000000-0000-0000-0000-0000000000aa"
    cache = {("User", "alice@example.com"): oid, ("User", "alice.smith@example.com"): oid}
    res_errors = Member.resolve_ids(grants, cache, rows)
    c5 = [e for e in gen_errors + res_errors if "rule C5" in e]
    assert len(c5) == 1, c5
    assert "Alice@Example.com" in c5[0]
    assert "alice.smith@example.com" in c5[0]
    assert oid in c5[0]
    assert "SalesAnalysts" in c5[0]
    assert "HRViewers" in c5[0]


def test_c5_same_objectid_in_two_letter_cases_cross_role_blocks():
    # SECURITY: the resolved-id pass groups roles by objectId, and an objectId is
    # case-INSENSITIVE hex — so ONE principal whose two config spellings resolve to the same id
    # written in two letter cases must still group as one. Left case-sensitive it becomes two
    # ids, each reaching a single role, and rule C5 is bypassed exactly the way the raw-case
    # member cache bypasses the duplicate-id guard.
    rows = cross_role_alias_rows()
    grants, gen_errors, _w, _s = Generate.rows(rows, CANON)
    assert not any("rule C5" in e for e in gen_errors), gen_errors
    oid_upper = "B0000000-0000-0000-0000-0000000000AA"
    oid_lower = "b0000000-0000-0000-0000-0000000000aa"
    cache = {
        ("User", "alice@example.com"): oid_upper,
        ("User", "alice.smith@example.com"): oid_lower,
    }
    res_errors = Member.resolve_ids(grants, cache, rows)
    c5 = [e for e in res_errors if "rule C5" in e]
    assert len(c5) == 1, c5
    assert "Alice@Example.com" in c5[0]
    assert "alice.smith@example.com" in c5[0]
    assert oid_upper in c5[0]  # the id is ECHOED as written, never lowered
    assert "SalesAnalysts" in c5[0]
    assert "HRViewers" in c5[0]


def test_c5_resolved_id_pass_dedups_same_spelling():
    # The SAME spelling in an RLS role and a different CLS role is already caught by the
    # value-level pass; the resolved-id pass must NOT emit a duplicate for it. Also locks rule
    # C5's original-casing fidelity: the message echoes the author's spelling, not the lowercased
    # grouping key.
    rows = [
        make_row(
            role_name="RlsRole",
            include_tables="sales.orders",
            rls_condition="region = 'th'",
            include_user_names="User1@Example.com",
        ),
        make_row(
            role_name="ClsRole",
            include_tables="hr.payroll",
            exclude_columns="salary",
            include_user_names="User1@Example.com",
        ),
    ]
    grants, gen_errors, _w, _s = Generate.rows(rows, CANON)
    value_c5 = [e for e in gen_errors if "rule C5" in e]
    assert len(value_c5) == 1, value_c5
    assert "User1@Example.com" in value_c5[0]  # original casing, not "user1@example.com"
    assert "'user1@example.com'" not in value_c5[0]
    res_errors = Member.resolve_ids(
        grants, {("User", "user1@example.com"): "b0000000-0000-0000-0000-0000000000bb"}, rows
    )
    assert [e for e in res_errors if "rule C5" in e] == []


def test_c5_resolved_id_pass_single_role_both_ok():
    # One role carrying BOTH RLS and CLS is doc-supported; after the member name resolves to an
    # objectId the resolved-id pass must not flag it (the id reaches only that single role).
    rows = [
        make_row(
            role_name="Combined",
            include_tables="hr.payroll",
            rls_condition="department = 'sales'",
            exclude_columns="salary",
            include_user_names="user1@example.com",
        )
    ]
    grants, gen_errors, _w, _s = Generate.rows(rows, CANON)
    assert not any("rule C5" in e for e in gen_errors), gen_errors
    res_errors = Member.resolve_ids(
        grants, {("User", "user1@example.com"): "b0000000-0000-0000-0000-0000000000cc"}, rows
    )
    assert not any("rule C5" in e for e in res_errors), res_errors


def test_c4_reports_original_member_casing():
    # C4 groups members case-insensitively but its warning must echo the author's original
    # spelling, not the lowercased grouping key (rule C4/C5 message fidelity).
    rows = [
        make_row(
            role_name="RoleOne", include_tables="sales.orders", include_group_names="SG-Analysts"
        ),
        make_row(
            role_name="RoleTwo", include_tables="sales.orders", include_group_names="SG-Analysts"
        ),
    ]
    errors, warnings = generate_warnings(rows)
    assert errors == []
    c4 = [w for w in warnings if "rule C4" in w]
    assert len(c4) == 1, c4
    assert "SG-Analysts" in c4[0]
    assert "'sg-analysts'" not in c4[0]


def _member_scope_keys(warnings):
    """The (member, scope) pair each C4/C8 message was emitted for.

    Assert on THIS, not on the message string. The sort key is the LOWERCASED (member, scope) while
    the message echoes the author's original casing, so a string-sorted assertion is a proxy that
    disagrees with the real contract the moment a fixture uses a capital letter — and it would then
    read as a determinism regression when the emission is perfectly deterministic.
    """
    return [(w.split("'")[1].lower(), w.split("'")[3]) for w in warnings]


# Five members x three shared tables. The fixture size is load-bearing, not decoration: the emitted
# order is the PRODUCT of two set iterations (members x scopes), not a free permutation of the
# warning list, so a fixture of M members and S scopes can only reach M!*S! orders. At 3x2 that is
# 12, and an unsorted-code guard passes by luck ~1 run in 12; at 5x3 it is 720. Measured on unsorted
# code over 300 hash seeds: 5x3 false-passes 1/300 (238 distinct orders), 3x2 false-passes 25/300
# (exactly 12 distinct orders, hitting the predicted ceiling).
_ORDER_MEMBERS = "sg-alpha;sg-beta;sg-gamma;sg-delta;sg-epsilon"
_ORDER_TABLES = "sales.orders;sales.leads;sales.returns"


def test_c4_warning_order_is_deterministic():
    """The C4 block was emitted in SET ITERATION order, so the same code against the same
    config produced the same warnings in a different ORDER on every run — CPython randomizes string
    hashing per process, and `member_scopes`' insertion order followed set iteration.

    Not a correctness bug — the multiset and every message were right. But it poisons diff-based
    review: a real-data audit reported 4 of 11 config workbooks as "differing" purely from this
    and had to pin PYTHONHASHSEED to get a clean read. That is the false signal that trains a
    reviewer to skim a differential.
    """
    rows = [
        make_row(role_name=role, include_tables=_ORDER_TABLES, include_group_names=_ORDER_MEMBERS)
        for role in ("RoleOne", "RoleTwo")
    ]

    errors, warnings = generate_warnings(rows)

    assert errors == []
    c4 = [w for w in warnings if "rule C4" in w]
    assert len(c4) == 15, c4  # 5 members x 3 shared tables
    keys = _member_scope_keys(c4)
    assert keys == sorted(keys), "C4 warnings are not emitted in (member, scope) order"


def test_c8_warning_order_is_deterministic():
    """C8 reuses `member_scopes` and already sorted it — this holds that sort in place, since the two
    loops sit next to each other and now look interchangeable.

    It uses the SAME 5x3 fixture as C4 deliberately. Review measured the first version of this test,
    at 3 members x 2 tables, silently passing on un-sorted code 25 times in 300 seeds — it could only
    reach 12 orders, so it was a coin flip rather than a guard, and `PYTHONHASHSEED=1` happens to be
    one of the seeds it misses.
    """
    rows = [
        make_row(
            role_name="Restricted",
            include_tables=_ORDER_TABLES,
            rls_condition="region = 'TH'",
            include_group_names=_ORDER_MEMBERS,
        ),
        make_row(
            role_name="Open", include_tables=_ORDER_TABLES, include_group_names=_ORDER_MEMBERS
        ),
    ]

    errors, warnings = generate_warnings(rows)

    assert errors == []
    c8 = [w for w in warnings if "rule C8" in w]
    assert len(c8) == 15, c8  # 5 members x 3 shared tables
    keys = _member_scope_keys(c8)
    assert keys == sorted(keys), "C8 warnings are not emitted in (member, scope) order"


def test_b3_readwrite_cannot_carry_rls():
    errors = generate_errors(
        [
            make_row(
                role_name="R",
                include_tables="sales.orders",
                permission="ReadWrite",
                rls_condition="region = 'th'",
                include_group_names="sg-analysts",
            )
        ]
    )
    assert any("rule B3" in e for e in errors)


def test_b3_readwrite_cannot_carry_cls():
    errors = generate_errors(
        [
            make_row(
                role_name="R",
                include_tables="hr.payroll",
                permission="ReadWrite",
                exclude_columns="salary",
                include_group_names="sg-analysts",
            )
        ]
    )
    assert any("rule B3" in e for e in errors)


def test_exact_duplicate_row_skipped():
    row = make_row(role_name="R", include_tables="sales.orders", include_group_names="sg-analysts")
    grants, errors, warnings, _ = Generate.rows([row, dict(row)], CANON)
    assert errors == []
    assert any("duplicate" in w for w in warnings)
    assert len(grants) == 1


def test_member_wildcard_rejected():
    errors = generate_errors(
        [make_row(role_name="R", include_tables="sales.orders", include_group_names="sg-*")]
    )
    assert any("wildcards not allowed in member values" in e for e in errors)


# ---------------------------------------------------------------------------------------------
# Member excludes: an exclude value does NOT have to match anything in its own include column.
# It only has to be a principal the member table knows (owner call, 2026-08-12) -- a defensive or
# forward-looking exclusion is a normal authoring pattern, so a dead one is accepted in silence.
# The gate that DOES apply is the No-Graph member gate reaching declared names (below).
# ---------------------------------------------------------------------------------------------


def test_dead_member_exclusion_of_a_known_principal_is_accepted():
    # 'sg-contractors' is a real principal in the member table but was never included on this
    # row, so the exclusion removes nothing. That is allowed on purpose: excludes are validated
    # against onelake_security_member, not against the row's own include column.
    grants, errors, warnings, _summary = Generate.rows(
        [
            make_row(
                role_name="R",
                include_tables="sales.orders",
                include_group_names="sg-analysts",
                exclude_group_names="sg-contractors",
            )
        ],
        CANON,
    )
    assert errors == [], errors
    assert not any("names nothing" in w for w in warnings), warnings
    assert grants[0]["member_group_names"] == "sg-analysts"


def test_c6_role_name_over_124_chars_blocks():
    # A 125-char role_name hard-fails the SQL analytics-endpoint security sync (no workaround).
    # C6 is a per-role platform-consequence rule; mutation-strong on the actual char count.
    # (B1 also fires on length, but assert C6 specifically here.)
    long_name = "R" + "a" * 124  # 125 chars, alphanumeric, letter-first
    c6 = [
        e
        for e in generate_errors(
            [
                make_row(
                    role_name=long_name,
                    include_tables="sales.orders",
                    include_group_names="sg-analysts",
                )
            ]
        )
        if "rule C6" in e
    ]
    assert len(c6) == 1, c6
    assert "125" in c6[0]


def test_c6_role_name_at_124_chars_ok():
    # Exactly 124 chars is the ceiling, not over it -> no C6.
    name_124 = "R" + "a" * 123  # 124 chars
    errors = generate_errors(
        [
            make_row(
                role_name=name_124,
                include_tables="sales.orders",
                include_group_names="sg-analysts",
            )
        ]
    )
    assert not any("rule C6" in e for e in errors)


def test_c12_role_name_underscore_blocks():
    # An underscore breaks Fabric's "Create a role" naming contract (rule C12).
    # (B1 also fires on charset, but assert C12 specifically here.)
    c12 = c12_errors("Sales_Readers")
    assert len(c12) == 1, c12
    assert "Sales_Readers" in c12[0]


def test_c12_role_name_leading_digit_blocks():
    # A leading digit breaks Fabric's "Create a role" naming contract (rule C12).
    c12 = c12_errors("2Sales")
    assert len(c12) == 1, c12
    assert "2Sales" in c12[0]


def test_c12_role_name_space_blocks():
    # A space breaks Fabric's "Create a role" naming contract (rule C12).
    c12 = c12_errors("Sales Team")
    assert len(c12) == 1, c12
    assert "Sales Team" in c12[0]


def test_c12_role_name_alphanumeric_ok():
    # Letter-first, alphanumeric-only role_name never trips C12.
    assert c12_errors("SalesReaders") == []


def test_c7_rls_condition_over_1000_chars_blocks():
    # An rls_condition over 1000 chars exceeds the RLS syntax-rules limit (rule C7). The long
    # string sits inside a quoted literal, so it does not trip C9; mutation-strong on the length.
    long_rls = "region = '" + "x" * 1000 + "'"  # 1011 chars, valid syntax
    c7 = [
        e
        for e in generate_errors(
            [
                make_row(
                    role_name="R",
                    include_tables="sales.orders",
                    rls_condition=long_rls,
                    include_group_names="sg-analysts",
                )
            ]
        )
        if "rule C7" in e
    ]
    assert len(c7) == 1, c7
    assert str(len(long_rls)) in c7[0]


def test_c7_rls_condition_within_1000_chars_ok():
    errors = generate_errors(
        [
            make_row(
                role_name="R",
                include_tables="sales.orders",
                rls_condition="region = 'th'",
                include_group_names="sg-analysts",
            )
        ]
    )
    assert not any("rule C7" in e for e in errors)


def test_c8_most_permissive_role_nullifies_restriction_warns():
    # sg-analysts reaches sales.orders via RestrictedRole (RLS) AND OpenRole (no RLS/CLS): the
    # union grants unfiltered access -> C8 warning (most permissive wins). Warning, not error.
    rows = [
        make_row(
            role_name="RestrictedRole",
            include_tables="sales.orders",
            rls_condition="region = 'th'",
            include_group_names="sg-analysts",
        ),
        make_row(
            role_name="OpenRole", include_tables="sales.orders", include_group_names="sg-analysts"
        ),
    ]
    errors, warnings = generate_warnings(rows)
    assert errors == []
    c8 = [w for w in warnings if "rule C8" in w]
    assert len(c8) == 1, c8
    assert "sg-analysts" in c8[0]
    assert "RestrictedRole" in c8[0]
    assert "OpenRole" in c8[0]
    assert "/Tables/sales/orders" in c8[0]


def test_c8_both_roles_restricted_no_c8():
    # Both roles put RLS on sales.orders -> no reaching role is unrestricted -> nothing is
    # nullified -> no C8. C4 still warns on the multi-role reach; assert C8 specifically absent.
    rows = [
        make_row(
            role_name="RestrictedOne",
            include_tables="sales.orders",
            rls_condition="region = 'th'",
            include_group_names="sg-analysts",
        ),
        make_row(
            role_name="RestrictedTwo",
            include_tables="sales.orders",
            rls_condition="region = 'us'",
            include_group_names="sg-analysts",
        ),
    ]
    errors, warnings = generate_warnings(rows)
    assert errors == []
    assert not any("rule C8" in w for w in warnings), warnings
    assert any("rule C4" in w for w in warnings)


def test_c9_unsupported_operator_blocks():
    # A LIKE predicate uses an operator outside the OneLake-supported RLS subset -> hard error
    # (unsupported syntax fails at query time). Mutation-strong: the C9 error names the token.
    c9 = [
        e
        for e in generate_errors(
            [
                make_row(
                    role_name="R",
                    include_tables="sales.orders",
                    rls_condition="region LIKE 'th%'",
                    include_group_names="sg-analysts",
                )
            ]
        )
        if "rule C9" in e
    ]
    assert len(c9) == 1, c9
    assert "LIKE" in c9[0]


@pytest.mark.parametrize("constant", ["1=0", "1 = 1"])
def test_c13_rls_condition_referencing_no_column_blocks(constant):
    # OLAF blocks a predicate that names no column of the selected table before submission.
    # This is validation/model guidance, not a claim about a service response.
    c13 = [
        e
        for e in generate_errors(
            [
                make_row(
                    role_name="R",
                    include_tables="sales.orders",
                    rls_condition=constant,
                    include_group_names="sg-analysts",
                )
            ]
        )
        if "rule C13" in e
    ]
    assert len(c13) == 1, c13
    assert constant in c13[0]  # names the offending condition


def test_c13_rls_condition_referencing_a_column_ok():
    # The neighbouring rules already cover the column's existence and case; C13 only asks
    # that the predicate name one at all, so a normal predicate must stay clean.
    errors = generate_errors(
        [
            make_row(
                role_name="R",
                include_tables="sales.orders",
                rls_condition="region = 'TH'",
                include_group_names="sg-analysts",
            )
        ]
    )
    assert [e for e in errors if "rule C13" in e] == []


@pytest.mark.parametrize("blank", ["", None], ids=["empty", "None"])
def test_c13_names_any_identifier_blank_is_no_identifier(blank):
    assert not RLS.names_any_identifier(blank)


def test_c13_bareword_inside_a_string_literal_does_not_count():
    assert not RLS.names_any_identifier("1 = 'region'")


def test_c13_lexers_diverge_only_on_the_supported_keywords():
    # Since T1 referenced_columns agrees with names_any_identifier on an UNSUPPORTED
    # keyword in operand position (`Between = 'x'`). The divergence this pins is the
    # remaining one: a SUPPORTED alphabetic keyword (AND/OR/NOT/IN/IS/NULL/TRUE/FALSE)
    # is still no column to referenced_columns, while C13's weaker lexer counts it --
    # which is exactly why C13 needs its own lexer.
    assert RLS.names_any_identifier("Between = 'x'")
    assert RLS.referenced_columns("Between = 'x'") == ["Between"]
    assert RLS.names_any_identifier("Null = 'x'")
    assert RLS.referenced_columns("Null = 'x'") == []


@pytest.mark.parametrize(
    # Written as \u escapes so this file stays pure ASCII; the runtime values are byte-identical
    # to the literal forms. They have to be non-Latin -- the rule under test is about an identifier
    # carrying no ASCII letter at all, which no English example can express.
    "predicate",
    [
        "\u5730\u533a = '\u5317'",  # CJK
        "\u0420\u0435\u0433\u0438\u043e\u043d = 'X'",  # Cyrillic
        "[\u5730\u533a] = '\u5317'",  # CJK, bracket-quoted
        "\u03a7\u03ce\u03c1\u03b1 = '\u0391'",  # Greek
    ],
)
def test_c13_non_ascii_identifier_known_limitation(predicate):
    # KNOWN LIMITATION, pinned so it is recorded rather than rediscovered.
    # names_any_identifier tests `[A-Za-z_]`, which is ASCII-only, so a FULLY non-Latin column
    # name carries no ASCII letter and C13 reports "references no column" about a predicate
    # that plainly names one. NOT fixed here: the estate has no non-Latin column names, and
    # widening the class would change which configs generate on the one rule whose design point
    # is raising no false errors. If this test ever needs updating, that is a behaviour change
    # and needs its own review.
    assert not RLS.names_any_identifier(predicate)
    # ... and the pipeline does NOT let it through: C9 reads the non-ASCII characters
    # as unsupported operator symbols, so the row is rejected on that ground too.
    assert RLS.unsupported_tokens(predicate)


def test_c13_accented_latin_is_rejected_by_c9_not_c13():
    # The honest scoping. `région` keeps its ASCII letters (it lexes as `r` + `gion`), so
    # C13 sees a bareword and stays quiet -- it is C9 that rejects the accent.
    assert RLS.names_any_identifier("région = 'x'")
    assert RLS.unsupported_tokens("région = 'x'") == ["é"]


def test_c13_ascii_identifier_is_clean_on_both_rules():
    # the control for the two tests above
    assert RLS.names_any_identifier("region = 'x'")
    assert RLS.unsupported_tokens("region = 'x'") == []


def test_c9_supported_operators_ok():
    # A predicate built only from the supported subset (= / IN / AND / OR / IS NULL) -> no C9.
    errors = generate_errors(
        [
            make_row(
                role_name="R",
                include_tables="sales.orders",
                rls_condition="region IN ('th') AND type = 'B2B' OR region IS NULL",
                include_group_names="sg-analysts",
            )
        ]
    )
    assert not any("rule C9" in e for e in errors)


def test_c10_high_complexity_predicate_warns():
    # More AND/OR connectives than the complexity heuristic -> warning (complex roles can fail
    # the security sync). Warning, not error. Mutation-strong: the message names the count.
    clause = " AND ".join("region = 'th'" for _ in range(22))  # 21 AND connectives
    clause = clause + " OR region = 'us' OR region = 'jp'"  # +2 OR -> 23 connectives
    errors, warnings = generate_warnings(
        [
            make_row(
                role_name="R",
                include_tables="sales.orders",
                rls_condition=clause,
                include_group_names="sg-analysts",
            )
        ]
    )
    assert errors == []
    c10 = [w for w in warnings if "rule C10" in w]
    assert len(c10) == 1, c10
    assert "23" in c10[0]


def test_c10_simple_predicate_no_warning():
    _errors, warnings = generate_warnings(
        [
            make_row(
                role_name="R",
                include_tables="sales.orders",
                rls_condition="region = 'th' AND type = 'B2B'",
                include_group_names="sg-analysts",
            )
        ]
    )
    assert not any("rule C10" in w for w in warnings)


def test_c9_c10_helper_edge_cases():
    # Direct helper coverage the rule-level tests don't reach: empty/None inputs, a SYMBOLIC
    # unsupported operator (the keyword path is covered by the LIKE test), and dedup of a
    # repeated unsupported token to a single entry.
    assert RLS.unsupported_tokens("") == []
    assert RLS.unsupported_tokens(None) == []
    assert RLS.connective_count("") == 0
    assert RLS.connective_count(None) == 0
    assert RLS.unsupported_tokens("amount + tax + fee > 10") == ["+"]


def c14_errors(rls_condition):
    """C14 errors from a single row carrying the given predicate."""
    return [
        e
        for e in generate_errors(
            [
                make_row(
                    role_name="R",
                    include_tables="sales.orders",
                    rls_condition=rls_condition,
                    include_group_names="sg-analysts",
                )
            ]
        )
        if "rule C14" in e
    ]


def test_c14_bare_boolean_value_blocks():
    """`= true` is refused at validate, where it used to reach apply.

    Lab-verified 2026-07-27: OneLake answers InvalidRLSPredicate, because an unquoted value is
    read as a COLUMN NAME. `region = Web` already blocked -- `Web` is not a keyword, so it lands
    on the column-existence check as a missing column -- but TRUE/FALSE are in
    RLS_SUPPORTED_OPERATORS, so both lexers skipped them and nothing caught it.
    """
    errors = c14_errors("region = true")
    assert errors, "a bare TRUE in value position must be a C14 error"
    assert "COLUMN NAME" in errors[0] and "'true'" in errors[0]


def test_c14_quoting_it_is_the_fix():
    assert c14_errors("region = 'true'") == []


def test_c14_numbers_are_the_exception():
    """`amount > 0` is accepted live, quoted or not, so C14 must not touch a numeric value."""
    assert c14_errors("amount > 0") == []
    assert c14_errors("amount > '0'") == []


def test_c14_flags_value_position_only():
    # value position -> flagged, in either case spelling, for either keyword
    assert RLS.bare_boolean_values("region = true") == ["true"]
    assert RLS.bare_boolean_values("region<>FALSE") == ["FALSE"]
    # a standalone keyword is the DOCUMENTED use of TRUE/FALSE and must survive: the position
    # guard is the whole difference between it and the broken form
    assert RLS.bare_boolean_values("TRUE") == []
    assert RLS.bare_boolean_values("region = 'x' OR TRUE") == []
    # a quoted literal that merely spells the keyword is a value, not a bareword
    assert RLS.bare_boolean_values("region = 'true'") == []


def test_c14_is_true_is_value_position_too():
    """`IS TRUE` is refused by the platform exactly like `= true`, so C14 must treat it as value
    position. Checked in the Fabric UI: InvalidRLSPredicate, same as the `=` form.

    Nothing else would catch it -- `IS` is a documented supported operator and so is TRUE, so C9
    skips both. `IS NULL` / `IS BLANK` stay valid: NULL and BLANK are not TRUE/FALSE, so they never
    reach the check.
    """
    assert RLS.bare_boolean_values("region IS TRUE") == ["TRUE"]
    assert RLS.bare_boolean_values("region IS NOT FALSE") == ["FALSE"]
    assert RLS.bare_boolean_values("region is true") == ["true"]
    assert c14_errors("region IS TRUE"), "IS TRUE must be a C14 error"
    # the documented IS forms are untouched
    assert RLS.bare_boolean_values("region IS NULL") == []
    assert RLS.bare_boolean_values("region IS BLANK") == []
    assert RLS.bare_boolean_values("region NOT IN ('a') OR region IS NULL") == []


def test_c14_covers_in_lists():
    """An IN list is all values by definition, so every slot counts -- not just the first.

    Checked in the Fabric UI: `Is_Current IN (TRUE)` is refused exactly like `= true`. C9 does not
    catch it either, since IN, TRUE and FALSE are all supported keywords.
    """
    assert RLS.bare_boolean_values("region IN (TRUE)") == ["TRUE"]
    assert RLS.bare_boolean_values("region IN (TRUE, FALSE)") == ["TRUE", "FALSE"]
    assert RLS.bare_boolean_values("region IN ('a', TRUE)") == ["TRUE"]
    assert RLS.bare_boolean_values("region NOT IN (FALSE)") == ["FALSE"]
    assert c14_errors("region IN (TRUE)"), "a bare TRUE inside IN must be a C14 error"
    # a list of quoted literals is untouched
    assert RLS.bare_boolean_values("region IN ('a','b')") == []


def test_c14_helper_edge_cases():
    # empty/None guards, both keywords in one predicate, and dedup of a repeat to one entry
    assert RLS.bare_boolean_values("") == []
    assert RLS.bare_boolean_values(None) == []
    assert RLS.bare_boolean_values("region = true AND type = false") == ["true", "false"]
    assert RLS.bare_boolean_values("region = true AND type = true") == ["true"]


# ---------------------------------------------------------------------------------------------
# rule B4 — the permission column is the DAR Action enum (external security audit 2026-08-16,
# issue #15). Normalization through PERMISSIONS also closes rule B3's case-sensitivity bypass.
# ---------------------------------------------------------------------------------------------


def test_an_unsupported_permission_is_rejected_naming_row_and_value():
    errors = generate_errors(
        [
            make_row(
                role_name="R",
                include_tables="sales.orders",
                include_group_names="sg-analysts",
                permission="Admin",
            )
        ]
    )
    assert any("permission 'Admin'" in e and "rule B4" in e for e in errors)


def test_a_differently_cased_permission_normalizes_to_the_canonical_token():
    from _fakes import generate_ok

    grants, _warnings, _summary = generate_ok(
        [
            make_row(
                role_name="R",
                include_tables="sales.orders",
                include_group_names="sg-analysts",
                permission="readwrite",
            )
        ]
    )
    assert {a["permission"] for a in grants} == {"ReadWrite"}  # canonical, not as-typed


def test_rule_b3_fires_for_a_lowercased_readwrite_with_rls():
    # the B3 regression the audit called out: 'readwrite' + RLS used to evade the exact
    # case-sensitive comparison and die at the platform instead of at generate.
    errors = generate_errors(
        [
            make_row(
                role_name="R",
                include_tables="sales.orders",
                include_group_names="sg-analysts",
                permission="readwrite",
                rls_condition="region = 'north'",
            )
        ]
    )
    assert any("rule B3" in e for e in errors)
    assert not any("rule B4" in e for e in errors)  # the value itself is valid


def test_a_missing_permission_still_defaults_to_read():
    from _fakes import generate_ok

    grants, _warnings, _summary = generate_ok(
        [
            make_row(
                role_name="R",
                include_tables="sales.orders",
                include_group_names="sg-analysts",
                permission=None,
            )
        ]
    )
    assert {a["permission"] for a in grants} == {"Read"}
