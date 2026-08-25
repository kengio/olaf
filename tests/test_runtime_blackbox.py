"""The runtime driven BLACK-BOX: `run_runtime_blackbox` execs the notebook's ▶️ Run dispatch cell
in a namespace seeded with the traced runtime globals, so every mode's envelope contract, guard and
error path is exercised through the real entrypoints.

Ported from `olaf_test_integration.ipynb` classes `RuntimeBlackBoxLifecycle`,
`RuntimeBlackBoxGuards`, `RuntimeSetupLakehouseAssertion`, `RuntimeRunByMemberLabelWiring`,
`RuntimeBlackBoxTrace`, `RuntimeBlackBoxErrorResilience`, `RuntimeLeanPayloadOnRaise`,
`RuntimeEntrypointDirect` and `RuntimeBlackBoxValidate` (scope "mock_integration").
"""

import json
import sys

import pytest

import _olaf_runtime
from _olaf_runtime import CONFIG_AUTHOR_COLUMNS, OLAF, run_and_exit, run_mode
from _fakes import (
    CONFIG_TABLE,
    LOG_TABLE,
    MAPPING_TABLE,
    MEMBER_TABLE,
    SAMPLE_COLUMNS,
    SAMPLE_SCHEMAS_TABLES,
    SVC_LOADER,
    SVC_LOADER_NAME,
    FakeFabricClient,
    FakeSpark,
    _RT_DEFAULT_CONTEXT,
    build_spark,
    carve_config_row,
    fake_role,
    make_dep,
    run_generate,
    run_runtime_blackbox,
    sample_config_rows,
    seed_sample_members,
    seed_validate_row,
)

CONTROL_TABLES = [CONFIG_TABLE, MAPPING_TABLE, LOG_TABLE, MEMBER_TABLE]


def seeded_workspace():
    """A runtime-set-up workspace with the short config authored and members seeded — the
    starting point for every mode driven through the black box."""
    spark, client = build_spark(), FakeFabricClient([])
    run_runtime_blackbox("setup", spark)
    spark._store[CONFIG_TABLE] = sample_config_rows()
    seed_sample_members(spark)  # No-Graph gate: member cache must hold every config member
    return spark, client


# ---------------------------------------------------------------------------------------------
# RuntimeBlackBoxLifecycle — every mode's envelope contract + the notebook.exit round-trip
# ---------------------------------------------------------------------------------------------


def test_all_modes_through_the_runtime():
    spark, client = build_spark(), FakeFabricClient([])

    o = run_runtime_blackbox("setup", spark)
    res, exitv = o.envelope, o.exit_value
    assert (res["mode"], res["status"]) == ("setup", "success")
    assert sorted(res["data"]["created"]) == sorted(CONTROL_TABLES)
    assert exitv["mode"] == "setup"  # notebook.exit received the same envelope JSON
    assert "config_hash" in res

    spark._store[CONFIG_TABLE] = sample_config_rows()  # author the short config
    seed_sample_members(spark)  # No-Graph gate: member cache must hold every config member

    res = run_runtime_blackbox("generate", spark, client=client).envelope
    assert (res["mode"], res["status"]) == ("generate", "success")
    assert res["changed"]
    assert res["data"]["grants"] == 3
    assert "SalesReaders" in res["data"]["summary"]

    res = run_runtime_blackbox("plan", spark, client=client).envelope
    assert res["status"] == "success"
    assert res["changed"]
    assert res["data"]["plan"] == {"SalesReaders": "create", "RawReaders": "create"}

    res = run_runtime_blackbox("apply", spark, client=client).envelope
    assert res["data"]["push_status"] == 200
    assert res["data"]["roles_written"] == 2
    assert res["data"]["request"] == "config_payload"
    assert res["data"]["drift_omission_candidates"] == []
    assert res["data"]["post_state_review_required"] is True

    run_runtime_blackbox("plan", spark, client=client)  # re-plan against populated live

    res = run_runtime_blackbox(
        "show", spark, client=client, params={"by": "role", "subject": "Sales*"}
    ).envelope
    assert res["data"]["by"] == "role"
    assert res["data"]["roles"] == ["SalesReaders"]
    assert "grants" in res["data"]


def test_idempotent_generate_status_skipped():
    # A second generate on unchanged config -> changed=False -> envelope status="skipped"
    # (native success: notebook.exit still carries the envelope).
    spark, client = seeded_workspace()
    run_runtime_blackbox("generate", spark, client=client)  # first build
    o = run_runtime_blackbox("generate", spark, client=client)  # unchanged -> skip
    assert (o.envelope["status"], o.envelope["changed"]) == ("skipped", False)
    assert o.envelope["data"]["grants"] == 3  # existing mapping count, not a rebuild
    assert o.exit_value["status"] == "skipped"  # skipped still exits natively
    assert o.raised is None


def test_no_drift_plan_status_skipped():
    # A plan whose desired == live -> changed=False -> envelope status="skipped".
    spark, client = seeded_workspace()
    run_runtime_blackbox("generate", spark, client=client)
    run_runtime_blackbox("plan", spark, client=client)
    run_runtime_blackbox("apply", spark, client=client)  # live now == desired
    o = run_runtime_blackbox("plan", spark, client=client)  # no drift
    assert (o.envelope["status"], o.envelope["changed"]) == ("skipped", False)
    assert o.envelope["data"]["counts"] == {"no_change": 2}
    assert o.envelope["data"]["dar_snapshot_safe"] is True
    assert o.envelope["data"]["workspace_isolation"] == "attested"
    assert o.envelope["data"]["dar_etag"] == '"fake-etag-1"'
    assert o.exit_value["status"] == "skipped"


def test_unexpected_exception_status_error():
    # A non-SystemExit failure inside a mode -> envelope status="error", the native-failure raise
    # carries the envelope JSON (o.raised, no notebook.exit), and a best-effort audit fail-row is
    # written on the error path.
    spark, _client = seeded_workspace()

    class _BoomClient(FakeFabricClient):
        def list_roles(self):
            raise RuntimeError("live DAR unreachable")

    before = len(spark._store[LOG_TABLE])
    o = run_runtime_blackbox(
        "show", spark, client=_BoomClient([]), params={"by": "role", "subject": "SalesReaders"}
    )
    assert o.envelope["status"] == "error"
    assert not o.envelope["changed"]
    assert "RuntimeError" in o.envelope["error"]
    assert o.raised is not None  # native failure: raised carries the envelope JSON
    assert '"status": "error"' in o.raised
    assert o.exit_value is None  # error never reaches notebook.exit
    failed = [r for r in spark._store[LOG_TABLE][before:] if r.get("status") == "failed"]
    assert failed  # audit.fail_row written on the error path


def test_setup_is_blocked_without_notebookutils_before_any_schema_write():
    # No notebookutils means no attached DAR target. Setup is sensitive too, so it cannot use the
    # old schema-only bootstrap fallback; it must return the normal blocked envelope before DDL.
    spark = build_spark()
    o = run_runtime_blackbox("setup", spark, with_notebookutils=False)
    assert o.envelope["mode"] == "setup"
    assert o.envelope["status"] == "blocked"
    assert "DAR control-data boundary" in o.envelope["error"]
    assert spark._store == {}
    assert o.exit_value is None


# ---------------------------------------------------------------------------------------------
# RuntimeBlackBoxGuards — fail-fast dispatch guards. Blocked guards carry the envelope JSON in
# o.raised; the pre-dispatch guards carry a plain reason.
# ---------------------------------------------------------------------------------------------


def test_unknown_mode_fails_fast():
    o = run_runtime_blackbox("nonsense", build_spark())
    assert o.raised is not None
    # the Run cell passes allowed=KNOWN_MODES, and an unknown mode is outside that set, so
    # run_and_exit's allowed-gate still rejects it BEFORE run_mode with the "not permitted"
    # message (run_mode's own "unknown mode" guard is covered by a direct call below).
    assert "not permitted" in o.raised


@pytest.mark.parametrize("value", ["dev", "prod", "uat-2", "feature_branch_7"])
def test_an_ordinary_env_label_runs(value):
    """The guard must be invisible to anyone naming an environment the way people do."""
    o = run_runtime_blackbox(
        "setup", build_spark(), params={"lakehouse_name": "LH_Demo", "env": value}
    )
    assert o.envelope["status"] == "success"
    assert o.envelope["params"]["env"] == value  # echoed as written, never normalised


@pytest.mark.parametrize(
    "value",
    ["dev' OR '1'='1", "dev'; DROP TABLE olaf.onelake_security_log; --", "dev prod", "A" * 65],
)
def test_an_env_that_is_not_a_label_is_refused_before_anything_is_built(value):
    """`env` is the one operator-supplied string that reaches Spark SQL as a WHERE literal (the
    log reads in Log.find_plan_record and Log.grant_provenance build their clause by
    interpolation), so its SHAPE is a boundary rather than a formatting preference.

    Refused, never repaired — the rule the boolean parameters and `verbosity` already follow: a
    sanitised value would run the operator's deploy under an environment label they never typed,
    and stamp that label on every audit row. It refuses beside the other parameter
    guards — INSIDE the envelope boundary, so the refusal is the same structured 'blocked'
    envelope every sibling parameter guard produces (no log context exists either way — no
    pre-target guard writes an audit row), and the pipeline-facing exit payload is the
    compact JSON envelope, parseable by automation."""
    o = run_runtime_blackbox(
        "setup", build_spark(), params={"lakehouse_name": "LH_Demo", "env": value}
    )
    assert o.envelope["status"] == "blocked"
    assert "env must be" in o.envelope["error"]
    assert o.envelope["params"]["env"] == ("" if value is None else str(value))  # echoed raw
    assert o.raised is not None  # native-failure: the activity FAILS
    lean = json.loads(o.raised)  # ...and the exit payload is parseable JSON
    assert lean["status"] == "blocked" and "env must be" in lean["error"]


def test_the_env_guard_and_the_facade_refuse_by_the_same_rule():
    """One rule, two boundaries. run_mode raises SystemExit and OLAF.configure raises UsageError —
    different currencies on purpose — but a value either accepts the other must accept, or a
    pipeline and an interactive session disagree about what an environment may be called."""
    from _olaf_runtime import Parse

    assert Parse.env_param("dev")[1] is None
    assert Parse.env_param("dev' OR '1'='1")[1] is not None
    # env is OPTIONAL — blank means "no environment label", and a missing Base parameter
    # (None) says the same thing, so neither is a refusal.
    assert Parse.env_param("")[1] is None
    assert Parse.env_param(None) == ("", None)
    assert Parse.env_param("   ")[1] is not None  # whitespace is a broken label, not a blank


def test_missing_tenant_for_mutating_mode():
    o = run_runtime_blackbox(
        "plan", build_spark(), client=FakeFabricClient([]), params={"tenant_id": ""}
    )
    assert o.raised is not None
    assert "tenant_id is required" in o.raised


def test_unresolvable_target_fails():
    # empty context, no ids
    o = run_runtime_blackbox("plan", build_spark(), client=FakeFabricClient([]), context={})
    assert o.raised is not None
    assert "no lakehouse attached" in o.raised


def test_show_invalid_axis_fails():
    spark, client = build_spark(), FakeFabricClient([])
    make_dep(spark, client, "setup").setup()
    spark._store[CONFIG_TABLE] = sample_config_rows()
    o = run_runtime_blackbox("show", spark, client=client, params={"by": "column", "subject": "x"})
    assert o.envelope["status"] == "blocked"  # a guard refused inside dispatch
    assert o.raised is not None
    assert "show requires by" in o.raised


# ---------------------------------------------------------------------------------------------
# RuntimeSetupLakehouseAssertion — mode=setup declares its lakehouse by NAME, and the name is an
# ASSERTION, never a selector: setup writes through two-part `olaf.…` names, which always resolve
# against the ATTACHED lakehouse, so the parameter cannot point the DDL anywhere else — it exists
# to be checked. Without it, running the documented FIRST step with a lakehouse attached from
# another workspace silently lays four control tables plus an audit row into that other workspace
# and says nothing. The check lives in run_mode beside the context read (setup stays OUT of
# NEEDS_TARGET, see test_setup_stays_outside_needs_target), reads the runtime context directly, and
# is skipped off-Fabric where there is no attachment to be wrong about.
# ---------------------------------------------------------------------------------------------


def test_declared_name_matching_in_another_letter_case_proceeds():
    # Fabric display names are matched case-insensitively everywhere else in this framework
    # (Target._single_named), so an operator typing the name by hand must not be refused over
    # capitalisation alone.
    o = run_runtime_blackbox("setup", build_spark(), params={"lakehouse_name": "lh_dEMO"})
    assert o.envelope["status"] == "success"
    assert o.envelope["params"]["lakehouse_name"] == "lh_dEMO"  # echoed for setup
    assert sorted(o.envelope["data"]["created"]) == sorted(CONTROL_TABLES)


def test_a_different_declared_name_is_refused_naming_both_sides():
    spark = build_spark()
    o = run_runtime_blackbox("setup", spark, params={"lakehouse_name": "LH_Other"})
    assert o.envelope["status"] == "blocked"
    assert "LH_Other" in o.envelope["error"]  # what the operator declared
    assert "LH_Demo" in o.envelope["error"]  # what is actually attached
    assert spark._store == {}  # no control table created


def test_a_missing_declared_name_is_refused():
    # Required, not optional (symmetry with config.lakehouse_name, which generate also demands):
    # an omitted assertion asserts nothing, so it cannot deliver the certainty it exists for.
    spark = build_spark()
    o = run_runtime_blackbox("setup", spark, params={"lakehouse_name": "   "})
    assert o.envelope["status"] == "blocked"
    assert "lakehouse_name is required" in o.envelope["error"]
    assert getattr(spark, "_last_sql", None) is None


def test_cross_workspace_attachment_is_refused_before_any_ddl():
    # Display names are NOT unique across workspaces, so the name check alone does not cover a
    # lakehouse pinned from another workspace — the ids do. The NAME here matches exactly; only
    # the workspace differs.
    spark = build_spark()
    o = run_runtime_blackbox(
        "setup",
        spark,
        params={"lakehouse_name": "LH_Demo"},
        context={**_RT_DEFAULT_CONTEXT, "defaultLakehouseWorkspaceId": "other-ws-guid"},
    )
    assert o.envelope["status"] == "blocked"
    assert "other-ws-guid" in o.envelope["error"]  # where the lakehouse lives
    assert "ws-guid" in o.envelope["error"]  # where this notebook runs
    # Refused BEFORE the DDL, not merely refused: FakeSpark stamps every statement it is handed
    # into _last_sql, and that attribute does not exist until .sql() has been called at all.
    assert getattr(spark, "_last_sql", None) is None
    assert spark._store == {}  # no schema, no table, no audit row


def test_cross_workspace_refusal_names_the_workspace_it_can_name():
    # setup's sibling guard. _RT_DEFAULT_CONTEXT names the notebook's workspace but not the
    # lakehouse's, so one message exercises BOTH label branches at once.
    o = run_runtime_blackbox(
        "setup",
        build_spark(),
        params={"lakehouse_name": "LH_Demo"},
        context={**_RT_DEFAULT_CONTEXT, "defaultLakehouseWorkspaceId": "other-ws-guid"},
    )
    err = o.envelope["error"]
    assert "'WS_Demo' (ws-guid)" in err  # named side
    assert "(other-ws-guid)" in err and "'' (other-ws-guid)" not in err  # unnamed side, bare guid


def test_lakehouse_workspace_echoing_this_notebook_proceeds():
    # The NEGATIVE half of the cross-workspace guard — and the half PRODUCTION takes: on real
    # Fabric `defaultLakehouseWorkspaceId` is populated, so `_lh_ws and _lh_ws != _ws` rests on
    # the SECOND operand on every run. Narrowing the guard to `if _lh_ws:` would refuse every
    # production setup, while a suite that only ever supplies a MISMATCHING id (or omits the key
    # entirely and short-circuits on the first operand) stays fully green — and branch coverage
    # cannot see it, because A-true/B-false is not a distinct outcome of `A and B`. The sibling
    # Target.resolve guard is pinned the same way.
    o = run_runtime_blackbox(
        "setup",
        build_spark(),
        params={"lakehouse_name": "LH_Demo"},
        context={**_RT_DEFAULT_CONTEXT, "defaultLakehouseWorkspaceId": "ws-guid"},
    )
    assert o.envelope["status"] == "success"
    assert sorted(o.envelope["data"]["created"]) == sorted(CONTROL_TABLES)


def test_setup_is_blocked_off_fabric_even_before_declared_name_validation():
    # Without Fabric the immutable DAR snapshot cannot exist. This gate precedes every setup
    # detail, including the declared name, and leaves the schema untouched.
    spark = build_spark()
    o = run_runtime_blackbox(
        "setup", spark, params={"lakehouse_name": ""}, with_notebookutils=False
    )
    assert o.envelope["status"] == "blocked"
    assert "DAR control-data boundary" in o.envelope["error"]
    assert spark._store == {}


def test_a_mismatched_declared_name_does_not_block_a_mode_that_never_reads_it():
    """The assertion is scoped to `if mode == "setup"`. Every OTHER mode takes its lakehouse
    name from the CONFIG table (generate's target guard, RUNBOOK §3b), never from this
    parameter — and both the smoke harness (which ships `lakehouse_name` in the base parameter
    set for every mode) and the cookbook (which has operators set it stickily via
    `OLAF.configure(...)`) hand the SAME value to modes that never read it. Widening the scope
    would therefore block plan/apply on a value they do not consume."""
    spark, client = build_spark(), FakeFabricClient([])
    run_runtime_blackbox("setup", spark, params={"lakehouse_name": "LH_Demo"})
    spark._store[CONFIG_TABLE] = sample_config_rows()
    seed_sample_members(spark)
    o = run_runtime_blackbox(
        "generate", spark, client=client, params={"lakehouse_name": "LH_Other"}
    )
    assert o.envelope["status"] == "success"
    assert o.raised is None
    assert "lakehouse_name" not in o.envelope["params"]  # generate never echoes it


def test_setup_requires_the_same_attached_target_as_other_sensitive_operations():
    # Setup now joins NEEDS_TARGET: it needs a fresh ETag-bearing DAR snapshot before its first
    # DDL, so a missing lakehouse blocks setup and generate alike with zero schema writes.
    ctx = {k: v for k, v in _RT_DEFAULT_CONTEXT.items() if k != "defaultLakehouseId"}
    blocked = run_runtime_blackbox(
        "generate", build_spark(), client=FakeFabricClient([]), context=ctx
    )
    assert "no lakehouse attached" in blocked.raised
    setup_spark = build_spark()
    o = run_runtime_blackbox(
        "setup", setup_spark, params={"lakehouse_name": "LH_Demo"}, context=ctx
    )
    assert o.envelope["status"] == "blocked"
    assert setup_spark._store == {}


# ---------------------------------------------------------------------------------------------
# RuntimeRunByMemberLabelWiring
# ---------------------------------------------------------------------------------------------


def test_a_written_log_row_labels_a_guid_run_by_from_the_member_table():
    """T4's production WIRING, black-box. `run_mode` must pass `member_table=` into the `Log` it
    constructs; `Log.__init__` defaults it to `None` and `resolve_principal` early-returns on a
    falsy table, so deleting that one keyword argument switches the whole feature off in production
    without failing anything — every other T4 assertion hand-builds `Log(..., member_table=...)`
    itself. The default black-box context cannot catch it either: its `userName` is a UPN, so
    `run_by` is never GUID-shaped there and the lookup is a no-op by construction. So drive a real
    mode with a GUID-shaped identity (a pipeline's service principal — no `userName`, only the
    `userId` objectId) over a seeded member table, and assert the WRITTEN log row."""
    spark, client = build_spark(), FakeFabricClient([])
    run_runtime_blackbox("setup", spark, params={"lakehouse_name": "LH_Demo"})
    spark._store[CONFIG_TABLE] = sample_config_rows()
    seed_sample_members(spark)  # SVC_LOADER is listed here, under SVC_LOADER_NAME
    # A pipeline run reports no userName — there is no user — only the running principal's
    # objectId as userId. That is the ONLY shape resolve_principal acts on.
    pipeline_ctx = {k: v for k, v in _RT_DEFAULT_CONTEXT.items() if k != "userName"}
    pipeline_ctx["userId"] = SVC_LOADER
    before = len(spark._store[LOG_TABLE])
    o = run_runtime_blackbox("generate", spark, client=client, context=pipeline_ctx)
    assert o.envelope["status"] == "success"
    written = spark._store[LOG_TABLE][before:]
    # generate logs — an empty slice would pass the set assertion below vacuously
    assert written
    # LABELLED, not the bare id, on every row
    assert {r["run_by"] for r in written} == {f"{SVC_LOADER_NAME} ({SVC_LOADER})"}


# ---------------------------------------------------------------------------------------------
# RuntimeBlackBoxTrace — mode=trace and mode=rollback through the runtime
# ---------------------------------------------------------------------------------------------


def test_trace_merges_report_into_result():
    """mode=trace: the runtime builds Audit with the live client and merges report() into the
    envelope data. Exercises the trace dispatch cell + the trace KNOWN_MODES/NEEDS_TARGET
    branches, and that trace (read-only) writes NO log row."""
    spark = build_spark()
    spark._store[CONFIG_TABLE] = sample_config_rows()  # short_rows/config_hash resolvable
    spark._store[MAPPING_TABLE] = []  # nothing generated -> current_generation None, is_stale True
    spark._store[LOG_TABLE] = [
        seed_validate_row(
            "SalesReaders",
            "sales.orders",
            "mem-established",
            run_at="2026-07-13T01",
            run_by="alice@example.com",
            config_version="1",
            mode="apply",
        )
    ]
    spark._store[MEMBER_TABLE] = []  # no cache needed -- report() only counts rows
    client = FakeFabricClient(
        roles=[fake_role("SalesReaders", ["sales.orders"], ["mem-established", "mem-oob"])]
    )
    o = run_runtime_blackbox("trace", spark, client=client)
    res, exitv = o.envelope, o.exit_value
    assert (res["mode"], res["status"]) == ("trace", "success")
    assert not res["changed"]
    for k in (
        "current_generation",
        "last_generate",
        "last_apply",
        "is_stale",
        "established_ever",
        "live_role_count",
        "live_grant_count",
        "desired_grant_count",
        "missing",
        "unexpected",
        "out_of_band",
        "policy_checked",
        "policy_mismatch",
        "in_sync",
    ):
        assert k in res["data"]
    assert res["data"]["established_ever"] == 1
    assert res["data"]["live_grant_count"] == 2  # both live grants, provenanced or not
    assert res["data"]["out_of_band"] == 1  # mem-oob live but not established
    assert res["data"]["is_stale"]
    assert "config_hash" in res  # the envelope stamps provenance for trace too
    assert exitv["mode"] == "trace"  # notebook.exit round-trips the same envelope
    assert len(spark._store[LOG_TABLE]) == 1  # trace is read-only: writes NO log row


def test_rollback_through_the_runtime():
    # the rollback dispatch cell: the runtime restores config, re-runs the chain, and merges the
    # rollback result into the envelope data (+ the notebook.exit round-trip). A prior mode='plan'
    # record for this config state unlocks the apply the rollback chain runs.
    spark = build_spark()
    client = FakeFabricClient([])
    make_dep(spark, client, "setup").setup()
    spark._store[CONFIG_TABLE] = sample_config_rows()
    seed_sample_members(spark)
    spark._history_rows = [{"version": 2}, {"version": 3}]
    run_generate(make_dep(spark, client, "generate"))
    make_dep(spark, client, "plan").plan()
    o = run_runtime_blackbox(
        "rollback",
        spark,
        client=client,
        params={"rollback_to_version": "", "rollback_reason": "revert via runtime"},
    )
    res, exitv = o.envelope, o.exit_value
    assert (res["mode"], res["status"]) == ("rollback", "success")
    assert res["data"]["rollback"]["to_version"] == 2
    assert res["changed"]
    assert exitv["mode"] == "rollback"  # notebook.exit round-trips the same envelope


# ---------------------------------------------------------------------------------------------
# RuntimeBlackBoxErrorResilience — the error path is best-effort: an unexpected (non-SystemExit)
# failure yields status="error" even when the envelope's config_hash recompute raises, or the
# audit fail-row write itself fails. Both are swallowed so nothing masks the error verdict.
# ---------------------------------------------------------------------------------------------


def test_error_envelope_tolerates_unreadable_config_hash():
    # setup fails BEFORE the config table is created, so the envelope's deployment.config_hash
    # read raises -> the _envelope `except Exception: ch = None` fallback. Status stays "error".
    class _BreakConfigCreateSpark(FakeSpark):
        def sql(self, query):
            q = " ".join(str(query).split()).upper()
            if q.startswith("CREATE TABLE") and CONFIG_TABLE.upper() in q:
                raise RuntimeError("cannot create the config control table")
            return super().sql(query)

    spark = _BreakConfigCreateSpark(SAMPLE_SCHEMAS_TABLES, SAMPLE_COLUMNS)
    o = run_runtime_blackbox("setup", spark)
    assert o.envelope["status"] == "error"
    assert o.envelope["config_hash"] is None  # config_hash unreadable -> None fallback
    assert o.raised is not None  # error path raises the envelope JSON
    assert o.exit_value is None  # error never reaches notebook.exit


def test_error_path_swallows_a_failing_audit_write():
    # setup fails, and the error handler's own audit.fail_row write ALSO fails (createDataFrame
    # down) -> the inner `except Exception: pass` swallows it; the envelope still reports error.
    class _BreakWriteSpark(FakeSpark):
        def createDataFrame(self, data, schema=None):
            raise RuntimeError("audit write backend down")

    spark = _BreakWriteSpark(SAMPLE_SCHEMAS_TABLES, SAMPLE_COLUMNS)
    o = run_runtime_blackbox("setup", spark)
    assert o.envelope["status"] == "error"
    assert o.envelope["config_hash"] is not None  # config table WAS created -> hash readable
    assert o.raised is not None
    assert spark._store.get(LOG_TABLE) == []  # the swallowed fail-row wrote nothing


# ---------------------------------------------------------------------------------------------
# RuntimeLeanPayloadOnRaise
# ---------------------------------------------------------------------------------------------


def test_long_blocked_reason_is_capped_but_payload_stays_parseable():
    """The raise path (blocked/error) emits a COMPACT payload: Fabric truncates a long raised
    exception mid-JSON, so a long collect-all reason is capped (marker + full reason in the log)
    and the payload stays valid JSON + parseable. The FULL reason is always in
    onelake_security_log."""
    spark, client = build_spark(), FakeFabricClient([])
    run_runtime_blackbox("setup", spark)  # create the control tables
    # a config whose 12 group members are all ABSENT from onelake_security_member -> one "not
    # found" error each -> a collect-all reason far longer than the 400-char cap.
    many = ";".join(f"sg-absent-{i}" for i in range(12))
    row = {c: None for c in CONFIG_AUTHOR_COLUMNS}
    row.update(
        {
            "role_name": "R",
            "lakehouse_name": "LH_Demo",
            "include_tables": "sales.orders",
            "permission": "Read",
            "include_group_names": many,
            "active": True,
        }
    )
    spark._store[CONFIG_TABLE] = [row]
    spark._store[MEMBER_TABLE] = []  # nothing resolves -> long member-gate error list

    o = run_runtime_blackbox("generate", spark, client=client, params={"rebuild": True})
    assert o.raised is not None  # native-failure: the activity raised
    env = json.loads(o.raised)  # COMPACT payload still parses (the whole point)
    assert env["status"] == "blocked"
    assert env["data"] == {}  # data dropped on the raise path
    assert "truncated; full reason in onelake_security_log" in env["error"]  # reason capped
    assert env["mode"] == "generate"
    assert "config_hash" in env
    # the FULL (untruncated) reason is durable in the log
    rej = [
        r
        for r in spark._store[LOG_TABLE]
        if r.get("action") == "guard" and r.get("status") == "rejected"
    ]
    assert any(len(str(r.get("message", ""))) > 400 for r in rej), (
        "the full untruncated reason must be in the log rejected row"
    )


# ---------------------------------------------------------------------------------------------
# RuntimeEntrypointDirect — the branches the black-box can no longer reach
# ---------------------------------------------------------------------------------------------


def test_run_and_exit_blocks_a_mode_outside_allowed():
    # driven with a RESTRICTED allowed set, since the Run cell always passes the full KNOWN_MODES
    with pytest.raises(SystemExit) as excinfo:
        run_and_exit("apply", allowed={"show"}, params={}, spark=build_spark())
    payload = json.loads(str(excinfo.value))
    assert payload["status"] == "blocked"
    assert "not permitted" in payload["error"]


def test_run_and_exit_leaves_the_envelope_in_last_result_too():
    """Every run now PRINTS `OLAF.last_result: {...}`, so the name has to hold on this path as
    well — the ▶️Run cell reaches run_and_exit directly, not through the OLAF facade that used to
    be the only thing setting it."""
    OLAF.last_result = None
    with pytest.raises(SystemExit):
        run_and_exit("apply", allowed={"show"}, params={}, spark=build_spark())
    assert OLAF.last_result["status"] == "blocked"
    assert OLAF.last_result is _olaf_runtime.envelope  # the same object, not a copy of it


def test_run_and_exit_keeps_success_envelope_when_notebook_exit_is_unavailable(monkeypatch):
    """A local caller keeps the successful result for inspection rather than failing on imports."""
    expected = {
        "mode": "show",
        "status": "success",
        "changed": False,
        "message": "shown",
        "params": {},
        "data": {},
        "error": None,
        "batch_id": "batch",
        "run_id": "run",
        "config_hash": "hash",
    }
    monkeypatch.setattr(_olaf_runtime, "run_mode", lambda *_args: expected)
    monkeypatch.delitem(sys.modules, "notebookutils", raising=False)

    assert run_and_exit("show", allowed={"show"}, params={}, spark=build_spark()) is None
    assert OLAF.last_result is expected


def test_the_run_cell_path_never_promises_a_frame(capsys):
    """run_and_exit reaches run_mode without the flag, so a pipeline log carries the verdict and
    nothing that suggests a value survived the exit."""
    with pytest.raises(SystemExit):
        run_and_exit("apply", allowed={"show"}, params={}, spark=build_spark())
    assert "DataFrame" not in capsys.readouterr().out


def test_run_mode_returns_blocked_envelope_for_unknown_mode():
    # the allowed-gate intercepts unknown modes before run_mode, so a direct call is the only
    # path to run_mode's own `unknown mode` guard
    env = run_mode("nonsense", {}, build_spark())
    assert env["status"] == "blocked"
    assert "unknown mode" in env["message"]


def test_runtime_treats_a_guard_with_unknown_write_outcome_as_an_error(monkeypatch):
    """A guard raised after an indeterminate write cannot be represented as a clean block."""
    guard = _olaf_runtime.ControlDataGuardError("write outcome unknown")
    guard.changed = None

    def raise_guard(_self, **_kwargs):
        raise guard

    monkeypatch.setattr(_olaf_runtime.Deployment, "setup", raise_guard)
    spark = build_spark()
    outcome = run_runtime_blackbox("setup", spark)

    assert outcome.envelope["status"] == "error"
    assert outcome.envelope["changed"] is None
    assert "write outcome unknown" in outcome.envelope["error"]
    assert outcome.raised is not None
    assert spark._store == {}
    assert spark._writes == []


# ---------------------------------------------------------------------------------------------
# RuntimeBlackBoxValidate — mode=validate through the runtime: the dispatch branch, the
# KNOWN_MODES/NEEDS_TARGET additions, and the tenant-exempt guard (validate is NOT in
# DESIRED_STATE_MODES | {generate}).
# ---------------------------------------------------------------------------------------------


def validate_seeded(rows):
    spark, client = build_spark(), FakeFabricClient([])
    run_runtime_blackbox("setup", spark)
    spark._store[CONFIG_TABLE] = rows
    seed_sample_members(spark)
    return spark, client


def test_validate_success_writes_nothing_and_is_tenant_exempt():
    spark, client = validate_seeded([carve_config_row()])
    log_before = list(spark._store[LOG_TABLE])
    # tenant_id="" proves validate is exempt from the tenant-required guard (which stays
    # DESIRED_STATE_MODES | {"generate"}) — a missing tenant would block generate but not validate.
    o = run_runtime_blackbox("validate", spark, client=client, params={"tenant_id": ""})
    res = o.envelope
    assert (res["mode"], res["status"]) == ("validate", "success")
    assert not res["changed"]
    assert res["data"]["grants"] == 1
    assert len(res["data"]["warnings"]) == 1  # the carve warning rides the envelope
    assert o.exit_value["mode"] == "validate"  # success -> notebook.exit round-trip
    assert o.raised is None
    assert spark._store[MAPPING_TABLE] == []  # dry-run wrote no mapping
    assert spark._store[LOG_TABLE] == log_before  # ...and no log row


def test_validate_blocked_leaves_store_untouched():
    bad = dict(sample_config_rows()[0], role_name="1illegal")
    spark, client = validate_seeded([bad])
    log_before = list(spark._store[LOG_TABLE])
    o = run_runtime_blackbox("validate", spark, client=client)
    assert o.envelope["status"] == "blocked"
    assert "validate blocked" in o.envelope["error"]
    assert o.raised is not None  # native failure raises the envelope JSON
    assert o.exit_value is None  # blocked never reaches notebook.exit
    assert spark._store[MAPPING_TABLE] == []
    assert spark._store[LOG_TABLE] == log_before  # not even a 'rejected' row
