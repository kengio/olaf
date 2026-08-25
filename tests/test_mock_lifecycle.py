"""generate idempotency, plan/apply REPLACE semantics, rollback re-running the whole chain, and
mode=validate's zero-write dry run — driven end-to-end on the fakes.

Ported from `olaf_test_integration.ipynb` classes `GenerateIdempotency`,
`PlanApplyReplaceAndRollback` and `ValidateDryRun` (scope "mock").
"""

import io
import json
import posixpath
from unittest import mock

import pytest

import _olaf_runtime as rt
from _olaf_runtime import Catalog, Log, PostWriteAuditError, __version__
from _fakes import (
    CONFIG_TABLE,
    GRP_READERS,
    GRP_READERS_NAME,
    LOG_TABLE,
    MAPPING_TABLE,
    MEMBER_TABLE,
    SAMPLE_FOLDERS,
    FakeFabricClient,
    _FakePandas,
    build_spark,
    carve_config_row,
    fake_role,
    lakehouse_writes,
    make_dep,
    member_cache_row,
    run_apply,
    run_generate,
    run_validate,
    sample_config_rows,
    seed_sample_members,
)


def ready(client=None, rows=None, seed_members=True):
    """A set-up workspace with the short config authored (and, by default, members seeded)."""
    spark = build_spark()
    client = client if client is not None else FakeFabricClient([])
    make_dep(spark, client, "setup").setup()
    spark._store[CONFIG_TABLE] = sample_config_rows() if rows is None else rows
    if seed_members:
        seed_sample_members(spark)
    return spark, client


def generated(client=None):
    """…and generate already run, so the mapping lock-file exists."""
    spark, client = ready(client)
    run_generate(make_dep(spark, client, "generate"))
    return spark, client


# ---------------------------------------------------------------------------------------------
# GenerateIdempotency — generate(rebuild=False) is idempotent: a config unchanged since the last
# generate rebuilds nothing and logs a single 'no_change' row; a content change (or rebuild=True)
# rebuilds.
# ---------------------------------------------------------------------------------------------


def test_skips_when_config_unchanged():
    spark, client = ready()
    first = run_generate(make_dep(spark, client, "generate"))
    assert first["changed"]
    mapping_before = [dict(r) for r in spark._store[MAPPING_TABLE]]
    second = run_generate(make_dep(spark, client, "generate", run="R2"))  # config unchanged
    assert not second["changed"]
    assert set(second) == {"changed", "message", "data"}
    assert set(second["data"]) == {"grants"}
    assert second["data"]["grants"] == 3  # existing mapping row count, not a rebuild
    assert spark._store[MAPPING_TABLE] == mapping_before  # untouched
    nochange = [
        r for r in spark._store[LOG_TABLE] if r["mode"] == "generate" and r["status"] == "no_change"
    ]
    assert len(nochange) == 1  # exactly one 'generate'/'no_change' row


def test_generate_skip_refuses_mixed_mapping_provenance_without_audit_or_mapping_write():
    """The idempotency fast path must not certify a lock-file whose tail belongs elsewhere."""
    spark, client = generated()
    spark._store[MAPPING_TABLE][1]["generated_at"] = "poisoned-generation"
    mapping_before = [dict(row) for row in spark._store[MAPPING_TABLE]]
    log_before = [dict(row) for row in spark._store[LOG_TABLE]]

    with pytest.raises(rt.UsageError, match="mapping provenance"):
        run_generate(make_dep(spark, client, "generate", run="R2"))

    assert spark._store[MAPPING_TABLE] == mapping_before
    assert spark._store[LOG_TABLE] == log_before


def test_rebuild_regenerates_even_when_unchanged():
    spark, client = generated()
    forced = run_generate(make_dep(spark, client, "generate", run="R2"), rebuild=True)
    assert forced["changed"]
    assert set(forced) == {"changed", "message", "data"}
    assert set(forced["data"]) == {
        "grants",
        "roles",
        "warnings",
        "csv",
        "summary",
        "lakehouse",
        "workspace",
        "dar_snapshot_safe",
        "workspace_isolation",
        "dar_etag",
    }


def test_reruns_when_config_content_changes():
    spark, client = generated()
    rows = sample_config_rows()
    rows[0] = dict(rows[0], rls_condition="region = 'south'")  # a real edit -> config_hash changes
    spark._store[CONFIG_TABLE] = rows
    again = run_generate(make_dep(spark, client, "generate", run="R3"))
    assert again["changed"]  # not skipped — content differs from the stored mapping


def test_idempotency_read_tolerates_absent_mapping():
    # a first-ever generate: the mapping table does not exist yet, so the idempotency read is
    # exception-safe (treats it as "no prior generation") and proceeds to build.
    spark, client = ready()
    del spark._store[MAPPING_TABLE]  # fresh vault: generate has never written the lock-file
    res = run_generate(make_dep(spark, client, "generate"))
    assert res["changed"]
    assert MAPPING_TABLE in spark._store  # generate (re)created it


ROTATED = "99999999-9999-9999-9999-999999999999"  # the group's objectId after an Entra rotation


def test_editing_a_member_row_objectid_defeats_the_idempotency_skip():
    """A member row's objectId changed under an UNCHANGED config — the case config_hash cannot
    see, because it fingerprints the config rows and the member table is not one of them.

    Skipped, generate reports no_change while the mapping (and so every later plan/apply) still
    carries the OLD id: access granted to a principal that no longer exists, or to a different
    one, under an audit trail saying nothing changed. The skip therefore re-derives the stamped
    ids from the member table and regenerates on any mismatch."""
    spark, client = generated()
    stamped_before = {r["member_group_ids"] for r in spark._store[MAPPING_TABLE]}
    assert GRP_READERS in stamped_before  # the pre-rotation id really is in the lock-file

    hash_before = make_dep(spark, client, "generate").config_hash
    spark._store[MEMBER_TABLE] = [
        member_cache_row("Group", GRP_READERS_NAME, ROTATED) if r["member_type"] == "Group" else r
        for r in spark._store[MEMBER_TABLE]
    ]
    dep = make_dep(spark, client, "generate", run="R2")
    assert dep.config_hash == hash_before, "the config did not change — that is the whole problem"

    res = run_generate(dep)

    assert res["changed"], "generate skipped, so the stale objectId would still deploy"
    ids = {r["member_group_ids"] for r in spark._store[MAPPING_TABLE] if r["member_group_ids"]}
    assert ids == {ROTATED}  # the mapping now carries the rotated id
    assert GRP_READERS not in ids


def test_deleting_a_member_row_defeats_the_skip_and_the_no_graph_gate_then_blocks():
    """The other half of the same read: a member row REMOVED leaves its name resolving to nothing,
    which is a mismatch against the stored id just as a changed id is. Regenerating is what lets
    the No-Graph member gate speak — skipping would have kept deploying a grant to a principal the
    member table no longer lists."""
    spark, client = generated()
    spark._store[MEMBER_TABLE] = [
        r for r in spark._store[MEMBER_TABLE] if r["member_type"] != "Group"
    ]
    with pytest.raises(SystemExit) as excinfo:
        run_generate(make_dep(spark, client, "generate", run="R2"))
    assert "not found in onelake_security_member" in str(excinfo.value)
    assert GRP_READERS_NAME in str(excinfo.value)


def test_a_conflicting_member_id_blocks_the_skip_even_when_the_stamped_id_wins():
    """External security audit (2026-08-16), issue #12. A second row remapping the SAME
    (type, name) to a DIFFERENT objectId is a hard cache error ('conflicting objectIds') on
    every full-validation path — but the fast skip loaded the cache through a helper that
    DISCARDED the error list, so with the rows ordered so that last-row-wins resolves to the
    STAMPED id (no drift), generate certified an error-state member table as clean, unchanged
    state. The skip must refuse whenever cache errors exist; falling through lands in the
    full validation, which blocks with the complete list."""
    spark, client = generated()
    # INSERT the conflicting row FIRST: last-row-wins keeps resolving to the original id, so
    # the drift check alone stays quiet — only the error list can catch this state.
    spark._store[MEMBER_TABLE].insert(0, member_cache_row("Group", GRP_READERS_NAME, ROTATED))
    with pytest.raises(SystemExit) as excinfo:
        run_generate(make_dep(spark, client, "generate", run="R2"))
    assert "generate blocked" in str(excinfo.value)
    assert "conflicting objectIds" in str(excinfo.value)


def test_a_duplicate_object_id_under_a_new_name_blocks_the_skip():
    # the mirror cache error: one objectId listed as a SECOND principal. The config's own
    # names still resolve to their stamped ids (no drift), so only the error list sees it.
    spark, client = generated()
    spark._store[MEMBER_TABLE].append(member_cache_row("Group", "sg-imposter", GRP_READERS))
    with pytest.raises(SystemExit) as excinfo:
        run_generate(make_dep(spark, client, "generate", run="R2"))
    assert "generate blocked" in str(excinfo.value)
    assert "more than one principal" in str(excinfo.value)


def test_a_failed_csv_export_leaves_the_mapping_uncommitted():
    """External security audit (2026-08-16), issue #11. The review CSV is exported BEFORE the
    mapping commit, so a failed export aborts the run while the lock-file is still
    uncommitted — a plain retry then rebuilds end-to-end instead of fast-skipping past the
    missing artifact."""
    spark, client = ready()
    with mock.patch.object(_FakePandas, "to_csv", side_effect=OSError("disk full")):
        with pytest.raises(OSError):
            run_generate(make_dep(spark, client, "generate"))
    assert spark._store[MAPPING_TABLE] == []  # nothing committed behind the failed artifact
    make_dep(spark, client, "generate")._boundary().clear_incident("access-review/retry")
    res = run_generate(make_dep(spark, client, "generate", run="R2"))  # plain retry
    assert res["changed"] and res["data"]["csv"]  # full rebuild, artifact included


def test_generate_skip_re_exports_a_missing_review_csv():
    # Self-healing half 1: the skip re-derives the generation's mapping_hash and re-exports
    # the review CSV when the history dir lacks it; when the file exists (the dedupe hit),
    # the skip writes nothing — no copies pile up.
    spark, client = ready()
    first = run_generate(make_dep(spark, client, "generate"))
    exported = []
    with mock.patch.object(_FakePandas, "to_csv", lambda self, *a, **k: exported.append(a)):
        # history lists nothing -> the artifact is missing -> the skip re-exports it
        res = run_generate(make_dep(spark, client, "generate", run="R2"))
        assert not res["changed"] and len(exported) == 1
        # history lists the file -> present -> the skip writes nothing
        existing = posixpath.basename(first["data"]["csv"])
        res = run_generate(make_dep(spark, client, "generate", run="R3"), history=[existing])
        assert not res["changed"] and len(exported) == 1


def test_generate_retry_after_a_failed_completion_write_repairs_the_audit_trail():
    """Self-healing half 2: a run that committed the mapping and then died before its audit
    rows landed leaves a generation with no completion record. The failure itself was loud —
    but the retry takes the skip, which now probes for the completion row and repairs it
    (explicitly marked as a repair) instead of logging only 'no_change'."""
    spark, client = ready()
    with mock.patch.object(Log, "write", side_effect=RuntimeError("log write failed")):
        with pytest.raises(RuntimeError):
            run_generate(make_dep(spark, client, "generate"))
    assert spark._store[MAPPING_TABLE]  # committed — the failure hit AFTER the commit
    assert not [r for r in spark._store[LOG_TABLE] if r.get("mode") == "generate"]
    make_dep(spark, client, "generate")._boundary().clear_incident("access-review/retry")
    res = run_generate(make_dep(spark, client, "generate", run="R2"))  # plain retry -> skip
    assert not res["changed"]
    completes = [
        r
        for r in spark._store[LOG_TABLE]
        if r.get("mode") == "generate" and r.get("action") == "complete"
    ]
    assert len(completes) == 1 and "completion record added" in completes[0]["message"]
    assert completes[0]["mapping_hash"]  # the repair row names the generation it repaired
    # a third run finds the completion record present and repairs nothing further
    res = run_generate(make_dep(spark, client, "generate", run="R3"))
    assert not res["changed"]
    completes_after = [
        r
        for r in spark._store[LOG_TABLE]
        if r.get("mode") == "generate" and r.get("action") == "complete"
    ]
    assert len(completes_after) == 1  # still exactly one — no duplicate repairs


def test_a_mapping_predating_the_provenance_columns_never_skips():
    """Issue #7's regression: a mapping written by a schema that predates the provenance
    columns (config_hash / framework_version / target ids missing entirely) must read as
    \"do not skip\" — the fail-closed path — and rebuild cleanly rather than KeyError."""
    spark, client = generated()
    spark._store[MAPPING_TABLE] = [
        {k: v for k, v in r.items() if k not in ("config_hash", "framework_version")}
        for r in spark._store[MAPPING_TABLE]
    ]
    res = run_generate(make_dep(spark, client, "generate", run="R2"))
    assert res["changed"]  # rebuilt — the unprovable generation was not certified
    assert {r["framework_version"] for r in spark._store[MAPPING_TABLE]} == {__version__}


def test_a_mapping_stamped_by_another_framework_version_never_skips():
    """The skip requires the stamping framework_version to equal the RUNNING version:
    content stamped by another version has not been validated under this version's rules,
    so it revalidates and re-stamps instead of attesting success (fresh CSV + completion
    row) to rules it never met — which also makes every new validation rule retroactive."""
    spark, client = generated()
    for r in spark._store[MAPPING_TABLE]:
        r["framework_version"] = "0.9.9"  # a stamp from some other build of the framework
    res = run_generate(make_dep(spark, client, "generate", run="R2"))
    assert res["changed"]  # revalidated and rebuilt, not skipped
    assert {r["framework_version"] for r in spark._store[MAPPING_TABLE]} == {__version__}


def test_a_foreign_named_file_embedding_the_hash_cannot_preempt_the_export():
    # The dedupe matches the tool's own CANONICAL filename pattern: a planted file whose
    # name merely embeds the hash must neither suppress the real export nor have its own
    # path recorded (the path is load-bearing — it lands in the complete row).
    spark, client = ready()
    first = run_generate(make_dep(spark, client, "generate"))
    hash_suffix = posixpath.basename(first["data"]["csv"]).rsplit("_", 1)[-1]  # '<hash>.csv'
    planted = f"evil_{hash_suffix}"
    exported = []
    with mock.patch.object(_FakePandas, "to_csv", lambda self, *a, **k: exported.append(a)):
        res = run_generate(
            make_dep(spark, client, "generate", run="R2"), history=[planted], rebuild=True
        )
    assert exported  # the planted file did not suppress the export
    assert "evil" not in res["data"]["csv"]  # ...and its path was never recorded


def test_an_untouched_member_table_still_takes_the_idempotency_skip():
    """The optimisation has to survive the new guard. Same config, same member table, second
    generate: still a skip, still one 'no_change' row, mapping untouched. A re-derivation that
    disagreed with the helper that did the stamping would regenerate on every run instead, and the
    only symptom would be a pipeline that had quietly stopped being idempotent."""
    spark, client = generated()
    mapping_before = [dict(r) for r in spark._store[MAPPING_TABLE]]
    members_before = [dict(r) for r in spark._store[MEMBER_TABLE]]

    second = run_generate(make_dep(spark, client, "generate", run="R2"))

    assert not second["changed"]
    assert "config unchanged" in second["message"]
    assert second["data"]["grants"] == len(mapping_before)  # the count comes off the same read
    assert spark._store[MAPPING_TABLE] == mapping_before  # nothing rebuilt
    assert spark._store[MEMBER_TABLE] == members_before  # ...and nothing written back
    nochange = [
        r for r in spark._store[LOG_TABLE] if r["mode"] == "generate" and r["status"] == "no_change"
    ]
    assert len(nochange) == 1


# ---------------------------------------------------------------------------------------------
# PlanApplyReplaceAndRollback
# ---------------------------------------------------------------------------------------------


def test_plan_drift_logs_only_the_changed_role():
    # live already carries SalesReaders == desired; only RawReaders drifts, so only IT is logged.
    spark, applied = generated()
    make_dep(spark, applied, "plan").plan()
    run_apply(make_dep(spark, applied, "apply"))  # applied now holds both roles (== desired)
    both = applied.list_roles()
    sales_only = FakeFabricClient([r for r in both if r["name"] == "SalesReaders"])
    before = len(spark._store[LOG_TABLE])
    res = make_dep(spark, sales_only, "plan").plan()
    assert res["changed"]
    assert res["data"]["drift"] == {"RawReaders": "create"}  # only the missing role
    added = spark._store[LOG_TABLE][before:]
    created = [r["role_name"] for r in added if r["action"] == "create"]
    assert created == ["RawReaders"]  # SalesReaders (no_change) is NOT logged
    assert any(r["action"] == "complete" for r in added)  # complete row unlocks apply


def test_apply_default_reports_prior_live_omission_candidates():
    # The config payload excludes these prior-live roles. OLAF reports candidates and requires
    # post-state review; the fake client's replacement semantics are not a platform outcome.
    spark, client = generated(
        FakeFabricClient(
            [
                fake_role("LegacyManual", ["/Tables/x/y"], [GRP_READERS]),
                fake_role("DefaultReader", ["/Tables/a/b"], [GRP_READERS]),
            ]
        )
    )
    make_dep(spark, client, "plan").plan()
    res = run_apply(make_dep(spark, client, "apply"))  # DEFAULT = replace
    assert not res["data"]["keep_unmanaged"]
    assert res["data"]["request"] == "config_payload"
    assert res["data"]["omitted_role_candidates"] == ["DefaultReader", "LegacyManual"]
    assert res["data"]["drift_omission_candidates"] == ["DefaultReader", "LegacyManual"]
    assert res["data"]["post_state_review_required"] is True


def test_apply_reports_the_push_status_and_the_written_count_as_separate_fields():
    """`applied` once carried the bulk PUT's HTTP status (200) in a count-shaped name, one
    column from an omission-candidate field, where it reads as "200 roles applied" mid-incident. The two
    facts are now two fields, and their values disagree — 200 vs 2 — so a reader cannot
    confuse them. The incremental branch counts the whole upserted body (the merged set the
    PUT actually wrote), which is why it is 3 here and not the 2 managed roles."""
    spark, client = generated(
        FakeFabricClient([fake_role("LegacyManual", ["/Tables/x/y"], [GRP_READERS])])
    )
    make_dep(spark, client, "plan").plan()
    res = run_apply(make_dep(spark, client, "apply"), keep_unmanaged=True)
    assert res["data"]["push_status"] == 200  # HTTP, from the client
    assert res["data"]["roles_written"] == 3  # 2 managed + the kept LegacyManual
    assert "applied" not in res["data"]  # renamed, not duplicated
    # a full-set PUT makes the written body the live set
    assert len(client.list_roles()) == res["data"]["roles_written"]


def test_apply_keep_unmanaged_reports_drift_omission_candidates_but_sends_them():
    # keep_unmanaged=True uses an incremental payload, so a prior-live role is carried rather
    # than an omission candidate in the submitted request.
    spark, client = generated(
        FakeFabricClient([fake_role("LegacyManual", ["/Tables/x/y"], [GRP_READERS])])
    )
    make_dep(spark, client, "plan").plan()
    res = run_apply(make_dep(spark, client, "apply"), keep_unmanaged=True)
    assert res["data"]["keep_unmanaged"]
    assert res["data"]["request"] == "incremental_payload"
    assert res["data"]["omitted_role_candidates"] == []
    assert res["data"]["drift_omission_candidates"] == ["LegacyManual"]
    assert res["data"]["post_state_review_required"] is True


def prepared_for_rollback(client=None):
    spark, client = ready(client)
    spark._history_rows = [{"version": 2}, {"version": 3}]  # a previous version to roll back to
    run_generate(make_dep(spark, client, "generate"))
    # a mode='plan' record for this config state (live still empty) unlocks the apply the rollback
    # chain runs (rollback's own plan writes mode='rollback', which the plan-gate does not match).
    make_dep(spark, client, "plan").plan()
    return spark, client


def rollback(dep, to_version="", reason="revert"):
    with (
        # lakehouse_writes covers os.makedirs AND the apply leg's pre-push role-backup write
        lakehouse_writes(),
        mock.patch.object(
            Catalog, "fs_folder_lister", lambda *_ids: lambda b: SAMPLE_FOLDERS.get(b, [])
        ),
        mock.patch.object(Catalog, "_export_lister", lambda p: []),
    ):
        return dep.rollback(to_version, reason)


def test_existing_sentinel_blocks_rollback_before_prepared_restore_or_live_put(monkeypatch):
    spark, client = prepared_for_rollback()
    log_before = [dict(row) for row in spark._store[LOG_TABLE]]

    def refuse(_boundary):
        raise rt.ControlDataGuardError("control-data incident sentinel already exists")

    monkeypatch.setattr(rt.ControlBoundary, "_create_sentinel", refuse)
    with pytest.raises(rt.ControlDataGuardError, match="sentinel already exists"):
        rollback(make_dep(spark, client, "rollback"), "2", "reviewed rollback")

    assert spark._store[LOG_TABLE] == log_before
    assert not [q for q in spark._sql_calls if q.upper().startswith("RESTORE TABLE")]
    assert not [call for call in client.put_calls if not call["dry_run"]]


def test_rollback_refuses_mixed_mapping_provenance_before_prepared_restore_or_live_put():
    """Rollback preflight reads every current mapping row before it records or restores anything."""
    spark, client = prepared_for_rollback()
    spark._store[MAPPING_TABLE][1]["generated_at"] = "poisoned-generation"
    log_before = [dict(row) for row in spark._store[LOG_TABLE]]

    with pytest.raises(rt.UsageError, match="mapping provenance"):
        rollback(make_dep(spark, client, "rollback"), "2", "reviewed rollback")

    assert spark._store[LOG_TABLE] == log_before
    assert not [q for q in spark._sql_calls if q.upper().startswith("RESTORE TABLE")]
    assert not [call for call in client.put_calls if not call["dry_run"]]


def test_rollback_prewrite_blocks_the_prepared_audit_after_dar_changes(monkeypatch):
    """Historical validation cannot authorize the durable rollback intent after a DAR race."""
    spark, client = prepared_for_rollback()
    dep = make_dep(spark, client, "rollback")
    log_before = [dict(row) for row in spark._store[LOG_TABLE]]
    write = dep.audit.write

    def rotate_then_write(rows):
        client.simulate_external_edit()
        return write(rows)

    monkeypatch.setattr(dep.audit, "write", rotate_then_write)
    with pytest.raises(rt.ControlDataGuardError, match="changed after the approved snapshot"):
        rollback(dep, "2", "reviewed rollback")

    assert spark._store[LOG_TABLE] == log_before
    assert not [q for q in spark._sql_calls if q.upper().startswith("RESTORE TABLE")]
    assert not [call for call in client.put_calls if not call["dry_run"]]


def test_rollback_prewrite_blocks_restore_after_a_safe_prepared_intent(monkeypatch):
    """The prepared row is durable history, not authorization for a later config restore."""
    spark, client = prepared_for_rollback()
    dep = make_dep(spark, client, "rollback")
    write = dep.audit.write

    def prepare_then_rotate(rows):
        result = write(rows)
        if any(row.get("action") == "rollback" and row.get("status") == "prepared" for row in rows):
            client.simulate_external_edit()
        return result

    monkeypatch.setattr(dep.audit, "write", prepare_then_rotate)
    with pytest.raises(rt.ControlDataGuardError, match="changed after the approved snapshot"):
        rollback(dep, "2", "reviewed rollback")

    assert [
        row
        for row in spark._store[LOG_TABLE]
        if row.get("action") == "rollback" and row.get("status") == "prepared"
    ]
    assert not [q for q in spark._sql_calls if q.upper().startswith("RESTORE TABLE")]
    assert not [call for call in client.put_calls if not call["dry_run"]]


def test_rollback_previous_version_reruns_chain():
    spark, client = prepared_for_rollback()
    before = len(spark._store[LOG_TABLE])
    res = rollback(make_dep(spark, client, "rollback"), "", "revert bad change")
    assert res["changed"]
    assert set(res["data"]) == {
        "rollback",
        "generate",
        "apply",
        "dar_snapshot_safe",
        "workspace_isolation",
        "dar_etag",
    }
    rollback_data = res["data"]["rollback"]
    assert {key: rollback_data[key] for key in ("from_version", "to_version", "reason")} == {
        "from_version": 3,
        "to_version": 2,
        "reason": "revert bad change",
    }
    assert rollback_data["source_config_hash"] and rollback_data["target_config_hash"]
    assert res["data"]["apply"]["push_status"] == 200
    # The durable prepared intent precedes RESTORE; restored is appended only after the target
    # version/hash are confirmed. Both carry the reviewed rollback identity.
    rollback_rows = [r for r in spark._store[LOG_TABLE][before:] if r["action"] == "rollback"]
    assert [row["status"] for row in rollback_rows] == ["prepared", "restored"]
    assert "revert bad change" in rollback_rows[-1]["message"]
    assert {r["name"] for r in client.list_roles()} == {"SalesReaders", "RawReaders"}


def test_rollback_reports_prior_live_omission_candidates():
    # The rollback apply leg submits the restored config payload and reports any prior-live
    # roles it omits as candidates; OLAF does not certify a platform deletion outcome.
    spark, client = prepared_for_rollback(
        client=FakeFabricClient([fake_role("LegacyManual", ["/Tables/x/y"], [GRP_READERS])])
    )
    res = rollback(make_dep(spark, client, "rollback"), "", "revert bad change")
    assert res["changed"]
    assert res["data"]["apply"]["request"] == "config_payload"
    assert res["data"]["apply"]["omitted_role_candidates"] == ["LegacyManual"]
    assert res["data"]["apply"]["post_state_review_required"] is True


def test_rollback_regenerates_even_when_the_restored_config_content_is_unchanged():
    """rollback's `self.generate(rebuild=True)` is the ONLY thing forcing the regeneration when
    the restored config's content-fingerprint is unchanged — and that is exactly the case the
    other rollback fixtures never hit, because a restore that changes content re-generates via
    generate's content-hash path whatever the flag says. Here the mapping already carries this
    config_hash, so rebuild=False would take the idempotent SKIP: no mapping rebuild, no CSV,
    and a 'no_change' log row — leaving apply to push a mapping the rollback never refreshed.
    The two adjacent calls (generate(rebuild=True) / apply(keep_unmanaged=False)) used to be
    one `force` literal meaning opposite things, so this pins the generate half."""
    spark, client = prepared_for_rollback()
    dep = make_dep(spark, client, "rollback")
    # precondition: mapping is already stamped with THIS config's hash -> the skip is armed.
    assert {str(r["config_hash"]) for r in spark._store[MAPPING_TABLE]} == {dep.config_hash}
    before = len(spark._store[LOG_TABLE])
    res = rollback(dep, "", "revert bad change")
    gen = res["data"]["generate"]
    # the full regeneration shape — the skip returns only {"grants": N}, with no csv/roles
    assert "csv" in gen
    assert (gen["roles"], gen["grants"]) == (2, 3)
    added = spark._store[LOG_TABLE][before:]
    assert not [r for r in added if r["status"] == "no_change"]  # never took the skip


def test_rollback_explicit_version_reruns_chain():
    spark, client = prepared_for_rollback()
    res = rollback(make_dep(spark, client, "rollback"), "2", "pin to v2")
    assert res["data"]["rollback"]["to_version"] == 2  # explicit version honored
    assert res["changed"]


def test_rollback_materializes_and_validates_historical_config_before_restore():
    spark, client = prepared_for_rollback()
    bad = dict(sample_config_rows()[0], role_name="1invalid")
    spark._version_store = {2: [bad], 3: [dict(row) for row in spark._store[CONFIG_TABLE]]}

    with pytest.raises(SystemExit, match="historical config"):
        rollback(make_dep(spark, client, "rollback"), "2", "review historical target")

    assert not [q for q in spark._sql_calls if q.upper().startswith("RESTORE TABLE")]
    assert not [call for call in client.put_calls if not call["dry_run"]]


def test_rollback_prepared_is_durable_before_restore_and_restored_is_after_restore():
    spark, client = prepared_for_rollback()
    observed = {}
    real_sql = spark.sql

    def spy(query):
        if str(query).strip().upper().startswith("RESTORE TABLE"):
            rows = [r for r in spark._store[LOG_TABLE] if r.get("action") == "rollback"]
            observed["at_restore"] = [(r["status"], r["message"]) for r in rows]
        return real_sql(query)

    spark.sql = spy
    rollback(make_dep(spark, client, "rollback"), "2", "ordered rollback")

    assert [status for status, _ in observed["at_restore"]] == ["prepared"]
    statuses = [r["status"] for r in spark._store[LOG_TABLE] if r.get("action") == "rollback"]
    assert statuses == ["prepared", "restored"]
    prepared = json.loads(observed["at_restore"][0][1])
    assert prepared["schema"] == 1 and prepared["phase"] == "prepared"
    assert prepared["from_version"] == 3 and prepared["to_version"] == 2
    assert prepared["source_config_hash"] and prepared["target_config_hash"]


def test_rollback_detects_current_version_race_before_restore():
    from _fakes import _HistoryFrame

    spark, client = prepared_for_rollback()
    real_sql = spark.sql
    describe_calls = 0

    def racing_sql(query):
        nonlocal describe_calls
        if str(query).strip().upper().startswith("DESCRIBE HISTORY"):
            describe_calls += 1
            if describe_calls >= 2:
                return _HistoryFrame([{"version": 2}, {"version": 3}, {"version": 4}])
        return real_sql(query)

    spark.sql = racing_sql
    with pytest.raises(SystemExit, match="version changed") as excinfo:
        rollback(make_dep(spark, client, "rollback"), "2", "race test")
    assert getattr(excinfo.value, "changed", False) is False
    assert not [q for q in spark._sql_calls if q.upper().startswith("RESTORE TABLE")]


def test_rollback_target_hash_mismatch_after_restore_stops_before_generate_or_put():
    spark, client = prepared_for_rollback()
    target = [dict(row) for row in spark._store[CONFIG_TABLE]]
    spark._version_store = {2: target, 3: target}
    real_sql = spark.sql

    def tamper_after_restore(query):
        result = real_sql(query)
        if str(query).strip().upper().startswith("RESTORE TABLE"):
            spark._store[CONFIG_TABLE] = [dict(target[0], role_name="UnexpectedReaders")]
        return result

    spark.sql = tamper_after_restore
    dep = make_dep(spark, client, "rollback")
    with pytest.raises(SystemExit, match="target config hash changed") as excinfo:
        rollback(dep, "2", "hash race")
    assert getattr(excinfo.value, "changed") is True
    assert not [call for call in client.put_calls if not call["dry_run"]]
    assert not [
        row
        for row in spark._store[LOG_TABLE]
        if row.get("action") == "rollback" and row.get("status") == "restored"
    ]


def test_rollback_restored_audit_failure_is_typed_changed_true_and_stops_chain():
    spark, client = prepared_for_rollback()
    dep = make_dep(spark, client, "rollback")
    real_write = dep.audit.write

    def fail_restored(rows):
        if any(row.get("action") == "rollback" and row.get("status") == "restored" for row in rows):
            raise RuntimeError("rollback audit unavailable")
        return real_write(rows)

    dep.audit.write = fail_restored
    with pytest.raises(PostWriteAuditError) as excinfo:
        rollback(dep, "2", "audit test")
    assert excinfo.value.changed is True
    assert excinfo.value.operation == "rollback"
    assert [q for q in spark._sql_calls if q.upper().startswith("RESTORE TABLE")]
    assert not [call for call in client.put_calls if not call["dry_run"]]


def test_rollback_rejects_a_mapping_generated_for_another_target():
    """External security audit (2026-08-16) A-02 follow-up: rollback re-runs the chain with
    generate(rebuild=True), which re-stamps the CURRENT target — so the plan/apply target
    guard could never fire inside the chain, and there is no human between its plan and its
    apply. The guard therefore runs on the PRE-restore mapping, before the RESTORE: a mapping
    stamped for another environment must refuse with nothing restored and nothing pushed."""
    spark, client = prepared_for_rollback()  # mapping stamped ws-guid / lh-guid
    config_before = [dict(r) for r in spark._store[CONFIG_TABLE]]
    other = FakeFabricClient([], workspace_id="other-ws-guid", item_id="other-lh-guid")
    with pytest.raises(SystemExit) as excinfo:
        make_dep(spark, other, "rollback").rollback("", "promote to prod")
    assert "TARGET MISMATCH" in str(excinfo.value)
    assert "mode=generate" in str(excinfo.value)
    assert other.put_calls == []  # nothing deployed to the wrong environment
    assert spark._store[CONFIG_TABLE] == config_before  # nothing restored
    # Rejected before the sentinel/restore: no sensitive audit write is permitted either.
    assert not [r for r in spark._store[LOG_TABLE] if r.get("action") == "rollback"]
    assert not [r for r in spark._store[LOG_TABLE] if r.get("status") == "rejected"]


def test_rollback_without_a_mapping_table_skips_the_target_check():
    # No mapping table at all (dropped, or a rollback attempted before any generate on a
    # rebuilt lakehouse) is no cross-target EVIDENCE — the guard steps aside and rollback
    # proceeds to its own version checks; the chain's generate stamps the live target.
    spark, client = ready(seed_members=False)
    spark._history_rows = [{"version": 2}, {"version": 3}]
    del spark._store[MAPPING_TABLE]
    with pytest.raises(SystemExit) as excinfo:
        make_dep(spark, client, "rollback").rollback("99", "bad version")
    assert "not found" in str(excinfo.value)  # got PAST the target guard to the version check


def test_rollback_requires_reason():
    spark, client = ready(seed_members=False)
    with pytest.raises(SystemExit) as excinfo:
        make_dep(spark, client, "rollback").rollback("", "")
    assert "requires a reason" in str(excinfo.value)


def test_rollback_no_previous_version():
    spark, client = ready(seed_members=False)  # default history has a single version (3)
    with pytest.raises(SystemExit) as excinfo:
        make_dep(spark, client, "rollback").rollback("", "no prior")
    assert "no previous config version" in str(excinfo.value)


def test_rollback_explicit_version_absent():
    spark, client = ready(seed_members=False)
    spark._history_rows = [{"version": 2}, {"version": 3}]
    with pytest.raises(SystemExit) as excinfo:
        make_dep(spark, client, "rollback").rollback("99", "bad version")
    assert "not found" in str(excinfo.value)


def test_rollback_preflight_classifies_inactive_missing_and_empty_historical_config():
    spark, client = ready(seed_members=False)
    dep = make_dep(spark, client, "rollback")
    active = dict(sample_config_rows()[0])
    inactive = dict(active, active=False)
    spark._version_store = {2: [inactive, active]}
    assert dep._config_rows_at_version(2) == [
        {column: active[column] for column in rt.CONFIG_AUTHOR_COLUMNS}
    ]

    spark._version_store = {2: [{"active": True}]}
    with pytest.raises(rt.UsageError, match="missing required columns"):
        dep._config_rows_at_version(2)
    with pytest.raises(SystemExit, match="0 active rows"):
        dep._historical_rollback_payload([], 2)


def test_rollback_artifact_probe_refuses_nonempty_readback_or_failed_cleanup():
    spark, client = ready(seed_members=False)
    dep = make_dep(spark, client, "rollback")
    lease = dep._begin_sensitive("rollback")
    real_open = open

    def nonempty_open(_path, mode="r", *args, **kwargs):
        if str(_path) == rt.ControlBoundary.SENTINEL_FULL_PATH:
            return real_open(_path, mode, *args, **kwargs)
        return io.StringIO("" if "x" in mode else "unexpected")

    with (
        mock.patch("os.makedirs"),
        mock.patch("os.remove"),
        mock.patch("builtins.open", nonempty_open),
        pytest.raises(OSError, match="read-back was not empty"),
    ):
        dep._probe_rollback_artifacts(lease)

    def empty_open(_path, mode="r", *args, **kwargs):
        if str(_path) == rt.ControlBoundary.SENTINEL_FULL_PATH:
            return real_open(_path, mode, *args, **kwargs)
        return io.StringIO("")

    with (
        mock.patch("os.makedirs"),
        mock.patch("os.remove", side_effect=PermissionError("cleanup denied")),
        mock.patch("builtins.open", empty_open),
        pytest.raises(OSError, match="probe cleanup failed"),
    ):
        dep._probe_rollback_artifacts(lease)


def test_rollback_artifact_probe_requires_an_active_prewrite_lease():
    """Rollback preflight must not create its probe before the sentinel-bound DAR check."""
    spark, client = ready(seed_members=False)
    dep = make_dep(spark, client, "rollback")

    with pytest.raises(rt.ControlDataGuardError, match="active control-data sentinel lease"):
        dep._probe_rollback_artifacts()


def test_rollback_artifact_probe_refuses_a_nonactive_lease():
    """A caller cannot supply a stale or foreign lease to authorize rollback artifacts."""
    spark, client = ready(seed_members=False)
    dep = make_dep(spark, client, "rollback")
    owner = dep._begin_sensitive("rollback")
    foreign = rt.ControlBoundaryLease(
        dep._boundary(), "rollback", owner.snapshot, owns_sentinel=False
    )

    with pytest.raises(
        rt.ControlDataGuardError, match="requires the active control-data sentinel lease"
    ):
        dep._probe_rollback_artifacts(foreign)


def test_rollback_artifact_probe_prewrite_blocks_creation_after_dar_changes(tmp_path, monkeypatch):
    """The sentinel-bound recheck must precede the artifact probe's first local write."""
    sentinel = tmp_path / "sentinel"
    monkeypatch.setattr(rt.ControlBoundary, "SENTINEL_FULL_PATH", str(sentinel))
    spark, client = ready(seed_members=False)
    dep = make_dep(spark, client, "rollback")
    lease = dep._begin_sensitive("rollback")
    client.simulate_external_edit()

    with mock.patch("os.makedirs") as makedirs:
        with pytest.raises(rt.ControlDataGuardError, match="changed after the approved snapshot"):
            dep._probe_rollback_artifacts(lease)

    makedirs.assert_not_called()
    assert sentinel.read_text(encoding="utf-8") == rt.ControlBoundary.SENTINEL_CONTENT


def test_rollback_schema_preflight_requires_both_tables_and_their_columns():
    spark, client = ready(seed_members=False)
    dep = make_dep(spark, client, "rollback")
    del spark._store[MAPPING_TABLE]
    with pytest.raises(rt.UsageError, match=MAPPING_TABLE):
        dep._require_rollback_schemas()

    spark._store[MAPPING_TABLE] = []
    spark._columns[MAPPING_TABLE.lower()] = []
    with pytest.raises(rt.UsageError, match="missing required columns"):
        dep._require_rollback_schemas()


def test_rollback_progress_metadata_is_best_effort_for_attribute_refusing_exceptions():
    class AttributeRefusingError(Exception):
        def __setattr__(self, name, value):
            raise AttributeError(name)

    spark, client = ready(seed_members=False)
    dep = make_dep(spark, client, "rollback")
    err = AttributeRefusingError("restore outcome unknown")
    assert dep._mark_rollback_progress(err, None, "restore", 3, 2, "hash") is err


def test_rollback_refuses_a_config_table_without_any_delta_history():
    spark, client = ready(seed_members=False)
    spark._history_rows = []
    with pytest.raises(SystemExit, match="no Delta history"):
        make_dep(spark, client, "rollback").rollback("", "reviewed")


def test_rollback_treats_an_unreadable_pre_restore_mapping_as_no_target_evidence():
    spark, client = prepared_for_rollback()
    real_table = spark.table
    first_mapping_read = True

    def table_once_unavailable(name):
        nonlocal first_mapping_read
        if name == MAPPING_TABLE and first_mapping_read:
            first_mapping_read = False
            raise RuntimeError("mapping temporarily unavailable")
        return real_table(name)

    spark.table = table_once_unavailable
    result = rollback(make_dep(spark, client, "rollback"), "2", "mapping read recovery")
    assert result["changed"] is True


def test_rollback_restore_exception_is_unknown_and_leaves_the_prepared_record():
    spark, client = prepared_for_rollback()
    real_sql = spark.sql

    def restore_fails(query):
        if str(query).strip().upper().startswith("RESTORE TABLE"):
            raise RuntimeError("restore connection lost")
        return real_sql(query)

    spark.sql = restore_fails
    with pytest.raises(RuntimeError, match="restore connection lost") as excinfo:
        rollback(make_dep(spark, client, "rollback"), "2", "restore failure")
    assert excinfo.value.changed is None
    assert excinfo.value.rollback_progress["phase"] == "restore-outcome-unknown"
    assert [
        row
        for row in spark._store[LOG_TABLE]
        if row.get("action") == "rollback" and row.get("status") == "prepared"
    ]


# ---------------------------------------------------------------------------------------------
# ValidateDryRun — mode=validate runs the IDENTICAL validation pipeline generate uses (via the
# shared _run_validation), but NEVER writes: no mapping, no CSV, and (unlike generate's _reject) no
# forensic 'rejected' log row.
# ---------------------------------------------------------------------------------------------


def test_validate_blocks_on_validation_error_and_writes_nothing():
    bad = dict(sample_config_rows()[0], role_name="1illegal")  # invalid role_name
    spark, client = ready(rows=[bad])
    log_before = list(spark._store[LOG_TABLE])
    with pytest.raises(SystemExit) as excinfo:
        run_validate(make_dep(spark, client, "validate"))
    assert "validate blocked" in str(excinfo.value)
    # ZERO writes: the mapping stays empty and NOT ONE log row was added (no 'rejected' row).
    assert spark._store[MAPPING_TABLE] == []
    assert spark._store[LOG_TABLE] == log_before
    assert not [r for r in spark._store[LOG_TABLE] if r.get("status") == "rejected"]


def test_validate_clean_config_succeeds_with_warning_and_no_writes():
    spark, client = ready(rows=[carve_config_row()])
    log_before = list(spark._store[LOG_TABLE])
    res = run_validate(make_dep(spark, client, "validate"))
    assert not res["changed"]
    assert res["data"]["grants"] == 1
    assert res["data"]["roles"] == 1
    assert len(res["data"]["warnings"]) == 1  # the carve warning rides in data
    assert "subtree" in res["data"]["warnings"][0]
    # ZERO writes on the clean path too: no mapping, no log row.
    assert spark._store[MAPPING_TABLE] == []
    assert spark._store[LOG_TABLE] == log_before


def test_validate_and_generate_surface_identical_error_set():
    # same bad config through both entrypoints -> the IDENTICAL collect-all error set. Members are
    # left unseeded so a missing-member error joins the role-name error (a meaningful multi-error
    # set).
    bad = dict(sample_config_rows()[0], role_name="1illegal")
    gen_spark, gen_client = ready(rows=[bad], seed_members=False)
    val_spark, val_client = ready(rows=[bad], seed_members=False)
    with pytest.raises(SystemExit) as gen_exc:
        run_generate(make_dep(gen_spark, gen_client, "generate"))
    with pytest.raises(SystemExit) as val_exc:
        run_validate(make_dep(val_spark, val_client, "validate"))

    def errset(msg):
        return set(str(msg).split("error(s): ", 1)[1].split(" | "))

    assert errset(gen_exc.value) == errset(val_exc.value)
    assert len(errset(val_exc.value)) > 1  # more than one error -> a real set


# ---------------------------------------------------------------------------------------------
# Guard A production-path wiring — the No-Graph member gate must reach a declared name on
# the REAL Deployment.validate() -> _run_validation -> Member.resolve_ids(grants, cache,
# self.short_rows) path, not just at the Member.resolve_ids unit level.
#
# The P3 shape (an include value cancelled by its own exclude) is used deliberately: it is the
# case that ONLY this gate can catch, since the name never becomes effective and so never reaches
# any other check. The config needs a second member type (include_group_names=sg-analysts)
# supplying survivors, so C1's empty-after-exclusion error does not fire instead of the gate.
# ---------------------------------------------------------------------------------------------


def test_guard_a_wired_to_production_validate_path_p3_shape():
    # THREE rows, and the arrangement is the point -- a single-row config cannot distinguish
    # `self.short_rows` from `self.short_rows[:1]` or from an UNFILTERED `.collect()`, so a
    # one-row version of this test passes under both of those wirings (measured: full suite
    # exit 0, 100% coverage, zero failures under each).
    #   row 1  active, clean            -> a `[:1]` wiring stops here and never sees the defect
    #   row 2  active, P3 shape         -> only Guard A's declared-name pass can catch it
    #   row 3  INACTIVE, unknown member -> must NOT be gated; the active filter lives in
    #                                      Deployment.short_rows' SQL, so a wiring that drops
    #                                      `.where("active = true")` names this member instead.
    base = sample_config_rows()[0]
    spark, client = ready(
        rows=[
            dict(
                base,
                role_name="First",
                include_tables="sales.orders",
                exclude_tables=None,
                rls_condition=None,
                include_group_names="sg-analysts",
                include_user_names=None,
                exclude_user_names=None,
            ),
            dict(
                base,
                role_name="R",
                include_tables="sales.leads",
                exclude_tables=None,
                rls_condition=None,
                include_group_names="sg-analysts",  # supplies survivors -> C1 does not fire
                include_user_names="ghost@x.com",
                exclude_user_names="ghost@x.com",  # cancels its own include -> P3
            ),
            dict(
                base,
                role_name="Sleeping",
                active=False,
                include_tables="sales.returns",
                exclude_tables=None,
                rls_condition=None,
                include_group_names="sg-never-registered",
                include_user_names=None,
                exclude_user_names=None,
            ),
        ],
        seed_members=False,
    )
    spark._store[MEMBER_TABLE] = [
        member_cache_row("Group", "sg-analysts", "a0000000-0000-0000-0000-000000000002")
        # ghost@x.com is deliberately NOT seeded -- it must never become effective, so only
        # Guard A's declared-name pass can catch it.
    ]
    with pytest.raises(SystemExit) as excinfo:
        run_validate(make_dep(spark, client, "validate"))
    msg = str(excinfo.value)
    assert "ghost@x.com" in msg
    assert "not found in onelake_security_member" in msg  # the gate's own literal string
    # The inactive row's member is unknown too. If it is named here, the active filter was lost.
    assert "sg-never-registered" not in msg, msg
