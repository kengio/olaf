"""The OLAF interactive facade's operation methods — setup/generate/plan/apply/rollback/explain,
configure()'s stickiness rules, the flatten to first-level members, and the Run cell's `if mode:`
dispatch guard.

Ported from `olaf_test_integration.ipynb` classes `RuntimeOLSFacade` (the ops half),
`RuntimeOLAFFlatFacade` and `RuntimeOLSDispatchGuard` (scope "mock_integration").
"""

import contextlib
import inspect
import io
import json
import sys
from unittest import mock

import pytest
from _fakes import (
    _RT_DEFAULT_CONTEXT,
    BACKUP_DIR,
    CONFIG_TABLE,
    CTL_MAPPING_HISTORY_DIR,
    GRP_READERS_NAME,
    LOG_TABLE,
    MAPPING_TABLE,
    MEMBER_TABLE,
    SVC_LOADER_NAME,
    TENANT,
    FakeFabricClient,
    _rt_sample_lister,
    build_spark,
    make_row,
    member_cache_row,
    ols_env,
    ols_rows,
    ols_run_cell_source,
    ols_seed,
    run_runtime_blackbox,
    sample_config_rows,
    seed_sample_members,
)
from _olaf_runtime import (
    _OLAF_AUDIT_PASSTHROUGH,
    CONFIG_AUTHOR_COLUMNS,
    DEFAULT_CONTROL_TABLES,
    EXPLAIN_COLUMNS,
    HINT_COLUMNS,
    OLAF,
    PARAM_DEFAULTS,
    PARAMS_COLUMNS,
    Catalog,
    Parse,
    Say,
    UsageError,
    __version__,
    run_mode,
)

BAD_ROLE_ROW = {
    **{c: None for c in CONFIG_AUTHOR_COLUMNS},
    "role_name": "BadRole",
    "active": True,
    "lakehouse_name": "LH_Demo",
    "permission": "Read",
}  # no include_tables/include_folders -> rule A1 (grants nothing) blocks generate


# What configure()/show_params() announce they hand back.
PARAMS_FRAME = f"DataFrame[{', '.join(PARAMS_COLUMNS)}]"


def authored(spark):
    """setup() + the short config + seeded members, inside the caller's ols_env."""
    OLAF.setup()
    spark._store[CONFIG_TABLE] = sample_config_rows()
    seed_sample_members(spark)


# ---------------------------------------------------------------------------------------------
# Ops: every facade method returns a DataFrame, ops flatten their outcome envelope into a compact
# view, a blocked outcome comes back as a DataFrame (never raises), and OLAF.last_result holds
# the raw dict.
# ---------------------------------------------------------------------------------------------


def test_maintenance_setup_returns_status_frame():
    spark = build_spark()
    with ols_env(spark):
        df = OLAF.setup()
    rows = ols_rows(df)
    assert len(rows) == 1
    assert (rows[0]["mode"], rows[0]["status"]) == ("setup", "success")
    assert "message" in df.columns


def test_deployment_generate_plan_apply_views():
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        authored(spark)

        gdf = OLAF.generate()
        grow = ols_rows(gdf)[0]
        assert (grow["mode"], grow["status"]) == ("generate", "success")
        assert grow["grants"] == "3"  # 3 mapping grants, flattened to the view
        assert "warnings" in gdf.columns

        pdf = OLAF.plan()
        pmap = {r["role"]: r["action"] for r in ols_rows(pdf)}
        assert pmap == {"SalesReaders": "create", "RawReaders": "create"}
        assert "action" in pdf.columns

        adf = OLAF.apply()
        arow = ols_rows(adf)[0]
        assert arow["status"] == "success"
        assert arow["push_status"] == "200"
        assert arow["roles_written"] == "2"
        assert "request" in adf.columns
        assert "omitted_role_candidates" in adf.columns
        assert "drift_omission_candidates" in adf.columns
        assert "post_state_review_required" in adf.columns


def test_plan_no_drift_returns_summary_row():
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        ols_seed(spark, upto="apply")  # live now == desired
        df = OLAF.plan()  # re-plan -> no drift
    rows = ols_rows(df)
    assert len(rows) == 1
    assert rows[0]["status"] == "skipped"
    assert rows[0]["role"] is None  # no-drift plan -> the None role/action summary row


def rollback_ready(spark, versions=(1, 2, 3)):
    """setup -> config -> members -> generate -> plan inside the caller's ols_env, with a
    multi-commit config history, so ONE OLAF.rollback(...) is unlocked."""
    authored(spark)
    spark._history_rows = [{"version": v} for v in versions]
    OLAF.generate()
    OLAF.plan()


def test_rollback_returns_status_frame():
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        rollback_ready(spark, versions=(2, 3))
        df = OLAF.rollback(rollback_reason="revert via OLAF")
    rows = ols_rows(df)
    assert (rows[0]["mode"], rows[0]["status"]) == ("rollback", "success")
    assert OLAF.last_result["data"]["rollback"]["to_version"] == 2


def test_successful_rollback_records_the_generate_stage_and_clears_pending_change():
    """A rollback's nested generate is a distinct provenanced chain stage, not just rollback noise."""
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        rollback_ready(spark, versions=(2, 3))
        OLAF.rollback(rollback_reason="certify restored generation")
        status = ols_rows(OLAF.status())[0]
    completions = [
        json.loads(row["message"])
        for row in spark._store[LOG_TABLE]
        if row.get("mode") == "rollback" and row.get("action") == "complete"
    ]
    assert any(message.get("operation") == "generate" for message in completions)
    assert status["last_deployment_mode"] == "rollback"
    assert status["pending_change"] == "False"


def test_a_configured_rollback_to_version_wins_when_the_call_leaves_it_blank():
    """Every facade method carries a signature default, so the key ALWAYS landed in _run's
    overrides and always beat _base_params — a configured rollback_to_version was silently dropped
    and you rolled back ONE version instead of to N. History here is [1, 2, 3]: the silent-loss
    behaviour lands on 2 (the immediately previous commit), the configured intent on 1, so the two
    outcomes cannot be confused."""
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        rollback_ready(spark)
        OLAF.configure(rollback_to_version="1")
        OLAF.rollback(rollback_reason="revert two commits")
    assert OLAF.last_result["status"] == "success"
    assert OLAF.last_result["data"]["rollback"]["to_version"] == 1


def test_an_explicit_rollback_to_version_still_beats_the_configured_one():
    """The narrow fix must not invert the precedence it was fixing: a value passed to the CALL
    is the most specific statement of intent and still wins over the configured one."""
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        rollback_ready(spark)
        OLAF.configure(rollback_to_version="1")
        OLAF.rollback(rollback_to_version="2", rollback_reason="revert one commit")
    assert OLAF.last_result["data"]["rollback"]["to_version"] == 2


def test_a_configured_rollback_reason_can_never_become_sticky():
    """The general _UNSET sentinel across the facade was DECLINED for exactly this: it would
    have made rollback_reason sticky, so a later, unrelated rollback would be stamped with an
    older rollback's reason — a quiet falsification of the audit trail, which is strictly
    worse than a loud refusal. rollback_reason is therefore passed UNCONDITIONALLY, blank
    included, so a configured value can never reach the engine: the reason guard fires and
    NOTHING carrying the stale reason is written."""
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        rollback_ready(spark)
        OLAF.configure(rollback_reason="a reason from an earlier, unrelated rollback")
        df = OLAF.rollback()  # no reason given for THIS one
    assert ols_rows(df)[0]["status"] == "blocked"  # loud, not silent
    assert "rollback requires a reason" in OLAF.last_result["message"]
    assert [r for r in spark._store[LOG_TABLE] if "earlier, unrelated" in str(r["message"])] == []
    assert [r for r in spark._store[LOG_TABLE] if r["action"] == "rollback"] == []


def test_configure_refuses_an_invalid_verbosity_and_stores_nothing():
    # the interactive twin of run_mode's verbosity guard (external security audit 2026-08-16,
    # issue #17): refused before the update, so a rejected call leaves nothing sticky.
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        OLAF.configure(verbosity="detail")  # a valid level is accepted and stored
        assert OLAF._base_params["verbosity"] == "detail"
        with pytest.raises(UsageError) as excinfo:
            OLAF.configure(verbosity="verbos")
        assert "verbosity must be one of" in str(excinfo.value)
        assert "'verbos'" in str(excinfo.value)
        assert OLAF._base_params["verbosity"] == "detail"  # the typo stored nothing


def test_apply_blocked_returns_frame_without_raising():
    # apply with no saved plan -> run_mode blocks -> OLAF returns a DataFrame (status="blocked"),
    # it does NOT raise, and OLAF.last_result carries the raw blocked dict.
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        authored(spark)
        OLAF.generate()
        df = OLAF.apply()  # no plan first -> saved-plan gate blocks
    rows = ols_rows(df)
    assert rows[0]["status"] == "blocked"
    assert "no successful plan" in rows[0]["message"]
    assert OLAF.last_result["status"] == "blocked"


def test_generate_blocked_returns_frame_without_raising():
    # generate whose members are unseeded -> the No-Graph member gate blocks -> a blocked frame.
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        OLAF.setup()
        spark._store[CONFIG_TABLE] = sample_config_rows()  # members NOT seeded
        df = OLAF.generate()
    rows = ols_rows(df)
    assert (rows[0]["mode"], rows[0]["status"]) == ("generate", "blocked")
    assert OLAF.last_result["status"] == "blocked"


# ---------------------------------------------------------------------------------------------
# OLAF.explain() — a dry projection of generate's OWN resolution, with zero side effects
# ---------------------------------------------------------------------------------------------


def test_deployment_explain_matches_generate_grants_with_zero_side_effects():
    """explain() projects Catalog.canonical -> Generate.rows -> Generate._build_grants over the SAME
    2-role sample config generate uses -- called BEFORE generate ever runs, so the mapping/log
    tables are still exactly as setup() left them -- then row-for-row against what generate ACTUALLY
    writes once it runs on the identical seeded config (mutation-strong: exact role/scope/permission
    /rls/cls/members per grant, not just a row count)."""
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        authored(spark)
        log_before = list(spark._store[LOG_TABLE])  # setup() itself already logged 4 rows
        df = OLAF.explain()
        preview = ols_rows(df)
        # zero side effects: explain() must not have written a mapping or a log row
        assert spark._store[MAPPING_TABLE] == []
        assert spark._store[LOG_TABLE] == log_before
        OLAF.generate()  # now actually build the mapping, for comparison
        mapping_rows = [r.asDict() for r in spark.table(MAPPING_TABLE).collect()]
    assert df.columns == [
        "role_name",
        "scope_path",
        "permission",
        "rls_condition",
        "visible_columns",
        "members",
    ]
    assert len(preview) == 3  # 2 sales tables + 1 raw folder -- generate's own 3 grants

    def members_of(row):
        names = []
        for name_col in (
            "member_group_names",
            "member_user_names",
            "member_sp_names",
            "member_mi_names",
        ):
            names += Parse.list(row.get(name_col))
        return ";".join(names) or None

    expected = {
        (r["role_name"], r["scope_path"]): {
            "permission": r["permission"],
            "rls_condition": r["rls_condition"],
            "visible_columns": r["visible_columns"],
            "members": members_of(r),
        }
        for r in mapping_rows
    }
    got = {
        (r["role_name"], r["scope_path"]): {
            "permission": r["permission"],
            "rls_condition": r["rls_condition"],
            "visible_columns": r["visible_columns"],
            "members": r["members"],
        }
        for r in preview
    }
    assert got == expected


def test_deployment_explain_empty_config_returns_empty_typed_frame():
    """explain() on a freshly set-up vault (0 active config rows): an EMPTY but TYPED frame --
    generate's own 'nothing to build' case, without generate's hard refusal -- and still zero side
    effects (nothing to project means nothing gets written either)."""
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        OLAF.setup()
        log_before = list(spark._store[LOG_TABLE])  # setup() itself already logged 4 rows
        df = OLAF.explain()
    assert df.columns == [
        "role_name",
        "scope_path",
        "permission",
        "rls_condition",
        "visible_columns",
        "members",
    ]
    assert df.count() == 0
    assert spark._store[MAPPING_TABLE] == []
    assert spark._store[LOG_TABLE] == log_before


def test_explain_says_it_ran_even_with_nothing_to_preview(capsys):
    """The empty exit returned its typed frame and printed NOTHING, which is indistinguishable
    from a cell that failed to run — the exact confusion the announcement exists to remove."""
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        OLAF.setup()
        capsys.readouterr()
        frame = OLAF.explain()
    printed = capsys.readouterr().out.splitlines()
    assert printed[0] == "✅ explain · 0 grant(s) previewed · no active config rows"
    assert printed[-1] == f"→  DataFrame[{', '.join(frame.columns)}]"


def test_explain_warns_rather_than_ticks_when_the_config_would_not_generate(capsys):
    """A ✅ over a config that generate would hard-reject reads as a clean preview. The call DID
    run and DID hand back a real frame — it just has nothing good to report."""
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        OLAF.setup()
        spark._store[CONFIG_TABLE] = [BAD_ROLE_ROW]
        capsys.readouterr()
        frame = OLAF.explain()
    printed = capsys.readouterr().out.splitlines()
    assert printed[0].startswith("⚠️  explain · ")
    assert printed[0].endswith("blocking error(s) · this config would NOT generate")
    assert printed[-1] == f"→  DataFrame[{', '.join(frame.columns)}]"


def test_deployment_explain_surfaces_validation_errors():
    """explain() on a config generate() would HARD-REJECT (a row granting neither a table nor a
    folder -- rule A1) does NOT show a confident grant preview: it returns the 1-column `error`
    frame surfacing each blocking error, never raises, and writes nothing (still a dry run)."""
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        OLAF.setup()
        spark._store[CONFIG_TABLE] = [BAD_ROLE_ROW]
        log_before = list(spark._store[LOG_TABLE])
        df = OLAF.explain()  # must NOT raise
        preview = ols_rows(df)
        assert spark._store[MAPPING_TABLE] == []  # zero side effects
        assert spark._store[LOG_TABLE] == log_before
    assert df.columns == ["error"]  # the honest error-surface shape, not a preview
    assert preview  # at least one blocking error surfaced
    assert any("BadRole" in r["error"] for r in preview)


def test_deployment_explain_wires_the_resolved_target_into_the_folder_lister():
    """The WIRING -- the GUIDs explain() ACTUALLY hands Catalog.fs_folder_lister. ols_env patches
    that seam as `lambda *_ids: _rt_sample_lister`, DISCARDING the ids, so a test that only checks
    the folder grant appears passes whether the ids are the real pair or (None, None) -- exactly the
    blindness that let the folder-preview bug ship. Capture the args instead: explain() must resolve the attached
    target (Target.resolve) and bind THOSE ids into the production lister.

    The anchor asserted here is _RT_DEFAULT_CONTEXT -- Target.resolve()'s OWN answer -- which is the
    correct anchor for explain(), because Target.resolve is exactly what explain() calls. It is NOT
    a check that explain and generate bind the same pair: Deployment.validated binds the CLIENT's
    ids, and in this harness those are different values. The two coincide in PRODUCTION only because
    OLAF._build_client constructs FabricClient(*Target.resolve())."""
    spark, client = build_spark(), FakeFabricClient([])
    captured = []

    def capturing_lister(workspace_id, item_id):
        captured.append((workspace_id, item_id))
        return _rt_sample_lister

    with ols_env(spark, client):
        authored(spark)
        # nested INSIDE ols_env: this patch wins over ols_env's id-discarding lambda, and
        # ols_env's own patch is restored when it exits.
        with mock.patch.object(Catalog, "fs_folder_lister", capturing_lister):
            df = OLAF.explain()  # must NOT raise
        preview = ols_rows(df)
    assert captured == [
        (_RT_DEFAULT_CONTEXT["currentWorkspaceId"], _RT_DEFAULT_CONTEXT["defaultLakehouseId"])
    ]
    assert len(preview) == 3  # and the folder grant still resolves


def test_deployment_explain_unresolvable_target_names_the_target_not_the_config():
    """Off-Fabric (no attached lakehouse -> Target.resolve raises SystemExit) a
    folder-scoped config cannot be previewed. explain() must say THAT, naming the target, and must
    NOT hand back the raw 'OneLake path needs the target workspace/item GUIDs' that reads as an
    operator config defect. The other blocking errors of the SAME config must survive.

    The folder message's MULTIPLICITY is pinned by COUNT -- FOR THIS FIXTURE, an observation about
    sample_config_rows() rather than a general rule. Here RawReaders declares both include_folders
    and exclude_folders, which _scope_pair resolves in two separate loops, so the message lands
    twice, byte-identical. An any() check cannot see that, and the repeat is exactly what forces the
    message to stay short."""
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        OLAF.setup()
        spark._store[CONFIG_TABLE] = sample_config_rows() + [BAD_ROLE_ROW]
        seed_sample_members(spark)
        # no attached lakehouse: the real off-Fabric / unpinned-lakehouse shape
        sys.modules["notebookutils"].runtime.context.pop("defaultLakehouseId")
        df = OLAF.explain()  # must NOT raise
        preview = ols_rows(df)
        assert spark._store[MAPPING_TABLE] == []  # still zero side effects
    assert df.columns == ["error"]
    errors = [r["error"] for r in preview]
    folder_errors = [e for e in errors if "folder scopes" in e and "target" in e]
    # the folder failure is attributed to the TARGET. Two rows is THIS FIXTURE's count, not a
    # rule: RawReaders' include_folders + exclude_folders pair is resolved in two loops here.
    assert len(folder_errors) == 2, errors
    assert len(set(folder_errors)) == 1, folder_errors
    # short enough that meeting it twice is tolerable -- the reason the repeat is left undeduped
    assert len(folder_errors[0]) < 300, folder_errors[0]
    # not attributed to the operator's config
    assert not any("OneLake path needs the target workspace/item GUIDs" in e for e in errors), (
        errors
    )
    # ...and it does not promise a preview the same call cannot return: when errors exist,
    # explain() hands back the 1-column error frame and NOTHING previews.
    assert "scopes preview normally" not in folder_errors[0]
    assert any("BadRole" in e for e in errors), errors  # aggregate survives
    assert len(errors) == 4, errors  # 2 folder rows + BadRole's A1 and C1


def test_deployment_explain_unresolvable_target_still_previews_a_tables_only_config():
    """The tables/columns-only path legitimately needs no ids (Catalog.canonical's own docstring
    blesses omitting them, and Audit.coverage() is an existing no-id caller). An unresolvable target
    must therefore change NOTHING for a config that declares no folder scope -- the named-error
    lister is injected, never consulted."""
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        OLAF.setup()
        spark._store[CONFIG_TABLE] = [sample_config_rows()[0]]  # SalesReaders: tables only
        seed_sample_members(spark)
        sys.modules["notebookutils"].runtime.context.pop("defaultLakehouseId")
        df = OLAF.explain()  # must NOT raise
        preview = ols_rows(df)
    assert df.columns == EXPLAIN_COLUMNS  # a real preview, not the error surface
    assert sorted(r["scope_path"] for r in preview) == [
        "/Tables/sales/leads",
        "/Tables/sales/orders",
    ]


def test_explain_warns_rather_than_ticks_when_the_config_cannot_be_resolved(capsys):
    """The third silent exit: a live listing that 403s collapses the preview to one honest row.
    It still ran and still handed back a frame, so it announces — with ⚠️, because a ✅ over a
    preview that could not be built reads as a clean one."""
    spark, client = build_spark(), FakeFabricClient([])

    def forbidden_lister(_workspace_id, _item_id):
        def _lister(_base):
            raise RuntimeError("Py4JJavaError: 403 Forbidden")

        return _lister

    with ols_env(spark, client):
        OLAF.setup()
        spark._store[CONFIG_TABLE] = sample_config_rows()
        seed_sample_members(spark)
        capsys.readouterr()
        with mock.patch.object(Catalog, "fs_folder_lister", forbidden_lister):
            frame = OLAF.explain()
    printed = capsys.readouterr().out.splitlines()
    assert printed[0] == "⚠️  explain · preview unavailable · the config could not be resolved"
    assert printed[-1] == f"→  DataFrame[{', '.join(frame.columns)}]"


def test_deployment_explain_surfaces_a_failing_folder_listing_without_raising():
    """The blocker that fix would otherwise INTRODUCE: once real GUIDs flow, the folder lister
    makes a live notebookutils.fs.ls call, and a 403 / missing path / throttle is NOT a
    ValidationError -- Generate._scope_pair catches ValidationError only, so it would escape
    explain() as a raw traceback. explain() guards the canonical+rows chain and surfaces the failure
    as a row instead.

    Seeded with an UNRELATED blocking error alongside (rule A1) so the ASYMMETRY between the two new
    failure modes is asserted, not incidental: an unresolvable target keeps the collect-all aggregate
    whole, while a FAILING LISTING escapes Generate.rows entirely and collapses the preview to ONE
    row, LOSING BadRole. That is the documented, intended behaviour: a listing that fails means the
    live catalog side is unavailable, so an aggregate that merely LOOKS complete is worse than one
    honest row."""
    spark, client = build_spark(), FakeFabricClient([])

    def forbidden_lister(_workspace_id, _item_id):
        def _lister(_base):
            raise RuntimeError("Py4JJavaError: 403 Forbidden")

        return _lister

    with ols_env(spark, client):
        OLAF.setup()
        spark._store[CONFIG_TABLE] = sample_config_rows() + [BAD_ROLE_ROW]
        seed_sample_members(spark)
        log_before = list(spark._store[LOG_TABLE])
        with mock.patch.object(Catalog, "fs_folder_lister", forbidden_lister):
            df = OLAF.explain()  # must NOT raise
        preview = ols_rows(df)
        assert spark._store[MAPPING_TABLE] == []  # still zero side effects
        assert spark._store[LOG_TABLE] == log_before
    assert df.columns == ["error"]
    assert any("403 Forbidden" in r["error"] for r in preview), preview
    # The intended collapse, asserted BOTH ways: exactly one row, and the config's other
    # blocking error is gone -- unlike the unresolvable-target case, which keeps it.
    assert len(preview) == 1, preview
    assert not any("BadRole" in r["error"] for r in preview), preview


def test_deployment_explain_guards_the_real_production_folder_lister():
    """The guard above is only worth having if it fires on the PRODUCTION path -- and every other
    case reaches it through a PATCHED Catalog.fs_folder_lister, so the real lister is never actually
    composed with explain(). This case composes it, with nothing about the lister faked: ols_env's
    stand-in `notebookutils` module defines runtime/notebook/credentials but NO `fs`, so the real
    Catalog.fs_folder_lister's _lister raises AttributeError the moment it touches notebookutils.fs.
    AttributeError is not a ValidationError, so Generate._scope_pair does not catch it: precisely
    the escape the guard exists for, driven end to end over the production seam."""
    production_lister = Catalog.fs_folder_lister  # captured BEFORE ols_env patches it
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        authored(spark)  # RawReaders declares folder scopes
        log_before = list(spark._store[LOG_TABLE])
        with mock.patch.object(Catalog, "fs_folder_lister", production_lister):
            df = OLAF.explain()  # must NOT raise
        preview = ols_rows(df)
        assert spark._store[MAPPING_TABLE] == []  # still zero side effects
        assert spark._store[LOG_TABLE] == log_before
    assert df.columns == ["error"]  # the guard caught it -> an error row, no traceback
    assert len(preview) == 1, preview
    assert "live catalog" in preview[0]["error"]
    # ...and the failure really came from the PRODUCTION lister reaching notebookutils.fs --
    # not from any stand-in raiser, which is the whole point of this case.
    assert "module 'notebookutils' has no attribute 'fs'" in preview[0]["error"]


def test_configure_threads_base_param_into_envelope():
    spark = build_spark()
    with ols_env(spark):
        OLAF.configure(env="qa")
        OLAF.setup()
    assert OLAF.last_result["params"]["env"] == "qa"
    assert "status" in OLAF.last_result


def test_configure_returns_the_whole_parameter_set_as_a_frame():
    """Every method on this facade returns a DataFrame, and configure used to be the exception —
    it returned the class, which a notebook renders as `__main__.OLAF`: no information, and it
    reads like a mistake. It now answers the question the caller actually has after configuring."""
    spark = build_spark()
    with ols_env(spark):
        df = OLAF.configure(env="qa")
    assert df.columns == PARAMS_COLUMNS
    values = {r["parameter"]: r["value"] for r in ols_rows(df)}
    assert values["env"] == "qa"
    # ols_env seeds these two, so the frame shows the WHOLE sticky set, not just this call's keys
    assert values["tenant_id"] == TENANT
    assert values["lakehouse_name"] == "LH_Demo"
    # ...and the defaults the caller never touched, which is what the run will actually use
    assert values["config_table"] == DEFAULT_CONTROL_TABLES["config_table"]
    assert values["by"] == "table"


def test_the_frame_says_where_each_value_came_from():
    """Without the source column a default and a deliberate choice render identically, and an
    operator reading `env dev` cannot tell whether someone chose dev or nobody chose anything."""
    spark = build_spark()
    with ols_env(spark):
        OLAF._base_params = {}
        df = OLAF.configure(env="qa")
    source = {r["parameter"]: r["source"] for r in ols_rows(df)}
    assert source["env"] == "set"
    assert source["config_table"] == "default"
    # the two configure() refuses outright — never sticky however the caller got here
    assert source["keep_unmanaged"] == "per-call"
    assert source["rebuild"] == "per-call"


def test_the_frame_is_never_empty_even_before_anything_is_configured():
    """It used to come back with zero rows, which reads as "nothing is set, so nothing will
    happen" — while a run right then would have used a full set of defaults."""
    spark = build_spark()
    with ols_env(spark):
        OLAF._base_params = {}
        df = OLAF.configure()
    assert df.columns == PARAMS_COLUMNS
    assert df.count() == len(PARAM_DEFAULTS)
    assert {r["source"] for r in ols_rows(df)} == {"default", "per-call"}


# ---------------------------------------------------------------------------------------------
# configure() refusals — the per-call parameters that can never be made sticky
# ---------------------------------------------------------------------------------------------


def configure_refusal(**kw):
    """configure() called with one per-call parameter (plus a perfectly valid one) -> the refusal
    message. Resets the SHARED _base_params first so the "nothing was stored" assertion is
    hermetic, and asserts the valid key was not stored either — the guard runs BEFORE any update,
    so a rejected call leaves no partial state behind."""
    OLAF._base_params = {}
    with pytest.raises(UsageError) as excinfo:
        OLAF.configure(env="qa", **kw)
    assert OLAF._base_params == {}
    return str(excinfo.value)


def test_configure_refuses_the_per_call_keep_unmanaged():
    """keep_unmanaged cannot be made sticky. apply() carries a SIGNATURE DEFAULT, so
    _run("apply", keep_unmanaged=...) always lands the key in _params()'s overrides, and the
    override WINS over _base_params — an operator who configured keep_unmanaged=True would
    silently get the destructive default REPLACE anyway. Refuse the key instead of shadowing
    it, naming the key and the per-call home it belongs to."""
    msg = configure_refusal(keep_unmanaged=True)
    assert "keep_unmanaged" in msg
    assert "OLAF.apply" in msg


def test_configure_refuses_the_per_call_rebuild():
    """Same rule for rebuild — generate() carries the mirror-image signature default."""
    msg = configure_refusal(rebuild=True)
    assert "rebuild" in msg
    assert "OLAF.generate" in msg


def test_configure_refuses_the_removed_force():
    """`force` was REMOVED and split in two, but configure() still accepted it and stored it as a
    dead key — and that key is the ONE reachable path by which a legacy `force` still reaches
    run_mode, since it rides the sticky _base_params through _params(). It is also the only place a
    migrating operator would ever be told about the rename, so the refusal has to name BOTH
    successors, where each now lives, and the inverted polarity."""
    message = configure_refusal(force=True)
    assert "force" in message
    assert "rebuild" in message
    assert "OLAF.generate" in message
    assert "keep_unmanaged" in message
    assert "OLAF.apply" in message
    assert "polarity" in message


@pytest.mark.parametrize("value", ["dev", "prod", "uat-2", "feature_branch_7", "A" * 64])
def test_configure_accepts_every_ordinary_environment_label(value):
    """The rule has to be invisible to anyone naming an environment the way people do."""
    with ols_env(build_spark()):  # configure() returns a frame, so it needs an ambient session
        OLAF._base_params = {}
        OLAF.configure(env=value)
        assert OLAF._base_params == {"env": value}


@pytest.mark.parametrize(
    "value",
    [
        "dev' OR '1'='1",  # closes the WHERE literal the log reads build
        "dev'; DROP TABLE olaf.onelake_security_log; --",
        "dev prod",  # a space is not a label, and it is how a second clause starts
        "   ",  # whitespace is a broken label, not the blank that means "unset"
        "A" * 65,  # past the bound
    ],
)
def test_configure_refuses_an_env_that_is_not_a_label(value):
    """`env` is the one operator-supplied string that reaches Spark SQL as a WHERE literal
    (Log.find_plan_record, Log.grant_provenance). A sticky env set here rides _params() into every
    later run AND into the two destructive utilities, which build their Log outside run_mode
    entirely — so this boundary is not optional just because the pipeline one exists.

    Refused, never repaired: a sanitised value would run the deploy under an environment label the
    operator never typed and stamp it on every audit row."""
    OLAF._base_params = {}
    with pytest.raises(UsageError) as excinfo:
        OLAF.configure(env=value)
    assert "env must be" in str(excinfo.value)
    assert OLAF._base_params == {}  # refused BEFORE the update, like every other configure guard


def test_configure_without_an_env_key_is_untouched_by_the_guard():
    """The guard is scoped to the key it validates — configuring anything else must not need one."""
    with ols_env(build_spark()):
        OLAF._base_params = {}
        OLAF.configure(tenant_id=TENANT)
        assert OLAF._base_params == {"tenant_id": TENANT}


def test_a_stray_force_key_stays_inert_inside_run_mode():
    """The refusal is scoped to configure() ALONE, deliberately: NO `force` guard inside run_mode
    and no params allowlist, because a pipeline hands one Base-parameter set to every mode and a key
    a mode never reads must not start failing it. `force` is likewise absent from _PER_CALL_PARAMS.
    Proven by bypassing configure()'s refusal (writing _base_params directly, the legacy shape) and
    driving a REAL apply: the key rides _params() all the way into run_mode and changes nothing."""
    assert "force" not in OLAF._PER_CALL_PARAMS
    assert "force" in OLAF._REMOVED_PARAMS
    spark = build_spark()
    with ols_env(spark) as client:
        OLAF._base_params["force"] = True  # what configure() now refuses to store
        ols_seed(spark, upto="plan")
        OLAF.apply()
        envelope = OLAF.last_result
    assert envelope["status"] == "success"
    # still the signature default: REPLACE. force neither blocked the run nor flipped the fork.
    assert not envelope["data"]["keep_unmanaged"]
    assert client.put_calls


def test_per_call_params_matches_deployment_signatures():
    """_PER_CALL_PARAMS is hand-kept; derive the expected set from the two facade signatures it
    actually shadows (generate/apply) so a new defaulted parameter added to either one forces an
    update here instead of silently becoming stickable-but-ignored. Scoped to generate/apply ONLY —
    deliberately not rollback/show — because widening it would silently expand the refusal set to
    parameters that are meant to stay configurable via OLAF.configure()."""
    shadowing = {
        name for m in (OLAF.generate, OLAF.apply) for name in inspect.signature(m).parameters
    }
    assert set(OLAF._PER_CALL_PARAMS) == shadowing


def test_last_result_is_set_on_every_interactive_outcome():
    """The flow-control contract: a notebook branches on OLAF.last_result instead of building a
    pipeline, so it has to be populated after a blocked run too — and a blocked run returns a frame
    rather than raising, which is exactly what makes the branch reachable."""
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        OLAF.setup()
        assert OLAF.last_result["status"] == "success"
        spark._store[CONFIG_TABLE] = sample_config_rows()  # members NOT seeded -> the No-Graph gate
        OLAF.generate()
        assert OLAF.last_result["status"] == "blocked"
        assert OLAF.last_result["error"]  # the fix-list is readable from here
        seed_sample_members(spark)
        # No clearance step here any more, and its absence is the point: a refusal that never
        # authorized a write hands its lease back, so fixing the config and re-running is all an
        # operator has to do. Requiring a reviewed clearance after every config typo taught people
        # to clear incidents without reading them.
        OLAF.generate()
        OLAF.generate()  # unchanged config -> idempotent skip
        assert OLAF.last_result["status"] == "skipped"
        assert OLAF.last_result["changed"] is False


def test_last_result_is_what_the_pipeline_receives():
    """The same dict on both paths: a pipeline reads it from the activity's exitValue, a notebook
    reads it from the attribute. If they could differ, a flow tested interactively would not be the
    flow the pipeline runs."""
    spark = build_spark()
    outcome = run_runtime_blackbox("setup", spark)
    assert outcome.exit_value == json.loads(json.dumps(outcome.envelope, default=str))


def test_params_exposes_what_a_run_will_use():
    """The sibling of last_result: an attribute, no parentheses, no run needed. show_params()
    returns the same content as a frame for display; this is the form you branch on."""
    spark = build_spark()
    with ols_env(spark):
        OLAF.configure(env="feature")
        assert OLAF.params["env"] == "feature"
        # ols_env seeds these, so params shows the WHOLE sticky set, not just this call's keys
        assert OLAF.params["tenant_id"] == TENANT
        assert OLAF.params["lakehouse_name"] == "LH_Demo"


def test_params_fills_every_gap_from_the_map_run_mode_itself_resolves_against():
    """The point of the whole change: a key nobody configured still has a value at run time, and
    reading it out of PARAM_DEFAULTS rather than restating it is why this cannot drift."""
    spark = build_spark()
    with ols_env(spark):
        OLAF._base_params = {}
        assert OLAF.params == PARAM_DEFAULTS
        OLAF.configure(env="feature")
        assert OLAF.params == {**PARAM_DEFAULTS, "env": "feature"}


def test_params_reports_the_per_call_defaults_a_bare_call_would_get():
    """`OLAF.apply()` with no argument performs a REPLACE. That is the single most destructive
    default in the framework, so the answer to "what will this run do" has to name it."""
    spark = build_spark()
    with ols_env(spark):
        assert OLAF.params["keep_unmanaged"] is False
        assert OLAF.params["rebuild"] is False


def test_params_is_a_copy_so_it_cannot_become_sticky_state():
    """configure() is the one way in, and it is where the per-call parameters are refused. A
    mutable view would route around that check."""
    spark = build_spark()
    with ols_env(spark):
        OLAF.configure(env="feature")
        OLAF.params["env"] = "mutated"
        OLAF.params["keep_unmanaged"] = True  # the key configure() refuses outright
        assert OLAF.params["env"] == "feature"
        assert OLAF.params["keep_unmanaged"] is False  # the default, not the mutation
        assert "keep_unmanaged" not in OLAF._base_params  # and it never became sticky


def test_show_params_reads_without_writing():
    """The reason it exists: reading the table used to mean calling the setter with no arguments,
    which then printed "N set" after setting nothing at all."""
    spark = build_spark()
    with ols_env(spark):
        OLAF._base_params = {}
        OLAF.configure(env="qa")
        before = OLAF.params
        df = OLAF.show_params()
        assert OLAF.params == before
        assert OLAF._base_params == {"env": "qa"}
    assert df.columns == PARAMS_COLUMNS
    assert {r["parameter"]: r["value"] for r in ols_rows(df)}["env"] == "qa"


def test_show_params_and_configure_return_the_same_table(capsys):
    """One shape, one truth — and the announcement splits chosen from inherited, which is the
    number an operator actually wants before a destructive mode."""
    spark = build_spark()
    with ols_env(spark):
        OLAF._base_params = {}
        configured = ols_rows(OLAF.configure(env="qa", tenant_id=TENANT))
        shown = ols_rows(OLAF.show_params())
    assert configured == shown
    # ...and this is the WHOLE output of both: neither draws its own frame, because a method that
    # renders is a method you cannot quietly use as an input
    verdict = f"· 2 set · {len(PARAM_DEFAULTS) - 2} default"  # known keys MOVE from the default
    assert capsys.readouterr().out.splitlines() == [  # column, not add to it
        f"✅ configure {verdict}",
        "",
        f"→  {PARAMS_FRAME}",
        f"✅ show_params {verdict}",
        "",
        f"→  {PARAMS_FRAME}",
    ]


def test_every_interactive_call_says_whether_it_ran(capsys):
    """The modes print their verdict through _print_result; everything else returned a DataFrame
    and printed NOTHING, so a cell that succeeded and one that came back empty looked identical
    until you rendered the frame."""
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        ols_seed(spark, upto="apply")
        capsys.readouterr()  # drop the seeding chatter
        OLAF.health()
        OLAF.status()
        OLAF.explain()
        OLAF.grants(role="SalesReaders")  # a metaclass passthrough
        OLAF.configure(env="qa")
    printed = capsys.readouterr().out
    for action in ("health", "status", "explain", "grants", "configure"):
        assert f"✅ {action}" in printed, action
    # each one names the COLUMNS it hands back, so the frame is neither missed nor opaque...
    assert printed.count("→  DataFrame[") == 5
    # ...on a line of its own, blank-line separated — the same footer a mode gets, so the answer to
    # "what can I use next" is in the same place whatever was called
    lines = printed.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("✅"):
            assert "DataFrame" not in line
            assert lines[i + 1] == ""
            assert lines[i + 2].startswith("→  DataFrame[")
    # ...but never a binding: the name is the caller's, so any guess would be wrong as often as
    # it was right, and how to render it is theirs too
    assert "display(" not in printed


def test_the_announcement_carries_the_answer_where_there_is_one(capsys):
    """A bare "it ran" is not worth a line. health and diagnose_member carry the tally, which is
    the thing the caller was going to look for in the frame anyway."""
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        ols_seed(spark, upto="apply")
        capsys.readouterr()
        OLAF.health()
        healthy = capsys.readouterr().out
        OLAF.diagnose_member("nobody@contoso.com")
        diagnosed = capsys.readouterr().out
    assert "9 check(s) · all pass" in healthy
    assert "first failing step: member_in_table" in diagnosed


def test_a_mode_names_its_frame_in_a_footer(capsys):
    """A mode's frame is built from the envelope AFTER the verdict block has printed, so its hint
    cannot ride the verdict line. The footer lands where the notebook echoes the frame's own repr,
    which is the line it exists to decode."""
    spark = build_spark()
    with ols_env(spark):
        frame = OLAF.setup()
    printed = capsys.readouterr().out.splitlines()
    # ALWAYS last, and never butted against the wrapped JSON above it: the one line that says what
    # you can use next is otherwise the one you cannot find
    assert printed[-1] == f"→  DataFrame[{', '.join(frame.columns)}]"
    assert printed[-2] == ""
    assert printed[-3].startswith("OLAF.last_result: ")


def test_a_long_schema_is_summarised_rather_than_wrapped(capsys):
    """The log passthroughs hand back 27 columns. Spelled out in full they push the verdict off
    the top of a narrow cell, which is the opposite of what a one-line hint is for."""
    spark = build_spark()
    with ols_env(spark):
        OLAF.setup()  # the log table has to exist before it can be queried
        frame = OLAF.runs()
    assert len(frame.columns) > HINT_COLUMNS  # the case this test is about still exists
    # the LAST hint: setup() printed one of its own on the way to making the log table exist
    hint = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("→  DataFrame[")][-1]
    named = hint.split("DataFrame[")[1].rstrip("]")
    assert named.endswith(f", +{len(frame.columns) - HINT_COLUMNS} more")
    assert named.split(", ")[:HINT_COLUMNS] == list(frame.columns[:HINT_COLUMNS])


def test_the_announcement_never_counts_a_lazy_frame(capsys):
    """config_at/at/table_history hand back a Spark frame that has not been collected. Announcing a
    row count would run a query the caller never asked for, so the line names the COLUMNS instead —
    schema metadata, which costs nothing to read."""
    spark = build_spark()
    with ols_env(spark):
        frame = OLAF.config_at(version=3)
    printed = capsys.readouterr().out.splitlines()
    assert printed == ["✅ config_at", "", f"→  DataFrame[{', '.join(frame.columns)}]"]
    assert not any(ch.isdigit() for ch in printed[0])  # no row count anywhere in the verdict


def test_audit_unknown_attribute_raises():
    with pytest.raises(AttributeError):
        OLAF.definitely_not_a_method  # noqa: B018 — triggers the metaclass reject


# ---------------------------------------------------------------------------------------------
# RuntimeOLAFFlatFacade — every method is a FIRST-LEVEL member of OLAF; the deployment/audit/
# maintenance namespaces are removed entirely; OLAF.validate() forwards the core `validate`
# mode faithfully with ZERO writes.
# ---------------------------------------------------------------------------------------------

FLAT_METHODS = (
    "generate",
    "validate",
    "plan",
    "apply",
    "rollback",
    "explain",  # deployment
    "show",
    "trace",  # audit (explicit)
    "setup",
    "health",
    "status",
    "diagnose_member",  # maintenance
    "load_config",  # authoring
    "grants",
    "out_of_band",
    "who_can_access",  # audit passthroughs
    "configure",  # pre-existing
)


@pytest.mark.parametrize("name", FLAT_METHODS)
def test_every_method_is_callable_at_first_level(name):
    assert hasattr(OLAF, name), f"OLAF.{name} is missing"
    assert callable(getattr(OLAF, name)), f"OLAF.{name} is not callable"


@pytest.mark.parametrize("ns", ["deployment", "audit", "maintenance"])
def test_the_three_namespaces_are_gone(ns):
    assert not hasattr(OLAF, ns), f"OLAF.{ns} should be removed by the flatten"


def test_a_metaclass_forwarded_audit_method_still_coerces_to_a_frame():
    # OLAF.grants(...) resolves through the metaclass __getattr__ (moved onto OLAF itself) and
    # rides OLAF._as_frame.
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        ols_seed(spark, upto="apply")
        df = OLAF.grants(role="SalesReaders")
    assert hasattr(df, "collect") and hasattr(df, "columns")  # a DataFrame
    assert "role_name" in df.columns


def test_show_and_trace_hit_the_explicit_methods_not_the_passthrough():
    # show/trace are explicit staticmethods (route through run_mode + _show_view/_trace_view);
    # they must shadow the metaclass, never resolve as passthroughs.
    assert "show" not in _OLAF_AUDIT_PASSTHROUGH
    assert "trace" not in _OLAF_AUDIT_PASSTHROUGH
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        ols_seed(spark, upto="apply")
        tdf = OLAF.trace()
    row = ols_rows(tdf)[0]
    assert (row["mode"], row["status"]) == ("trace", "success")
    assert "live_role_count" in tdf.columns  # the trace snapshot, not a passthrough frame


def test_validate_forwards_the_run_mode_envelope_on_a_clean_config():
    """Evidentiary: OLAF.validate() stashes the SAME envelope run_mode('validate', …)
    produces (a one-line forward), and writes NOTHING — a clean config -> success."""
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        authored(spark)
        log_before = list(spark._store.get(LOG_TABLE, []))
        df = OLAF.validate()
        via_facade = OLAF.last_result
        direct = run_mode("validate", OLAF._params(), OLAF._spark())
        log_after = list(spark._store.get(LOG_TABLE, []))
    assert via_facade["mode"] == "validate"  # NOT plan/generate — catches a mis-forward
    assert via_facade["status"] == "success"
    assert (via_facade["mode"], via_facade["status"], via_facade["message"]) == (
        direct["mode"],
        direct["status"],
        direct["message"],
    )
    assert ols_rows(df)[0]["mode"] == "validate"  # the compact _view status row
    assert log_before == log_after  # ZERO writes — no log row, not even a rejected one


def test_validate_forwards_the_run_mode_envelope_on_an_invalid_config():
    """Evidentiary, blocking half: an invalid config (members unseeded -> the No-Graph gate)
    makes validate BLOCK, and the facade forwards that same blocked envelope WITHOUT raising — the
    SAME error surface generate would show."""
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        OLAF.setup()
        spark._store[CONFIG_TABLE] = sample_config_rows()  # members NOT seeded
        df = OLAF.validate()
        via_facade = OLAF.last_result
        direct = run_mode("validate", OLAF._params(), OLAF._spark())
    assert (via_facade["mode"], via_facade["status"]) == ("validate", "blocked")
    assert via_facade["status"] == direct["status"]
    assert ols_rows(df)[0]["status"] == "blocked"  # never raises


# ---------------------------------------------------------------------------------------------
# RuntimeOLSDispatchGuard — the Run cell's `if mode:` guard: an empty `mode` (the %run-as-library
# case) dispatches NOTHING; a set `mode` dispatches normally.
# ---------------------------------------------------------------------------------------------


def exec_run_cell(mode, spy):
    g = {
        "mode": mode,
        "spark": build_spark(),
        "run_and_exit": spy,
        # the no-mode branch prints the runtime's version, which lives in the notebook's namespace
        "__version__": __version__,
        "keep_unmanaged": False,
        "rebuild": False,
        "if_match": True,
        "control_data_isolation_attestation": "test-access-review/run-cell",
        "tenant_id": "",
        "lakehouse_name": "LH_Demo",
        "config_table": CONFIG_TABLE,
        "mapping_table": MAPPING_TABLE,
        "log_table": LOG_TABLE,
        "member_table": MEMBER_TABLE,
        "mapping_history_dir": CTL_MAPPING_HISTORY_DIR,
        "role_backup_dir": BACKUP_DIR,
        "verbosity": "verbose",
        # the run cell prints through the gate, so the gate must be in the namespace it execs in
        "Say": Say,
        "env": "dev",
        "batch_id": "",
        "by": "table",
        "subject": "",
        "rollback_to_version": "",
        "rollback_reason": "",
    }
    exec(compile(ols_run_cell_source(), "<run-cell>", "exec"), g)


def test_empty_mode_does_not_dispatch():
    calls = []
    exec_run_cell("", lambda *a, **k: calls.append((a, k)))
    assert calls == []  # mode="" -> guard skips dispatch (and never notebook.exit)


def test_nonempty_mode_dispatches():
    calls = []
    exec_run_cell("setup", lambda *a, **k: calls.append((a, k)))
    assert len(calls) == 1
    assert calls[0][0][0] == "setup"  # dispatched with the set mode


def test_every_declared_parameter_reaches_the_params_dict():
    """The trap: the Run cell builds `params` from a HARDCODED key list, so a parameter declared in
    the parameters cell but absent from that list never reaches run_mode AT ALL — no error, no
    default, silently missing, and every guard that reads it is dead. Pin the two lists to each
    other instead of trusting a reviewer to notice (`mode` is the dispatch switch, not a parameter,
    and is the only name that legitimately appears in one and not the other)."""
    import ast
    import json

    from _fakes import _rt_find_ipynb

    nb = json.loads(open(_rt_find_ipynb(), encoding="utf-8").read())
    declared, dispatched = set(), set()
    for cell in nb["cells"]:
        tags = cell.get("metadata", {}).get("tags", [])
        source = "".join(cell["source"])
        if "parameters" in tags:
            declared = {
                target.id
                for node in ast.parse(source).body
                if isinstance(node, ast.Assign)
                for target in node.targets
                if isinstance(target, ast.Name)
            } - {"mode"}
        elif "run" in tags:
            for node in ast.walk(ast.parse(source)):
                if isinstance(node, ast.keyword) and node.arg == "params":
                    dispatched = {key.value for key in node.value.keys}
    assert declared  # the scrape found the cells at all
    assert declared == dispatched


def test_explain_previews_and_still_reports_the_environment_problems():
    """explain() runs the same checks as validate() -- config rules AND the No-Graph member gate --
    and reports everything, but it never gates: a config whose RULES are sound still gets its
    preview, because an unseeded member says nothing about what the config would produce.

    The one documented exception is the lakehouse target guard, which resolves the declared
    lakehouse through the Fabric API and so needs a live client explain() does not have. The
    message says so rather than leaving it implied.
    """
    spark, client = build_spark(), FakeFabricClient([])
    buf = io.StringIO()
    with ols_env(spark, client), contextlib.redirect_stdout(buf):
        OLAF.setup()
        spark._store[CONFIG_TABLE] = [
            make_row(
                role_name="R1",
                include_tables="sales.orders",
                rls_condition="region = 'th'",
                include_group_names="sg-analysts",
            )
        ]  # members deliberately NOT seeded
        df = OLAF.explain()
        verdict = ols_rows(OLAF.validate())[0]
    out = buf.getvalue()

    assert df.columns == EXPLAIN_COLUMNS, "a sound config still previews"
    assert "onelake_security_member" in out, "the member gate must be reported here too"
    assert "lakehouse target unchecked" in out, "the one skipped layer has to name itself"
    assert verdict["status"] == "blocked", "validate is the gate; explain is not"


def test_explain_preview_survives_p3_shape_guard_a_alone(capsys):
    """Pins the explain() boundary. The P3 shape (an include value absent from the
    member table, cancelled by its own exclude) is the case only this gate can catch: the name
    never becomes effective, so nothing else looks at it. The config's RULES are sound, so
    Generate.rows returns no errors and the preview MUST survive even though the
    declared-but-not-effective name is unknown to the member table -- which is what proves the
    gate's error landed in `resolution_errors` and not in `errors`, the config-author list
    explain() branches on. Move it to `errors` and this test goes red."""
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        OLAF.setup()
        spark._store[CONFIG_TABLE] = [
            make_row(
                role_name="R",
                include_tables="sales.orders",
                include_group_names="sg-analysts",  # supplies survivors -> C1 does not fire
                include_user_names="ghost@x.com",
                exclude_user_names="ghost@x.com",  # cancels its own include -> P3
            )
        ]
        spark._store[MEMBER_TABLE] = [
            member_cache_row("Group", "sg-analysts", "a0000000-0000-0000-0000-000000000002")
            # ghost@x.com is deliberately NOT seeded -- it never becomes effective, so only
            # Guard A's declared-name pass sees it.
        ]
        capsys.readouterr()
        df = OLAF.explain()
    out = capsys.readouterr().out
    assert df.columns == EXPLAIN_COLUMNS, "P3 fires Guard A alone -- the preview must survive"
    preview = ols_rows(df)
    assert len(preview) == 1
    assert preview[0]["role_name"] == "R"
    assert "ghost@x.com" in out
    assert "not found in onelake_security_member" in out  # the gate's own literal string
    assert "problem(s) above" in out  # all_errors non-empty, still announced


def test_explain_previews_what_a_member_pattern_expanded_to_not_the_pattern():
    """The mitigation for the security shape member wildcards introduce.

    A member pattern makes onelake_security_member an IMPLICIT GRANT LIST — adding a principal
    there, often for an unrelated role, joins it to every matching role with nothing in the config
    diff to show it. A reviewer approving a deploy therefore has to be able to see the names, not
    the glob, and `explain()` is the only surface that can show them: `plan()` returns
    {role: action} read from the MAPPING, which by design stores the expansion rather than the
    pattern, so plan structurally cannot display `sg-* -> [a, b]`.

    No machinery was needed — expansion runs inside the shared validation pipeline explain() calls,
    so it renders the expanded names for free. This asserts that, because "for free" is exactly the
    kind of claim that rots.
    """
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        OLAF.setup()
        spark._store[CONFIG_TABLE] = [
            {**r, "include_group_names": "glob:sg-*"} for r in sample_config_rows()
        ]
        seed_sample_members(spark)
        rows = ols_rows(OLAF.explain())

    members = {r["members"] for r in rows}
    assert members == {GRP_READERS_NAME, f"{GRP_READERS_NAME};{SVC_LOADER_NAME}"}, members
    assert not any("*" in r["members"] for r in rows), "explain showed the glob, not the expansion"
