"""White-box pure-logic gaps, autotrim at the read seams, and strict boolean-parameter typing,
as pytest functions.

Ported from `olaf_test_unit.ipynb` classes `LibPureLogicGaps`, `AutotrimTests` and
`BoolParamTyping`.
"""

import json

import pytest

from _olaf_runtime import (
    CONFIG_AUTHOR_COLUMNS,
    DAR,
    MAPPING_COLUMNS,
    RLS,
    Catalog,
    Generate,
    Member,
    Parse,
    ScopePath,
    ValidationError,
    ZeroMatchError,
)
from _fakes import (
    BASE,
    CANON,
    CONFIG_TABLE,
    MEMBER_TABLE,
    MISSING_NAME,
    FakeFabricClient,
    build_spark,
    directory_cache,
    generate_errors,
    generate_warnings,
    make_dep,
    make_row,
    member_cache_row,
    resolve_grants,
)


# ---------------------------------------------------------------------------------------------
# LibPureLogicGaps — normalization / parsing rejections
# ---------------------------------------------------------------------------------------------


def test_folder_to_path_rejects_non_files():
    with pytest.raises(ValidationError) as excinfo:
        ScopePath.folder("randomdir/x")
    assert "under /Files" in str(excinfo.value)


@pytest.mark.parametrize("lister", [None, 123], ids=["None", "scalar"])
def test_folder_children_non_callable_non_dict_returns_empty(lister):
    assert Catalog._folder_children(lister, "/Files") == []


def test_parse_table_entry_bad_tables_path():
    with pytest.raises(ValidationError) as excinfo:
        Parse.table_entry("/Tables/onlyone")
    assert "/Tables/schema/table" in str(excinfo.value)


def test_parse_table_entry_other_absolute_path_rejected():
    with pytest.raises(ValidationError) as excinfo:
        Parse.table_entry("/weird/path")
    assert "schema.table or /Tables/schema/table" in str(excinfo.value)


@pytest.mark.parametrize("entry", [".orders", "sales."], ids=["empty schema", "empty table"])
def test_parse_table_entry_empty_part_rejected(entry):
    with pytest.raises(ValidationError):
        Parse.table_entry(entry)


# ---------------------------------------------------------------------------------------------
# LibPureLogicGaps — diff 'update' verdict
# ---------------------------------------------------------------------------------------------


def test_diff_roles_update_when_role_changed():
    grants, _e, _w, _s = Generate.rows([BASE], CANON)
    resolve_grants(grants, [BASE])
    dar = DAR.build_desired(grants, "TENANT")[0]
    changed = json.loads(json.dumps(dar))
    changed["members"]["microsoftEntraMembers"] = [
        {"objectId": "someone-else", "objectType": "Group", "tenantId": "TENANT"}
    ]
    assert DAR.diff([dar], [changed]) == {"SalesReaders": "update"}


# ---------------------------------------------------------------------------------------------
# LibPureLogicGaps — Generate.rows error / warning branches (each via a targeted config row)
# ---------------------------------------------------------------------------------------------


def test_role_name_invalid_b1():
    errors = generate_errors(
        [make_row(role_name="1bad", include_tables="sales.orders", include_group_names="sg-x")]
    )
    assert any("rule B1" in e for e in errors)


def test_member_exclude_without_include_pairing():
    # groups exclude with no groups include, but a users include keeps include_total > 0
    errors = generate_errors(
        [
            make_row(
                role_name="R",
                include_tables="sales.orders",
                include_user_names="alice@example.com",
                exclude_group_names="sg-x",
            )
        ]
    )
    assert any("exclude_group_names without include_group_names" in e for e in errors)


def test_no_member_declared_c1():
    errors = generate_errors([make_row(role_name="R", include_tables="sales.orders")])
    assert any("at least one member" in e and "rule C1" in e for e in errors)


def test_within_cell_member_case_collision_errors():
    # two group names differing only by case in ONE cell are DIFFERENT principals; parse_list would
    # silently dedupe them, so Generate._members must flag it (mirrors Member.resolve_ids' guard).
    errors = generate_errors(
        [
            make_row(
                role_name="R",
                include_tables="sales.orders",
                include_group_names="OneLake-x;ONELAKE-x",
            )
        ]
    )
    assert any("differing only by case" in e for e in errors), (
        f"expected within-cell case-collision error, got {errors}"
    )


def test_members_empty_after_exclusion_c1():
    errors = generate_errors(
        [
            make_row(
                role_name="R",
                include_tables="sales.orders",
                include_group_names="sg-x",
                exclude_group_names="sg-x",
            )
        ]
    )
    assert any("empty after exclusion" in e for e in errors)


def test_direct_user_member_does_not_warn():
    """A direct user member is a first-class grant, not something to warn about.

    OLS applies a role to every member type it supports -- user, group, service principal,
    managed identity -- identically, so singling out users said nothing about the platform, only
    about a governance preference. It also cannot hold where per-person access is the only
    expressible form: an OLS static RLS predicate cannot say "current user", so owner/manager
    access is one role per user carrying that user's id, and a security group cannot express it.
    Rule B4 was removed 2026-08-05; this test is the guard against it coming back."""
    _errors, warnings = generate_warnings(
        [
            make_row(
                role_name="R", include_tables="sales.orders", include_user_names="alice@example.com"
            )
        ]
    )
    assert not any("B4" in w or "direct 'user'" in w for w in warnings), warnings


def test_exclude_side_parse_error_recorded():
    # exclude_tables carries a schema-wildcard (illegal) -> resolver raises even with allow_zero,
    # exercising the exclude-loop except-branch in Generate._scope_pair.
    errors = generate_errors(
        [
            make_row(
                role_name="R",
                include_tables="sales.orders",
                exclude_tables="*.x",
                include_group_names="sg-x",
            )
        ]
    )
    assert any("schema part must be literal" in e for e in errors)


def test_a_bare_not_in_no_longer_warns():
    """G3 was a runtime warning until 1.1.0. It fired on every deny-list a config held, forever,
    and the only mitigation it named was one of two valid ones — so it became noise that hid real
    warnings. The trap it described is real and now lives in docs/architecture.md instead."""
    _errors, warnings = generate_warnings(
        [
            make_row(
                role_name="R",
                include_tables="sales.orders",
                rls_condition="region NOT IN ('x')",
                include_group_names="sg-x",
            )
        ]
    )
    assert warnings == [], warnings


def test_rls_without_tables_b3():
    errors = generate_errors(
        [
            make_row(
                role_name="R",
                include_folders="/Files/raw/region_a",
                rls_condition="x = 1",
                include_group_names="sg-x",
            )
        ]
    )
    assert any("rule B2" in e for e in errors)


def test_cls_without_tables():
    errors = generate_errors(
        [
            make_row(
                role_name="R",
                include_folders="/Files/raw/region_a",
                include_columns="a",
                include_group_names="sg-x",
            )
        ]
    )
    assert any("column security applies to tables only" in e for e in errors)


def test_duplicate_folder_scope_deduped_in_grants():
    # Two DISTINCT rows of one role both granting /Files/raw/region_a (one literal, one glob)
    # -> the second (role, folder) grant is skipped in Generate._build_grants (folder dedupe `continue`).
    rows = [
        make_row(role_name="R", include_folders="/Files/raw/region_a", include_group_names="sg-x"),
        make_row(role_name="R", include_folders="/Files/raw/region_*", include_group_names="sg-x"),
    ]
    grants, errors, _w, _s = Generate.rows(rows, CANON)
    assert errors == []
    assert len([a for a in grants if a["scope_path"] == "/Files/raw/region_a"]) == 1
    assert "/Files/raw/region_b" in [a["scope_path"] for a in grants]


def test_to_dar_dedupes_member_across_typed_columns():
    # The SAME objectId appearing in two typed member-id columns collapses to one member grant
    # (the `value.lower() not in seen` false branch in DAR.to_role).
    grants = [
        {
            "role_name": "R",
            "scope_path": "/Tables/sales/orders",
            "scope_type": "Table",
            "permission": "Read",
            "rls_condition": None,
            "visible_columns": None,
            "member_group_names": "dup",
            "member_group_ids": "dup-id",
            "member_user_names": "dup",
            "member_user_ids": "dup-id",
            "member_sp_names": None,
            "member_sp_ids": None,
        }
    ]
    dar = DAR.build_desired(grants, "T")[0]
    ids = [m["objectId"] for m in dar["members"]["microsoftEntraMembers"]]
    assert ids == ["dup-id"]


# ---------------------------------------------------------------------------------------------
# LibPureLogicGaps — platform-ceiling warnings + member-count error
# ---------------------------------------------------------------------------------------------


def test_paths_nearing_limit_warns():
    canon = {
        "tables": {f"big.t{i:03}": f"big.t{i:03}" for i in range(450)},
        "columns": {},
        "folders": {},
    }
    _errors, warnings = generate_warnings(
        [make_row(role_name="Big", include_tables="big.*", include_group_names="sg-x")], canon=canon
    )
    assert any("nearing" in w and "per-role" in w for w in warnings)


def test_members_exceeding_limit_errors():
    members = ";".join(f"m{i}" for i in range(501))
    errors = generate_errors(
        [make_row(role_name="R", include_tables="sales.orders", include_group_names=members)]
    )
    assert any("members exceed" in e for e in errors)


def test_members_nearing_limit_warns():
    members = ";".join(f"m{i}" for i in range(450))
    _errors, warnings = generate_warnings(
        [make_row(role_name="R", include_tables="sales.orders", include_group_names=members)]
    )
    assert any("members nearing" in w for w in warnings)


def test_roles_nearing_item_limit_warns():
    rows = [
        make_row(role_name=f"Role{i:03}", include_tables="sales.orders", include_group_names="sg-x")
        for i in range(200)
    ]
    _errors, warnings = generate_warnings(rows)
    assert any("per-item platform limit" in w for w in warnings)


def test_roles_exceeding_item_limit_errors():
    # External security audit (2026-08-16), issue #14: the ceiling's own comment says "fail
    # at generate, not at apply", and the two sibling ceilings (paths, members) both do —
    # over the 250-role limit is now a blocking error, not a warning that dies at the API.
    rows = [
        make_row(role_name=f"Role{i:03}", include_tables="sales.orders", include_group_names="sg-x")
        for i in range(251)
    ]
    errors = generate_errors(rows)
    assert any("251 roles exceed the 250-per-item platform limit" in e for e in errors)


def test_cls_leaving_zero_visible_columns_errors():
    # A blacklist that excludes EVERY column of a table would hide it entirely — a caller error
    # (deny the table instead). CLS.visible_for_table returns [] and Generate.rows flags it.
    errors = generate_errors(
        [
            make_row(
                role_name="R",
                include_tables="hr.payroll",
                exclude_columns="employee_id;name;department;salary;bank_account",
                include_group_names="sg-x",
            )
        ]
    )
    assert any("0 visible columns" in e for e in errors)


def test_resolve_member_ids_rejects_config_case_collision():
    # Two grants naming the SAME-type member in different case are different principals — a hard
    # resolution error (the config-side mirror of the cache/table case-collision guards). Seeding
    # the (type, lower) key in the cache isolates the collision branch from a not-found error.
    grants = []
    for role, name in (("A", "sg-x"), ("B", "SG-X")):
        grant = {c: None for c in MAPPING_COLUMNS}
        grant.update(
            role_name=role,
            scope_path=f"/Tables/s/{role}",
            scope_type="Table",
            member_group_names=name,
        )
        grants.append(grant)
    errors = Member.resolve_ids(
        grants,
        {("Group", "sg-x"): "11111111-1111-1111-1111-111111111111"},
        [],  # grants are hand-built here, nothing extra is declared
    )
    assert any("differing only by case" in e for e in errors)


# ---------------------------------------------------------------------------------------------
# AutotrimTests — every STRING value read at the config short_rows seam and the member-cache load
# seam is stripped of leading/trailing whitespace before validation/resolution, so a stray space in
# a role_name/pattern/member name never silently fails to match. The rls_condition field's OUTER
# whitespace is stripped, never a space INSIDE a quoted literal (that's data, not read-seam noise).
# ---------------------------------------------------------------------------------------------


def test_config_row_whitespace_trimmed_resolves_like_trimmed_form():
    # (a) a config row whose role_name/include_tables/rls_condition carries leading/
    # trailing spaces must resolve identically to the already-trimmed form.
    spaced_row = {
        "role_name": "  SalesReaders  ",
        "workspace_name": "W",
        "lakehouse_name": " L ",
        "include_tables": "  sales.*  ",
        "exclude_tables": None,
        "include_folders": None,
        "exclude_folders": None,
        "permission": " Read ",
        "rls_condition": "  region = 'A B'  ",
        "include_columns": None,
        "exclude_columns": None,
        "include_group_names": "  sg-x  ",
        "exclude_group_names": None,
        "include_user_names": None,
        "exclude_user_names": None,
        "include_sp_names": None,
        "exclude_sp_names": None,
        "include_mi_names": None,
        "exclude_mi_names": None,
        "active": True,
        "notes": "  some notes  ",
    }
    # the read seam PROJECTS to CONFIG_AUTHOR_COLUMNS as it trims — the foreign
    # workspace_name column above must come out along the way
    trimmed_row = {
        k: (v.strip() if isinstance(v, str) else v)
        for k, v in spaced_row.items()
        if k in CONFIG_AUTHOR_COLUMNS
    }

    spark = build_spark()
    spark._store[CONFIG_TABLE] = [dict(spaced_row)]
    dep = make_dep(spark, FakeFabricClient(), "generate")

    assert dep.short_rows[0] == trimmed_row

    grants_spaced, errs_spaced, warns_spaced, _s1 = Generate.rows(dep.short_rows, CANON)
    grants_trimmed, errs_trimmed, warns_trimmed, _s2 = Generate.rows([trimmed_row], CANON)
    assert errs_spaced == []
    assert errs_spaced == errs_trimmed
    assert warns_spaced == warns_trimmed
    assert grants_spaced == grants_trimmed


def test_member_cache_whitespace_trimmed_matches_config_reference():
    # (b) a member row with padded member_name must match a config reference to the
    # trimmed spelling — a trailing space in a member name silently failing to match is the
    # security-relevant bug this task closes.
    spark = build_spark()
    spark._store[MEMBER_TABLE] = [
        member_cache_row("User", "  alice@x.com  ", "e0000000-0000-0000-0000-000000000099"),
    ]
    dep = make_dep(spark, FakeFabricClient(), "generate")

    cache, spellings, cache_errors = dep._load_member_cache()
    assert cache_errors == []
    assert cache == {("User", "alice@x.com"): "e0000000-0000-0000-0000-000000000099"}
    # the as-written map carries the SAME autotrim — a padded name must not reach the
    # mapping's member_*_names with its padding intact, which would move mapping_hash
    assert spellings == {("User", "alice@x.com"): "alice@x.com"}

    grants = [
        {
            **{c: None for c in MAPPING_COLUMNS},
            "role_name": "R",
            "scope_path": "/Tables/s/r",
            "scope_type": "Table",
            "member_user_names": "alice@x.com",  # config side already Parse.list-trimmed
        }
    ]
    errors = Member.resolve_ids(grants, cache, [])  # hand-built grants, nothing declared
    assert errors == []
    assert grants[0]["member_user_ids"] == "e0000000-0000-0000-0000-000000000099"


# ---------------------------------------------------------------------------------------------
# Member._check_known -- the single gate check both passes of resolve_ids call.
#
# The two call SITES are pinned elsewhere (neuter either and 15 / 6 tests go red). This pins the
# helper's own contract, which nothing else does: review found two edits to it that alter real
# behaviour while the whole suite stays green at 100% coverage --
#   * `cached is not None` -> `if cached:`  (a blank cached id stops being a hit)
#   * swapping the GUID check and the cache lookup (the GUID guard becomes fail-open for a
#     GUID-shaped member_name, which _load_member_cache does NOT reject -- it shape-checks
#     member_id only -- and a mapping gets written that should have been blocked)
# Both are killed by the assertions below.
# ---------------------------------------------------------------------------------------------


def test_check_known_contract():
    GUID = "a0000000-0000-0000-0000-000000000009"
    cache = {
        ("Group", "sg-known"): "11111111-1111-1111-1111-111111111111",
        ("Group", "sg-blank"): "",  # only a caller-supplied cache can hold this
        ("Group", GUID.lower()): "22222222-2222-2222-2222-222222222222",
    }

    # hit -> returns the id, says nothing
    errs = []
    assert Member._check_known("Group", "sg-known", cache, errs) == cache[("Group", "sg-known")]
    assert errs == []

    # a blank cached id is a HIT, not a miss -- `is not None`, never truthiness.
    # Under `if cached:` this returns None and appends a not-found error instead.
    errs = []
    assert Member._check_known("Group", "sg-blank", cache, errs) == ""
    assert errs == []

    # ORDER: a GUID-shaped name is rejected even when the cache could resolve it. Swap the two
    # checks and this returns the id with no error -- the fail-open review reproduced end to end.
    errs = []
    assert Member._check_known("Group", GUID, cache, errs) is None
    assert len(errs) == 1 and "looks like an objectId" in errs[0]

    # miss -> None plus EXACTLY ONE error naming the member and its type
    errs = []
    assert Member._check_known("Group", "sg-nobody", cache, errs) is None
    assert len(errs) == 1
    assert "sg-nobody" in errs[0] and "(Group)" in errs[0]
    assert "not found in onelake_security_member" in errs[0]

    # lookup is case-insensitive on the name, and the key is derived inside the helper -- there is
    # no separate `lower` argument that could disagree with `name`.
    errs = []
    assert Member._check_known("Group", "SG-Known", cache, errs) == cache[("Group", "sg-known")]
    assert errs == []


# ---------------------------------------------------------------------------------------------
# Guard A: the No-Graph member gate must reach every DECLARED member name, not just the
# effective set Generate._members hands over -- so an exclude value, or an include value its own
# exclude cancels, is still required to exist in onelake_security_member. Probe shapes P1/P3/P4
# are the design's own measurements against unpatched `main`, each verified silent there.
# ---------------------------------------------------------------------------------------------


def test_guard_a_p1_dead_exclude_names_unknown_principal():
    # P1: an exclude value absent from the member table, discarded by Generate._members' plain
    # string subtraction before the gate ever saw it.
    rows = [
        make_row(
            role_name="R",
            include_tables="sales.orders",
            include_group_names="sg-sales-readers",
            exclude_group_names="sg-totally-fictional",
        )
    ]
    grants, _gen_errors, _warnings, _summary = Generate.rows(rows, CANON)
    errors = Member.resolve_ids(grants, directory_cache(), rows)
    assert any(
        "sg-totally-fictional" in e and "not found in onelake_security_member" in e for e in errors
    )


def test_guard_a_p3_include_value_cancelled_by_own_exclude_still_gated():
    # P3: an include value absent from the member table, cancelled by its own exclude -- neither
    # copy survives into Generate._members' by_col, so only the declared-name pass catches it.
    rows = [
        make_row(
            role_name="R",
            include_tables="sales.orders",
            include_group_names=f"sg-sales-readers;{MISSING_NAME}",
            exclude_group_names=MISSING_NAME,
        )
    ]
    grants, _gen_errors, _warnings, _summary = Generate.rows(rows, CANON)
    errors = Member.resolve_ids(grants, directory_cache(), rows)
    assert any(MISSING_NAME in e and "not found in onelake_security_member" in e for e in errors)


def test_guard_a_p4_guid_shaped_exclude_rejected():
    # P4: a GUID-shaped exclude value -- config takes display names, not objectIds. Same
    # rejection the effective pass already applies, now reached from a declared-only column.
    rows = [
        make_row(
            role_name="R",
            include_tables="sales.orders",
            include_group_names="sg-sales-readers",
            exclude_group_names="a0000000-0000-0000-0000-000000000009",
        )
    ]
    grants, _gen_errors, _warnings, _summary = Generate.rows(rows, CANON)
    errors = Member.resolve_ids(grants, directory_cache(), rows)
    assert any(
        "a0000000-0000-0000-0000-000000000009" in e and "looks like an objectId" in e
        for e in errors
    )


def test_guard_a_declared_only_name_known_to_cache_no_error():
    # Negative arm: sg-analysts is declared (include AND its own exclude) but not effective --
    # and IS in the member table. Without this case the cache.get(...) is None FALSE branch on
    # the declared-only pass is never exercised and the 100% branch gate fails.
    rows = [
        make_row(
            role_name="R",
            include_tables="sales.orders",
            include_group_names="sg-sales-readers;sg-analysts",
            exclude_group_names="sg-analysts",
        )
    ]
    grants, gen_errors, _warnings, _summary = Generate.rows(rows, CANON)
    assert gen_errors == []  # sanity: a valid exclusion, config itself is clean
    errors = Member.resolve_ids(grants, directory_cache(), rows)
    assert errors == []


def test_guard_a_resolves_a_declared_name_under_ITS_OWN_member_type():
    # The gate is keyed on (member_type, name), never on the name alone. 'sg-analysts' is a real
    # GROUP in the member table, but naming it in an SP column asks for a SERVICE PRINCIPAL of
    # that name, which does not exist -- so it must block, and the error must say
    # ServicePrincipal. Without the type in the key, a name present under ANY type would satisfy
    # the gate for EVERY type, which is how a group could quietly stand in for a service
    # principal on the path that decides who receives grants (owner call, 2026-08-12).
    rows = [
        make_row(
            role_name="R",
            include_tables="sales.orders",
            include_group_names=None,
            include_sp_names="svc-etl",
            exclude_sp_names="sg-analysts",  # a real Group, in the ServicePrincipal column
        )
    ]
    grants, _gen_errors, _warnings, _summary = Generate.rows(rows, CANON)
    errors = Member.resolve_ids(grants, directory_cache(), rows)
    assert any("sg-analysts" in e and "(ServicePrincipal)" in e for e in errors), errors
    # and the same name in its OWN type resolves cleanly, so this is about the type, not the name
    ok_rows = [
        make_row(
            role_name="R",
            include_tables="sales.orders",
            include_group_names="sg-sales-readers",
            exclude_group_names="sg-analysts",
        )
    ]
    ok_grants, _e, _w, _s = Generate.rows(ok_rows, CANON)
    ok_errors = Member.resolve_ids(ok_grants, directory_cache(), ok_rows)
    assert ok_errors == [], ok_errors


def test_guard_a_dedup_effective_and_unknown_reported_once():
    # A name that is BOTH effective (survives the subtraction) and declared must be reported
    # exactly once -- the effective pass already caught it, so the declared-only pass must skip
    # it via the `(mtype, lower) in pairs` dedup rather than reporting it a second time.
    rows = [
        make_row(
            role_name="R",
            include_tables="sales.orders",
            include_group_names=f"sg-sales-readers;{MISSING_NAME}",
        )
    ]
    grants, _gen_errors, _warnings, _summary = Generate.rows(rows, CANON)
    errors = Member.resolve_ids(grants, directory_cache(), rows)
    hits = [e for e in errors if MISSING_NAME in e and "not found in onelake_security_member" in e]
    assert len(hits) == 1, hits


def test_guard_a_inactive_row_members_not_gated():
    # "active=true only" needs no code of its own in resolve_ids/_declared_names -- it is
    # achieved entirely by the caller (Deployment.short_rows) never handing an inactive row's
    # names into `rows` at all.
    #
    # SCOPE, honestly: this test does that filtering ITSELF, so it proves only that resolve_ids
    # ignores names absent from the list it is handed -- it does NOT pin the caller's contract.
    # A wiring that dropped `.where("active = true")` passes here. The production contract is
    # pinned by test_guard_a_wired_to_production_validate_path_p3_shape (test_mock_lifecycle),
    # whose third row is inactive and carries an unknown member.
    active_row = make_row(
        role_name="Active", include_tables="sales.orders", include_group_names="sg-sales-readers"
    )
    inactive_row = make_row(
        role_name="Inactive",
        include_tables="sales.orders",
        include_group_names=MISSING_NAME,  # would gate if this row reached `rows`
        active=False,
    )
    active_rows = [r for r in (active_row, inactive_row) if r["active"]]
    assert active_rows == [active_row]
    grants, _gen_errors, _warnings, _summary = Generate.rows(active_rows, CANON)
    errors = Member.resolve_ids(grants, directory_cache(), active_rows)
    assert not any(MISSING_NAME in e for e in errors)


@pytest.mark.parametrize("glob", ["sg-*", "sg-analyst?"])
def test_guard_a_wildcard_exclude_not_gated(glob):
    # RT B3: a wildcard value must never reach the gate -- "add sg-* (with its objectId) before
    # generate" is wrong advice on the path that decides who receives grants. The wildcard rule
    # in Generate._members owns this mistake; _declared_names must skip it.
    # BOTH metacharacters: the skip is `"*" in name or "?" in name`, one branch, so a mutation
    # dropping `?` kept 100% coverage and passed the whole suite while the gate emitted
    # "member 'sg-analyst?' ... not found in onelake_security_member -- add it (with its objectId)".
    rows = [
        make_row(
            role_name="R",
            include_tables="sales.orders",
            include_group_names="sg-sales-readers",
            exclude_group_names=glob,
        )
    ]
    grants, gen_errors, _warnings, _summary = Generate.rows(rows, CANON)
    assert any("wildcards not allowed in member values" in e for e in gen_errors)
    errors = Member.resolve_ids(grants, directory_cache(), rows)
    assert not any(glob in e for e in errors)


def test_rls_condition_inner_literal_space_preserved():
    # (c) autotrim strips only the field's OUTER whitespace — a space INSIDE a quoted
    # rls_condition literal is DATA and must survive untouched.
    spark = build_spark()
    spark._store[CONFIG_TABLE] = [
        make_row(
            include_tables="sales.orders",
            rls_condition="  region = 'A B'  ",
            include_group_names="sg-x",
        )
    ]
    dep = make_dep(spark, FakeFabricClient(), "generate")

    assert dep.short_rows[0]["rls_condition"] == "region = 'A B'"


def test_trim_row_leaves_non_string_values_untouched():
    # Parse.trim_row must not stringify None or bool -- only actual str values are stripped,
    # so a NULL column stays None (not the literal string "None") and 'active' stays a bool.
    row = {"active": True, "notes": None, "role_name": "  R  "}
    assert Parse.trim_row(row) == {"active": True, "notes": None, "role_name": "R"}


# ---------------------------------------------------------------------------------------------
# BoolParamTyping — Parse.bool_param, strict typing for the notebook's boolean Base parameters.
# Fabric Base parameters default to the String type, so `keep_unmanaged` reaches the runtime as a
# STRING and `bool("false")` is True. Every spelling below must be PARSED, and anything
# unrecognised REJECTED — never coerced onto the wrong path by truthiness.
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("value", [True, False])
def test_real_booleans_pass_through(value):
    # the OLAF facade, direct run_mode calls, and this harness all pass real booleans
    assert Parse.bool_param("keep_unmanaged", value) == (value, None)


@pytest.mark.parametrize("value", ["true", "True", "TRUE", "1", "yes", "YES", " true "])
def test_truthy_spellings_parse_to_true(value):
    assert Parse.bool_param("keep_unmanaged", value) == (True, None)


@pytest.mark.parametrize("value", ["false", "False", "FALSE", "0", "no", "NO", " false "])
def test_falsy_spellings_parse_to_false(value):
    assert Parse.bool_param("keep_unmanaged", value) == (False, None)


@pytest.mark.parametrize("value", ["", "   "])
def test_blank_string_is_an_unset_parameter(value):
    # Fabric passes "" for a Base parameter left blank — unset, not garbage
    assert Parse.bool_param("keep_unmanaged", value) == (False, None)


def test_none_is_an_unset_parameter():
    # Fabric can genuinely deliver null for a Base parameter left unset — the same
    # "no value was given" case as a missing key or a blank string, not a garbage value
    assert Parse.bool_param("keep_unmanaged", None) == (False, None)


@pytest.mark.parametrize(
    "value",
    [{}.get("keep_unmanaged", False), "", None],
    ids=["missing key", "blank string", "None"],
)
def test_three_spellings_of_absence_all_yield_false(value):
    # missing key (what run_mode's params.get("keep_unmanaged", False) hands over when
    # keep_unmanaged is absent), a blank string, and None all mean "no value was given" and
    # must parse identically — asserted together so the claim can't silently drift apart
    assert Parse.bool_param("keep_unmanaged", value) == (False, None)


def test_unrecognised_spelling_is_rejected_naming_parameter_and_value():
    parsed, error = Parse.bool_param("keep_unmanaged", "ture")
    assert parsed is False  # never fall back to truthiness
    assert "keep_unmanaged" in error
    assert "'ture'" in error


@pytest.mark.parametrize(("value", "expected"), [(0, False), (1, True)])
def test_int_zero_and_one_are_accepted_as_real_booleans(value, expected):
    # Fabric also offers Int as a Base-parameter type, so a pipeline may hand `keep_unmanaged`
    # over as a real int. 0/1 are the only unambiguous int spellings, and they parse to the bool
    # SINGLETONS (`is`) — the envelope echo and apply's REPLACE fork are both `is`-checked.
    parsed, error = Parse.bool_param("keep_unmanaged", value)
    assert error is None
    assert parsed is expected


@pytest.mark.parametrize("value", [True, False])
def test_real_bools_are_matched_before_the_int_table(value):
    # isinstance(True, int) is True, so the bool check MUST stay ahead of the int handling:
    # a real bool passes through as itself, never round-tripped through the int branch.
    parsed, error = Parse.bool_param("keep_unmanaged", value)
    assert error is None
    assert parsed is value
    assert isinstance(parsed, bool)


@pytest.mark.parametrize("value", [2, -1, 42])
def test_ambiguous_int_is_rejected(value):
    # only 0/1 are unambiguous — 2 / -1 are guesses, and guessing wrong on `keep_unmanaged`
    # leaves roles live that the default REPLACE was meant to delete
    parsed, error = Parse.bool_param("keep_unmanaged", value)
    assert parsed is False
    assert error.endswith(f"got {value!r}"), error


@pytest.mark.parametrize("value", [["true"], 1.0, 0.0])
def test_non_string_non_bool_is_rejected(value):
    parsed, error = Parse.bool_param("keep_unmanaged", value)
    assert parsed is False
    assert error.endswith(f"got {value!r}"), error


def test_zero_match_folder_error_survives_a_callable_lister():
    """`folders` is a CALLABLE in production — the notebookutils.fs.ls seam — and a dict only in
    tests. `_folder_children` handles both; the ZeroMatchError path did not, and called
    `sorted(folders)` on it.

    So the message meant to help ("folder matched 0 paths… did you mean…?") raised
    `TypeError: 'function' object is not iterable` instead, on real Fabric, for any config naming
    a folder that does not exist. Every one of the suite's other tests walked this path with a
    dict, where `sorted()` happens to work — which is why 100% coverage said nothing about it.

    Drive it with a callable, the way Fabric does.
    """
    listing = {"/Files": ["raw", "curated"], "/Files/raw": []}

    def lister(base):
        return listing.get(base, [])

    with pytest.raises(ZeroMatchError) as seen:
        Catalog.resolve_folders("Files/nope", lister)
    assert "matched 0 paths" in str(seen.value)

    # and the dict form must keep working, suggestion and all
    with pytest.raises(ZeroMatchError) as as_dict:
        Catalog.resolve_folders("Files/raww", listing)
    assert "did you mean 'raw'" in str(as_dict.value).lower() or "matched 0 paths" in str(
        as_dict.value
    )
