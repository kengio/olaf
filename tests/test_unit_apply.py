"""DAR payload/diff, apply semantics, audit-log rows, control-table DDL and the provenance
model, as pytest functions.

Ported from `olaf_test_unit.ipynb` classes `PayloadAndDiff`, `ApplySemantics`, `LogSemantics`,
`SetupDefinitions` and `ProvenanceModel`.
"""

import json

import pytest

from _olaf_runtime import (
    DAR,
    DARHTTPError,
    LIST_SEP,
    LOG_COLUMNS,
    MAPPING_COLUMNS,
    MAPPING_PROVENANCE_COLUMNS,
    MEMBER_CACHE_COLUMNS,
    Catalog,
    Generate,
    Hash,
    Log,
    OLAFError,
    Parse,
    TableSchema,
    ValidationError,
)
from _fakes import (
    BASE,
    DIRECTORY_GROUPS,
    DIRECTORY_SPS,
    DIRECTORY_USERS,
    _FakeHistorySpark,
    _FakeLogSpark,
    make_row,
    resolved_grants,
)


@pytest.fixture
def base_grants():
    return resolved_grants([BASE])


@pytest.fixture
def base_dar(base_grants):
    return DAR.build_desired(base_grants, "TENANT")[0]


# ---------------------------------------------------------------------------------------------
# PayloadAndDiff — grant grain, DAR payload shape, diff, config hash
# ---------------------------------------------------------------------------------------------


def test_grants_are_single_valued(base_grants):
    log_grants = Generate.to_log_grants(base_grants)
    assert len(log_grants) == 3  # 3 tables x 1 member
    assert all(LIST_SEP not in str(a["scope_path"]) for a in log_grants)


def test_decision_rules_grouped_by_policy(base_dar):
    # sales.* all share one policy -> a single grouped decisionRule with 3 paths
    assert len(base_dar["decisionRules"]) == 1
    paths = base_dar["decisionRules"][0]["permission"][0]["attributeValueIncludedIn"]
    assert len(paths) == 3


def test_rls_wrapped_per_table(base_dar):
    value = base_dar["decisionRules"][0]["constraints"]["rows"][0]["value"]
    assert value.startswith("SELECT * FROM sales.")


def test_member_type_from_config(base_dar):
    assert base_dar["members"]["microsoftEntraMembers"][0]["objectType"] == "Group"


@pytest.fixture
def mixed_member_read_back():
    """Grants carrying all three member types, projected to the persisted MAPPING_COLUMNS — the only
    fields a table read-back would retain."""
    grants = resolved_grants(
        [
            make_row(
                role_name="MixedRead",
                include_tables="ref.calendar",
                include_group_names="sg-analysts",
                include_user_names="alice@example.com",
                include_sp_names="svc-etl",
            )
        ]
    )
    return [{c: a.get(c) for c in MAPPING_COLUMNS} for a in grants]


def test_member_type_dispatches_by_typed_column(mixed_member_read_back):
    """objectType comes from WHICH typed member column a value sits in — not a hardcoded default.
    This is what catches a Group hardcode; it does not exercise real Spark persistence (untestable
    at unit scope), only the multi-type dispatch."""
    dar = DAR.build_desired(mixed_member_read_back, "TENANT")[0]
    # DAR members carry the RESOLVED objectIds, typed by which member column they came from.
    types = {m["objectId"]: m["objectType"] for m in dar["members"]["microsoftEntraMembers"]}
    assert types == {
        DIRECTORY_GROUPS["sg-analysts"]: "Group",
        DIRECTORY_USERS["alice@example.com"]: "User",
        DIRECTORY_SPS["svc-etl"]: "ServicePrincipal",
    }


def test_log_grants_carry_the_display_name_and_type(mixed_member_read_back):
    log_types = {
        a["member_name"]: a["member_type"] for a in Generate.to_log_grants(mixed_member_read_back)
    }
    assert log_types == {
        "sg-analysts": "Group",
        "alice@example.com": "User",
        "svc-etl": "ServicePrincipal",
    }


def test_log_grants_carry_the_resolved_member_id(mixed_member_read_back):
    log_ids = {
        a["member_name"]: a["member_id"] for a in Generate.to_log_grants(mixed_member_read_back)
    }
    assert log_ids == {
        "sg-analysts": DIRECTORY_GROUPS["sg-analysts"],
        "alice@example.com": DIRECTORY_USERS["alice@example.com"],
        "svc-etl": DIRECTORY_SPS["svc-etl"],
    }


def test_cls_constraint_in_payload():
    grants = resolved_grants(
        [
            make_row(
                role_name="HrRead",
                include_tables="hr.payroll",
                exclude_columns="salary;bank_account",
                include_group_names="sg-analysts",
            )
        ]
    )
    dar = DAR.build_desired(grants, "TENANT")[0]
    cls = dar["decisionRules"][0]["constraints"]["columns"][0]
    assert cls["tablePath"] == "/Tables/hr/payroll"
    # DAR CLS is an allow-list: columnNames = the VISIBLE columns (catalog minus excluded)
    assert cls["columnNames"] == ["employee_id", "name", "department"]


def test_cls_grouping_is_order_insensitive():
    # the SAME visible-column set declared in different column order must collapse into ONE
    # decisionRule — the grouping key sorts the visible set. employee_id/name/department exist
    # in both hr tables, so a whitelist of them yields the same visible set for each.
    grants = resolved_grants(
        [
            make_row(
                role_name="HrRead",
                include_tables="hr.payroll",
                include_columns="employee_id;name;department",
                include_group_names="sg-analysts",
            ),
            make_row(
                role_name="HrRead",
                include_tables="hr.employees",
                include_columns="department;name;employee_id",
                include_group_names="sg-analysts",
            ),
        ]
    )
    dar = DAR.build_desired(grants, "TENANT")[0]
    assert len(dar["decisionRules"]) == 1


def test_diff_new_role_is_create(base_dar):
    assert DAR.diff([base_dar], []) == {"SalesReaders": "create"}


def test_diff_identical_is_no_change(base_dar):
    assert DAR.diff([base_dar], [base_dar]) == {"SalesReaders": "no_change"}


def test_config_hash_is_stable():
    assert Hash.config([BASE]) == Hash.config([dict(BASE)])


def test_config_hash_is_order_independent():
    # collect() order is an engine detail, not a contract — the same row multiset must
    # fingerprint identically in any order (the mapping hash solved this in 1.0.0; the
    # config hash's own docstring promised it without delivering), while a real content
    # change must still move the hash.
    rows = [dict(BASE, role_name="A"), dict(BASE, role_name="B")]
    assert Hash.config(rows) == Hash.config(list(reversed(rows)))
    changed = [dict(r, permission="ReadWrite") for r in rows]
    assert Hash.config(rows) != Hash.config(changed)


def test_config_hash_sorts_a_copy_not_the_callers_list():
    # short_rows is a cached list — an in-place sort would silently reorder the authored
    # config for every later reader.
    rows = [dict(BASE, role_name="B"), dict(BASE, role_name="A")]
    Hash.config(rows)
    assert [r["role_name"] for r in rows] == ["B", "A"]  # untouched


def test_log_grants_keep_both_names_when_two_resolve_to_one_id():
    # Two distinct display names can resolve to the SAME objectId (a user's UPN + a mail alias);
    # both must land in the audit log — the id list must not be deduped independently of names.
    grant = {c: None for c in MAPPING_COLUMNS}
    grant.update(
        role_name="R",
        scope_path="/Tables/s/t",
        scope_type="Table",
        member_user_names="alice@x.com;alias@x.com",
        member_user_ids="G;G",
    )
    rows = Generate.to_log_grants([grant])
    assert [r["member_name"] for r in rows] == ["alice@x.com", "alias@x.com"]
    assert [r["member_id"] for r in rows] == ["G", "G"]


def test_managed_identity_member_resolves_and_tags_object_type():
    # A ManagedIdentity config member resolves via the servicePrincipals endpoint but rides the
    # DAR payload tagged objectType="ManagedIdentity" (the 4th Entra principal type).
    grants = resolved_grants(
        [
            make_row(
                role_name="LoaderRead", include_tables="sales.orders", include_mi_names="mi-loader"
            )
        ]
    )
    assert grants[0]["member_mi_names"] == "mi-loader"
    assert grants[0]["member_mi_ids"] == "d0000000-0000-0000-0000-000000000001"
    dar = DAR.build_desired(grants, "TENANT")[0]
    assert {
        "objectId": "d0000000-0000-0000-0000-000000000001",
        "objectType": "ManagedIdentity",
        "tenantId": "TENANT",
    } in dar["members"]["microsoftEntraMembers"]


# ---------------------------------------------------------------------------------------------
# ApplySemantics — upsert vs replace, role paths/members, subject matching
# ---------------------------------------------------------------------------------------------


@pytest.fixture
def live_roles():
    return [
        {"name": "DefaultReader"},
        {"name": "LegacyManual"},
        {"name": "SalesReaders", "old": True},
    ]


def test_merge_upsert_never_deletes(base_dar, live_roles):
    merged = DAR.merge_upsert(live_roles, [base_dar])
    assert sorted(r["name"] for r in merged) == ["DefaultReader", "LegacyManual", "SalesReaders"]
    assert not any(r.get("old") for r in merged if r["name"] == "SalesReaders")


def test_merge_replace_returns_only_config(base_dar):
    # replace = config is the full truth: the result is exactly `desired`; every live role not in
    # config is dropped, INCLUDING Default* (a leftover Default* reader would bypass RLS/CLS).
    merged = DAR.merge_replace([base_dar])
    assert sorted(r["name"] for r in merged) == ["SalesReaders"]


def test_role_paths_and_members(base_dar):
    paths, members = DAR.paths_and_members(base_dar)
    assert all(p.startswith("/Tables/sales/") for p in paths)
    # live DAR members are the resolved objectIds (sg-sales-readers -> its id)
    assert members == [DIRECTORY_GROUPS["sg-sales-readers"]]


def test_subject_match_is_case_insensitive_equality():
    assert Parse.subject_match("salesreaders", "SalesReaders")


def test_subject_match_refuses_a_bare_prefix():
    """The defect this rule exists for: these identifiers nest. Under substring matching,
    asking about sg-sales also answered about sg-sales-managers — on a mode
    whose whole job is "who can reach what", extra rows read as confirmed access."""
    assert not Parse.subject_match("sg-sales", "sg-sales-managers")
    assert not Parse.subject_match("sales", "SalesReaders")
    assert not Parse.subject_match("sales.order", "sales.orders")


def test_subject_match_still_does_partial_when_it_is_asked_for():
    """Partial matching is not gone, it is opt-in — with the wildcard the error messages already
    advertise."""
    assert Parse.subject_match("sg-sales*", "sg-sales-managers")
    assert Parse.subject_match("sales*", "SalesReaders")


def test_subject_match_glob_against_dotted_form():
    assert Parse.subject_match("sales.order*", "/Tables/sales/orders", "sales.orders")


def test_subject_match_glob_rejects_non_match():
    assert not Parse.subject_match("finance*", "sales.orders")


# ---------------------------------------------------------------------------------------------
# LogSemantics — audit row context, per-mode delete treatment, completion + failure rows
# ---------------------------------------------------------------------------------------------


@pytest.fixture
def audit():
    return Log(
        spark=None,
        table="ctl.log",
        batch_id="B1",
        run_id="R1",
        env="dev",
        mode="plan",
        workspace_name="W",
        lakehouse_name="L",
    )


@pytest.fixture
def plan():
    return {"RoleA": "update", "RoleB": "omit", "RoleC": "no_change"}


def test_context_carries_run_by(audit):
    row = audit.row("start", "success")
    assert row["batch_id"] == "B1"
    assert row["run_id"] == "R1"
    assert row["env"] == "dev"
    assert row["mode"] == "plan"
    assert row["run_by"] is None  # run_by resolves to None off-Fabric


def test_run_by_override():
    audit = Log(
        spark=None,
        table="t",
        batch_id="B",
        run_id="R",
        env="dev",
        mode="plan",
        workspace_name="W",
        lakehouse_name="L",
        run_by="alice@example.com",
    )
    assert audit.row("start", "success")["run_by"] == "alice@example.com"


def test_plan_mode_records_an_omission_candidate_as_drift(audit, plan):
    rows = audit.action_rows(plan, "planned", "planned", "drift")
    omission_row = next(r for r in rows if r["action"] == "omission_candidate")
    assert (omission_row["status"], omission_row["message"]) == ("drift", "planned")


def test_config_payload_records_omission_candidates_without_deletion_status(audit, plan):
    rows = audit.action_rows(
        plan,
        "submitted",
        "submitted as omission candidate; post-state review required",
        "submitted",
    )
    omission_row = next(r for r in rows if r["action"] == "omission_candidate")
    assert (omission_row["status"], omission_row["message"]) == (
        "submitted",
        "submitted as omission candidate; post-state review required",
    )


def test_complete_row_stamps_duration(audit, plan):
    row = audit.complete_row(plan, omitted_role_candidates=["RoleB"])
    assert row["run_duration"] is not None
    assert float(row["run_duration"]) >= 0.0
    msg = json.loads(row["message"])
    # the message carries the plan, not the hash (config_hash is a first-class column)
    assert msg["plan"] == dict(sorted(plan.items()))
    assert "source_hash" not in msg
    assert "config_hash" not in msg
    assert msg["omitted_role_candidates"] == ["RoleB"]


def test_error_category_classification():
    assert OLAFError.classify(ValueError("x")) == "validation"
    assert OLAFError.classify(SystemExit("x")) == "guard"
    assert OLAFError.classify(RuntimeError("x")) == "unexpected"

    class FakeHTTP(Exception):
        pass

    FakeHTTP.__module__ = "requests.exceptions"
    assert OLAFError.classify(FakeHTTP("boom")) == "http"
    assert OLAFError.classify(ValidationError("x")) == "validation"
    assert OLAFError.classify(DARHTTPError("x")) == "http"


def test_fail_row_sets_category(audit):
    row = audit.fail_row("apply", ValueError("bad config"))
    assert row["status"] == "failed"
    assert row["error_category"] == "validation"
    assert "bad config" in row["message"]


# ---------------------------------------------------------------------------------------------
# SetupDefinitions — the four control-table DDLs
# ---------------------------------------------------------------------------------------------


def default_ddl():
    return TableSchema.definitions("c.a", "c.b", "c.c", "c.d")


def test_custom_names_and_idempotence():
    ddl = TableSchema.definitions("meta.cfg", "meta.mapping", "meta.audit", "meta.member")
    assert set(ddl) == {"meta.cfg", "meta.mapping", "meta.audit", "meta.member"}
    for statement in ddl.values():
        assert "CREATE TABLE IF NOT EXISTS" in statement


@pytest.mark.parametrize(
    "column",
    [
        "include_tables",
        "exclude_tables",
        "include_folders",
        "exclude_folders",
        "include_columns",
        "exclude_columns",
        "include_group_names",
        "exclude_group_names",
        "include_sp_names",
        "exclude_sp_names",
    ],
)
def test_config_has_include_exclude_columns(column):
    assert f"`{column}`" in default_ddl()["c.a"]


def test_config_active_is_boolean():
    assert "`active` BOOLEAN" in default_ddl()["c.a"]


@pytest.mark.parametrize(
    "column",
    [
        "role_name",
        "workspace_name",
        "lakehouse_name",
        "scope_path",
        "scope_type",
        "permission",
        "rls_condition",
        "visible_columns",
        "member_group_names",
        "member_group_ids",
        "member_user_names",
        "member_user_ids",
        "member_sp_names",
        "member_sp_ids",
        "generated_at",
        "config_hash",
        "config_version",
        "framework_version",
    ],
)
def test_mapping_grain_and_provenance(column):
    assert f"`{column}`" in default_ddl()["c.b"]


def test_mapping_v1_names_are_gone():
    ddl = default_ddl()["c.b"]
    assert "generator_version" not in ddl
    assert "`members`" not in ddl


def test_catalog_snapshot_at_absent_from_provenance_columns():
    assert "catalog_snapshot_at" not in MAPPING_PROVENANCE_COLUMNS
    assert MAPPING_PROVENANCE_COLUMNS == [
        "generated_at",
        "config_hash",
        "config_version",
        "framework_version",
    ]


def test_catalog_snapshot_at_absent_from_mapping_ddl():
    ddl = default_ddl()["c.b"]
    assert "catalog_snapshot_at" not in ddl
    assert "source_hash" not in ddl  # and no v1 source_hash


def test_log_table_matches_audit_writer():
    ddl = default_ddl()["c.c"]
    for column in Log.COLUMNS:
        assert f"`{column}`" in ddl
    for column in ("member_name", "member_id", "member_type"):
        assert f"`{column}`" in ddl  # resolved objectId rides next to name + type


def test_member_cache_table_has_resolution_columns():
    # The 4th control table (onelake_security_member) — the name->objectId resolution cache.
    # 3 columns only: source/resolved_at were removed (single source, no per-row timestamp).
    ddl = TableSchema.definitions("c.a", "c.b", "c.c", "c.member")["c.member"]
    for column in ("member_type", "member_name", "member_id"):
        assert f"`{column}`" in ddl
    assert MEMBER_CACHE_COLUMNS == ["member_type", "member_name", "member_id"]
    # the removed timestamp/source columns are gone
    assert "`source`" not in ddl
    assert "`resolved_at`" not in ddl


# ---------------------------------------------------------------------------------------------
# ProvenanceModel — config_version capture and config provenance on log rows
# ---------------------------------------------------------------------------------------------


def test_config_table_version_reads_delta_history():
    assert Catalog.config_version(_FakeHistorySpark(7), "olaf.onelake_security_config") == 7


def test_config_table_version_falls_back_to_none():
    # not Delta / DESCRIBE HISTORY unavailable -> None (config_hash stays the staleness guard)
    assert Catalog.config_version(_FakeHistorySpark(None, raises=True), "ctl.cfg") is None


def test_config_provenance_are_first_class_log_columns():
    assert "config_hash" in LOG_COLUMNS
    assert "config_version" in LOG_COLUMNS


def test_config_provenance_is_unset_before_stamping(audit):
    blank = audit.row("start", "success")
    assert blank["config_hash"] is None
    assert blank["config_version"] is None


@pytest.mark.parametrize("action", ["start", "validate", "action", "complete"])
def test_log_rows_carry_config_provenance_once_stamped(audit, action):
    audit.set_config_provenance("284ae40f8b47a294", 7)
    row = audit.row(action, "success")
    assert row["config_hash"] == "284ae40f8b47a294"
    assert row["config_version"] == "7"


@pytest.fixture
def plan_log_audit():
    rows = [
        {
            "mode": "plan",
            "action": "complete",
            "status": "success",
            "env": "dev",
            "config_hash": "AAAA1111",
            "mapping_hash": "MAPA0001",
            "run_at": "2026-01-01T00:00:00Z",
            "message": json.dumps({"plan": {"RoleA": "create"}}),
        },
        {
            "mode": "plan",
            "action": "complete",
            "status": "success",
            "env": "dev",
            "config_hash": "BBBB2222",
            "mapping_hash": "MAPB0002",
            "run_at": "2026-01-02T00:00:00Z",
            "message": json.dumps({"plan": {"RoleB": "update"}}),
        },
        {
            # a plan row with NO stamped mapping_hash — every framework release stamps it, so
            # this models an externally written or hand-edited log row, not a past release
            "mode": "plan",
            "action": "complete",
            "status": "success",
            "env": "dev",
            "config_hash": "CCCC3333",
            "mapping_hash": None,
            "run_at": "2026-01-03T00:00:00Z",
            "message": json.dumps({"plan": {"RoleC": "create"}}),
        },
    ]
    return Log(
        spark=_FakeLogSpark(rows),
        table="ctl.log",
        batch_id="B",
        run_id="R",
        env="dev",
        mode="apply",
        workspace_name="W",
        lakehouse_name="L",
    )


def test_find_plan_record_matches_on_config_and_mapping_hash_columns(plan_log_audit):
    assert plan_log_audit.find_plan_record("BBBB2222", "MAPB0002")["plan"] == {"RoleB": "update"}


def test_find_plan_record_returns_none_for_unknown_hash(plan_log_audit):
    assert plan_log_audit.find_plan_record("ZZZZ9999", "MAPB0002") is None


def test_find_plan_record_rejects_a_plan_for_another_mapping_generation(plan_log_audit):
    # same config_hash, different mapping_hash — the regenerated-mapping case the gate binds
    # against: the saved plan reviewed a different resolution, so it must not surface here.
    assert plan_log_audit.find_plan_record("BBBB2222", "MAPA0001") is None


def test_find_plan_record_never_matches_a_row_without_mapping_hash(plan_log_audit):
    # a plan row with no stamped mapping_hash (externally written or hand-edited — every
    # framework release stamps it) cannot prove WHICH mapping it reviewed, so it fails
    # closed (re-plan) rather than unlocking apply.
    assert plan_log_audit.find_plan_record("CCCC3333", "MAPC0003") is None


def test_find_plan_record_with_a_none_filter_matches_no_null_row(plan_log_audit):
    # Spark SQL semantics: `mapping_hash = 'None'` never matches a NULL cell (NULL compares
    # unknown), so even a caller that somehow passed None cannot pair an unstamped filter
    # with an unstamped row. The fake mirrors that NULL-awareness instead of stringifying.
    assert plan_log_audit.find_plan_record("CCCC3333", None) is None
