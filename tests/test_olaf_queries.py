"""The OLAF facade's read surface: show/trace views and the Audit passthroughs that ride the
metaclass (grants, out_of_band, effective_access, who_can_access, drift, coverage, table_history,
config_at, at, config_diff, value_history) — every one coerced to a DataFrame.

Ported from `olaf_test_integration.ipynb` class `RuntimeOLSFacade` (the query half, scope
"mock_integration").
"""

from unittest import mock

import pytest

from _olaf_runtime import GRANT_COLUMNS, OLAF, Target, UsageError, _CONFIG_DIFF_FIELD_COLUMNS
from _fakes import (
    CONFIG_TABLE,
    GRP_READERS,
    GRP_READERS_NAME,
    LOG_TABLE,
    MAPPING_TABLE,
    MEMBER_TABLE,
    FakeFabricClient,
    build_spark,
    fake_role,
    member_cache_row,
    ols_env,
    ols_rows,
    ols_seed,
    seed_validate_row,
)

ORDERS_PATH = "/Tables/sales/orders"


# ---------------------------------------------------------------------------------------------
# show / trace — the explicit view methods
# ---------------------------------------------------------------------------------------------


def test_audit_show_returns_grant_table():
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        ols_seed(spark, upto="apply")
        df = OLAF.show(by="role", subject="Sales*")
    rows = ols_rows(df)
    assert "role_name" in df.columns
    assert any(r["role_name"] == "SalesReaders" for r in rows)
    assert "provenance" in df.columns


@pytest.mark.parametrize(
    "subject", ["/Tables/sales/orders", "sales.orders"], ids=["path", "dotted"]
)
def test_show_by_table_returns_only_the_table_that_was_asked_for(subject):
    """SalesReaders reaches BOTH /Tables/sales/leads and /Tables/sales/orders. Asking about orders
    used to select the role and then emit every scope it reaches, so `leads` rows came back from a
    query that never named leads — they read as grants on a table the caller had not asked about.

    Both spellings of a table subject are accepted, and both must narrow the same way.
    """
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        ols_seed(spark, upto="apply")
        rows = ols_rows(OLAF.show(by="table", subject=subject))
    assert rows  # the role IS selected...
    assert {r["scope_path"] for r in rows} == {"/Tables/sales/orders"}  # ...one scope, not both
    assert {r["role_name"] for r in rows} == {"SalesReaders"}


def test_show_by_table_still_spans_every_role_that_reaches_it():
    """Narrowing is per-SCOPE, not per-role: a table reached by several roles still comes back
    once per role, which is the question `by=table` exists to answer."""
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        ols_seed(spark, upto="apply")
        client._roles.append(
            fake_role("SecondReader", ["/Tables/sales/orders"], [GRP_READERS], permission="Read")
        )
        rows = ols_rows(OLAF.show(by="table", subject="sales.orders"))
    assert {r["role_name"] for r in rows} == {"SalesReaders", "SecondReader"}
    assert {r["scope_path"] for r in rows} == {"/Tables/sales/orders"}


@pytest.mark.parametrize(
    ("by", "subject", "first"),
    [
        ("table", "sales.orders", ["scope_path"]),
        ("role", "SalesReaders", ["role_name"]),
        ("member", GRP_READERS, ["member", "member_name"]),
    ],
)
def test_each_axis_leads_with_the_column_it_pivots_on(by, subject, first):
    """All three axes return the same nine columns, so a fixed order buried the thing you searched
    by mid-row and made the three frames indistinguishable at a glance. Only the ORDER changes —
    every axis still returns the full grant table."""
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        ols_seed(spark, upto="apply")
        columns = OLAF.show(by=by, subject=subject).columns
    assert columns[: len(first)] == first
    assert set(columns) == set(GRANT_COLUMNS)
    assert columns == first + [c for c in GRANT_COLUMNS if c not in first]


def test_show_by_role_returns_the_whole_role():
    """`by=role` narrows nothing — asking about a role means asking for all of it, and narrowing
    would leave no way to see one in full."""
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        ols_seed(spark, upto="apply")
        rows = ols_rows(OLAF.show(by="role", subject="SalesReaders"))
    assert {r["scope_path"] for r in rows} == {"/Tables/sales/leads", "/Tables/sales/orders"}


@pytest.mark.parametrize("subject", [GRP_READERS, GRP_READERS_NAME], ids=["objectId", "name"])
def test_show_by_member_accepts_the_id_or_the_display_name(subject):
    """`by=member: objectId/name` is the documented contract, and the name half did not work: the
    live DAR exposes objectIds, the predicate matched only those, and a name came back 0 matches —
    while the rows this mode returns carry that same name in `member_name`, so it is the obvious
    thing to paste back in. The name is resolved from onelake_security_member."""
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        ols_seed(spark, upto="apply")
        rows = ols_rows(OLAF.show(by="member", subject=subject))
    assert [(r["role_name"], r["scope_path"], r["permission"]) for r in rows] == [
        ("SalesReaders", "/Tables/sales/leads", "Read"),
        ("SalesReaders", "/Tables/sales/orders", "Read"),
        ("RawReaders", "/Files/raw/region_a", "Read"),
    ]


def test_show_by_member_returns_only_that_members_grants():
    """Asking about one person used to return their colleagues' rows too — every member of every
    role that happened to include them. "Which roles is X in, and what can X read" is the question;
    who else is in those roles is not part of the answer."""
    spark, client = build_spark(), FakeFabricClient([])
    other = "22222222-2222-2222-2222-222222222222"
    with ols_env(spark, client):
        ols_seed(spark, upto="apply")
        client._roles.append(
            fake_role("SharedRole", ["/Tables/sales/orders"], [GRP_READERS, other])
        )
        rows = ols_rows(OLAF.show(by="member", subject=GRP_READERS))
    assert other not in {r["member"] for r in rows}
    assert "SharedRole" in {r["role_name"] for r in rows}  # ...the role still matches


def test_a_member_whose_name_is_a_prefix_of_another_is_not_over_matched():
    """`sg-sales` is a prefix of `sg-sales-managers`, and both sit in the
    same role — so a substring match returned the manager's rows from a query that named neither
    the manager nor their access."""
    spark, client = build_spark(), FakeFabricClient([])
    manager_id = "44444444-4444-4444-4444-444444444444"
    with ols_env(spark, client):
        ols_seed(spark, upto="apply")
        spark._store[MEMBER_TABLE].append(
            member_cache_row("Group", GRP_READERS_NAME + "-managers", manager_id)
        )
        client._roles.append(
            fake_role("SharedRole", ["/Tables/sales/orders"], [GRP_READERS, manager_id])
        )
        rows = ols_rows(OLAF.show(by="member", subject=GRP_READERS_NAME))
    assert manager_id not in {r["member"] for r in rows}

    with ols_env(spark, client):  # ...and the wildcard still reaches both, when asked
        wide = ols_rows(OLAF.show(by="member", subject=GRP_READERS_NAME + "*"))
    assert manager_id in {r["member"] for r in wide}


def test_a_zero_match_says_how_to_widen_it(capsys):
    """Exact matching surprises exactly here, and this is the line the caller is already reading."""
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        ols_seed(spark, upto="apply")
        capsys.readouterr()
        OLAF.show(by="member", subject=GRP_READERS_NAME[:4])
    assert "0 match(es) — matched exactly; add * for a partial match" in capsys.readouterr().out


def test_an_unnamed_member_still_matches_on_its_objectId():
    """The display lookup is a widening, never a gate: a live objectId with no row in the member
    cache — an out-of-band grant, the case this mode exists to surface — must still be findable."""
    spark, client = build_spark(), FakeFabricClient([])
    stranger = "33333333-3333-3333-3333-333333333333"
    with ols_env(spark, client):
        ols_seed(spark, upto="apply")
        client._roles.append(fake_role("Unmanaged", ["/Tables/sales/orders"], [stranger]))
        rows = ols_rows(OLAF.show(by="member", subject=stranger))
    assert {r["role_name"] for r in rows} == {"Unmanaged"}
    assert rows[0]["provenance"].startswith("out-of-band")


def test_a_folder_scope_is_not_mangled_into_a_dotted_candidate():
    """`ScopePath.to_table` leaves a non-table scope alone. The inline conversion this replaced
    turned `/Files/raw/region_a` into `.Files.raw.region_a`, a spelling nobody would ask for and
    a substring match nobody intended."""
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        ols_seed(spark, upto="apply")
        rows = ols_rows(OLAF.show(by="table", subject="/Files/raw/region_a"))
    assert {r["scope_path"] for r in rows} == {"/Files/raw/region_a"}
    assert {r["role_name"] for r in rows} == {"RawReaders"}


def test_audit_show_no_match_returns_summary():
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        ols_seed(spark, upto="apply")
        df = OLAF.show(by="role", subject="NoSuchRoleZZZ")
    rows = ols_rows(df)
    assert len(rows) == 1
    assert rows[0]["matches"] == "0"
    assert "subject" in df.columns


def test_audit_trace_returns_snapshot_frame():
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        ols_seed(spark, upto="apply")
        df = OLAF.trace()
    rows = ols_rows(df)
    assert (rows[0]["mode"], rows[0]["status"]) == ("trace", "success")
    for col in (
        "live_role_count",
        "live_grant_count",
        "policy_checked",
        "policy_mismatch",
        "in_sync",
        "is_stale",
    ):
        # _trace_view has its own hardcoded column list. Deleting a key from it drops the key
        # from every trace frame silently — the suite stayed green at 100% when that was tried.
        assert col in df.columns


# ---------------------------------------------------------------------------------------------
# The Audit passthroughs — a DataFrame passes straight through; scalars/dicts/dataclasses/None
# are coerced into one.
# ---------------------------------------------------------------------------------------------


def test_audit_grants_passthrough_returns_frame():
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        ols_seed(spark, upto="apply")
        df = OLAF.grants(role="SalesReaders")
    assert hasattr(df, "collect")  # a DataFrame passes straight through
    assert "role_name" in df.columns


def test_audit_scalar_dict_dataclass_and_none_passthroughs():
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        ols_seed(spark, upto="apply")
        stale = OLAF.is_stale()  # bool -> wrapped scalar
        chain = OLAF.verify_chain()  # ChainStatus dataclass -> flattened
        report = OLAF.report()  # dict -> copied row
        missing = OLAF.provenance("NoSuchRole", "no.scope")  # None -> value=None
    assert ols_rows(stale)[0]["value"] == "False"  # not stale after generate+apply
    assert "ok" in chain.columns
    assert "live_role_count" in report.columns
    assert ols_rows(missing)[0]["value"] is None


def test_out_of_band_passthrough_binds_a_live_client():
    """out_of_band rides the metaclass passthrough: OLAF._trail() lazily binds the live
    FabricClient (Target.resolve -> FabricClient, both supplied by ols_env's fakes), so the util
    reads the seeded live DAR. mem-oob is live but not established -> the one out-of-band row."""
    client = FakeFabricClient(roles=[fake_role("R", ["s"], ["mem-established", "mem-oob"])])
    spark = build_spark()
    spark._store[LOG_TABLE] = [
        seed_validate_row(
            "R",
            "s",
            "mem-established",
            run_at="2026-07-13T01",
            run_by="alice@example.com",
            config_version="1",
            mode="apply",
        )
    ]
    spark._store[MEMBER_TABLE] = []  # mem-oob absent from the cache -> member_name falls back
    with ols_env(spark, client):
        df = OLAF.out_of_band()
    rows = ols_rows(df)
    assert [r["member_id"] for r in rows] == ["mem-oob"]  # only the un-established grant
    assert rows[0]["member_name"] == "mem-oob"  # not in cache -> id surfaces
    assert df.columns == ["role_name", "scope_path", "member_id", "member_name"]


def test_out_of_band_raises_when_target_unresolvable():
    """When target resolution raises (off-Fabric / no attached lakehouse), _build_client swallows
    it and _trail() binds client=None, so the live-DAR util raises its clear 'needs a FabricClient'
    error (UsageError) rather than a raw resolution traceback. Pins the SystemExit catch too."""
    spark = build_spark()
    with ols_env(spark):
        with mock.patch.object(Target, "resolve", side_effect=SystemExit("no lakehouse attached")):
            with pytest.raises(UsageError) as excinfo:
                OLAF.out_of_band()
    assert "FabricClient" in str(excinfo.value)


def test_out_of_band_resolves_member_name_with_id_fallback():
    """member_name (Task 5): out_of_band() resolves member_id -> member_name via the member cache
    table (Audit._member_names(), the SAME id->name lookup who_can_access uses) -- an out-of-band
    member usually isn't in the table, so an id absent from it falls back to surfacing the id
    itself, never erroring (the DAR is a live fact)."""
    known_id = "66666666-6666-6666-6666-666666666666"
    unknown_id = "77777777-7777-7777-7777-777777777777"
    client = FakeFabricClient(roles=[fake_role("R", ["s"], [known_id, unknown_id])])
    spark = build_spark()
    spark._store[LOG_TABLE] = []  # no established grants -> both members are out-of-band
    spark._store[MEMBER_TABLE] = [member_cache_row("User", "Somchai P.", known_id)]
    with ols_env(spark, client):
        df = OLAF.out_of_band()
    by_member = {r["member_id"]: r for r in ols_rows(df)}
    assert by_member[known_id]["member_name"] == "Somchai P."  # resolved from cache
    assert by_member[unknown_id]["member_name"] == unknown_id  # not cached -> id surfaces
    assert df.columns == ["role_name", "scope_path", "member_id", "member_name"]


def test_effective_access_open_role_nullifies_restricted_access():
    """effective_access() rides the OLAF passthrough: two roles reach sales.orders for
    somchai@contoso.com -- APACReaders (RLS region = 'APAC', CLS restricted to order_id/region) and
    OpenReaders (fully unrestricted). Most-permissive-wins (rule C8's 'unrestricted nullifies
    filter'): the open role nullifies APACReaders' row filter AND column restriction, so the
    effective row shows full rows and all columns, and lists BOTH roles as granting_role."""
    member_name = "somchai@contoso.com"
    member_id = "44444444-4444-4444-4444-444444444444"
    client = FakeFabricClient(
        roles=[
            fake_role(
                "APACReaders",
                [ORDERS_PATH],
                [member_id],
                rls={ORDERS_PATH: "region = 'APAC'"},
                visible_columns={ORDERS_PATH: ["order_id", "region"]},
            ),
            fake_role("OpenReaders", [ORDERS_PATH], [member_id]),
        ]
    )
    spark = build_spark()
    spark._store[MEMBER_TABLE] = [member_cache_row("User", member_name, member_id)]
    with ols_env(spark, client):
        df = OLAF.effective_access(member=member_name, table="sales.orders", engine="spark")
    got = ols_rows(df)
    by_role = {r["role_name"]: r for r in got if r["role_name"] is not None}
    assert by_role["APACReaders"]["rls_condition"] == "region = 'APAC'"
    assert by_role["APACReaders"]["visible_columns"] == "order_id;region"
    assert by_role["OpenReaders"]["rls_condition"] is None
    assert by_role["OpenReaders"]["visible_columns"] is None
    effective = next(r for r in got if r["effective"] == "True")
    assert effective["rls_condition"] is None  # open role nullifies the APAC filter
    assert effective["visible_columns"] is None  # open role nullifies the CLS restriction
    assert effective["granting_role"] == "APACReaders;OpenReaders"


def test_effective_access_name_resolves_to_same_frame_as_guid():
    """effective_access() accepts a member name/UPN (Task 4): resolved via _resolve_member BEFORE
    matching the live DAR. A member-table row maps somchai@contoso.com -> a GUID, and the DAR grants
    that SAME GUID on sales.orders -- calling by name returns the IDENTICAL net-access frame as
    calling by the GUID directly (GUID pass-through is unchanged)."""
    member_name = "somchai@contoso.com"
    member_id = "55555555-5555-5555-5555-555555555555"
    client = FakeFabricClient(roles=[fake_role("SalesReaders", [ORDERS_PATH], [member_id])])
    spark = build_spark()
    spark._store[MEMBER_TABLE] = [member_cache_row("User", member_name, member_id)]
    with ols_env(spark, client):
        by_name = ols_rows(
            OLAF.effective_access(member=member_name, table="sales.orders", engine="spark")
        )
        by_guid = ols_rows(
            OLAF.effective_access(member=member_id, table="sales.orders", engine="spark")
        )
    assert by_name == by_guid
    assert by_name[0]["role_name"] == "SalesReaders"  # sanity: not trivially empty


def test_who_can_access_two_members_via_two_roles():
    """who_can_access() rides the OLAF passthrough: two DIFFERENT members reach sales.orders via
    two DIFFERENT roles. Mutation-strong: asserts the exact member/role pairing (not just row
    count), the resolved member_name for an id present in the member cache, and the raw-id fallback
    for an id absent from it."""
    somchai = "somchai@contoso.com"
    alice = "alice@contoso.com"
    client = FakeFabricClient(
        roles=[
            fake_role(
                "APACReaders",
                [ORDERS_PATH],
                [somchai],
                rls={ORDERS_PATH: "region = 'APAC'"},
                visible_columns={ORDERS_PATH: ["order_id", "region"]},
            ),
            fake_role("OpenReaders", [ORDERS_PATH], [alice]),
        ]
    )
    spark = build_spark()
    spark._store[MEMBER_TABLE] = [member_cache_row("User", "Somchai P.", somchai)]
    with ols_env(spark, client):
        df = OLAF.who_can_access(table="sales.orders")
    got = ols_rows(df)
    assert len(got) == 2
    by_member = {r["member_id"]: r for r in got}
    assert by_member[somchai]["via_role"] == "APACReaders"
    assert by_member[somchai]["member_name"] == "Somchai P."  # resolved from cache
    assert by_member[somchai]["permission"] == "Read"
    assert by_member[somchai]["rls_cls_summary"] == "rows: region = 'APAC'; cols: order_id;region"
    assert by_member[alice]["via_role"] == "OpenReaders"
    assert by_member[alice]["member_name"] == alice  # not in cache -> id surfaces
    assert by_member[alice]["rls_cls_summary"] == "unrestricted"


def test_audit_drift_passthrough_returns_categorized_rows():
    """drift() rides the OLAF metaclass passthrough: seeds ONE grant per category --
    FrameworkRole/scope-f/mem-f is live AND established via an apply-mode log row (framework);
    OOBRole/scope-o/mem-o is live but never logged (out_of_band); MissingRole/scope-m/mem-m sits in
    the mapping lock-file but is entirely absent from the live DAR (missing). Mutation-strong:
    asserts the category keyed by (role_name, scope), and pins the detail + column shape too.
    member_name: mem-f is seeded into the member cache while mem-o/mem-m are NOT (id fallback) --
    the SAME names.get(...) lookup is proven to fire from both the live-grant loop and the
    desired-grant loop. member_id rides ALONGSIDE member_name, appended at the END of the frame."""
    client = FakeFabricClient(
        roles=[
            fake_role("FrameworkRole", ["scope-f"], ["mem-f"]),
            fake_role("OOBRole", ["scope-o"], ["mem-o"]),
        ]
    )
    spark = build_spark()
    spark._store[LOG_TABLE] = [
        seed_validate_row(
            "FrameworkRole",
            "scope-f",
            "mem-f",
            run_at="2026-07-13T01",
            run_by="alice@example.com",
            config_version="1",
            mode="apply",
        )
    ]
    provenance = {
        "config_hash": "fixture-config",
        "config_version": 1,
        "framework_version": "0.1.0",
        "generated_at": "2026-07-13T00",
    }
    spark._store[MAPPING_TABLE] = [
        {
            "role_name": "FrameworkRole",
            "scope_path": "scope-f",
            "member_group_ids": "mem-f",
            **provenance,
        },
        {
            "role_name": "MissingRole",
            "scope_path": "scope-m",
            "member_group_ids": "mem-m",
            **provenance,
        },
    ]
    spark._store[MEMBER_TABLE] = [member_cache_row("User", "Frank Framework", "mem-f")]
    with ols_env(spark, client):
        df = OLAF.drift()
    assert df.columns == [
        "role_name",
        "scope_path",
        "category",
        "detail",
        "member_id",
        "member_name",
    ]
    rows = ols_rows(df)
    got = {(r["role_name"], r["scope_path"]): r["category"] for r in rows}
    assert got == {
        ("FrameworkRole", "scope-f"): "framework",
        ("OOBRole", "scope-o"): "out_of_band",
        ("MissingRole", "scope-m"): "missing",
    }
    names = {(r["role_name"], r["scope_path"]): r["member_name"] for r in rows}
    assert names[("FrameworkRole", "scope-f")] == "Frank Framework"  # resolved from cache
    assert names[("OOBRole", "scope-o")] == "mem-o"  # not cached -> id surfaces
    ids = {(r["role_name"], r["scope_path"]): r["member_id"] for r in rows}
    assert ids == {
        ("FrameworkRole", "scope-f"): "mem-f",
        ("OOBRole", "scope-o"): "mem-o",
        ("MissingRole", "scope-m"): "mem-m",
    }
    # the PAIR is what makes the fallback detectable: resolved -> id != name, fallback -> equal
    assert ids[("FrameworkRole", "scope-f")] != names[("FrameworkRole", "scope-f")]
    assert ids[("OOBRole", "scope-o")] == names[("OOBRole", "scope-o")]
    assert names[("MissingRole", "scope-m")] == "mem-m"  # not cached -> id surfaces
    details = {(r["role_name"], r["scope_path"]): r["detail"] for r in rows}
    assert "mem-f" in details[("FrameworkRole", "scope-f")]
    assert "mem-o" in details[("OOBRole", "scope-o")]
    assert "mem-m" in details[("MissingRole", "scope-m")]


def test_audit_coverage_passthrough_returns_protected_surface():
    """coverage() rides the OLAF metaclass passthrough: a seeded mapping over the sample catalog
    (sales.orders protected by one RLS role; every other real-catalog table a gap) comes back as
    the protected-surface frame -- exercised THROUGH the facade, not the bare Audit method."""
    spark, client = build_spark(), FakeFabricClient([])
    spark._store[MAPPING_TABLE] = [
        {
            "role_name": "SalesReaders",
            "scope_path": ORDERS_PATH,
            "scope_type": "Table",
            "rls_condition": "region = 'north'",
            "visible_columns": None,
            "config_hash": "fixture-config",
            "config_version": 1,
            "framework_version": "0.1.0",
            "generated_at": "2026-07-13T00",
        }
    ]
    with ols_env(spark, client):
        df = OLAF.coverage()
    assert df.columns == ["table", "protected", "roles_count", "has_rls", "has_cls"]
    by_table = {r["table"]: r for r in ols_rows(df)}
    assert by_table["sales.orders"]["protected"] == "True"
    assert by_table["sales.orders"]["has_rls"] == "True"
    assert by_table["hr.payroll"]["protected"] == "False"  # a real-catalog gap


def test_audit_table_history_passthrough_returns_readable_history():
    """table_history() rides the passthrough: a seeded DESCRIBE HISTORY comes back as the
    readable version/timestamp/user/operation/rows projection, through the facade."""
    spark, client = build_spark(), FakeFabricClient([])
    spark._history_rows = [
        {
            "version": 5,
            "timestamp": "2026-07-15T08:00:00+00:00",
            "userName": "alice@x.com",
            "operation": "WRITE",
            "operationParameters": {},
            "operationMetrics": {"numOutputRows": "9"},
        }
    ]
    with ols_env(spark, client):
        df = OLAF.table_history("config")
    assert df.columns == ["version", "timestamp", "user", "operation", "rows"]
    row = ols_rows(df)[0]
    assert (row["version"], row["user"], row["rows"]) == ("5", "alice@x.com", "9")


def test_audit_config_at_passthrough_emits_time_travel_sql():
    """config_at() rides the passthrough: OLAF.config_at(version=...) reaches the bound Audit and
    emits the VERSION AS OF time-travel read (asserted via the fake's spark-level _last_sql --
    FakeSpark can't actually time-travel)."""
    spark = build_spark()
    with ols_env(spark):
        df = OLAF.config_at(version=5)
    assert hasattr(df, "collect")  # a DataFrame comes back through the facade
    assert spark._last_sql == f"SELECT * FROM {CONFIG_TABLE} VERSION AS OF 5"


def test_audit_at_passthrough_emits_time_travel_sql():
    """at() rides the passthrough: OLAF.at("mapping", version=...) resolves the mapping table and
    emits its VERSION AS OF read through the facade."""
    spark = build_spark()
    with ols_env(spark):
        df = OLAF.at("mapping", version=2)
    assert hasattr(df, "collect")
    assert spark._last_sql == f"SELECT * FROM {MAPPING_TABLE} VERSION AS OF 2"


def test_audit_config_diff_passthrough_returns_typed_frame():
    """config_diff() rides the passthrough and runs end-to-end through the facade: FakeSpark's
    time-travel reads are empty, so the diff is the empty-but-typed frame (its 6 columns,
    scope_key included) -- the documented no-change result, produced via OLAF."""
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        df = OLAF.config_diff(1, 2)
    assert df.columns == ["change_type", "role_name", "scope_key", "field", "old", "new"]
    assert df.count() == 0


def test_audit_value_history_passthrough_returns_typed_frame():
    """value_history() rides the passthrough and runs end-to-end through the facade: with
    FakeSpark's empty time-travel reads the subject matches nothing, so the result is the
    empty-but-typed frame (config_version/role_name/scope_key/.../changed/window_truncated),
    via OLAF."""
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        df = OLAF.value_history(subject="SalesReaders")
    assert df.columns == [
        "config_version",
        "role_name",
        "scope_key",
    ] + _CONFIG_DIFF_FIELD_COLUMNS + ["changed", "window_truncated"]
    assert df.count() == 0
