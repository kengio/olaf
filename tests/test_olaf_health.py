"""OLAF.health(), OLAF.status() and OLAF.diagnose_member() — the operational self-checks: every
check independent, every degraded path a concrete verdict, and never a raise.

Ported from `olaf_test_integration.ipynb` class `RuntimeOLSFacade` (the health/status/diagnose
half, scope "mock_integration").
"""

import json
import sys

from unittest import mock

from _olaf_runtime import OLAF, Audit, Target
from _fakes import (
    CONFIG_TABLE,
    LOG_TABLE,
    MAPPING_TABLE,
    MEMBER_TABLE,
    FakeFabricClient,
    build_spark,
    drop_live_column,
    fake_role,
    member_cache_row,
    ols_env,
    ols_rows,
    ols_seed,
    sample_config_rows,
    seed_sample_members,
)

DIAG_USER_NAME = "somchai@contoso.com"
DIAG_USER_ID = "33333333-3333-3333-3333-333333333333"


def diag_config_rows(include_user=True):
    """sample_config_rows(), optionally with DIAG_USER_NAME added to SalesReaders' user members --
    diagnose_member()'s own fixture (kept separate from ols_seed's fixed sample roles)."""
    rows = sample_config_rows()
    if include_user:
        rows[0]["include_user_names"] = DIAG_USER_NAME
    return rows


def diag_authored(spark, include_user=True, with_diag_member=True):
    """setup() + the diagnose config + members, inside the caller's ols_env."""
    OLAF.setup()
    spark._store[CONFIG_TABLE] = diag_config_rows(include_user=include_user)
    seed_sample_members(spark)
    if with_diag_member:
        spark._store[MEMBER_TABLE].append(member_cache_row("User", DIAG_USER_NAME, DIAG_USER_ID))


def diag_deployed(spark, include_user=True, with_diag_member=True):
    """…and the full generate -> plan -> apply chain run."""
    diag_authored(spark, include_user=include_user, with_diag_member=with_diag_member)
    OLAF.generate()
    OLAF.plan()
    OLAF.apply()


# ---------------------------------------------------------------------------------------------
# OLAF.health()
# ---------------------------------------------------------------------------------------------


def test_health_all_checks_pass():
    """A fully seeded, freshly-applied vault is healthy on every check. Names every check and
    asserts each row's status is 'pass' -- a mutation-strong assertion (exact per-check statuses),
    not merely 'returns seven rows'."""
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        ols_seed(spark, upto="apply")
        df = OLAF.health()
    assert df.columns == ["check", "status", "detail"]
    status = {r["check"]: r["status"] for r in ols_rows(df)}
    assert status == {
        "control_tables": "pass",
        "table_location": "pass",
        "mapping_staleness": "pass",
        "dar_reachable": "pass",
        "control_data_exposure": "pass",
        "identity_preflight": "pass",
        "runtime_prerequisites": "pass",
        "last_apply_age": "pass",
        "out_of_band": "pass",
    }


def test_health_performs_one_bounded_dar_read_and_reports_spark_prerequisites():
    """Health must exercise the DAR seam and reject a Spark runtime below the supported baseline."""
    spark, client = build_spark(), FakeFabricClient([])
    spark.version = "3.4.2"
    calls = []
    real_quick = client.list_roles_quick

    def quick_read():
        calls.append("quick")
        return real_quick()

    client.list_roles_quick = quick_read
    with ols_env(spark, client):
        OLAF.setup()
        calls.clear()  # setup's safety gate legitimately reads DAR; this test measures health only.
        rows = {r["check"]: r for r in ols_rows(OLAF.health())}
    assert calls == ["quick"]
    assert rows["dar_reachable"]["status"] == "pass"
    assert rows["runtime_prerequisites"]["status"] == "fail"
    assert "Spark 3.5+" in rows["runtime_prerequisites"]["detail"]


def test_health_no_client_degrades_dar_rows_but_evaluates_the_rest():
    """No live client (Target.resolve raises -> _build_client returns None): the DAR-dependent rows
    (dar_reachable/identity_preflight/out_of_band) degrade to fail/warn WITH a detail, the
    log/mapping rows (control_tables/mapping_staleness/last_apply_age) are STILL evaluated, and a
    FRAME comes back -- health never raises."""
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        ols_seed(spark, upto="apply")
        with mock.patch.object(Target, "resolve", side_effect=SystemExit("no lakehouse attached")):
            df = OLAF.health()
    assert df.columns == ["check", "status", "detail"]
    assert df.count() == 9  # a frame, not a raise
    rows = {r["check"]: r for r in ols_rows(df)}
    assert rows["dar_reachable"]["status"] == "fail"
    assert rows["control_data_exposure"]["status"] == "fail"
    assert rows["identity_preflight"]["status"] == "warn"
    assert rows["out_of_band"]["status"] == "warn"
    for name in ("dar_reachable", "control_data_exposure", "identity_preflight", "out_of_band"):
        assert rows[name]["detail"]  # a clear detail, never blank
    assert rows["control_tables"]["status"] == "pass"
    assert rows["table_location"]["status"] == "pass"  # context-based, needs no client
    assert rows["mapping_staleness"]["status"] == "pass"
    assert rows["last_apply_age"]["status"] == "pass"


def test_health_flags_missing_and_drifted_control_tables():
    """control_tables fails when a table is absent OR missing an expected column: drop the member
    table entirely and a column from the log table -> one 'fail' row naming both problems."""
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        OLAF.setup()  # creates all four control tables
        del spark._store[MEMBER_TABLE]  # an absent control table
        drop_live_column(spark, LOG_TABLE, "config_hash")  # schema drift on another
        df = OLAF.health()
    row = next(r for r in ols_rows(df) if r["check"] == "control_tables")
    assert row["status"] == "fail"
    assert MEMBER_TABLE in row["detail"]
    assert "config_hash" in row["detail"]


def test_health_flags_stale_mapping():
    """mapping_staleness warns when the active config changed after generate (is_stale True with a
    mapping present): the deployed lock-file no longer matches the live config."""
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        OLAF.setup()
        spark._store[CONFIG_TABLE] = sample_config_rows()
        seed_sample_members(spark)
        OLAF.generate()
        spark._store[CONFIG_TABLE][0]["rls_condition"] = "region = 'south'"  # drift the config
        df = OLAF.health()
    row = next(r for r in ols_rows(df) if r["check"] == "mapping_staleness")
    assert row["status"] == "warn"
    assert "stale" in row["detail"]


def test_health_flags_missing_mapping_and_missing_apply():
    """Control tables created but nothing generated or applied yet: mapping_staleness warns (no
    mapping) and last_apply_age warns (never applied) -- both log/mapping rows reach a concrete
    verdict rather than erroring."""
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        OLAF.setup()
        df = OLAF.health()
    rows = {r["check"]: r for r in ols_rows(df)}
    assert rows["mapping_staleness"]["status"] == "warn"
    assert "no mapping" in rows["mapping_staleness"]["detail"]
    assert rows["last_apply_age"]["status"] == "warn"
    assert "no successful deployment" in rows["last_apply_age"]["detail"]


def test_health_flags_out_of_band_grants_and_stale_apply_age():
    """out_of_band warns on a live grant with no framework provenance, and last_apply_age warns
    when the newest apply is old. Seeds a healthy vault, injects a rogue live grant, and back-dates
    the apply log."""
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        ols_seed(spark, upto="apply")
        client._roles.append(fake_role("Rogue", ["/Tables/sales/orders"], ["somchai@contoso.com"]))
        for r in spark._store[LOG_TABLE]:
            if r.get("mode") == "apply":
                r["run_at"] = "2020-01-01T00:00:00+00:00"
        df = OLAF.health()
    rows = {r["check"]: r for r in ols_rows(df)}
    assert rows["out_of_band"]["status"] == "warn"
    assert "out-of-band" in rows["out_of_band"]["detail"]
    assert rows["last_apply_age"]["status"] == "warn"
    assert "day(s) ago" in rows["last_apply_age"]["detail"]


def test_health_table_location_flags_a_cross_workspace_attachment():
    """setup and every other mode refuse a lakehouse pinned from another workspace, but a
    setup that ran BEFORE that guard existed left four tables plus an audit row over there and
    nothing detected them. health() reports it -- and reports, never raises."""
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        ols_seed(spark, upto="apply")
        ctx = sys.modules["notebookutils"].runtime.context
        ctx["defaultLakehouseWorkspaceId"] = "other-ws-guid"
        ctx["defaultLakehouseWorkspaceName"] = "Finance Lakehouses"
        df = OLAF.health()
    row = next(r for r in ols_rows(df) if r["check"] == "table_location")
    assert row["status"] == "fail"
    assert "'Finance Lakehouses' (other-ws-guid)" in row["detail"]  # workspace labels, reused
    assert "'WS_Demo' (ws-guid)" in row["detail"]


def test_health_table_location_flags_an_attachment_that_moved_under_the_tables():
    """The live-forward half: the mapping stamps the workspace/lakehouse it was generated against,
    so an attachment that changed afterwards is detectable -- every OTHER check reads 'the control
    tables' as though there were only one set, when they are whatever the attached lakehouse holds."""
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        ols_seed(spark, upto="apply")
        sys.modules["notebookutils"].runtime.context["defaultLakehouseId"] = "a-different-lakehouse"
        df = OLAF.health()
    row = next(r for r in ols_rows(df) if r["check"] == "table_location")
    assert row["status"] == "fail"
    assert "lh-guid" in row["detail"]  # where the mapping says it was generated
    assert "a-different-lakehouse" in row["detail"]  # where the notebook points now


def test_health_table_location_passes_before_the_first_generate():
    """An empty mapping is not a location problem: the attachment is still checkable, and there is
    simply no stamped target to cross-check yet. Saying so beats a second warn duplicating
    mapping_staleness's 'no mapping generated yet'."""
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        OLAF.setup()  # tables exist, nothing generated
        df = OLAF.health()
    row = next(r for r in ols_rows(df) if r["check"] == "table_location")
    assert row["status"] == "pass"
    assert "no mapping generated yet" in row["detail"]
    assert {r["check"] for r in ols_rows(df)} >= {"table_location"}


def test_health_table_location_warns_off_fabric():
    """No runtime context at all -- the check cannot know, and says so rather than passing."""
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        ols_seed(spark, upto="apply")
        saved = sys.modules.pop("notebookutils")
        try:
            df = OLAF.health()
        finally:
            sys.modules["notebookutils"] = saved
    row = next(r for r in ols_rows(df) if r["check"] == "table_location")
    assert row["status"] == "warn"
    assert "off Fabric" in row["detail"]


def test_health_isolates_a_failing_check():
    """A check that raises internally must not abort the others: its row comes back 'fail' with a
    detail, and all seven checks are still reported (independence -- health never raises)."""
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        ols_seed(spark, upto="apply")
        with mock.patch.object(Audit, "is_stale", side_effect=RuntimeError("boom")):
            df = OLAF.health()
    rows = {r["check"]: r for r in ols_rows(df)}
    assert len(rows) == 9  # every check still reported -> independence
    assert rows["mapping_staleness"]["status"] == "fail"
    assert "boom" in rows["mapping_staleness"]["detail"]


def test_boundary_health_calls_bounded_list_and_reports_snapshot_and_attestation_separately():
    """Health works before setup and makes one real bounded DAR read, while keeping the
    point-in-time DAR result distinct from the operator's isolation attestation."""
    spark, client = build_spark(), FakeFabricClient([])
    real_list = client.list_roles_quick
    calls = 0

    def spy():
        nonlocal calls
        calls += 1
        return real_list()

    client.list_roles_quick = spy
    with ols_env(spark, client):
        df = OLAF.health()

    rows = {r["check"]: r for r in ols_rows(df)}
    facts = json.loads(rows["control_data_exposure"]["detail"])
    assert calls == 1
    assert rows["control_tables"]["status"] == "fail"  # genuinely pre-setup
    assert rows["dar_reachable"]["status"] == "pass"
    assert rows["control_data_exposure"]["status"] == "pass"
    assert facts["dar_snapshot_safe"] is True
    assert facts["dar_etag"] == '"fake-etag-0"'
    assert facts["workspace_isolation"] == "attested"
    assert "/files/security" in facts["reserved_paths"]


def test_boundary_health_safe_snapshot_without_attestation_is_not_reported_as_isolated():
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        OLAF._base_params["control_data_isolation_attestation"] = ""
        df = OLAF.health()

    row = next(r for r in ols_rows(df) if r["check"] == "control_data_exposure")
    facts = json.loads(row["detail"])
    assert row["status"] == "fail"
    assert facts["dar_snapshot_safe"] is True
    assert facts["workspace_isolation"] == "unknown"


def test_health_dar_probe_failure_never_passes_merely_because_a_client_resolved():
    spark, client = build_spark(), FakeFabricClient([])
    client.list_roles_quick = mock.Mock(side_effect=TimeoutError("bounded read timed out"))
    with ols_env(spark, client):
        df = OLAF.health()

    rows = {r["check"]: r for r in ols_rows(df)}
    facts = json.loads(rows["control_data_exposure"]["detail"])
    assert client.list_roles_quick.call_count == 1
    assert rows["dar_reachable"]["status"] == "fail"
    assert rows["control_data_exposure"]["status"] == "fail"
    assert facts["dar_snapshot_safe"] is False
    assert "TimeoutError" in facts["snapshot_error"]


# ---------------------------------------------------------------------------------------------
# OLAF.status()
# ---------------------------------------------------------------------------------------------

STATUS_COLUMNS = [
    "n_roles",
    "n_members",
    "last_generate",
    "last_apply",
    "last_deployment",
    "last_deployment_mode",
    "live_config_version",
    "pending_change",
]


def test_maintenance_status_returns_exact_snapshot_values():
    """A fully seeded, freshly-applied vault reports the EXACT scalar snapshot -- n_roles/n_members
    straight off the mapping, last_generate/last_apply matching the log's own newest rows for each
    mode, live_config_version off the mapping's stamped provenance, and pending_change=False
    (mapping in sync with the last apply)."""
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        diag_deployed(spark)
        df = OLAF.status()
        log_rows = [r.asDict() for r in spark.table(LOG_TABLE).collect()]
    assert df.columns == STATUS_COLUMNS
    rows = ols_rows(df)
    assert len(rows) == 1
    row = rows[0]
    # the log's run_at is a real TIMESTAMP; status() renders it through _df, which stringifies
    # every cell for display — so the expectation is the stringified newest row per mode
    expected_generate = str(max(r["run_at"] for r in log_rows if r["mode"] == "generate"))
    expected_apply = str(
        max(
            r["run_at"]
            for r in log_rows
            if r["mode"] == "apply" and r["action"] == "complete" and r["status"] == "success"
        )
    )
    assert row["n_roles"] == "2"  # SalesReaders + RawReaders
    assert row["n_members"] == "3"  # sg-readers, svc-loader, + the diag user
    assert row["last_generate"] == expected_generate
    assert row["last_apply"] == expected_apply
    assert row["last_deployment"] == expected_apply
    assert row["last_deployment_mode"] == "apply"
    assert row["live_config_version"] == "3"  # BIGINT 3, stringified by the display frame
    assert row["pending_change"] == "False"


def test_maintenance_status_empty_state_returns_zeroed_one_row_frame():
    """Nothing generated or applied yet (fresh setup only): status() still returns ONE row -- never
    an empty frame -- with zeroed counts and None for everything log/mapping-derived, and
    pending_change=False (nothing generated means nothing can be pending)."""
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        OLAF.setup()
        df = OLAF.status()
    rows = ols_rows(df)
    assert len(rows) == 1  # a 1-row frame, not empty
    assert rows[0] == {
        "n_roles": "0",
        "n_members": "0",
        "last_generate": None,
        "last_apply": None,
        "last_deployment": None,
        "last_deployment_mode": None,
        "live_config_version": None,
        "pending_change": "False",
    }


def test_maintenance_status_flags_pending_change_when_never_applied():
    """generate() ran but apply() never has: last_apply is None and pending_change is True -- an
    unapplied generation is exactly a pending change."""
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        OLAF.setup()
        spark._store[CONFIG_TABLE] = sample_config_rows()
        seed_sample_members(spark)
        OLAF.generate()
        df = OLAF.status()
    row = ols_rows(df)[0]
    assert row["last_apply"] is None
    assert row["pending_change"] == "True"


def test_maintenance_status_flags_pending_change_after_regenerate_post_apply():
    """Mapping regenerated after the last apply (rebuild=True, unchanged config -- just a fresh
    timestamp): pending_change flips True even though a prior apply IS recorded."""
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        ols_seed(spark, upto="apply")
        OLAF.generate(rebuild=True)  # re-timestamps the mapping, no re-apply
        df = OLAF.status()
    row = ols_rows(df)[0]
    assert row["last_apply"] is not None
    assert row["pending_change"] == "True"


# ---------------------------------------------------------------------------------------------
# OLAF.diagnose_member()
# ---------------------------------------------------------------------------------------------

DIAG_STEPS = ["member_in_table", "id_resolved", "in_mapping", "live_in_dar", "apply_in_sync"]
SKIPPED = "skipped — prerequisite failed"


def test_diagnose_member_all_steps_pass_when_healthy():
    """diagnose_member() walks the full chain for a member seeded in the cache, resolved, mapped
    into a role, pushed live, with the mapping in sync with the last apply -- every step is ok=True,
    in order (a mutation-strong assertion on both the step names and their order)."""
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        diag_deployed(spark)
        df = OLAF.diagnose_member(DIAG_USER_NAME)
    rows = ols_rows(df)
    assert df.columns == ["step", "ok", "detail"]
    assert [r["step"] for r in rows] == DIAG_STEPS
    assert all(r["ok"] == "True" for r in rows), rows


def test_diagnose_member_absent_short_circuits():
    """A member never seeded into onelake_security_member: step 1 fails naming the member, and every
    later step is short-circuited with a clear 'skipped' detail instead of probing a chain whose
    first link is already broken (never raises -- a frame comes back)."""
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        diag_deployed(spark, include_user=False, with_diag_member=False)
        df = OLAF.diagnose_member("nobody@contoso.com")
    rows = {r["step"]: r for r in ols_rows(df)}
    assert rows["member_in_table"]["ok"] == "False"
    assert "not found" in rows["member_in_table"]["detail"]
    for step in DIAG_STEPS[1:]:
        assert rows[step]["ok"] == "False"
        assert rows[step]["detail"] == SKIPPED


def test_diagnose_member_accepts_an_objectid_directly():
    """GUID pass-through (fix wave 1): an objectId copied straight out of who_can_access()'s
    member_id column works in diagnose_member exactly as it does in effective_access -- step 1
    reports ok=True naming the pass-through (no name lookup can match a GUID), and every later step
    runs against that id unchanged. The member row is deliberately seeded under a NAME that is not
    the GUID, proving the pass-through fires rather than an incidental name match."""
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        diag_deployed(spark)
        df = OLAF.diagnose_member(DIAG_USER_ID)
    rows = ols_rows(df)
    by_step = {r["step"]: r for r in rows}
    assert df.columns == ["step", "ok", "detail"]  # per-step contract unchanged
    assert [r["step"] for r in rows] == DIAG_STEPS
    assert by_step["member_in_table"]["ok"] == "True"
    assert "objectId-shaped" in by_step["member_in_table"]["detail"]
    assert all(r["ok"] == "True" for r in rows), rows


def test_diagnose_member_non_guid_unknown_still_reports_not_found():
    """Negative counterpart: a non-GUID name absent from the cache keeps the pre-existing
    'not found' report -- the GUID branch must not swallow the name path."""
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        diag_authored(spark, include_user=False, with_diag_member=False)
        OLAF.generate()
        df = OLAF.diagnose_member("e0000000-NOT-A-GUID")
    rows = {r["step"]: r for r in ols_rows(df)}
    assert rows["member_in_table"]["ok"] == "False"
    assert "not found" in rows["member_in_table"]["detail"]


def test_diagnose_member_unresolved_id_short_circuits():
    """A member row present in onelake_security_member but with a blank id (a bad preload, e.g. an
    empty Excel cell): step 1 passes, step 2 fails, and steps 3-5 short-circuit."""
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        diag_authored(spark, include_user=False, with_diag_member=False)
        spark._store[MEMBER_TABLE].append(member_cache_row("User", DIAG_USER_NAME, ""))
        OLAF.generate()
        df = OLAF.diagnose_member(DIAG_USER_NAME)
    rows = {r["step"]: r for r in ols_rows(df)}
    assert rows["member_in_table"]["ok"] == "True"
    assert rows["id_resolved"]["ok"] == "False"
    for step in ("in_mapping", "live_in_dar", "apply_in_sync"):
        assert rows[step]["detail"] == SKIPPED


def test_diagnose_member_not_in_mapping_short_circuits():
    """A member resolved in the cache but never placed in any config role: steps 1-2 pass, step 3
    fails (absent from every mapping row's member_*_ids), and steps 4-5 short-circuit."""
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        diag_authored(spark, include_user=False)
        OLAF.generate()
        df = OLAF.diagnose_member(DIAG_USER_NAME)
    rows = {r["step"]: r for r in ols_rows(df)}
    assert rows["member_in_table"]["ok"] == "True"
    assert rows["id_resolved"]["ok"] == "True"
    assert rows["in_mapping"]["ok"] == "False"
    for step in ("live_in_dar", "apply_in_sync"):
        assert rows[step]["detail"] == SKIPPED


def test_diagnose_member_no_client_degrades_live_step():
    """No live client resolves (off-Fabric): live_in_dar reports ok=False detail 'no live client'
    WITHOUT raising, while the rest of the chain -- including apply_in_sync, which is independent of
    step 4 -- is still evaluated normally (mirrors health()'s dar_reachable degrade)."""
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        diag_deployed(spark)
        with mock.patch.object(Target, "resolve", side_effect=SystemExit("no lakehouse attached")):
            df = OLAF.diagnose_member(DIAG_USER_NAME)
    rows = {r["step"]: r for r in ols_rows(df)}
    assert rows["in_mapping"]["ok"] == "True"
    assert rows["live_in_dar"]["ok"] == "False"
    assert rows["live_in_dar"]["detail"] == "no live client"
    assert rows["apply_in_sync"]["ok"] == "True"  # not skipped -- independent of the live-DAR check


def test_diagnose_member_not_applied_yet():
    """generate() ran (the member IS mapped) but apply() never has: live_in_dar finds no live grant
    (a resolved client with an empty list_roles()) and apply_in_sync reports no successful apply --
    concrete verdicts, neither one a short-circuit skip."""
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        diag_authored(spark)
        OLAF.generate()
        df = OLAF.diagnose_member(DIAG_USER_NAME)
    rows = {r["step"]: r for r in ols_rows(df)}
    assert rows["in_mapping"]["ok"] == "True"
    assert rows["live_in_dar"]["ok"] == "False"
    assert "not found live" in rows["live_in_dar"]["detail"]
    assert rows["apply_in_sync"]["ok"] == "False"
    assert "no successful apply" in rows["apply_in_sync"]["detail"]


def test_diagnose_member_flags_apply_sync_lag():
    """Mapping regenerated after the last apply (rebuild=True, unchanged config -- just a fresh
    timestamp): live_in_dar still finds the grant pushed by the EARLIER apply, but apply_in_sync
    flags that the mapping has since moved ahead of it."""
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        diag_deployed(spark)
        OLAF.generate(rebuild=True)  # re-timestamps the mapping, no re-apply
        df = OLAF.diagnose_member(DIAG_USER_NAME)
    rows = {r["step"]: r for r in ols_rows(df)}
    assert rows["live_in_dar"]["ok"] == "True"  # still live from the earlier apply
    assert rows["apply_in_sync"]["ok"] == "False"
    assert "newer than the last apply" in rows["apply_in_sync"]["detail"]


def test_diagnose_member_isolates_a_failing_step():
    """A step that raises internally must not abort the chain: it reports ok=False with the
    exception's message instead of propagating (mirrors health()'s per-check isolation)."""
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        diag_deployed(spark)
        with mock.patch.object(
            Audit, "last_successful_deployment", side_effect=RuntimeError("boom")
        ):
            df = OLAF.diagnose_member(DIAG_USER_NAME)
    rows = {r["step"]: r for r in ols_rows(df)}
    assert rows["apply_in_sync"]["ok"] == "False"
    assert "boom" in rows["apply_in_sync"]["detail"]


def test_health_notes_coexisting_columns_on_author_owned_tables():
    """A foreign column on config/member is supported coexistence, not a defect: the check
    stays 'pass' and the detail names the column and why it is safe."""
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        OLAF.setup()
        spark._columns[CONFIG_TABLE.lower()].append("load_ts")
        df = OLAF.health()
    row = next(r for r in ols_rows(df) if r["check"] == "control_tables")
    assert row["status"] == "pass"
    assert "coexisting" in row["detail"] and "load_ts" in row["detail"]
    assert "ignored by config_hash" in row["detail"]


def test_health_warns_on_unmanaged_columns_on_framework_owned_tables():
    """The mirror: an extra column on the mapping or the log is NOT coexistence — generate
    rewrites the mapping in full, and the log is append-only history — so the check warns."""
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        OLAF.setup()
        spark._columns[MAPPING_TABLE.lower()].append("stray_col")
        df = OLAF.health()
    row = next(r for r in ols_rows(df) if r["check"] == "control_tables")
    assert row["status"] == "warn"
    assert "stray_col" in row["detail"] and MAPPING_TABLE in row["detail"]
