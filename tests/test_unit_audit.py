"""The Audit read surface — run/failure queries, generation lineage, grant provenance, coverage,
drift, config diffing and member resolution — as pytest functions.

Ported from `olaf_test_unit.ipynb` class `AuditTests`.
"""

import json
from unittest import mock

import pytest

from _olaf_runtime import (
    CONFIG_AUTHOR_COLUMNS,
    LOG_COLUMNS,
    MAPPING_COLUMNS,
    Audit,
    Catalog,
    Hash,
    Log,
    Parse,
    UsageError,
    _CONFIG_DIFF_FIELD_COLUMNS,
)
from _fakes import (
    CONFIG_TABLE,
    LOG_TABLE,
    MAPPING_TABLE,
    MEMBER_TABLE,
    FakeFabricClient,
    FakeRow,
    FakeSpark,
    _Result,
    build_spark,
    fake_role,
    member_cache_row,
    sample_config_rows,
    seed_validate_row,
)


def trail(log_rows):
    """An Audit over a spark seeded with the given log rows."""
    spark = build_spark()
    spark._store[LOG_TABLE] = log_rows
    return Audit(spark, CONFIG_TABLE, MAPPING_TABLE, LOG_TABLE)


def history_trail(history_rows):
    """An Audit whose spark answers DESCRIBE HISTORY with the given rows."""
    spark = build_spark()
    spark._history_rows = history_rows
    return Audit(spark, CONFIG_TABLE, MAPPING_TABLE, LOG_TABLE)


def bare_audit():
    """A bare Audit trail (no log rows) for config_at's SQL-capture tests -- FakeSpark.sql()
    stashes the emitted query on the returned _Result's _captured_sql (see fixtures)."""
    return Audit(build_spark(), CONFIG_TABLE, MAPPING_TABLE, LOG_TABLE)


def mapping_row(config_hash_value, role_name="SalesReaders", config_version=1):
    """A realistic onelake_security_mapping row: MAPPING_COLUMNS (security fields, generate's
    actual output) + MAPPING_PROVENANCE_COLUMNS -- NEVER mapping_hash/mapping_version, which
    are not columns on this table (generate stamps those only onto the log row, see
    Log.set_mapping_provenance). current_generation() must COMPUTE them instead."""
    row = {c: None for c in MAPPING_COLUMNS}
    row.update(
        {
            "role_name": role_name,
            "workspace_name": "WS_Demo",
            "lakehouse_name": "LH_Demo",
            "scope_path": "sales.orders",
            "scope_type": "table",
            "permission": "Read",
        }
    )
    row.update(
        {
            "config_hash": config_hash_value,
            "config_version": config_version,
            "framework_version": "0.1.0",
            "generated_at": "2026-07-13T00",
        }
    )
    return row


def _fixture_mapping_provenance():
    """A complete one-generation stamp for policy fixtures that exercise read-only consumers."""
    return {
        "config_hash": "fixture-config",
        "config_version": 1,
        "framework_version": "0.1.0",
        "generated_at": "2026-07-13T00",
    }


def config_row(role_name, include_tables, **overrides):
    """A minimal onelake_security_config row -- every CONFIG_AUTHOR_COLUMNS key defaults to
    None (sample_config_rows()' base-dict style) with role_name/include_tables set and any
    other authored field overridden."""
    row = {c: None for c in CONFIG_AUTHOR_COLUMNS}
    row.update(role_name=role_name, include_tables=include_tables, **overrides)
    return row


def diff_audit(versions):
    """An Audit whose config_at is monkeypatched PER INSTANCE to return the given
    version -> raw-config-rows mapping. FakeSpark's VERSION AS OF branch cannot time-travel
    (see the config_at SQL-capture tests) -- it always returns an empty _Result -- so
    config_diff's tests bind config_at directly instead of routing through spark.sql().
    config_diff only ever calls self.config_at(v).collect() and reads each row via .asDict(),
    so a lambda returning an _Result of FakeRow(...) is a legal, minimal stand-in for that
    seam (documented in task-10-report.md)."""
    a = Audit(build_spark(), CONFIG_TABLE, MAPPING_TABLE, LOG_TABLE)
    a.config_at = lambda version=None, date=None: _Result([FakeRow(r) for r in versions[version]])
    return a


def value_audit(versions):
    """An Audit whose table_history("config") AND config_at are BOTH monkeypatched per
    instance -- value_history's version-series faking seam, extending diff_audit's
    config_at-only stub to also fake the version list (value_history discovers versions via
    table_history FIRST). `versions` is version(int) -> raw config rows for that version;
    table_history("config") is stubbed to report exactly those version numbers (nothing
    else -- value_history only reads the "version" column off its result), and config_at(v)
    returns versions[v], the same _Result/FakeRow shape diff_audit already uses."""
    a = Audit(build_spark(), CONFIG_TABLE, MAPPING_TABLE, LOG_TABLE)
    a.table_history = lambda table: _Result([FakeRow({"version": v}) for v in versions])
    a.config_at = lambda version=None, date=None: _Result([FakeRow(r) for r in versions[version]])
    return a


# ---------------------------------------------------------------------------------------------
# runs / last_run / failures / log_history / batch
# ---------------------------------------------------------------------------------------------


def test_runs_filters_by_mode_and_status():
    rows = [
        {
            "mode": "apply",
            "status": "success",
            "env": "dev",
            "run_at": "2026-07-13T01",
            "batch_id": "b1",
        },
        {
            "mode": "plan",
            "status": "success",
            "env": "dev",
            "run_at": "2026-07-13T00",
            "batch_id": "b0",
        },
    ]
    got = [r.asDict() for r in trail(rows).runs(mode="apply").collect()]
    assert len(got) == 1
    assert got[0]["mode"] == "apply"


def test_runs_since_floors_by_run_at():
    rows = [
        {
            "mode": "apply",
            "status": "success",
            "env": "dev",
            "run_at": "2026-07-13T01",
            "batch_id": "b1",
        },
        {
            "mode": "apply",
            "status": "success",
            "env": "dev",
            "run_at": "2026-07-10T00",
            "batch_id": "b0",
        },
    ]
    got = [r.asDict() for r in trail(rows).runs(since="2026-07-12").collect()]
    assert len(got) == 1
    assert got[0]["batch_id"] == "b1"


def test_runs_with_no_matches_returns_empty_df():
    rows = [
        {
            "mode": "plan",
            "status": "success",
            "env": "dev",
            "run_at": "2026-07-13T00",
            "batch_id": "b0",
        }
    ]
    out = trail(rows).runs(mode="apply")
    assert out.collect() == []
    assert out.columns == LOG_COLUMNS  # empty result still carries the typed log schema


def test_last_run_returns_newest_of_mode():
    rows = [
        {"mode": "apply", "run_at": "2026-07-13T02", "status": "success"},
        {"mode": "apply", "run_at": "2026-07-13T05", "status": "success"},
    ]
    assert trail(rows).last_run("apply")["run_at"] == "2026-07-13T05"


def test_last_run_none_when_no_rows():
    assert trail([]).last_run("apply") is None


def test_last_run_defaults_to_no_mode_filter():
    rows = [
        {"mode": "apply", "run_at": "2026-07-13T01", "status": "success"},
        {"mode": "plan", "run_at": "2026-07-13T09", "status": "success"},
    ]
    assert trail(rows).last_run()["mode"] == "plan"


def test_failures_selects_error_rows():
    rows = [
        {"status": "success", "error_category": None},
        {"status": "rejected", "error_category": "guard", "message": "STALE"},
    ]
    assert len(trail(rows).failures().collect()) == 1


def test_failures_since_floors_by_run_at():
    rows = [
        {"status": "rejected", "error_category": "guard", "run_at": "2026-07-10T00"},
        {"status": "rejected", "error_category": "guard", "run_at": "2026-07-13T00"},
    ]
    got = [r.asDict() for r in trail(rows).failures(since="2026-07-12").collect()]
    assert len(got) == 1
    assert got[0]["run_at"] == "2026-07-13T00"


def test_log_history_filters_by_subject():
    rows = [{"role_name": "A", "run_at": "1"}, {"role_name": "B", "run_at": "2"}]
    assert len(trail(rows).log_history(role="A").collect()) == 1


def test_batch_returns_all_rows_of_batch():
    rows = [{"batch_id": "b1"}, {"batch_id": "b1"}, {"batch_id": "b2"}]
    assert len(trail(rows).batch("b1").collect()) == 2


# ---------------------------------------------------------------------------------------------
# current_generation / is_stale / verify_chain
# ---------------------------------------------------------------------------------------------


def test_current_generation_and_is_stale():
    spark = build_spark()
    mapping_rows = [mapping_row("OLD")]
    spark._store[MAPPING_TABLE] = mapping_rows
    spark._store[CONFIG_TABLE] = sample_config_rows()  # active rows -> a real, different hash
    at = Audit(spark, CONFIG_TABLE, MAPPING_TABLE, LOG_TABLE)
    gen = at.current_generation()
    assert gen["config_version"] == 1
    # mapping_hash/mapping_version are COMPUTED, not read off (real mapping rows never carry
    # those columns) -- verify against the same functions generate() itself calls.
    assert gen["mapping_hash"] == Hash.mapping_content(mapping_rows)
    assert gen["mapping_version"] == Catalog.config_version(spark, MAPPING_TABLE)
    assert at.is_stale()  # OLD != hash(active config)


def test_is_stale_false_when_hash_matches():
    spark = build_spark()
    spark._store[CONFIG_TABLE] = sample_config_rows()
    # the SAME read is_stale() makes: Catalog.active_config_rows — active rows projected
    # to CONFIG_AUTHOR_COLUMNS. (sample_config_rows carries a foreign workspace_name
    # column, so a full-row hash would NOT match — that asymmetry is the point.)
    active = Catalog.active_config_rows(spark, CONFIG_TABLE)
    spark._store[MAPPING_TABLE] = [mapping_row(Hash.config(active))]
    assert not Audit(spark, CONFIG_TABLE, MAPPING_TABLE, LOG_TABLE).is_stale()


def test_is_stale_ignores_inactive_config_rows():
    """is_stale() must honor the SAME 'where active = true' filter the generator applies when
    it builds the mapping's config_hash -- a change to an INACTIVE row must never
    flip the verdict, only a change to an active row may."""
    spark = build_spark()
    active_row = {
        **{c: None for c in CONFIG_AUTHOR_COLUMNS},
        "role_name": "ActiveRole",
        "active": True,
        "permission": "Read",
    }
    inactive_row = {
        **{c: None for c in CONFIG_AUTHOR_COLUMNS},
        "role_name": "InactiveRole",
        "active": False,
        "permission": "Write",
    }
    spark._store[CONFIG_TABLE] = [active_row, inactive_row]
    # config_hash computed over the ACTIVE row only -- what a real generate() would stamp.
    spark._store[MAPPING_TABLE] = [mapping_row(Hash.config([active_row]))]
    at = Audit(spark, CONFIG_TABLE, MAPPING_TABLE, LOG_TABLE)
    assert not at.is_stale()
    # Flip the INACTIVE row's content (would change config_hash if it were included) --
    # the verdict must not change, proving the inactive row is excluded from the comparison.
    inactive_row["role_name"] = "InactiveRoleChanged"
    inactive_row["permission"] = "Admin"
    assert not at.is_stale()


def test_current_generation_none_when_no_mapping():
    spark = build_spark()
    spark._store[MAPPING_TABLE] = []  # table exists (post-setup), no generation written yet
    assert Audit(spark, CONFIG_TABLE, MAPPING_TABLE, LOG_TABLE).current_generation() is None


def test_is_stale_true_when_no_mapping():
    spark = build_spark()
    spark._store[MAPPING_TABLE] = []
    assert Audit(spark, CONFIG_TABLE, MAPPING_TABLE, LOG_TABLE).is_stale()


def _stage_complete(mode, operation, config_hash, mapping_hash, run_at, **extra):
    """A versioned completion record, hand-authored so a wrong Audit predicate cannot satisfy it."""
    return {
        "mode": mode,
        "action": "complete",
        "status": "success",
        "config_hash": config_hash,
        "mapping_hash": mapping_hash,
        "run_at": run_at,
        "message": json.dumps({"schema": 1, "operation": operation, **extra}),
    }


def test_verify_chain_requires_the_exact_ordered_stage_completions():
    spark = build_spark()
    mapping_rows = [mapping_row("C")]
    spark._store[MAPPING_TABLE] = mapping_rows
    real_mapping_hash = Hash.mapping_content(mapping_rows)  # what current_generation() computes
    spark._store[LOG_TABLE] = [
        _stage_complete("generate", "generate", "C", real_mapping_hash, "2026-07-13T01"),
        _stage_complete("plan", "plan", "C", real_mapping_hash, "2026-07-13T02"),
        _stage_complete(
            "apply",
            "apply",
            "C",
            real_mapping_hash,
            "2026-07-13T03",
            backup_path="Files/security/role-backups/a.json",
            payload_hash="p",
        ),
    ]
    status = Audit(spark, CONFIG_TABLE, MAPPING_TABLE, LOG_TABLE).verify_chain()
    assert status.ok
    assert status.details["state"] == "applied"
    assert status.details["stage_rows"] == {"generated": 1, "planned": 1, "applied": 1}


def test_verify_chain_broken_when_log_mapping_hash_disagrees():
    # A completion for another mapping must not certify the current lock-file.
    spark = build_spark()
    spark._store[MAPPING_TABLE] = [mapping_row("C")]
    spark._store[LOG_TABLE] = [
        _stage_complete("generate", "generate", "C", "OTHER", "2026-07-13T01"),
        _stage_complete("plan", "plan", "C", "OTHER", "2026-07-13T02"),
        _stage_complete(
            "apply",
            "apply",
            "C",
            "OTHER",
            "2026-07-13T03",
            backup_path="Files/security/role-backups/a.json",
            payload_hash="p",
        ),
    ]
    status = Audit(spark, CONFIG_TABLE, MAPPING_TABLE, LOG_TABLE).verify_chain()
    assert not status.ok
    assert status.details["state"] == "missing_mapping"


def test_verify_chain_rejects_an_apply_that_precedes_the_matching_plan():
    """Reordering the two terminal records must not make a stale plan look reviewed."""
    spark = build_spark()
    mapping_rows = [mapping_row("C")]
    mapping_hash = Hash.mapping_content(mapping_rows)
    spark._store[MAPPING_TABLE] = mapping_rows
    spark._store[LOG_TABLE] = [
        _stage_complete("generate", "generate", "C", mapping_hash, "2026-07-13T01"),
        _stage_complete(
            "apply",
            "apply",
            "C",
            mapping_hash,
            "2026-07-13T02",
            backup_path="Files/security/role-backups/a.json",
            payload_hash="p",
        ),
        _stage_complete("plan", "plan", "C", mapping_hash, "2026-07-13T03"),
    ]
    status = Audit(spark, CONFIG_TABLE, MAPPING_TABLE, LOG_TABLE).verify_chain()
    assert not status.ok
    assert status.details["state"] == "incomplete"


def test_verify_chain_rejects_unmatched_stages_and_unproven_terminal_apply():
    """Only a current ordered chain counts: wrong operations, old plans, and proofless apply do not."""
    spark = build_spark()
    mapping_rows = [mapping_row("C")]
    mapping_hash = Hash.mapping_content(mapping_rows)
    spark._store[MAPPING_TABLE] = mapping_rows
    spark._store[LOG_TABLE] = [
        _stage_complete("generate", "plan", "C", mapping_hash, "2026-07-13T01"),
        _stage_complete("plan", "plan", "C", mapping_hash, "2026-07-13T01"),
        _stage_complete("generate", "generate", "C", mapping_hash, "2026-07-13T02"),
        _stage_complete("apply", "apply", "C", mapping_hash, "2026-07-13T03"),
    ]
    status = Audit(spark, CONFIG_TABLE, MAPPING_TABLE, LOG_TABLE).verify_chain()
    assert not status.ok
    assert status.details["state"] == "incomplete"
    assert status.details["stage_rows"] == {"generated": 1, "planned": 1, "applied": 0}


def test_verify_chain_no_mapping():
    spark = build_spark()
    spark._store[MAPPING_TABLE] = []
    status = Audit(spark, CONFIG_TABLE, MAPPING_TABLE, LOG_TABLE).verify_chain()
    assert not status.ok
    assert status.details == {"reason": "no mapping"}


def test_last_successful_deployment_ignores_newer_incomplete_and_failed_apply_rows():
    """A post-PUT intent/failure can be newer than the completed rollback but is not deployment proof."""
    rows = [
        _stage_complete(
            "rollback",
            "apply",
            "C",
            "M",
            "2026-07-13T01",
            backup_path="Files/security/role-backups/rollback.json",
            payload_hash="rollback-payload",
        ),
        {
            "mode": "apply",
            "action": "push",
            "status": "prepared",
            "config_hash": "C2",
            "mapping_hash": "M2",
            "run_at": "2026-07-13T03",
        },
        {
            "mode": "apply",
            "action": "complete",
            "status": "failed",
            "config_hash": "C2",
            "mapping_hash": "M2",
            "run_at": "2026-07-13T04",
        },
    ]
    selected = trail(rows).last_successful_deployment()
    assert selected["mode"] == "rollback"
    assert selected["run_at"] == "2026-07-13T01"


def test_last_successful_deployment_accepts_only_a_narrow_legacy_completion_fallback():
    """Old successful deployment records remain readable only when they prove plan + backup."""
    legacy = {
        "mode": "apply",
        "action": "complete",
        "status": "success",
        "config_hash": "C",
        "mapping_hash": "M",
        "run_at": "2026-07-13T01",
        "message": json.dumps({"plan": {"create": 1}, "backup_path": "Files/backups/a.json"}),
    }
    malformed = {
        **legacy,
        "run_at": "2026-07-13T02",
        "message": json.dumps({"backup_path": "Files/backups/b.json"}),
    }
    assert trail([legacy, malformed]).last_successful_deployment() == legacy


def test_last_successful_deployment_rejects_a_message_with_an_unknown_schema():
    """A versioned record never falls through to legacy parsing when its schema is not recognized."""
    row = {
        "mode": "apply",
        "action": "complete",
        "status": "success",
        "config_hash": "C",
        "mapping_hash": "M",
        "run_at": "2026-07-13T01",
        "message": json.dumps(
            {"schema": 2, "plan": {"create": 1}, "backup_path": "Files/backups/a.json"}
        ),
    }
    assert trail([row]).last_successful_deployment() is None


def test_last_successful_deployment_rejects_a_scalar_legacy_plan():
    """Legacy completion proof is an object plan, never a truthy scalar masquerading as one."""
    row = {
        "mode": "apply",
        "action": "complete",
        "status": "success",
        "config_hash": "C",
        "mapping_hash": "M",
        "run_at": "2026-07-13T01",
        "message": json.dumps({"plan": "create", "backup_path": "Files/backups/a.json"}),
    }
    assert trail([row]).last_successful_deployment() is None


def test_current_generation_refuses_mixed_mapping_provenance_in_any_row():
    """Changing only a tail generation stamp used to be silently certified from row zero."""
    spark = build_spark()
    rows = [mapping_row("C"), mapping_row("C", role_name="Other")]
    rows[1]["generated_at"] = "later"
    spark._store[MAPPING_TABLE] = rows
    with pytest.raises(UsageError, match="mapping provenance"):
        Audit(spark, CONFIG_TABLE, MAPPING_TABLE, LOG_TABLE).current_generation()


def test_drift_refuses_mixed_mapping_provenance_instead_of_using_a_tail_row_as_desired_state():
    """Read-only desired-state reports must fail closed on the same poisoned mapping generation."""
    spark = build_spark()
    rows = [mapping_row("C"), mapping_row("C", role_name="Other")]
    rows[1]["framework_version"] = "different"
    spark._store[MAPPING_TABLE] = rows
    spark._store[LOG_TABLE] = []
    spark._store[MEMBER_TABLE] = []
    audit = Audit(
        spark,
        CONFIG_TABLE,
        MAPPING_TABLE,
        LOG_TABLE,
        client=FakeFabricClient(roles=[]),
    )
    with pytest.raises(UsageError, match="mapping provenance"):
        audit.drift()


# ---------------------------------------------------------------------------------------------
# Task 4: grant provenance (grants / provenance / out_of_band)
# ---------------------------------------------------------------------------------------------


def test_grants_dedups_mixed_case_keeping_earliest():
    """Two validate/apply/success rows for the SAME (role, scope, member_id) triple in DIFFERENT
    case merge to ONE grant (lowered dedup key), and the EARLIEST run_at's original-case display
    values + provenance win -- pins both the lowered key and the min-run_at selection."""
    rows = [
        seed_validate_row(
            "SalesReaders",
            "sales.orders",
            "MEM-1",
            run_at="2026-07-13T05",
            run_by="bob@example.com",
            config_version="9",
        ),
        seed_validate_row(
            "salesreaders",
            "SALES.ORDERS",
            "mem-1",
            run_at="2026-07-13T01",
            run_by="alice@example.com",
            config_version="7",
        ),
    ]
    got = [r.asDict() for r in trail(rows).grants().collect()]
    assert len(got) == 1  # lowered triple merges the two mixed-case rows
    g = got[0]
    assert g["role_name"] == "SalesReaders"  # original case of the LATEST row -- the spelling
    assert g["scope_path"] == "sales.orders"  # the live DAR is currently holding
    assert g["member_id"] == "MEM-1"
    assert g["first_applied"] == "2026-07-13T01"  # earliest end, with the principal who set it
    assert g["first_granted_by"] == "alice@example.com"
    assert g["last_applied"] == "2026-07-13T05"  # latest end, with a DIFFERENT principal
    assert g["last_granted_by"] == "bob@example.com"
    assert g["config_version"] == "9"  # the version in effect now, not the one that started it


def test_grants_excludes_non_establishing_rows():
    """One row per exclusion reason -- none is an establishing grant, so grants() is empty. Fails
    if ANY of the four filters (action/status/mode/member_id) were dropped."""
    bad_action = seed_validate_row("R", "s", "m", run_at="1", run_by="u", config_version="1")
    bad_action["action"] = "start"
    bad_status = seed_validate_row("R", "s", "m", run_at="2", run_by="u", config_version="1")
    bad_status["status"] = "failed"
    bad_mode = seed_validate_row(
        "R", "s", "m", run_at="3", run_by="u", config_version="1", mode="plan"
    )
    no_member = seed_validate_row("R", "s", None, run_at="4", run_by="u", config_version="1")
    assert trail([bad_action, bad_status, bad_mode, no_member]).grants().collect() == []


def test_a_rollback_success_row_establishes_provenance_everywhere():
    """External security audit (2026-08-16), issue #16: a rollback chain's apply stamps its
    per-grant success rows mode=rollback. All three provenance consumers — grants() (and the
    established set / out_of_band it feeds) and trace() — must count them exactly like
    apply's own, matching the plan-record loader's mode IN ('plan','rollback') precedent."""
    rows = [
        seed_validate_row(
            "R",
            "s",
            "mem-1",
            run_at="2026-08-01T00",
            run_by="pipeline",
            config_version="7",
            mode="rollback",
        )
    ]
    audit = trail(rows)
    got = [r.asDict() for r in audit.grants().collect()]
    assert len(got) == 1 and got[0]["member_id"] == "mem-1"  # established, not dropped
    assert ("r", "s", "mem-1") in audit._established_set()
    traced = [r.asDict() for r in audit.trace().collect()]
    assert len(traced) == 1 and traced[0]["config_version"] == "7"  # trace shows the deploy


def test_a_failed_rollback_row_still_establishes_nothing():
    # the negative half: apply's failure path re-stamps its validate rows failed — a broken
    # rollback push must not adopt the grant it never landed.
    row = seed_validate_row(
        "R", "s", "mem-1", run_at="1", run_by="u", config_version="1", mode="rollback"
    )
    row["status"] = "failed"
    audit = trail([row])
    assert audit.grants().collect() == []
    assert audit.trace().collect() == []


def test_a_rollback_established_grant_is_not_out_of_band():
    log = [
        seed_validate_row(
            "R",
            "s",
            "mem-1",
            run_at="2026-08-01T00",
            run_by="pipeline",
            config_version="1",
            mode="rollback",
        )
    ]
    client = FakeFabricClient(roles=[fake_role("R", ["s"], ["mem-1"])])
    spark = build_spark()
    spark._store[LOG_TABLE] = log
    spark._store[MEMBER_TABLE] = []
    at = Audit(spark, CONFIG_TABLE, MAPPING_TABLE, LOG_TABLE, client=client)
    assert at.out_of_band().collect() == []  # restored legitimately — no false alert


def test_since_returns_dict_then_none():
    rows = [
        seed_validate_row(
            "R", "s", "m1", run_at="2026-07-13T01", run_by="alice@example.com", config_version="4"
        )
    ]
    audit = trail(rows)
    got = audit.provenance("R", "s")
    assert got is not None
    assert got["member_id"] == "m1"
    # a single establishing row makes both ends the same instant -- that is a real answer, not a
    # collapse: the grant was pushed once and never re-asserted.
    assert got["first_applied"] == "2026-07-13T01"
    assert got["last_applied"] == "2026-07-13T01"
    assert audit.provenance("Nope", "nope") is None  # no matching grant -> None


def test_out_of_band_requires_client():
    at = Audit(build_spark(), CONFIG_TABLE, MAPPING_TABLE, LOG_TABLE)
    with pytest.raises(UsageError):
        at.out_of_band()


def test_out_of_band_compares_against_established_grants_not_raw_log():
    """A live grant whose only log row is plan-mode (present in RAW _log_rows but NOT an
    establishing grant) must still surface as out-of-band -- the decoy the brief warns about. An
    apply-established grant is suppressed; a grant with no log row surfaces."""
    log = [
        seed_validate_row(
            "R",
            "s",
            "mem-established",
            run_at="2026-07-13T01",
            run_by="alice@example.com",
            config_version="1",
            mode="apply",
        ),
        seed_validate_row(
            "R",
            "s",
            "mem-planonly",
            run_at="2026-07-13T02",
            run_by="bob@example.com",
            config_version="1",
            mode="plan",
        ),
    ]
    client = FakeFabricClient(
        roles=[fake_role("R", ["s"], ["mem-established", "mem-planonly", "mem-nolog"])]
    )
    spark = build_spark()
    spark._store[LOG_TABLE] = log
    spark._store[MEMBER_TABLE] = []  # no cache needed -- id-fallback covered elsewhere
    at = Audit(spark, CONFIG_TABLE, MAPPING_TABLE, LOG_TABLE, client=client)
    got = sorted(r.asDict()["member_id"] for r in at.out_of_band().collect())
    assert got == ["mem-nolog", "mem-planonly"]


def test_out_of_band_empty_when_all_established_is_typed():
    """When every live grant is established (here via a replace-mode row), out_of_band() is empty
    but still carries its typed 4-column schema (empty-schema lesson)."""
    log = [
        seed_validate_row("R", "s", "m", run_at="1", run_by="u", config_version="1", mode="replace")
    ]
    client = FakeFabricClient(roles=[fake_role("R", ["s"], ["m"])])
    spark = build_spark()
    spark._store[LOG_TABLE] = log
    out = Audit(spark, CONFIG_TABLE, MAPPING_TABLE, LOG_TABLE, client=client).out_of_band()
    assert out.collect() == []
    assert out.columns == ["role_name", "scope_path", "member_id", "member_name"]


# ---------------------------------------------------------------------------------------------
# Task 5: generation lineage (timeline / trace / authored_by / config_at)
# ---------------------------------------------------------------------------------------------


def test_timeline_aggregates_by_generation():
    """Rows across two (config_version, config_hash) generations collapse to one entry each:
    runs = count, first_seen/last_seen = min/max run_at within that generation."""
    rows = [
        {"config_version": "7", "config_hash": "AAAA", "run_at": "2026-07-13T03"},
        {"config_version": "7", "config_hash": "AAAA", "run_at": "2026-07-13T01"},
        {"config_version": "7", "config_hash": "AAAA", "run_at": "2026-07-13T05"},
        {"config_version": "9", "config_hash": "BBBB", "run_at": "2026-07-14T08"},
        {"config_version": "9", "config_hash": "BBBB", "run_at": "2026-07-14T02"},
    ]
    got = {r.asDict()["config_version"]: r.asDict() for r in trail(rows).timeline().collect()}
    assert got["7"]["runs"] == "3"  # _df stringifies every cell
    assert got["7"]["first_seen"] == "2026-07-13T01"
    assert got["7"]["last_seen"] == "2026-07-13T05"
    assert got["7"]["config_hash"] == "AAAA"
    assert got["9"]["runs"] == "2"
    assert got["9"]["first_seen"] == "2026-07-14T02"
    assert got["9"]["last_seen"] == "2026-07-14T08"


def test_timeline_orders_generations_numerically_not_lexically():
    """config_version is a BIGINT, so the ordering must be numeric: sorted as text, generation 10
    lands BEFORE generation 9 and the timeline reads backwards past the first double-digit config
    commit. Versions 9/10/2 are chosen because they order differently under the two rules."""
    rows = [
        {"config_version": 10, "config_hash": "CCCC", "run_at": "2026-07-15T01"},
        {"config_version": 9, "config_hash": "BBBB", "run_at": "2026-07-14T01"},
        {"config_version": 2, "config_hash": "AAAA", "run_at": "2026-07-13T01"},
    ]
    got = [r.asDict()["config_version"] for r in trail(rows).timeline().collect()]
    assert got == ["2", "9", "10"]  # _df stringifies for display; the ORDER is the assertion


def test_timeline_puts_a_versionless_generation_first():
    """config_version is null when the config table cannot report a version (not Delta / DESCRIBE
    HISTORY unavailable). It must not raise and must not be compared against an int."""
    rows = [
        {"config_version": 4, "config_hash": "BBBB", "run_at": "2026-07-14T01"},
        {"config_version": None, "config_hash": "AAAA", "run_at": "2026-07-13T01"},
    ]
    got = [r.asDict()["config_version"] for r in trail(rows).timeline().collect()]
    assert got == [None, "4"]


def test_trace_deploying_generations_newest_first():
    """trace() returns the DEPLOYING (mode in apply) validate+success rows for a subject,
    newest first, projected to (config_version, config_hash, run_at, run_by). A newer plan-mode
    validate row (a dry run that never deployed) is excluded -- same 'drop plan runs' rule as
    grants(); pins the mode-filter decision."""
    apply_row = seed_validate_row(
        "SalesReaders",
        "sales.orders",
        "mem-1",
        run_at="2026-07-13T05",
        run_by="bob@example.com",
        config_version="9",
        mode="apply",
    )
    apply_row["config_hash"] = "BBBB"
    replace_row = seed_validate_row(
        "SalesReaders",
        "sales.orders",
        "mem-1",
        run_at="2026-07-13T02",
        run_by="alice@example.com",
        config_version="7",
        mode="replace",
    )
    replace_row["config_hash"] = "AAAA"
    plan_row = seed_validate_row(
        "SalesReaders",
        "sales.orders",
        "mem-1",
        run_at="2026-07-13T09",
        run_by="mallory@example.com",
        config_version="99",
        mode="plan",
    )
    got = [
        r.asDict()
        for r in trail([apply_row, replace_row, plan_row]).trace(role="SalesReaders").collect()
    ]
    assert [g["config_version"] for g in got] == ["9", "7"]  # plan (T09, cv 99) dropped
    assert got[0]["run_by"] == "bob@example.com"
    assert got[0]["config_hash"] == "BBBB"


def test_trace_excludes_non_deploying_rows():
    """One row per exclusion reason (bad action / bad status / plan mode) -- none is a deploying
    generation, so trace() is empty. Fails if any of the three filters were dropped."""
    bad_action = seed_validate_row("R", "s", "m", run_at="1", run_by="u", config_version="1")
    bad_action["action"] = "start"
    bad_status = seed_validate_row("R", "s", "m", run_at="2", run_by="u", config_version="1")
    bad_status["status"] = "failed"
    plan_mode = seed_validate_row(
        "R", "s", "m", run_at="3", run_by="u", config_version="1", mode="plan"
    )
    assert trail([bad_action, bad_status, plan_mode]).trace().collect() == []


def test_authored_by_returns_username():
    rows = [
        {
            "version": 7,
            "timestamp": "2026-07-13T10:00",
            "userName": "alice@x.com",
            "operationParameters": {},
        }
    ]
    assert history_trail(rows).authored_by(7) == {
        "version": 7,
        "timestamp": "2026-07-13T10:00",
        "user": "alice@x.com",
    }


def test_authored_by_falls_back_to_operation_parameters():
    """userName None -> fall back to operationParameters.userName (the `or` right branch)."""
    rows = [
        {
            "version": 7,
            "timestamp": "2026-07-13T10:00",
            "userName": None,
            "operationParameters": {"userName": "bob@x.com"},
        }
    ]
    assert history_trail(rows).authored_by(7)["user"] == "bob@x.com"


def test_authored_by_none_when_version_absent():
    rows = [{"version": 7, "timestamp": "t", "userName": "a@x.com", "operationParameters": {}}]
    assert history_trail(rows).authored_by(999) is None


def test_authored_by_none_when_no_history():
    # default build_spark -> _history_rows is None -> empty DESCRIBE HISTORY -> None
    assert trail([]).authored_by(1) is None


def test_config_at_builds_version_as_of_query():
    """config_at issues a Delta time-travel read; assert the exact VERSION AS OF SQL, that a
    numeric string is accepted, and that int() coercion blocks non-numeric (SQL-injection-ish)
    passthrough. No FakeSpark time-travel needed -- a capturing fake covers the line honestly."""
    captured = {}

    class _CapSpark:
        def sql(self, q):
            captured["q"] = q
            return "df"

    at = Audit(_CapSpark(), CONFIG_TABLE, MAPPING_TABLE, LOG_TABLE)
    assert at.config_at(5) == "df"
    assert captured["q"] == f"SELECT * FROM {CONFIG_TABLE} VERSION AS OF 5"
    at.config_at("5")  # numeric string accepted, coerced to 5
    assert captured["q"] == f"SELECT * FROM {CONFIG_TABLE} VERSION AS OF 5"
    with pytest.raises(ValueError):  # int() rejects a non-numeric version
        at.config_at("5; DROP TABLE onelake_security_config")


def test_config_at_version_sql():
    """config_at(version=...) still emits VERSION AS OF -- captured via FakeSpark's
    _captured_sql (the generic SQL-capture branch added alongside this generalization)."""
    assert "VERSION AS OF 3" in bare_audit().config_at(3)._captured_sql


def test_config_at_date_sql():
    """config_at(date=...) emits TIMESTAMP AS OF instead of VERSION AS OF."""
    assert "TIMESTAMP AS OF '2026-07-01'" in bare_audit().config_at(date="2026-07-01")._captured_sql


@pytest.mark.parametrize(
    "kwargs", [{}, {"version": 3, "date": "2026-07-01"}], ids=["neither", "both"]
)
def test_config_at_requires_exactly_one(kwargs):
    """Neither given, or both given -- ValueError (the exactly-one-of guard)."""
    with pytest.raises(ValueError):
        bare_audit().config_at(**kwargs)


def test_config_at_date_rejects_bad_shape():
    """A `date` that isn't a plausible Delta timestamp/date (e.g. a SQL-injection payload)
    raises ValueError BEFORE any TIMESTAMP AS OF interpolation -- the date-side counterpart to
    int()'s version-side coercion. A well-formed date (incl. an HH:MM:SS clock) still builds."""
    a = bare_audit()
    with pytest.raises(ValueError):
        a.config_at(date="2026'; DROP TABLE onelake_security_config")
    assert (
        "TIMESTAMP AS OF '2026-07-01 12:30:00'"
        in a.config_at(date="2026-07-01 12:30:00")._captured_sql
    )


# ---------------------------------------------------------------------------------------------
# Task 6: report() operational snapshot
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    ["current_generation", "last_generate", "last_apply", "established_ever", "is_stale"],
)
def test_report_carries_every_snapshot_key(key):
    assert key in _report_without_client()


def _report_without_client():
    """report() with NO client, over one establishing apply grant and an empty mapping table."""
    rows = [
        seed_validate_row(
            "R",
            "s",
            "m1",
            run_at="2026-07-13T01",
            run_by="alice@example.com",
            config_version="1",
            mode="apply",
        )
    ]
    spark = build_spark()
    spark._store[LOG_TABLE] = rows
    spark._store[MAPPING_TABLE] = []  # table exists, nothing generated yet
    return Audit(spark, CONFIG_TABLE, MAPPING_TABLE, LOG_TABLE).report()


def test_report_shape():
    """established_ever counts the establishing grants; no mapping yet -> current_generation None
    and is_stale True. Every CURRENT-state field is client-gated and absent without one."""
    rep = _report_without_client()
    assert rep["current_generation"] is None
    assert rep["established_ever"] == 1
    assert rep["is_stale"]
    for gated in (
        "out_of_band",
        "live_grant_count",
        "live_role_count",
        "desired_grant_count",
        "missing",
        "unexpected",
        "policy_checked",
        "policy_mismatch",
        "in_sync",
    ):
        assert gated not in rep, gated  # reading the live DAR needs a client


def test_report_counts_out_of_band_with_client():
    """report() WITH a client also carries out_of_band -- an int equal to the live-grants-minus-
    established count (one live grant established via an apply row, one not -> 1). Covers the
    `if self.client is not None` true branch and pins report() to a SINGLE out_of_band() call."""
    log = [
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
    client = FakeFabricClient(roles=[fake_role("R", ["s"], ["mem-established", "mem-oob"])])
    spark = build_spark()
    spark._store[LOG_TABLE] = log
    spark._store[MAPPING_TABLE] = []
    spark._store[MEMBER_TABLE] = []  # no cache needed -- id-fallback covered elsewhere
    rep = Audit(spark, CONFIG_TABLE, MAPPING_TABLE, LOG_TABLE, client=client).report()
    assert "out_of_band" in rep
    assert isinstance(rep["out_of_band"], int)
    assert rep["out_of_band"] == 1  # mem-oob is live but not established
    assert rep["established_ever"] == 1
    assert rep["live_grant_count"] == 2  # both live grants, provenanced or not
    assert rep["live_role_count"] == 1
    # equivalence with the standalone listing — report derives the count from its OWN grants
    # collection now (issue #2: it used to collect the whole log a second time per call)
    at = Audit(spark, CONFIG_TABLE, MAPPING_TABLE, LOG_TABLE, client=client)
    assert rep["out_of_band"] == len(at.out_of_band().collect())


def test_report_counts_describe_the_live_state_not_the_whole_log():
    """A key that reads as current state has to be current state.

    The log accumulates across versions and environments — a retired role stays in it forever —
    so counting from the log under a name like role_count made a lakehouse holding 30 roles
    report 84.
    """
    log = [
        seed_validate_row(
            "Retired",
            "s",
            "m1",
            run_at="2026-07-01T01",
            run_by="a@b.c",
            config_version="1",
            mode="apply",
        ),
        seed_validate_row(
            "Current",
            "s",
            "m1",
            run_at="2026-07-02T01",
            run_by="a@b.c",
            config_version="2",
            mode="apply",
        ),
    ]
    client = FakeFabricClient(roles=[fake_role("Current", ["s"], ["m1"])])
    spark = build_spark()
    spark._store[LOG_TABLE] = log
    spark._store[MAPPING_TABLE] = []
    spark._store[MEMBER_TABLE] = []
    rep = Audit(spark, CONFIG_TABLE, MAPPING_TABLE, LOG_TABLE, client=client).report()
    assert rep["established_ever"] == 2  # history: both Retired and Current
    assert rep["live_role_count"] == 1  # today: only Current survives
    assert rep["live_grant_count"] == 1


def test_report_sees_a_retired_role_that_came_back():
    """out_of_band compares against the cumulative established set, so a retired role that
    reappears still looks provenanced.

    `unexpected` compares against the CURRENT generation's mapping and is the only field that
    catches this — and it is the case most worth catching: something deleted that came back.
    """
    log = [
        seed_validate_row(
            "Retired",
            "s",
            "m1",
            run_at="2026-07-01T01",
            run_by="a@b.c",
            config_version="1",
            mode="apply",
        ),
    ]
    client = FakeFabricClient(roles=[fake_role("Retired", ["s"], ["m1"])])
    spark = build_spark()
    spark._store[LOG_TABLE] = log
    spark._store[MAPPING_TABLE] = []  # this generation does not declare Retired
    spark._store[MEMBER_TABLE] = []
    rep = Audit(spark, CONFIG_TABLE, MAPPING_TABLE, LOG_TABLE, client=client).report()
    assert rep["out_of_band"] == 0  # provenanced in history, so not caught here
    assert rep["unexpected"] == 1  # but this generation never declared it
    assert rep["in_sync"] is False


def test_report_is_in_sync_when_live_matches_the_mapping():
    log = [
        seed_validate_row(
            "R",
            "/Tables/s",
            "m1",
            run_at="2026-07-01T01",
            run_by="a@b.c",
            config_version="1",
            mode="apply",
        ),
    ]
    client = FakeFabricClient(roles=[fake_role("R", ["/Tables/s"], ["m1"])])
    spark = build_spark()
    spark._store[LOG_TABLE] = log
    spark._store[MAPPING_TABLE] = [
        {
            **_fixture_mapping_provenance(),
            "role_name": "R",
            "scope_path": "/Tables/s",
            "member_group_ids": "m1",
        }
    ]
    spark._store[MEMBER_TABLE] = []
    spark._store[CONFIG_TABLE] = sample_config_rows()  # is_stale() reads it once a mapping exists
    rep = Audit(spark, CONFIG_TABLE, MAPPING_TABLE, LOG_TABLE, client=client).report()
    assert rep["desired_grant_count"] == 1
    assert rep["live_grant_count"] == 1
    assert rep["missing"] == 0 and rep["unexpected"] == 0
    assert rep["in_sync"] is True


def test_report_collects_the_established_grants_once():
    # Issue #2: report() called grants() directly AND again inside out_of_band() — two full
    # log collections per trace. Pinned to ONE grants() computation per report().
    from unittest import mock

    client = FakeFabricClient(roles=[])
    spark = build_spark()
    spark._store[LOG_TABLE] = []
    spark._store[MAPPING_TABLE] = []
    at = Audit(spark, CONFIG_TABLE, MAPPING_TABLE, LOG_TABLE, client=client)
    calls = []
    real = Audit.grants

    def spy(self, *a, **k):
        calls.append(1)
        return real(self, *a, **k)

    with mock.patch.object(Audit, "grants", spy):
        at.report()
    assert len(calls) == 1


def test_log_rows_pushed_filters_match_the_python_reference():
    """Issue #2 equivalence: the equality filters now ride into Spark's .where, and a value
    that cannot (a quote-carrying display name, a non-string) falls back to the Python-side
    comparison — results must be identical to filtering everything in Python."""
    rows = [
        {
            "mode": "apply",
            "env": "dev",
            "role_name": "R1",
            "member_name": "sg-a",
            "batch_id": "b1",
            "run_at": "1",
            "status": "success",
        },
        {
            "mode": "plan",
            "env": "dev",
            "role_name": "R1",
            "member_name": "sg-a",
            "batch_id": "b2",
            "run_at": "2",
            "status": "success",
        },
        {
            "mode": "apply",
            "env": "qa",
            "role_name": "R2",
            "member_name": "O'Brien alias",
            "batch_id": "b3",
            "run_at": "3",
            "status": "failed",
        },
        {
            "mode": "apply",
            "env": "dev",
            "role_name": None,
            "member_name": None,
            "batch_id": 7,
            "run_at": "4",
            "status": "success",
        },
        {
            "mode": "apply",
            "env": "dev",
            "role_name": "R3",
            "member_name": "CONTOSO\\alice",
            "batch_id": "b4",
            "run_at": "5",
            "status": "success",
        },
        {
            "mode": "apply",
            "env": "dev",
            "role_name": "R4",
            "member_name": "trailing\\",
            "batch_id": "b5",
            "run_at": "6",
            "status": "success",
        },
    ]
    audit = trail(rows)

    def reference(**eq):
        got = list(rows)
        for k, v in eq.items():
            if v is not None:
                got = [r for r in got if r.get(k) == v]
        return got

    cases = [
        {},
        {"mode": "apply"},
        {"mode": "apply", "env": "dev"},
        {"role_name": "R1", "member_name": "sg-a"},
        {"member_name": "O'Brien alias"},  # the quote-carrying fallback path
        # Backslash-carrying values MUST take the Python fallback too: Spark string literals
        # process backslash escapes by default, so pushed down, 'CONTOSO\\alice' would
        # compare against the unescaped WRONG value — and a trailing backslash would swallow
        # the closing quote (ParseException). Triple-confirmed by review; the fakes cannot
        # model the escape processing, so only this oracle equality holds the line.
        {"member_name": "CONTOSO\\alice"},
        {"member_name": "trailing\\"},
        {"batch_id": 7},  # the non-string fallback path
        {"mode": "nope"},
    ]
    for eq in cases:
        assert audit._log_rows(**eq) == reference(**eq), eq


# ---------------------------------------------------------------------------------------------
# Task 4: effective_access / who_can_access (net live-DAR access)
# ---------------------------------------------------------------------------------------------


def test_effective_access_requires_client():
    at = Audit(build_spark(), CONFIG_TABLE, MAPPING_TABLE, LOG_TABLE)
    with pytest.raises(UsageError):
        at.effective_access("somchai@contoso.com", "sales.orders", engine="spark")


def test_effective_access_requires_a_supported_engine_before_reporting_access():
    at = Audit(build_spark(), CONFIG_TABLE, MAPPING_TABLE, LOG_TABLE, client=FakeFabricClient())
    with pytest.raises(UsageError, match="engine"):
        at.effective_access("e0000000-0000-0000-0000-000000000099", "sales.orders")
    with pytest.raises(UsageError, match="sql_endpoint"):
        at.effective_access("e0000000-0000-0000-0000-000000000099", "sales.orders", engine="portal")


def test_effective_access_no_reaching_role_is_empty_typed():
    """A member with no role reaching the table -- effective_access() returns an EMPTY frame
    that still carries its 5 typed columns (via columns=), the same empty-schema lesson
    out_of_band's negative tests pin."""
    client = FakeFabricClient(
        roles=[fake_role("APACReaders", ["/Tables/sales/orders"], ["someone-else@contoso.com"])]
    )
    at = Audit(build_spark(), CONFIG_TABLE, MAPPING_TABLE, LOG_TABLE, client=client)
    out = at.effective_access(
        "e0000000-0000-0000-0000-000000000099", "sales.orders", engine="spark"
    )
    assert out.collect() == []
    assert out.columns == [
        "role_name",
        "rls_condition",
        "visible_columns",
        "granting_role",
        "effective",
        "engine",
    ]


def test_effective_access_unions_when_every_role_restricts():
    """When NO granting role is fully open, the effective row is the UNION (not
    most-permissive-wins): the OR of each role's distinct rls_condition, and the union of
    their visible-column allow-lists (an overlapping column collapses once)."""
    path = "/Tables/sales/orders"
    member = "e0000000-0000-0000-0000-000000000042"
    client = FakeFabricClient(
        roles=[
            fake_role(
                "RegionAReaders",
                [path],
                [member],
                rls={path: "region = 'APAC'"},
                visible_columns={path: ["order_id", "region"]},
            ),
            fake_role(
                "RegionBReaders",
                [path],
                [member],
                rls={path: "region = 'EMEA'"},
                visible_columns={path: ["region", "amount"]},
            ),
        ]
    )
    at = Audit(build_spark(), CONFIG_TABLE, MAPPING_TABLE, LOG_TABLE, client=client)
    got = [
        r.asDict() for r in at.effective_access(member, "sales.orders", engine="spark").collect()
    ]
    effective = next(r for r in got if r["effective"] == "True")
    assert effective["rls_condition"] == "(region = 'APAC') OR (region = 'EMEA')"
    assert effective["visible_columns"] == "amount;order_id;region"
    assert effective["granting_role"] == "RegionAReaders;RegionBReaders"


def test_effective_access_intersects_cls_for_sql_endpoint_but_unions_it_for_spark():
    """An unrestricted SQL role is a universal set, not an override of another role's deny set."""
    path = "/Tables/sales/orders"
    member = "e0000000-0000-0000-0000-000000000045"
    client = FakeFabricClient(
        roles=[
            fake_role(
                "Restricted", [path], [member], visible_columns={path: ["order_id", "region"]}
            ),
            fake_role(
                "AlsoRestricted", [path], [member], visible_columns={path: ["region", "amount"]}
            ),
            fake_role("Open", [path], [member]),
        ]
    )
    at = Audit(build_spark(), CONFIG_TABLE, MAPPING_TABLE, LOG_TABLE, client=client)
    sql = [
        r.asDict()
        for r in at.effective_access(member, "sales.orders", engine="sql_endpoint").collect()
    ]
    spark = [
        r.asDict() for r in at.effective_access(member, "sales.orders", engine="spark").collect()
    ]
    sql_effective = next(r for r in sql if r["effective"] == "True")
    spark_effective = next(r for r in spark if r["effective"] == "True")
    assert sql_effective["visible_columns"] == "region"
    assert spark_effective["visible_columns"] is None
    assert sql_effective["engine"] == "sql_endpoint"


def test_effective_access_sql_endpoint_keeps_columns_open_when_every_role_is_unrestricted():
    path = "/Tables/sales/orders"
    member = "e0000000-0000-0000-0000-000000000046"
    client = FakeFabricClient(roles=[fake_role("Open", [path], [member])])
    rows = [
        row.asDict()
        for row in Audit(build_spark(), CONFIG_TABLE, MAPPING_TABLE, LOG_TABLE, client=client)
        .effective_access(member, "sales.orders", engine="sql_endpoint")
        .collect()
    ]
    assert next(row for row in rows if row["effective"])["visible_columns"] is None


def test_effective_access_ignores_constraints_on_other_table_paths():
    """DAR.row_predicate/column_allowlist scan ALL of a role's decisionRules -- a role that
    ALSO carries an RLS/CLS constraint for a DIFFERENT table path must not have that
    constraint leak onto the queried table: SalesReaders reaches sales.orders unrestricted,
    but its RLS/CLS is declared against sales.leads (a decoy other-table entry) -- the queried
    table stays open."""
    orders_path = "/Tables/sales/orders"
    leads_path = "/Tables/sales/leads"
    member = "e0000000-0000-0000-0000-000000000043"
    client = FakeFabricClient(
        roles=[
            fake_role(
                "SalesReaders",
                [orders_path],
                [member],
                rls={leads_path: "region = 'APAC'"},
                visible_columns={leads_path: ["lead_id"]},
            )
        ]
    )
    at = Audit(build_spark(), CONFIG_TABLE, MAPPING_TABLE, LOG_TABLE, client=client)
    got = [
        r.asDict() for r in at.effective_access(member, "sales.orders", engine="spark").collect()
    ]
    detail = next(r for r in got if r["role_name"] == "SalesReaders")
    assert detail["rls_condition"] is None  # decoy constraint is for sales.leads
    assert detail["visible_columns"] is None


def test_effective_access_member_name_not_in_member_table_raises_usage_error():
    """A member name absent from the seeded member table is a hard No-Graph error, raised
    BEFORE any live-DAR read: effective_access() resolves config-declared members
    (groups/users/SPs) via the member table -- it does not expand group membership."""
    spark = build_spark()
    spark._store[MEMBER_TABLE] = []
    at = Audit(spark, CONFIG_TABLE, MAPPING_TABLE, LOG_TABLE, client=FakeFabricClient(roles=[]))
    with pytest.raises(UsageError) as excinfo:
        at.effective_access("ghost@contoso.com", "sales.orders", engine="spark")
    assert "not in the member table" in str(excinfo.value)
    assert "No-Graph" in str(excinfo.value)


def test_effective_access_matches_live_dar_id_in_a_different_letter_case():
    """The member table stores the objectId as the author wrote it and _resolve_member returns
    it UN-lowered, while the live DAR returns its own spelling — an objectId is
    case-INSENSITIVE hex, so the two are the same principal. Left an exact comparison, the
    member reads as reaching NO role and effective_access reports an empty frame for a
    principal that in fact holds access — a diagnostic false negative on the safe-looking side."""
    path = "/Tables/sales/orders"
    stored_id = "E0000000-0000-0000-0000-0000000000AB"  # as written in the member table
    live_id = "e0000000-0000-0000-0000-0000000000ab"  # as the live DAR returns it
    spark = build_spark()
    spark._store[MEMBER_TABLE] = [member_cache_row("User", "somchai", stored_id)]
    client = FakeFabricClient(
        roles=[fake_role("SalesReaders", [path], [live_id], rls={path: "region = 'APAC'"})]
    )
    at = Audit(spark, CONFIG_TABLE, MAPPING_TABLE, LOG_TABLE, client=client)
    got = [
        r.asDict() for r in at.effective_access("somchai", "sales.orders", engine="spark").collect()
    ]
    assert "SalesReaders" in [r["role_name"] for r in got]
    # and by the id itself, spelled in the other case
    by_id = [
        r.asDict() for r in at.effective_access(stored_id, "sales.orders", engine="spark").collect()
    ]
    assert "SalesReaders" in [r["role_name"] for r in by_id]


def test_who_can_access_requires_client():
    at = Audit(build_spark(), CONFIG_TABLE, MAPPING_TABLE, LOG_TABLE)
    with pytest.raises(UsageError):
        at.who_can_access("sales.orders")


def test_who_can_access_no_reaching_role_is_empty_typed():
    """No role reaches the table -- who_can_access() returns an EMPTY frame that still
    carries its 5 typed columns (via columns=), the same empty-schema lesson
    effective_access's negative test pins."""
    client = FakeFabricClient(
        roles=[fake_role("OtherReaders", ["/Tables/sales/leads"], ["somchai@contoso.com"])]
    )
    out = Audit(
        build_spark(), CONFIG_TABLE, MAPPING_TABLE, LOG_TABLE, client=client
    ).who_can_access("sales.orders")
    assert out.collect() == []
    assert out.columns == ["member_name", "member_id", "via_role", "permission", "rls_cls_summary"]


# ---------------------------------------------------------------------------------------------
# Task 3: _resolve_member (name|guid -> objectId, No-Graph strict)
# ---------------------------------------------------------------------------------------------


def test_resolve_member_guid_shaped_passes_through_unchanged():
    # A GUID-shaped member is already an objectId -- returned as-is, no member-table lookup.
    at = Audit(build_spark(), CONFIG_TABLE, MAPPING_TABLE, LOG_TABLE)
    guid = "e0000000-0000-0000-0000-000000000099"
    assert at._resolve_member(guid) == guid


def member_table_audit(*rows):
    spark = build_spark()
    spark._store[MEMBER_TABLE] = list(rows)
    return Audit(spark, CONFIG_TABLE, MAPPING_TABLE, LOG_TABLE)


@pytest.mark.parametrize(
    "spelling", ["somchai@contoso.com", "SOMCHAI@contoso.com"], ids=["exact", "case-insensitive"]
)
def test_resolve_member_name_returns_member_id(spelling):
    at = member_table_audit(
        member_cache_row("User", "somchai@contoso.com", "e0000000-0000-0000-0000-000000000099")
    )
    assert at._resolve_member(spelling) == "e0000000-0000-0000-0000-000000000099"


def test_resolve_member_name_not_found_raises_usage_error_naming_no_graph():
    at = member_table_audit(
        member_cache_row("User", "somchai@contoso.com", "e0000000-0000-0000-0000-000000000099")
    )
    with pytest.raises(UsageError) as excinfo:
        at._resolve_member("ghost@contoso.com")
    assert "not in the member table" in str(excinfo.value)
    assert "No-Graph" in str(excinfo.value)


# ---------------------------------------------------------------------------------------------
# Task 8: coverage (protected vs unprotected table surface)
# ---------------------------------------------------------------------------------------------


def test_coverage_marks_protected_vs_gap_tables_with_role_rls_cls_detail():
    """coverage(): table universe (Catalog.canonical) = sales.orders / ml_gold.metrics /
    raw.events. sales.orders is protected by 2 roles (one carries RLS); ml_gold.metrics is
    protected by 1 role carrying CLS only; raw.events has ZERO /Tables/ mapping rows -- a gap
    (protected=False). A FOLDER-scope mapping row is also seeded at scope_path
    '/Files/raw/events' -- a path that (minus the /Tables/ guard) would mis-parse via
    ScopePath.to_table into 'raw.events', the SAME name as the gap table -- and it carries its
    own rls/cls markers. raw.events staying protected=False with has_rls/has_cls False proves
    the /Tables/-only filter actually prevents that collision, not just that it doesn't crash."""
    spark = FakeSpark(
        {"sales": ["orders"], "ml_gold": ["metrics"], "raw": ["events"]},
        {
            "sales.orders": ["order_id", "region"],
            "ml_gold.metrics": ["metric", "value"],
            "raw.events": ["event_id"],
        },
    )
    spark._store[MAPPING_TABLE] = [
        {
            **_fixture_mapping_provenance(),
            "role_name": "SalesReaders",
            "scope_path": "/Tables/sales/orders",
            "scope_type": "Table",
            "rls_condition": "region = 'APAC'",
            "visible_columns": None,
        },
        {
            **_fixture_mapping_provenance(),
            "role_name": "AuditReaders",
            "scope_path": "/Tables/sales/orders",
            "scope_type": "Table",
            "rls_condition": None,
            "visible_columns": None,
        },
        {
            **_fixture_mapping_provenance(),
            "role_name": "MLReaders",
            "scope_path": "/Tables/ml_gold/metrics",
            "scope_type": "Table",
            "rls_condition": None,
            "visible_columns": "metric;value",
        },
        {
            **_fixture_mapping_provenance(),
            "role_name": "FolderRoleShouldNotCount",
            "scope_path": "/Files/raw/events",
            "scope_type": "Folder",
            "rls_condition": "should_not_leak",
            "visible_columns": "should_not_leak",
        },
    ]
    at = Audit(spark, CONFIG_TABLE, MAPPING_TABLE, LOG_TABLE)
    got = {r["table"]: r for r in (row.asDict() for row in at.coverage().collect())}
    assert set(got) == {"sales.orders", "ml_gold.metrics", "raw.events"}
    assert got["sales.orders"]["protected"] == "True"
    assert got["sales.orders"]["roles_count"] == "2"
    assert got["sales.orders"]["has_rls"] == "True"
    assert got["sales.orders"]["has_cls"] == "False"
    assert got["ml_gold.metrics"]["protected"] == "True"
    assert got["ml_gold.metrics"]["roles_count"] == "1"
    assert got["ml_gold.metrics"]["has_rls"] == "False"
    assert got["ml_gold.metrics"]["has_cls"] == "True"
    assert got["raw.events"]["protected"] == "False"
    assert got["raw.events"]["roles_count"] == "0"
    assert got["raw.events"]["has_rls"] == "False"
    assert got["raw.events"]["has_cls"] == "False"


def test_coverage_empty_mapping_marks_every_universe_table_unprotected():
    """Negative: an empty mapping table -- every table in the REAL universe (Catalog.canonical,
    option (a): the lakehouse's actual tables, not just names mentioned in config/mapping)
    comes back protected=False, not an empty frame (the universe itself still has rows)."""
    spark = FakeSpark(
        {"sales": ["orders"], "ml_gold": ["metrics"], "raw": ["events"]},
        {"sales.orders": [], "ml_gold.metrics": [], "raw.events": []},
    )
    spark._store[MAPPING_TABLE] = []
    got = [
        r.asDict()
        for r in Audit(spark, CONFIG_TABLE, MAPPING_TABLE, LOG_TABLE).coverage().collect()
    ]
    assert {r["table"] for r in got} == {"sales.orders", "ml_gold.metrics", "raw.events"}
    assert all(r["protected"] == "False" for r in got)
    assert all(r["roles_count"] == "0" for r in got)
    assert all(r["has_rls"] == "False" and r["has_cls"] == "False" for r in got)


# ---------------------------------------------------------------------------------------------
# Task 9: drift (categorized desired-vs-live comparison)
# ---------------------------------------------------------------------------------------------


def test_drift_requires_client():
    at = Audit(build_spark(), CONFIG_TABLE, MAPPING_TABLE, LOG_TABLE)
    with pytest.raises(UsageError):
        at.drift()


def test_drift_all_framework_when_live_matches_desired_exactly():
    """Negative: live DAR == the mapping's desired grants exactly (nothing out-of-band, nothing
    missing) -- every row comes back category='framework', and NO 'missing'/'out_of_band' row
    appears at all."""
    client = FakeFabricClient(roles=[fake_role("R", ["s"], ["m"])])
    spark = build_spark()
    spark._store[LOG_TABLE] = [
        seed_validate_row("R", "s", "m", run_at="1", run_by="u", config_version="1", mode="apply")
    ]
    spark._store[MEMBER_TABLE] = []  # no cache needed -- id-fallback covered elsewhere
    spark._store[MAPPING_TABLE] = [
        {
            **_fixture_mapping_provenance(),
            "role_name": "R",
            "scope_path": "s",
            "member_group_ids": "m",
        }
    ]
    got = [
        r.asDict()
        for r in Audit(spark, CONFIG_TABLE, MAPPING_TABLE, LOG_TABLE, client=client)
        .drift()
        .collect()
    ]
    assert len(got) == 1
    assert got[0]["role_name"] == "R"
    assert got[0]["scope_path"] == "s"
    assert got[0]["category"] == "framework"
    assert {r["category"] for r in got} == {"framework"}


def test_drift_desired_and_live_oob_grant_reports_once_as_out_of_band():
    """A DESIRED grant (seeded in the mapping) that is ALSO live but has NO framework
    provenance surfaces as a SINGLE out_of_band row -- never ALSO reported 'missing'. The
    `missing` branch tests the FULL live set (not just the established set), so an
    out-of-band-pushed grant still counts as live and is excluded from missing."""
    client = FakeFabricClient(roles=[fake_role("R", ["s"], ["m"])])  # live, but no log row
    spark = build_spark()
    spark._store[LOG_TABLE] = []  # nothing established -> the live grant is out_of_band
    spark._store[MEMBER_TABLE] = []  # no cache needed -- id-fallback covered elsewhere
    spark._store[MAPPING_TABLE] = [
        {
            **_fixture_mapping_provenance(),
            "role_name": "R",
            "scope_path": "s",
            "member_group_ids": "m",
        }  # ALSO desired
    ]
    got = [
        r.asDict()
        for r in Audit(spark, CONFIG_TABLE, MAPPING_TABLE, LOG_TABLE, client=client)
        .drift()
        .collect()
    ]
    assert len(got) == 1  # exactly one row -- NOT out_of_band + missing
    assert got[0]["category"] == "out_of_band"
    assert (got[0]["role_name"], got[0]["scope_path"]) == ("R", "s")
    assert "missing" not in {r["category"] for r in got}


# ---------------------------------------------------------------------------------------------
# Task 10: config_diff (role/scope/member changes between config Delta versions)
# ---------------------------------------------------------------------------------------------


def test_config_diff_identical_versions_is_empty_typed():
    """Negative: the same rows at both versions -- an empty frame that still carries the 6
    typed config_diff columns (the same empty-schema lesson every other Audit method's
    negative test pins)."""
    rows = [config_row("SalesReaders", "sales.*", permission="Read")]
    out = diff_audit({1: rows, 2: rows}).config_diff(1, 2)
    assert out.collect() == []
    assert out.columns == ["change_type", "role_name", "scope_key", "field", "old", "new"]


def test_config_diff_added_removed_and_changed_field():
    """Positive: v1 -> v2 has ONE added role (NewRole), ONE removed role (OldRole), and ONE
    changed field on SalesReaders (rls_condition) -- asserts EXACTLY those 3 rows, keyed by
    (role_name, scope). Mutation-strong: permission stays 'Read' on SalesReaders in both
    versions, so it must NOT also surface as a spurious 'changed' row."""
    v1 = [
        config_row("SalesReaders", "sales.*", permission="Read", rls_condition="Region = 'TH'"),
        config_row("OldRole", "hr.*", permission="Read"),
    ]
    v2 = [
        config_row("SalesReaders", "sales.*", permission="Read", rls_condition="Region = 'US'"),
        config_row("NewRole", "ref.*", permission="Read"),
    ]
    got = [r.asDict() for r in diff_audit({1: v1, 2: v2}).config_diff(1, 2).collect()]
    assert len(got) == 3
    by_type = {r["change_type"]: r for r in got}
    assert set(by_type) == {"added", "removed", "changed"}
    assert by_type["added"]["role_name"] == "NewRole"
    assert by_type["added"]["scope_key"] == "ref.*|||"
    assert by_type["added"]["field"] is None
    assert by_type["added"]["old"] is None
    assert by_type["added"]["new"] is None
    assert by_type["removed"]["role_name"] == "OldRole"
    assert by_type["removed"]["scope_key"] == "hr.*|||"
    assert by_type["removed"]["field"] is None
    assert by_type["removed"]["old"] is None
    assert by_type["removed"]["new"] is None
    assert by_type["changed"]["role_name"] == "SalesReaders"
    assert by_type["changed"]["scope_key"] == "sales.*|||"
    assert by_type["changed"]["field"] == "rls_condition"
    assert by_type["changed"]["old"] == "Region = 'TH'"
    assert by_type["changed"]["new"] == "Region = 'US'"


# ---------------------------------------------------------------------------------------------
# Task 11: table_history / _control_table
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("logical", "expected"),
    [("config", CONFIG_TABLE), ("mapping", MAPPING_TABLE), ("log", LOG_TABLE)],
)
def test_control_table_maps_all_three_logical_names(logical, expected):
    """_control_table (the shared helper Task 12's `at` also reuses) maps each of the 3
    logical names to its configured table name."""
    assert bare_audit()._control_table(logical) == expected


def test_control_table_rejects_unknown_name():
    """Negative: anything outside config/mapping/log is a clear ValueError."""
    with pytest.raises(ValueError):
        bare_audit()._control_table("bogus")


def test_table_history_config_emits_describe_history_and_projects_columns():
    """table_history("config") emits DESCRIBE HISTORY against the config table (asserted via
    FakeSpark's spark-level _last_sql -- table_history collects+projects the raw DESCRIBE
    HISTORY frame before returning, so unlike config_at it can't expose _captured_sql on its
    own return value) and projects the 5 readable columns, pulling `rows` out of the nested
    operationMetrics map. Two history rows: one with a populated operationMetrics
    (numOutputRows), one with an EMPTY operationMetrics and a null userName (falls back to
    operationParameters.userName) -- mutation-strong for both the metrics lookup and the user
    fallback."""
    rows = [
        {
            "version": 3,
            "timestamp": "2026-07-13T10:00",
            "userName": "alice@x.com",
            "operation": "WRITE",
            "operationParameters": {},
            "operationMetrics": {"numOutputRows": "12"},
        },
        {
            "version": 2,
            "timestamp": "2026-07-12T09:00",
            "userName": None,
            "operation": "CREATE TABLE",
            "operationParameters": {"userName": "bob@x.com"},
            "operationMetrics": {},
        },
    ]
    a = history_trail(rows)
    out = a.table_history("config")
    assert a.spark._last_sql == f"DESCRIBE HISTORY {CONFIG_TABLE}"
    assert out.columns == ["version", "timestamp", "user", "operation", "rows"]
    assert [r.asDict() for r in out.collect()] == [
        {
            "version": "3",
            "timestamp": "2026-07-13T10:00",
            "user": "alice@x.com",
            "operation": "WRITE",
            "rows": "12",
        },
        {
            "version": "2",
            "timestamp": "2026-07-12T09:00",
            "user": "bob@x.com",
            "operation": "CREATE TABLE",
            "rows": None,
        },
    ]


def test_table_history_mapping_targets_mapping_table():
    """table_history("mapping") targets the mapping table, not config -- pins _control_table's
    dispatch, not just that SOME DESCRIBE HISTORY fires."""
    a = history_trail(
        [
            {
                "version": 1,
                "timestamp": "t",
                "userName": "x@example.com",
                "operation": "WRITE",
                "operationParameters": {},
                "operationMetrics": {"numOutputRows": "1"},
            }
        ]
    )
    a.table_history("mapping")
    assert a.spark._last_sql == f"DESCRIBE HISTORY {MAPPING_TABLE}"


def test_table_history_rejects_unknown_table():
    """Negative: table_history("bogus") raises ValueError via _control_table."""
    with pytest.raises(ValueError):
        bare_audit().table_history("bogus")


# ---------------------------------------------------------------------------------------------
# Task 12: at(table, version, date) -- generic time-travel snapshot
# ---------------------------------------------------------------------------------------------


def test_at_version_sql_targets_the_resolved_table():
    """at("mapping", version=2) emits VERSION AS OF against the MAPPING table (not
    config) -- pins both the exact SQL and that _control_table's dispatch selects the
    right table."""
    got = bare_audit().at("mapping", version=2)._captured_sql
    assert got == f"SELECT * FROM {MAPPING_TABLE} VERSION AS OF 2"


def test_at_date_sql_targets_the_resolved_table():
    """at("log", date=...) emits TIMESTAMP AS OF against the LOG table."""
    got = bare_audit().at("log", date="2026-07-01")._captured_sql
    assert got == f"SELECT * FROM {LOG_TABLE} TIMESTAMP AS OF '2026-07-01'"


@pytest.mark.parametrize(
    "kwargs", [{}, {"version": 3, "date": "2026-07-01"}], ids=["neither", "both"]
)
def test_at_requires_exactly_one(kwargs):
    """Neither given, or both given -- ValueError (the SAME exactly-one guard config_at uses)."""
    with pytest.raises(ValueError):
        bare_audit().at("config", **kwargs)


def test_at_rejects_unknown_table_before_the_guard():
    """Negative: at("bogus", version=1) raises ValueError from _control_table -- table
    validation happens FIRST, before the exactly-one guard would even run."""
    with pytest.raises(ValueError):
        bare_audit().at("bogus", version=1)


def test_at_date_rejects_bad_shape():
    """at() shares config_at's date shape-guard via the common _timetravel_sql helper: a bad
    date raises ValueError, a well-formed one still targets the resolved table."""
    a = bare_audit()
    with pytest.raises(ValueError):
        a.at("config", date="2026'; DROP")
    assert "TIMESTAMP AS OF '2026-07-01'" in a.at("log", date="2026-07-01")._captured_sql


# ---------------------------------------------------------------------------------------------
# Task 13: value_history(subject) -- per-version config projection of one role/scope
# ---------------------------------------------------------------------------------------------


def value_history_columns():
    return (
        ["config_version", "role_name", "scope_key"]
        + _CONFIG_DIFF_FIELD_COLUMNS
        + ["changed", "window_truncated"]
    )


def test_value_history_first_appearance_and_field_change_flip_changed():
    """Positive: 3 config versions where SalesReaders' rls_condition differs v1->v2 then
    stays the same v2->v3 -- 3 rows (the subject is present in all 3), `changed` True at v1
    (first appearance -- a change from "absent", per this method's documented rule) AND v2
    (a real field change), False at v3 (no further change from v2). permission stays "Read"
    throughout -- mutation-strong for the field-diff loop only flagging the field that
    actually moved."""
    v1 = [config_row("SalesReaders", "sales.*", permission="Read", rls_condition="Region = 'TH'")]
    v2 = [config_row("SalesReaders", "sales.*", permission="Read", rls_condition="Region = 'US'")]
    v3 = [config_row("SalesReaders", "sales.*", permission="Read", rls_condition="Region = 'US'")]
    out = value_audit({1: v1, 2: v2, 3: v3}).value_history("SalesReaders")
    assert out.columns == value_history_columns()
    got = [r.asDict() for r in out.collect()]
    assert [r["config_version"] for r in got] == ["1", "2", "3"]
    assert [r["changed"] for r in got] == ["True", "True", "False"]
    assert [r["rls_condition"] for r in got] == ["Region = 'TH'", "Region = 'US'", "Region = 'US'"]
    assert all(r["role_name"] == "SalesReaders" for r in got)
    assert all(r["scope_key"] == "sales.*|||" for r in got)
    assert all(r["permission"] == "Read" for r in got)


def test_value_history_last_bounds_the_walk_to_the_newest_versions():
    """Issue #9: one time-travel read per walked version is unbounded on a long-lived config
    table. `last=N` walks only the N newest versions; the oldest row of the bounded walk is
    judged a first appearance (changed=True), exactly like a reappearance after a gap."""
    v1 = [config_row("SalesReaders", "sales.*", rls_condition="Region = 'TH'")]
    v2 = [config_row("SalesReaders", "sales.*", rls_condition="Region = 'TH'")]
    v3 = [config_row("SalesReaders", "sales.*", rls_condition="Region = 'TH'")]
    reads = []
    audit = value_audit({1: v1, 2: v2, 3: v3})
    inner = audit.config_at
    audit.config_at = lambda version=None, date=None: (
        reads.append(version) or inner(version=version)
    )
    got = [r.asDict() for r in audit.value_history("SalesReaders", last=2).collect()]
    assert reads == [2, 3]  # version 1 was never read at all — the walk is genuinely bounded
    assert [r["config_version"] for r in got] == ["2", "3"]
    # v2 is the bounded walk's first row -> first appearance; v3 is unchanged from it
    assert [r["changed"] for r in got] == ["True", "False"]


def test_value_history_refuses_a_non_positive_last():
    with pytest.raises(ValueError) as excinfo:
        value_audit({1: []}).value_history("SalesReaders", last=0)
    assert "last must be a positive" in str(excinfo.value)


def test_value_history_last_coercion_accepts_int_valued_and_refuses_the_rest():
    """int and int-VALUED inputs only: bool is an int subclass but last=True is a caller
    bug, and 2.9 silently floored would walk a different window than asked for."""
    present = [config_row("SalesReaders", "sales.*", rls_condition="Region = 'TH'")]
    audit = value_audit({1: present, 2: present, 3: present})
    for bad in (True, False, 2.9, "2.9", "abc"):
        with pytest.raises(ValueError) as excinfo:
            audit.value_history("SalesReaders", last=bad)
        assert "last must be a positive" in str(excinfo.value), bad
    # int-valued spellings are accepted and mean the same window
    for ok in (2, "2", 2.0):
        got = [r.asDict() for r in audit.value_history("SalesReaders", last=ok).collect()]
        assert [r["config_version"] for r in got] == ["2", "3"], ok


def test_value_history_window_truncated_marks_a_genuinely_cut_walk():
    present = [config_row("SalesReaders", "sales.*", rls_condition="Region = 'TH'")]
    audit = value_audit({1: present, 2: present, 3: present})
    # last=2 of 3 -> the walk was cut: every row says so
    got = [r.asDict() for r in audit.value_history("SalesReaders", last=2).collect()]
    assert {r["window_truncated"] for r in got} == {"True"}
    # unbounded, or a bound wider than history -> nothing was cut
    got = [r.asDict() for r in audit.value_history("SalesReaders").collect()]
    assert {r["window_truncated"] for r in got} == {"False"}
    got = [r.asDict() for r in audit.value_history("SalesReaders", last=5).collect()]
    assert {r["window_truncated"] for r in got} == {"False"}


def test_value_history_gap_resets_first_appearance():
    """A subject PRESENT at v1, ABSENT at v2, PRESENT again at v3: NO row for v2 (a version
    with no match contributes nothing AND resets the diff state), and `changed=True` at v3 --
    a reappearance after a gap is a FRESH first-appearance from "absent", never diffed against
    the stale pre-gap v1 value (asserted even though v3's value is byte-identical to v1's)."""
    present = [
        config_row("SalesReaders", "sales.*", permission="Read", rls_condition="Region = 'TH'")
    ]
    absent = [config_row("OtherRole", "hr.*")]  # SalesReaders absent this version
    got = [
        r.asDict()
        for r in value_audit({1: present, 2: absent, 3: present})
        .value_history("SalesReaders")
        .collect()
    ]
    assert [r["config_version"] for r in got] == ["1", "3"]  # v2 contributes no row
    # v3 is a reappearance -> changed=True even though its value equals the pre-gap v1 value
    assert [r["changed"] for r in got] == ["True", "True"]
    assert all(r["scope_key"] == "sales.*|||" for r in got)


def test_value_history_absent_subject_is_empty_typed():
    """Negative: subject never appears in any config version -- an empty frame that still
    carries the typed config_version/role_name/scope/.../changed columns (the same
    empty-schema lesson every other Audit method's negative test pins)."""
    v1 = [config_row("OtherRole", "hr.*")]
    v2 = [config_row("OtherRole", "hr.*")]
    out = value_audit({1: v1, 2: v2}).value_history("SalesReaders")
    assert out.collect() == []
    assert out.columns == value_history_columns()


# ---------------------------------------------------------------------------------------------
# Fix wave 1 (R1/R2): autotrim at the Audit read seams, member_type, frame shape
# ---------------------------------------------------------------------------------------------


def test_is_stale_false_when_config_rows_carry_incidental_whitespace():
    """C1: is_stale() must hash the active config through the SAME autotrim read seam
    Deployment.short_rows applies before stamping config_hash. Without it, a config carrying
    incidental leading/trailing whitespace (exactly what autotrim exists to neutralize) makes
    is_stale()'s freshly recomputed hash diverge from the stamped one FOREVER -- stale one
    second after a clean generate()."""
    spark = build_spark()
    padded = {
        **{c: None for c in CONFIG_AUTHOR_COLUMNS},
        "role_name": "  SalesReaders  ",
        "active": True,
        "permission": " Read ",
    }
    spark._store[CONFIG_TABLE] = [padded]
    # What a real generate() stamps: Hash.config over Deployment.short_rows -- TRIMMED rows.
    stamped = Hash.config([Parse.trim_row(padded)])
    spark._store[MAPPING_TABLE] = [mapping_row(stamped)]
    assert not Audit(spark, CONFIG_TABLE, MAPPING_TABLE, LOG_TABLE).is_stale()
    # Mutation-strong: the untrimmed hash really is a different value, so the assertion above
    # is not vacuously true for any hashing implementation.
    assert stamped != Hash.config([padded])


def test_is_stale_true_when_an_active_row_actually_changed():
    """The trimmed comparison must still DETECT a real config change -- trimming widens what
    counts as 'unchanged', it must not make is_stale() blind."""
    spark = build_spark()
    row = {
        **{c: None for c in CONFIG_AUTHOR_COLUMNS},
        "role_name": "  SalesReaders  ",
        "active": True,
        "permission": " Read ",
    }
    spark._store[CONFIG_TABLE] = [row]
    spark._store[MAPPING_TABLE] = [mapping_row(Hash.config([Parse.trim_row(row)]))]
    at = Audit(spark, CONFIG_TABLE, MAPPING_TABLE, LOG_TABLE)
    assert not at.is_stale()
    row["permission"] = " Write "  # a genuine authored change, not whitespace noise
    assert at.is_stale()


def test_resolve_member_strips_whitespace_from_the_returned_member_id():
    """I1: the returned objectId goes through the same autotrim Deployment._load_member_cache
    applies -- a trailing-space preload artifact must not yield an id that can never match the
    live DAR's clean objectIds (which would silently make effective_access report 'no access')."""
    at = member_table_audit(
        member_cache_row("User", " somchai@contoso.com ", " e0000000-0000-0000-0000-000000000099 ")
    )
    assert at._resolve_member("somchai@contoso.com") == "e0000000-0000-0000-0000-000000000099"


def test_member_names_strips_whitespace_from_the_display_name():
    """I2: the map VALUE (the display name surfaced by who_can_access/out_of_band/drift) is
    trimmed too, not only the id KEY."""
    at = member_table_audit(
        member_cache_row("User", "  Somchai P.  ", "  E0000000-0000-0000-0000-000000000099  ")
    )
    assert at._member_names() == {"e0000000-0000-0000-0000-000000000099": "Somchai P."}


def test_member_names_leaves_a_null_display_name_as_none():
    """Autotrim only touches STRING values -- a NULL member_name stays None (never the literal
    string 'None'), the same guarantee Parse.trim_row gives the config seam."""
    at = member_table_audit(member_cache_row("User", None, "e0000000-0000-0000-0000-000000000099"))
    assert at._member_names() == {"e0000000-0000-0000-0000-000000000099": None}


# ---------------------------------------------------------------------------------------------
# One objectId carrying MORE THAN ONE name in the member cache table
# ---------------------------------------------------------------------------------------------

AMBIGUOUS_MARKER = "<ambiguous: 2 names in member table>"


def test_member_names_marks_an_id_with_two_names_as_ambiguous_never_a_pick():
    """Two names for one objectId is the EXACT condition Log.resolve_principal refuses to
    guess on ("an unresolved value is honest, an arbitrary one is not"). _member_names read the
    same table with a plain dict comprehension, so the LAST row silently won. It now renders a
    bounded marker saying the table cannot answer -- neither candidate name, and (deliberately)
    not the bare id either, which is the absent-id fallback drift() documents."""
    at = member_table_audit(
        member_cache_row("User", "Alice", "e0000000-0000-0000-0000-0000000000ff"),
        member_cache_row("User", "Mallory", "E0000000-0000-0000-0000-0000000000FF"),
    )
    got = at._member_names()["e0000000-0000-0000-0000-0000000000ff"]
    assert got == AMBIGUOUS_MARKER
    assert got not in ("Alice", "Mallory")
    assert got != "e0000000-0000-0000-0000-0000000000ff"


def test_who_can_access_renders_an_ambiguous_id_differently_from_an_absent_id():
    """The point of choosing a marker over the bare id: drift()'s docstring documents that
    comparing member_id with member_name is how a caller DETECTS the "not in the member table"
    fallback. Collapsing "ambiguous" onto that same rendering would destroy the signal, so the
    two must stay distinguishable -- absent still shows the id in the caller's OWN spelling
    (never a lower-cased GUID), ambiguous shows the marker."""
    client = FakeFabricClient(
        roles=[
            fake_role(
                "SalesReaders",
                ["/Tables/sales/orders"],
                [
                    "E0000000-0000-0000-0000-0000000000FF",  # ambiguous in the member table
                    "e0000000-0000-0000-0000-0000000000aa",  # absent from the member table
                ],
            )
        ]
    )
    spark = build_spark()
    spark._store[MEMBER_TABLE] = [
        member_cache_row("User", "Alice", "e0000000-0000-0000-0000-0000000000ff"),
        member_cache_row("User", "Mallory", "e0000000-0000-0000-0000-0000000000ff"),
    ]
    at = Audit(spark, CONFIG_TABLE, MAPPING_TABLE, LOG_TABLE, client=client)
    by_id = {
        r.asDict()["member_id"]: r.asDict() for r in at.who_can_access("sales.orders").collect()
    }
    ambiguous = by_id["E0000000-0000-0000-0000-0000000000FF"]["member_name"]
    absent = by_id["e0000000-0000-0000-0000-0000000000aa"]["member_name"]
    assert ambiguous == AMBIGUOUS_MARKER
    assert absent == "e0000000-0000-0000-0000-0000000000aa"
    assert ambiguous != absent


def test_member_names_counts_case_variant_names_as_two_names():
    """Pinned deliberately: Log.resolve_principal builds a CASE-SENSITIVE set of stripped names,
    so "Alice" and "alice" are TWO names there. _member_names uses the SAME predicate -- an
    "alignment" that silently differed on case would ship a NEW inconsistency under the banner
    of fixing one."""
    at = member_table_audit(
        member_cache_row("User", "Alice", "e0000000-0000-0000-0000-0000000000ff"),
        member_cache_row("User", "alice", "e0000000-0000-0000-0000-0000000000ff"),
    )
    assert at._member_names()["e0000000-0000-0000-0000-0000000000ff"] == AMBIGUOUS_MARKER


def test_member_names_one_id_spelled_twice_with_the_same_name_is_not_ambiguous():
    """Whitespace is not ambiguity: both surfaces compare STRIPPED names, so a stray-space
    preload artifact of the same name is one name, and the trimmed display name still surfaces.
    TWO INDEPENDENT strips carry that claim, and each is driven separately here, because the
    table-seeded case alone can only ever prove the first: Parse.trim_row at the read seam
    (_member_rows) hands _member_names rows that are ALREADY trimmed, so deleting the explicit
    .strip() from _member_names left the whole suite green. The second block therefore feeds
    rows that BYPASS the read seam, where the explicit .strip() is the only thing that can
    collapse the two spellings into one name."""
    member_id = "e0000000-0000-0000-0000-0000000000ff"
    at = member_table_audit(
        member_cache_row("User", "Alice", member_id),
        member_cache_row("User", "  Alice  ", member_id),
    )
    assert at._member_names()[member_id] == "Alice"  # the read seam trimmed
    # Same two spellings, but reaching _member_names UNTRIMMED (Parse.trim_row bypassed): only
    # _member_names' own .strip() can still judge them one name.
    untrimmed = [
        member_cache_row("User", "Alice", member_id),
        member_cache_row("User", "  Alice  ", member_id),
    ]
    with mock.patch.object(Audit, "_member_rows", lambda _self: list(untrimmed)):
        got = at._member_names()[member_id]
    assert "ambiguous" not in got  # not the marker -- the explicit .strip() bit
    assert got.strip() == "Alice"


def test_member_names_unique_id_still_resolves_to_its_name():
    """The unambiguous case is untouched -- the marker fires only on genuine ambiguity."""
    at = member_table_audit(
        member_cache_row("User", "Somchai P.", "e0000000-0000-0000-0000-000000000099")
    )
    assert at._member_names() == {"e0000000-0000-0000-0000-000000000099": "Somchai P."}


def test_member_names_ignores_a_null_name_row_when_judging_ambiguity():
    """resolve_principal excludes FALSY names from its name set (`and d.get("member_name")`),
    so ONE real name plus a NULL row is NOT ambiguous. Same predicate here. A blank name is a
    different condition from ambiguity, and the blank-name behaviour itself is unchanged (see
    test_member_names_leaves_a_null_display_name_as_none).

    NULL row FIRST -- the ordering under which the non-ambiguous branch's values[-1] happens to
    land on the real name. The REVERSED ordering is pinned separately, deliberately, because it
    does NOT (see the next test)."""
    at = member_table_audit(
        member_cache_row("User", None, "e0000000-0000-0000-0000-0000000000ff"),
        member_cache_row("User", "Alice", "e0000000-0000-0000-0000-0000000000ff"),
    )
    assert at._member_names()["e0000000-0000-0000-0000-0000000000ff"] == "Alice"


def test_member_names_null_name_row_last_still_wins_by_row_order():
    """The same two rows as the previous test, in the opposite order -- and the answer CHANGES:
    [None, "Alice"] -> "Alice", but ["Alice", None] -> None. Pinned as KNOWN, PRE-EXISTING
    behaviour, not as an endorsement.

    The "never a row-order pick" guarantee is scoped to the AMBIGUITY branch: two distinct
    truthy names for one id render the marker regardless of order. A blank/NULL name is excluded
    from that distinct set, so this pair takes the NON-ambiguous branch, which returns values[-1]
    -- the last row read. Row order therefore still decides here. That is a separate condition
    needing its own decision (the sibling pin test_member_names_leaves_a_null_display_name_as_none
    asserts the same None result for a lone NULL row), so this case exists to make the dependence
    VISIBLE rather than leave it hidden by whichever order a fixture happened to seed."""
    at = member_table_audit(
        member_cache_row("User", "Alice", "e0000000-0000-0000-0000-0000000000ff"),
        member_cache_row("User", None, "e0000000-0000-0000-0000-0000000000ff"),
    )
    got = at._member_names()["e0000000-0000-0000-0000-0000000000ff"]
    assert got is None  # the LAST row's NULL name, not "Alice" -- row order decided
    assert "ambiguous" not in str(got)  # and NOT the ambiguity marker: this is not ambiguity


@pytest.mark.parametrize(
    "second_name", ["Mallory", "alice"], ids=["two distinct names", "case variants"]
)
def test_ambiguous_id_is_refused_by_both_run_by_and_who_can_access(second_name):
    """The ambiguity rule's cross-surface consistency, scoped to the ONLY case where the two surfaces CAN
    agree: PR-A made Log.resolve_principal render a UNIQUE id as "name (id)", so on a unique id
    the two differ BY DESIGN and must not be "aligned". On the AMBIGUOUS id both must refuse to
    invent a name -- run_by keeps the bare attested id, who_can_access renders the marker --
    and neither surface may emit either candidate name.

    The CASE-VARIANT pair is asserted alongside the two-distinct-names pair because whether
    "Alice"/"alice" is one name or two is a judgement call, so it is the axis most likely to
    drift; asserting it on _member_names alone would let a case-folding edit to EITHER reader
    pass the whole suite."""
    ambiguous_id = "e0000000-0000-0000-0000-0000000000ff"
    spark = build_spark()
    spark._store[MEMBER_TABLE] = [
        member_cache_row("User", "Alice", ambiguous_id),
        member_cache_row("User", second_name, ambiguous_id),
    ]
    run_by = Log.resolve_principal(spark, MEMBER_TABLE, ambiguous_id)
    assert run_by == ambiguous_id  # bare attested id -- no label invented
    client = FakeFabricClient(
        roles=[fake_role("SalesReaders", ["/Tables/sales/orders"], [ambiguous_id])]
    )
    at = Audit(spark, CONFIG_TABLE, MAPPING_TABLE, LOG_TABLE, client=client)
    rendered = [r.asDict()["member_name"] for r in at.who_can_access("sales.orders").collect()]
    assert rendered == [AMBIGUOUS_MARKER]
    for surface in (run_by, rendered[0]):
        assert "Alice" not in surface
        assert second_name not in surface


# ---------------------------------------------------------------------------------------------
# I3 / M1 / F5 / M2: member_type disambiguation, input guards, drift frame shape
# ---------------------------------------------------------------------------------------------


def test_resolve_member_ambiguous_name_across_member_types_raises_usage_error():
    """I3: the member table's logical PK is (member_type, lower(member_name)) -- a Group and a
    User legitimately share a display name. With member_type omitted, _resolve_member must
    NAME the ambiguity instead of silently returning whichever row was read first."""
    at = member_table_audit(
        member_cache_row("Group", "finance-team", "e0000000-0000-0000-0000-00000000000a"),
        member_cache_row("User", "finance-team", "e0000000-0000-0000-0000-00000000000b"),
    )
    with pytest.raises(UsageError) as excinfo:
        at._resolve_member("finance-team")
    assert "member_type" in str(excinfo.value)
    assert "Group" in str(excinfo.value)
    assert "User" in str(excinfo.value)


@pytest.mark.parametrize(
    ("member_type", "expected"),
    [
        ("Group", "e0000000-0000-0000-0000-00000000000a"),
        ("User", "e0000000-0000-0000-0000-00000000000b"),
    ],
)
def test_resolve_member_member_type_disambiguates_same_name_across_types(member_type, expected):
    """I3: passing member_type scopes the match to (member_type, lower(member_name)) -- each
    type resolves to ITS OWN objectId, mutation-strong on both directions."""
    at = member_table_audit(
        member_cache_row("Group", "finance-team", "e0000000-0000-0000-0000-00000000000a"),
        member_cache_row("User", "finance-team", "e0000000-0000-0000-0000-00000000000b"),
    )
    assert at._resolve_member("finance-team", member_type=member_type) == expected


def test_resolve_member_member_type_with_no_matching_row_raises_usage_error():
    """I3 negative: the name exists but not for the requested type -- a hard No-Graph error
    naming the type, never a fall-back to the other type's principal."""
    at = member_table_audit(
        member_cache_row("User", "finance-team", "e0000000-0000-0000-0000-00000000000b")
    )
    with pytest.raises(UsageError) as excinfo:
        at._resolve_member("finance-team", member_type="Group")
    assert "not in the member table" in str(excinfo.value)
    assert "Group" in str(excinfo.value)


def test_resolve_member_unambiguous_name_still_resolves_without_member_type():
    """I3 back-compat: one row for the name -> member_type stays optional, every pre-existing
    call site keeps working with no argument."""
    at = member_table_audit(
        member_cache_row("Group", "finance-team", "e0000000-0000-0000-0000-00000000000a")
    )
    assert at._resolve_member("finance-team") == "e0000000-0000-0000-0000-00000000000a"


def test_resolve_member_none_raises_usage_error_not_type_error():
    """M1: a None/blank member is a UsageError like every other Audit input guard -- not the
    raw TypeError GUID_RE.match(None) would raise."""
    at = Audit(build_spark(), CONFIG_TABLE, MAPPING_TABLE, LOG_TABLE)
    with pytest.raises(UsageError) as excinfo:
        at._resolve_member(None)
    assert "member" in str(excinfo.value)


def test_resolve_member_blank_string_raises_usage_error():
    at = Audit(build_spark(), CONFIG_TABLE, MAPPING_TABLE, LOG_TABLE)
    with pytest.raises(UsageError):
        at._resolve_member("   ")


def test_resolve_member_guid_shaped_strips_surrounding_whitespace():
    """F5: a GUID copied out of who_can_access()/an Excel export often carries stray
    whitespace -- GUID_RE is ^...$-anchored, so _resolve_member must strip its own `member`
    argument before the match (diagnose_member already does this; this closes the
    asymmetry between the two entry points)."""
    at = Audit(build_spark(), CONFIG_TABLE, MAPPING_TABLE, LOG_TABLE)
    guid = "e0000000-0000-0000-0000-000000000099"
    assert at._resolve_member(f"  {guid}  ") == guid


def test_resolve_member_name_strips_surrounding_whitespace():
    """F5: same asymmetry on the name path -- member.lower() must compare against a
    stripped input, not one still padded with the copy-paste whitespace itself."""
    at = member_table_audit(
        member_cache_row("User", "somchai@contoso.com", "e0000000-0000-0000-0000-000000000099")
    )
    assert at._resolve_member("  somchai@contoso.com  ") == "e0000000-0000-0000-0000-000000000099"


def test_effective_access_member_type_selects_the_right_same_named_principal():
    """I3 end-to-end: effective_access plumbs member_type through to _resolve_member, so a
    Group and a User sharing a display name resolve to DIFFERENT live-DAR results instead of
    one silently standing in for the other."""
    path = "/Tables/sales/orders"
    group_id = "e0000000-0000-0000-0000-00000000000a"
    user_id = "e0000000-0000-0000-0000-00000000000b"
    spark = build_spark()
    spark._store[MEMBER_TABLE] = [
        member_cache_row("Group", "finance-team", group_id),
        member_cache_row("User", "finance-team", user_id),
    ]
    client = FakeFabricClient(roles=[fake_role("UserOnlyReaders", [path], [user_id])])
    at = Audit(spark, CONFIG_TABLE, MAPPING_TABLE, LOG_TABLE, client=client)
    by_user = [
        r.asDict()
        for r in at.effective_access(
            "finance-team", "sales.orders", member_type="User", engine="spark"
        ).collect()
    ]
    assert by_user[0]["role_name"] == "UserOnlyReaders"
    assert (
        at.effective_access(
            "finance-team", "sales.orders", member_type="Group", engine="spark"
        ).collect()
        == []
    )
    with pytest.raises(UsageError):  # omitted -> ambiguous, never a silent pick
        at.effective_access("finance-team", "sales.orders", engine="spark")


def test_drift_appends_member_id_and_member_name_at_the_end_of_the_frame():
    """M2: drift() now carries member_id ALONGSIDE member_name (as out_of_band/who_can_access/
    grants do), APPENDED after the pre-existing columns so role_name/scope_path/category/detail
    keep their positions. The pairing is what makes the documented id-fallback detectable:
    a cached id shows a real name, an uncached one shows the id in BOTH columns."""
    client = FakeFabricClient(
        roles=[fake_role("R", ["s"], ["e0000000-0000-0000-0000-00000000000a", "mem-oob"])]
    )
    spark = build_spark()
    spark._store[LOG_TABLE] = []
    spark._store[MAPPING_TABLE] = []
    spark._store[MEMBER_TABLE] = [
        member_cache_row("User", "Somchai P.", "e0000000-0000-0000-0000-00000000000a")
    ]
    out = Audit(spark, CONFIG_TABLE, MAPPING_TABLE, LOG_TABLE, client=client).drift()
    assert out.columns == [
        "role_name",
        "scope_path",
        "category",
        "detail",
        "member_id",
        "member_name",
    ]
    by_id = {r.asDict()["member_id"]: r.asDict() for r in out.collect()}
    cached = by_id["e0000000-0000-0000-0000-00000000000a"]
    assert cached["member_name"] == "Somchai P."  # resolved -> id != name
    assert by_id["mem-oob"]["member_name"] == "mem-oob"  # fallback -> id == name


# ── policy-aware in_sync ─────────────────────────────────────────────────────────────────────
# in_sync used to compare (role, scope_path, member_id) triples and nothing else, so an RLS
# predicate rewritten live to 1=1 left it TRUE — the one edit an operator most needs a trace to
# surface. These pin the policy axis: what it catches, what it must NOT cry drift over, and the
# two shapes where it deliberately abstains or fails closed.


def _policy_world(live_role, mapping_row=None):
    """One grant, declared once and deployed once — with whatever policy each side carries.

    The mapping row defaults to the plain Read/no-constraint shape fake_role() emits by default,
    so a test only has to state the ONE thing it is varying.
    """
    row = {
        **_fixture_mapping_provenance(),
        "role_name": "R",
        "scope_path": "/Tables/s",
        "member_group_ids": "m1",
        "scope_type": "Table",
        "permission": "Read",
    }
    row.update(mapping_row or {})
    spark = build_spark()
    spark._store[LOG_TABLE] = [
        seed_validate_row(
            "R",
            "/Tables/s",
            "m1",
            run_at="2026-07-01T01",
            run_by="a@b.c",
            config_version="1",
            mode="apply",
        )
    ]
    spark._store[MAPPING_TABLE] = [row]
    spark._store[MEMBER_TABLE] = []
    spark._store[CONFIG_TABLE] = sample_config_rows()
    client = FakeFabricClient(roles=[live_role])
    return Audit(spark, CONFIG_TABLE, MAPPING_TABLE, LOG_TABLE, client=client)


def _expect_policy_drift(audit):
    """A policy difference must move the policy axis and leave the identity axis alone.

    Asserting missing/unexpected are 0 is the whole point: the grant did not move, only the rule
    on it did, and an operator sent to the missing-grant runbook would be looking for the wrong
    thing entirely.
    """
    rep = audit.report()
    assert rep["missing"] == 0 and rep["unexpected"] == 0, rep
    assert rep["policy_checked"] == 1, rep
    assert rep["policy_mismatch"] == 1, rep
    assert rep["in_sync"] is False, rep
    return rep


@pytest.mark.parametrize(
    "label,live,mapping",
    [
        (
            "rls rewritten to a tautology",
            fake_role("R", ["/Tables/s"], ["m1"], rls={"/Tables/s": "1 = 1"}),
            {"rls_condition": "region = 'TH'"},
        ),
        (
            "rls constraint deleted outright",
            fake_role("R", ["/Tables/s"], ["m1"]),
            {"rls_condition": "region = 'TH'"},
        ),
        (
            "cls allow-list widened to expose a column",
            fake_role("R", ["/Tables/s"], ["m1"], visible_columns={"/Tables/s": ["id", "salary"]}),
            {"visible_columns": "id"},
        ),
        (
            "cls constraint deleted outright",
            fake_role("R", ["/Tables/s"], ["m1"]),
            {"visible_columns": "id"},
        ),
        (
            "permission raised from Read to ReadWrite",
            fake_role("R", ["/Tables/s"], ["m1"], permission="ReadWrite"),
            {"permission": "Read"},
        ),
    ],
)
def test_a_policy_edited_live_breaks_in_sync(label, live, mapping):
    """The five edits that used to pass unseen. Each leaves the triple untouched."""
    _expect_policy_drift(_policy_world(live, mapping))


def test_a_clean_deployment_with_rls_and_cls_stays_in_sync():
    """The other half: a correct deployment must never read as drifted.

    A comparison that cried drift on a clean workspace would be worse than the blind spot it
    replaces — operators switch off a light that is always red.
    """
    audit = _policy_world(
        fake_role(
            "R",
            ["/Tables/s"],
            ["m1"],
            rls={"/Tables/s": "region = 'TH'"},
            visible_columns={"/Tables/s": ["id", "amount"]},
        ),
        {"rls_condition": "region = 'TH'", "visible_columns": "id;amount"},
    )
    rep = audit.report()
    assert rep["policy_checked"] == 1 and rep["policy_mismatch"] == 0, rep
    assert rep["in_sync"] is True, rep


def test_column_order_and_case_are_not_drift():
    """An allow-list is a SET. to_role groups its rules on sorted-lowered columns, so two
    spellings the framework itself treats as one policy must compare equal here too."""
    audit = _policy_world(
        fake_role("R", ["/Tables/s"], ["m1"], visible_columns={"/Tables/s": ["Amount", "ID"]}),
        {"visible_columns": "id;amount"},
    )
    rep = audit.report()
    assert rep["policy_mismatch"] == 0 and rep["in_sync"] is True, rep


def test_an_empty_allow_list_is_not_the_same_as_no_cls():
    """`visible_columns` absent means "every column"; present-but-empty means "none of them".
    Collapsing the two would silently pass a role that hides everything."""
    assert Audit._policy_key("Read", None, None) != Audit._policy_key("Read", None, [])


def test_policy_is_only_compared_on_grants_both_sides_agree_exist():
    """A missing grant has no live policy to read. Scoring its absent side as "open" would count
    one identity fault twice — once as `missing`, once as policy drift — and the two counts would
    stop being independent."""
    audit = _policy_world(
        fake_role("R", ["/Tables/s"], []),  # declared, never deployed to anyone
        {"rls_condition": "region = 'TH'"},
    )
    rep = audit.report()
    assert rep["missing"] == 1, rep
    assert rep["policy_checked"] == 0 and rep["policy_mismatch"] == 0, rep
    assert rep["in_sync"] is False, rep


def _rule(paths, actions, rows=None, cols=None):
    rule = {
        "effect": "Permit",
        "permission": [
            {"attributeName": "Path", "attributeValueIncludedIn": list(paths)},
            {"attributeName": "Action", "attributeValueIncludedIn": list(actions)},
        ],
    }
    constraints = {}
    if rows is not None:
        constraints["rows"] = rows
    if cols is not None:
        constraints["columns"] = cols
    if constraints:
        rule["constraints"] = constraints
    return rule


def _hand_role(*rules):
    return {
        "name": "R",
        "kind": "Policy",
        "decisionRules": list(rules),
        "members": {
            "microsoftEntraMembers": [{"objectId": "m1", "objectType": "Group", "tenantId": "t"}]
        },
    }


@pytest.mark.parametrize(
    "label,role",
    [
        (
            "a second unconstrained rule ADDED for the same path",
            _hand_role(
                _rule(
                    ["/Tables/s"],
                    ["Read"],
                    rows=[{"tablePath": "/Tables/s", "value": "SELECT * FROM s WHERE x = 1"}],
                ),
                _rule(["/Tables/s"], ["Read"]),
            ),
        ),
        (
            "two Action values in one rule",
            _hand_role(_rule(["/Tables/s"], ["Read", "ReadWrite"])),
        ),
        (
            "one tablePath carrying two row constraints",
            _hand_role(
                _rule(
                    ["/Tables/s"],
                    ["Read"],
                    rows=[
                        {"tablePath": "/Tables/s", "value": "SELECT * FROM s WHERE x = 1"},
                        {"tablePath": "/Tables/s", "value": "SELECT * FROM s WHERE 1 = 1"},
                    ],
                )
            ),
        ),
        (
            "one tablePath carrying two column constraints",
            _hand_role(
                _rule(
                    ["/Tables/s"],
                    ["Read"],
                    cols=[
                        {"tablePath": "/Tables/s", "columnNames": ["id"]},
                        {"tablePath": "/Tables/s", "columnNames": ["id", "salary"]},
                    ],
                )
            ),
        ),
    ],
)
def test_a_live_shape_the_framework_cannot_emit_fails_closed(label, role):
    """`to_role` puts each path in exactly one rule, with one Action and at most one constraint
    entry per tablePath. Any multiplicity is somebody else's edit.

    The per-path readers (row_predicate / column_allowlist / path_permissions) take the first
    match and the last Action, so under these encodings they hand back the framework's OWN value
    and the edit reads as clean. Resolving to an unmatchable sentinel is the fix: unreadable is
    reported as different, never as fine.
    """
    rep = _policy_world(role).report()
    assert rep["policy_mismatch"] == 1, (label, rep)
    assert rep["in_sync"] is False, (label, rep)


def test_a_path_repeated_with_the_same_policy_is_not_ambiguous():
    """to_role emits one rule listing a path twice when two mapping rows agree. Reporting drift
    on the framework's own output would be a permanent false red."""
    role = _hand_role(_rule(["/Tables/s", "/Tables/s"], ["Read"]))
    rep = _policy_world(role).report()
    assert rep["policy_mismatch"] == 0 and rep["in_sync"] is True, rep


def test_a_mapping_that_disagrees_with_itself_makes_the_scope_abstain():
    """Two mapping rows for one (role, scope) carrying DIFFERENT policy.

    Rule C3 refuses this at generate — but C3 validates the CONFIG, and a rollback-restored or
    hand-repaired mapping reaches report() without passing it. to_role emits both as rules, so
    `plan` says no_change; scoring it as drift would be a permanent red on a workspace plan calls
    clean, and no re-apply could ever clear it. There is no single desired policy, so the scope
    leaves the comparison rather than being guessed at.
    """
    audit = _policy_world(fake_role("R", ["/Tables/s"], ["m1"]))
    audit.spark._store[MAPPING_TABLE].append(
        {
            **_fixture_mapping_provenance(),
            "role_name": "R",
            "scope_path": "/Tables/s",
            "member_group_ids": "m1",
            "scope_type": "Table",
            "permission": "Read",
            "rls_condition": "region = 'TH'",
        }
    )
    rep = audit.report()
    assert rep["policy_checked"] == 0, rep
    assert rep["policy_mismatch"] == 0 and rep["in_sync"] is True, rep


def test_a_folder_scope_never_reads_as_policy_drift():
    """to_role emits no rows/columns constraint for a folder scope, so an rls_condition sitting on
    a folder row never reaches the live DAR and must not be compared against it."""
    audit = _policy_world(
        fake_role("R", ["/Files/x"], ["m1"]),
        {"scope_path": "/Files/x", "scope_type": "Folder", "rls_condition": "region = 'TH'"},
    )
    rep = audit.report()
    assert rep["policy_mismatch"] == 0 and rep["in_sync"] is True, rep


def test_the_new_keys_are_absent_without_a_client():
    """Both live-state keys are written after the no-client gate, like every other one."""
    spark = build_spark()
    spark._store[LOG_TABLE] = []
    spark._store[MAPPING_TABLE] = []
    spark._store[MEMBER_TABLE] = []
    spark._store[CONFIG_TABLE] = sample_config_rows()
    rep = Audit(spark, CONFIG_TABLE, MAPPING_TABLE, LOG_TABLE, client=None).report()
    assert "policy_checked" not in rep and "policy_mismatch" not in rep, rep
    assert "in_sync" not in rep, rep


def test_drift_itemises_the_policy_difference_report_counted():
    """runbook.md sends the operator from `in_sync == false` to drift(). A red light whose
    itemiser says "matches framework provenance" is a dead end, so the category ships together
    with the count that points at it — and the detail names both sides."""
    audit = _policy_world(
        fake_role("R", ["/Tables/s"], ["m1"], rls={"/Tables/s": "1 = 1"}),
        {"rls_condition": "region = 'TH'"},
    )
    rows = [r.asDict() for r in audit.drift().collect()]
    policy = [r for r in rows if r["category"] == "policy"]
    assert len(policy) == 1, rows
    assert "region = 'TH'" in policy[0]["detail"] and "1 = 1" in policy[0]["detail"]
    assert policy[0]["role_name"] == "R" and policy[0]["scope_path"] == "/Tables/s"


def test_drift_says_ambiguous_in_words_not_as_a_tuple():
    """The sentinel means "this live role describes the path more than once" — a SHAPE, not a
    value. Printing the raw sentinel would read as a policy someone could go and compare."""
    audit = _policy_world(_hand_role(_rule(["/Tables/s"], ["Read", "ReadWrite"])))
    detail = [
        r.asDict()["detail"] for r in audit.drift().collect() if r.asDict()["category"] == "policy"
    ]
    assert len(detail) == 1 and "describes this path more than once" in detail[0], detail
    assert "__ambiguous__" not in detail[0], detail


def test_a_grant_with_no_provenance_is_out_of_band_even_when_its_policy_also_differs():
    """One row carries one category. A stranger holding the grant outranks the rule reading
    differently — and a grant nobody established has no framework policy to have drifted from."""
    audit = _policy_world(
        fake_role("R", ["/Tables/s"], ["m1"], rls={"/Tables/s": "1 = 1"}),
        {"rls_condition": "region = 'TH'"},
    )
    audit.spark._store[LOG_TABLE] = []  # nothing was ever established
    cats = [r.asDict()["category"] for r in audit.drift().collect()]
    assert cats.count("out_of_band") == 1 and "policy" not in cats, cats


def test_drift_abstains_on_a_self_contradicting_mapping_too():
    """report() and drift() must agree about which scopes are comparable, or the count and the
    itemisation would tell the operator different stories."""
    audit = _policy_world(fake_role("R", ["/Tables/s"], ["m1"]))
    audit.spark._store[MAPPING_TABLE].append(
        {
            **_fixture_mapping_provenance(),
            "role_name": "R",
            "scope_path": "/Tables/s",
            "member_group_ids": "m1",
            "scope_type": "Table",
            "permission": "Read",
            "rls_condition": "region = 'TH'",
        }
    )
    cats = {r.asDict()["category"] for r in audit.drift().collect()}
    assert "policy" not in cats, cats


def test_provenance_reports_both_ends_and_claims_no_continuity():
    """`since` claimed something OLAF cannot know.

    The log records what OLAF did. It cannot record a role deleted straight from the Fabric UI, so
    an apply, an out-of-band deletion, and a re-apply are indistinguishable from one continuous
    grant. Reporting the earliest timestamp as `since` therefore asserted unbroken access across a
    gap nothing observed — the same overreach as calling an omitted role `deleted`.

    Neither end alone is right, either: taking the LATEST would reset the date on every routine
    re-deploy of an unchanged config, which is exactly the question an access review is asking.

    So report both ends, name them for what they are, and let a wide gap be the operator's cue to
    look. `first_applied` and `last_applied` are each exactly knowable; continuity is not, and
    nothing here claims it.
    """
    rows = [
        seed_validate_row(
            "R",
            "/T",
            "mem-1",
            run_at="2026-08-19T01",
            run_by="alice@example.com",
            config_version="12",
        ),
        # ... a UI deletion happens here and leaves no row at all ...
        seed_validate_row(
            "R",
            "/T",
            "mem-1",
            run_at="2026-08-24T05",
            run_by="bob@example.com",
            config_version="15",
        ),
    ]
    got = [r.asDict() for r in trail(rows).grants().collect()]
    assert len(got) == 1
    g = got[0]
    assert g["first_applied"] == "2026-08-19T01"
    assert g["last_applied"] == "2026-08-24T05"
    assert "since" not in g, "the word claimed continuity the log cannot establish"
    # the actor is split across both ends: one `granted_by` beside two timestamps would leave
    # the reader guessing which end it answered for, and here the two ends differ.
    assert g["first_granted_by"] == "alice@example.com"
    assert g["last_granted_by"] == "bob@example.com"
    assert "granted_by" not in g, "an unqualified actor cannot say which end it belongs to"
    # config_version follows the LAST push: that is the state now in effect
    assert g["config_version"] == "15"
