"""End-to-end orchestration over the mock-Fabric fakes: every mode's control flow, the write-path
guards, show's audit enrichment, and setup's additive schema migration.

Ported from `olaf_test_integration.ipynb` classes `MockFabricPipeline`, `MockFabricGuards`,
`MockShowEnrichment` and `MockSetupMigration` (scope "mock").
"""

import json
import posixpath
import re
from unittest import mock

import pytest

from _olaf_runtime import (
    Audit,
    DARConflictError,
    Hash,
    Log,
    MAPPING_COLUMNS,
    UsageError,
    __version__,
)
from _fakes import (
    CONFIG_TABLE,
    GRP_READERS,
    GRP_READERS_NAME,
    LOG_TABLE,
    MAPPING_TABLE,
    MEMBER_TABLE,
    MISSING_NAME,
    SAMPLE_COLUMNS,
    SAMPLE_SCHEMAS_TABLES,
    SVC_LOADER,
    SVC_LOADER_NAME,
    TENANT,
    FakeDataFrame,
    FakeFabricClient,
    FakeSpark,
    FakeWriter,
    _FakePandas,
    build_spark,
    drop_live_column,
    fake_role,
    make_dep,
    run_apply,
    run_generate,
    run_runtime_blackbox,
    sample_config_rows,
    seed_sample_members,
    seed_validate_row,
)

CONTROL_TABLES = [CONFIG_TABLE, MAPPING_TABLE, LOG_TABLE, MEMBER_TABLE]


def ready(rows=None):
    """A set-up workspace with the short config authored and the member cache seeded — the
    No-Graph gate resolves member names from that cache only. `rows` overrides the config."""
    spark, client = build_spark(), FakeFabricClient([])
    make_dep(spark, client, "setup").setup()
    spark._store[CONFIG_TABLE] = sample_config_rows() if rows is None else rows
    seed_sample_members(spark)  # sample members present; block-path rows use unseeded names
    return spark, client


@pytest.fixture
def fresh():
    return ready()


@pytest.fixture
def bare():
    """A blank spark + client, before setup() has run."""
    return build_spark(), FakeFabricClient([])


def setup_run():
    """A set-up workspace with config + members seeded, with setup() already logged."""
    spark, client = build_spark(), FakeFabricClient([])
    make_dep(spark, client, "setup").setup()
    spark._store[CONFIG_TABLE] = sample_config_rows()
    seed_sample_members(spark)
    return spark, client


# ---------------------------------------------------------------------------------------------
# MockFabricPipeline — every mode's control flow + notebook.exit result contract
# ---------------------------------------------------------------------------------------------


def test_setup_creates_four_tables_idempotently(bare):
    spark, client = bare
    first = make_dep(spark, client, "setup").setup()["data"]
    assert sorted(first["created"]) == sorted(CONTROL_TABLES)
    assert first["migrated"] == {}
    assert first["unchanged"] == []
    second = make_dep(spark, client, "setup").setup()["data"]
    assert second["created"] == []
    assert second["migrated"] == {}
    assert sorted(second["unchanged"]) == sorted(CONTROL_TABLES)


def test_generate_writes_mapping_and_per_role_summary(fresh):
    spark, client = fresh
    res = run_generate(make_dep(spark, client, "generate"))
    assert res["changed"]
    d = res["data"]
    assert set(d) == {
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
    assert d["grants"] == 3  # orders + leads (returns excluded) + region_a
    assert len(spark._store[MAPPING_TABLE]) == 3
    assert d["summary"]["SalesReaders"] == {"included": 2, "excluded": 1, "warnings": 0}
    assert d["summary"]["RawReaders"] == {"included": 1, "excluded": 1, "warnings": 0}


def test_generate_versioned_csv_name_and_logged_path(fresh):
    # generate exports a versioned, per-generation CSV directly under mapping_history_dir
    # (timestamp + v<mapping_version> + mapping_hash), and the generate 'complete' log row
    # records that path (csv=<path>).
    spark, client = fresh
    res = run_generate(make_dep(spark, client, "generate"))
    csv = res["data"]["csv"]
    assert re.match(
        r"^Files/security/mapping-history/onelake_security_mapping_"
        r"\d{8}-\d{6}_v3_[0-9a-f]{16}\.csv$",
        csv,
    ), csv
    completes = [
        r for r in spark._store[LOG_TABLE] if r["mode"] == "generate" and r["action"] == "complete"
    ]
    assert len(completes) == 1
    assert f"csv={csv}" in completes[0]["message"]  # path logged for lookup


def test_generate_dedupes_on_mapping_hash_reusing_existing_export(fresh):
    # Re-generating the SAME config content (same mapping content -> same mapping_hash) reuses
    # the already-exported file instead of writing a new timestamped copy — EVEN when the
    # config table's Delta version has advanced. The reused filename keeps the FIRST export's
    # v{N}; the log records THIS run's config_version.
    spark, client = fresh
    res1 = run_generate(make_dep(spark, client, "generate", run="R1"))  # fresh export
    existing_name = posixpath.basename(res1["data"]["csv"])
    assert "_v3_" in existing_name  # first export baked mapping_version 3 into the name
    writes = []

    def _spy_to_csv(self, *a, **k):
        writes.append(a)
        return None

    # Advance the config table's Delta version WITHOUT changing content: mapping_hash is
    # unchanged (dedupe still fires, since the mapping content is identical) while
    # mapping_version now differs (9 vs 3).
    spark._history_version = 9
    # 2nd generate sees the 1st run's file already present in mapping_history_dir (same
    # mapping_hash).
    with mock.patch.object(_FakePandas, "to_csv", _spy_to_csv):
        # rebuild=True: config is unchanged, so a rebuild=False generate would idempotently SKIP
        # (returning changed=False) before reaching the CSV-dedupe path this test exercises.
        res2 = run_generate(
            make_dep(spark, client, "generate", run="R2"), history=[existing_name], rebuild=True
        )
    assert res2["data"]["csv"] == res1["data"]["csv"]  # reused the OLD file — name still embeds v3
    assert writes == []  # nothing re-written on the deduped re-generate
    # The reused filename keeps the first export's version, but the 2nd run's 'complete' log row
    # records THIS run's (newer) config_version — the intended, now-verified behavior.
    complete2 = next(
        r
        for r in spark._store[LOG_TABLE]
        if r["mode"] == "generate" and r["action"] == "complete" and r["run_id"] == "R2"
    )
    assert complete2["config_version"] == 9  # BIGINT, not its string spelling


def test_generate_version_tag_falls_back_to_vna_when_version_unknown():
    # config_version resolves to None (DESCRIBE HISTORY unavailable) -> filename uses vNA (not vNone).
    spark = FakeSpark(SAMPLE_SCHEMAS_TABLES, SAMPLE_COLUMNS, history_version=None)
    client = FakeFabricClient([])
    make_dep(spark, client, "setup").setup()
    spark._store[CONFIG_TABLE] = sample_config_rows()
    seed_sample_members(spark)
    res = run_generate(make_dep(spark, client, "generate"))
    assert "_vNA_" in res["data"]["csv"]


def test_full_lifecycle_setup_generate_plan_apply(fresh):
    spark, client = fresh
    run_generate(make_dep(spark, client, "generate"))

    plan_res = make_dep(spark, client, "plan").plan()
    assert plan_res["changed"]
    assert set(plan_res["data"]) == {
        "counts",
        "plan",
        "drift",
        "dar_snapshot_safe",
        "workspace_isolation",
        "dar_etag",
    }
    assert plan_res["data"]["plan"] == {"SalesReaders": "create", "RawReaders": "create"}
    assert plan_res["data"]["drift"] == {"SalesReaders": "create", "RawReaders": "create"}

    apply_res = run_apply(make_dep(spark, client, "apply"))  # default REPLACE
    assert apply_res["changed"]
    a = apply_res["data"]
    assert set(a) == {
        "push_status",
        "roles_written",
        "keep_unmanaged",
        "counts",
        "request",
        "backup_path",
        "omitted_role_candidates",
        "drift_omission_candidates",
        "post_state_review_required",
        "if_match",
        "dar_snapshot_safe",
        "workspace_isolation",
        "dar_etag",
    }
    assert a["if_match"] == "conditional"  # the fake serves ETags, so the PUT was conditional
    assert a["push_status"] == 200  # the bulk PUT's HTTP status
    assert a["roles_written"] == 2  # ...and, separately, a REAL role count
    assert not a["keep_unmanaged"]
    assert a["request"] == "config_payload"
    assert a["omitted_role_candidates"] == []
    assert a["drift_omission_candidates"] == []
    assert a["post_state_review_required"] is True

    # re-plan against the now-populated live state -> no drift, changed=False, no complete row
    replan = make_dep(spark, client, "plan").plan()
    assert not replan["changed"]
    assert set(replan["data"]) == {
        "counts",
        "dar_snapshot_safe",
        "workspace_isolation",
        "dar_etag",
    }


def test_show_result_contract_and_framework_provenance(fresh):
    spark, client = fresh
    run_generate(make_dep(spark, client, "generate"))
    make_dep(spark, client, "plan").plan()
    run_apply(make_dep(spark, client, "apply"))
    res = make_dep(spark, client, "show").show("role", "Sales*")
    assert not res["changed"]
    d = res["data"]
    assert set(d) == {"subject", "matches", "roles", "grants", "out_of_band", "by"}
    assert d["by"] == "role"
    assert d["subject"] == "Sales*"
    assert d["roles"] == ["SalesReaders"]
    assert d["grants"]
    assert all(g["provenance"] == "framework" for g in d["grants"])  # applied by the framework
    assert d["out_of_band"] == 0


# ---------------------------------------------------------------------------------------------
# MockFabricGuards — write-path guards, all reachable through the fakes
# ---------------------------------------------------------------------------------------------


def test_saved_plan_gate_blocks_apply_without_plan(fresh):
    spark, client = fresh
    run_generate(make_dep(spark, client, "generate"))
    with pytest.raises(SystemExit) as excinfo:
        run_apply(make_dep(spark, client, "apply"))
    assert "no successful plan" in str(excinfo.value)


def test_drift_gate_blocks_apply_when_live_moved(fresh):
    spark, client = fresh
    run_generate(make_dep(spark, client, "generate"))
    make_dep(spark, client, "plan").plan()
    client._roles = [fake_role("GhostRole", ["/Tables/x/y"], [GRP_READERS])]  # live drifted
    with pytest.raises(SystemExit) as excinfo:
        run_apply(make_dep(spark, client, "apply"))
    assert "drift" in str(excinfo.value)


def test_guid_identity_guard_blocks_non_guid_member(fresh):
    # The apply gate is a safety net: generate always resolves to GUIDs, so to reach the guard we
    # hand-corrupt the lock-file's id column to a non-GUID (a tampered mapping).
    spark, client = fresh
    run_generate(make_dep(spark, client, "generate"))
    for r in spark._store[MAPPING_TABLE]:
        if r.get("member_group_ids"):
            r["member_group_ids"] = "grp-by-name"
    make_dep(spark, client, "plan").plan()
    with pytest.raises(SystemExit) as excinfo:
        run_apply(make_dep(spark, client, "apply"))
    assert "GUID" in str(excinfo.value)


def test_plan_gate_blocks_apply_when_mapping_regenerated_after_plan(fresh):
    """External security audit (2026-08-16) A-01. config_hash fingerprints the config ROWS only,
    so remapping a member objectId in onelake_security_member and regenerating yields a NEW
    mapping (new member ids, new mapping_hash) under the SAME config_hash. The saved plan was
    reviewed against the OLD ids — apply must reject rather than deploy an identity nobody
    reviewed."""
    spark, client = fresh
    run_generate(make_dep(spark, client, "generate"))
    make_dep(spark, client, "plan").plan()  # the reviewed plan — against the ORIGINAL member id
    for row in spark._store[MEMBER_TABLE]:  # remap the group name to a DIFFERENT principal
        if row["member_name"] == GRP_READERS_NAME:
            row["member_id"] = "99999999-9999-9999-9999-999999999999"
    # member-id drift defeats the idempotency skip: same config, a genuinely new mapping
    assert run_generate(make_dep(spark, client, "generate"))["changed"]
    with pytest.raises(SystemExit) as excinfo:
        run_apply(make_dep(spark, client, "apply"))
    assert "no successful plan" in str(excinfo.value)
    assert "mode=plan" in str(excinfo.value)
    assert client.put_calls == []  # the unreviewed objectId never reached the DAR API
    rejected = [r for r in spark._store[LOG_TABLE] if r.get("status") == "rejected"]
    assert rejected, "the denial must leave a forensic 'rejected' trace"
    # a fresh plan against the regenerated mapping re-opens the gate
    make_dep(spark, client, "plan").plan()
    assert run_apply(make_dep(spark, client, "apply"))["changed"]


def test_plan_rejects_a_mapping_generated_for_another_target(fresh):
    """External security audit (2026-08-16) A-02. The mapping is a lock-file for ONE
    workspace/lakehouse — control tables copied to another lakehouse carry the same
    (content-based) config_hash, so only the stamped workspace_id/lakehouse_id can tell the
    environments apart. plan/apply compare them to the ATTACHED target and refuse a mismatch."""
    spark, client = fresh
    run_generate(make_dep(spark, client, "generate"))  # stamps ws-guid / lh-guid
    other = FakeFabricClient([], workspace_id="other-ws-guid", item_id="other-lh-guid")
    with pytest.raises(SystemExit) as excinfo:
        make_dep(spark, other, "plan").plan()
    assert "TARGET MISMATCH" in str(excinfo.value)
    assert "mode=generate" in str(excinfo.value)  # the remedy is a fresh generate, not a re-plan
    rejected = [r for r in spark._store[LOG_TABLE] if r.get("status") == "rejected"]
    assert rejected and "TARGET MISMATCH" in rejected[-1]["message"]  # forensic trace


def test_apply_rejects_a_mapping_generated_for_another_target(fresh):
    # even WITH a valid plan record on file, apply against the wrong target must refuse
    # before anything reaches the client.
    spark, client = fresh
    run_generate(make_dep(spark, client, "generate"))
    make_dep(spark, client, "plan").plan()
    other = FakeFabricClient([], workspace_id="other-ws-guid", item_id="other-lh-guid")
    with pytest.raises(SystemExit) as excinfo:
        run_apply(make_dep(spark, other, "apply"))
    assert "TARGET MISMATCH" in str(excinfo.value)
    assert other.put_calls == []  # nothing was deployed to the wrong environment


def test_a_lakehouse_mismatch_alone_is_still_a_target_mismatch(fresh):
    # same workspace, different lakehouse — the finer-grained of the two mismatches refuses too
    spark, client = fresh
    run_generate(make_dep(spark, client, "generate"))
    other = FakeFabricClient([], workspace_id="ws-guid", item_id="other-lh-guid")
    with pytest.raises(SystemExit) as excinfo:
        make_dep(spark, other, "plan").plan()
    assert "TARGET MISMATCH" in str(excinfo.value)


def test_a_mapping_with_no_stamped_target_is_refused(fresh):
    # a mapping whose provenance columns carry no target ids (a hand-nulled column, a table
    # written outside the framework) cannot prove which environment it was generated for —
    # fail closed to a fresh generate rather than assume it matches.
    spark, client = fresh
    run_generate(make_dep(spark, client, "generate"))
    for r in spark._store[MAPPING_TABLE]:
        r["workspace_id"] = None
        r["lakehouse_id"] = None
    with pytest.raises(SystemExit) as excinfo:
        make_dep(spark, client, "plan").plan()
    assert "TARGET MISMATCH" in str(excinfo.value)
    assert "mode=generate" in str(excinfo.value)


def test_a_mapping_with_one_blank_target_id_is_refused(fresh):
    # the one-sided variant: workspace_id intact, lakehouse_id blanked — half an identity
    # proves nothing, so it refuses exactly like the fully unstamped case.
    spark, client = fresh
    run_generate(make_dep(spark, client, "generate"))
    for r in spark._store[MAPPING_TABLE]:
        r["lakehouse_id"] = ""
    with pytest.raises(SystemExit) as excinfo:
        make_dep(spark, client, "plan").plan()
    assert "TARGET MISMATCH" in str(excinfo.value)


def test_a_poisoned_tail_row_cannot_hide_behind_a_clean_lead_row(fresh):
    # the guard checks EVERY row's stamped target, not just rows[0] — a mapping whose first
    # row matches the attached target while a later row names another environment must refuse
    # regardless of read order.
    spark, client = fresh
    run_generate(make_dep(spark, client, "generate"))
    rows = spark._store[MAPPING_TABLE]
    assert len(rows) > 1  # the scenario needs a tail to poison
    for r in rows[1:]:
        r["workspace_id"] = "other-ws-guid"
        r["lakehouse_id"] = "other-lh-guid"
    with pytest.raises(SystemExit) as excinfo:
        make_dep(spark, client, "plan").plan()
    assert "TARGET MISMATCH" in str(excinfo.value)


def test_plan_refuses_mixed_mapping_generation_provenance_before_live_mutation(fresh):
    """A tail row with a different generation stamp must not inherit trust from row zero."""
    spark, client = fresh
    run_generate(make_dep(spark, client, "generate"))
    spark._store[MAPPING_TABLE][1]["generated_at"] = "poisoned-generation"
    with pytest.raises(UsageError, match="mapping provenance"):
        make_dep(spark, client, "plan").plan()
    assert client.put_calls == []


def test_plan_refuses_mixed_mapping_tenant_provenance_before_live_mutation(fresh):
    """A mapping lock-file may name only one tenant for all payload members."""
    spark, client = fresh
    run_generate(make_dep(spark, client, "generate"))
    spark._store[MAPPING_TABLE][1]["tenant_id"] = "other-tenant"
    with pytest.raises(SystemExit, match="TENANT MISMATCH"):
        make_dep(spark, client, "plan").plan()
    assert client.put_calls == []


def test_target_ids_compare_case_insensitively(fresh):
    # GUIDs are case-insensitive hex: a mapping stamped with an uppercased spelling of the
    # SAME target must not read as a foreign environment.
    spark, client = fresh
    run_generate(make_dep(spark, client, "generate"))
    for r in spark._store[MAPPING_TABLE]:
        r["workspace_id"] = str(r["workspace_id"]).upper()
        r["lakehouse_id"] = str(r["lakehouse_id"]).upper()
    res = make_dep(spark, client, "plan").plan()
    assert res["changed"]  # planned normally — no TARGET MISMATCH


def test_generate_skip_is_defeated_by_target_drift(fresh):
    """The TARGET MISMATCH remedy must actually work: 're-run mode=generate' on an UNCHANGED
    config used to take the idempotency skip (keyed on config_hash + member ids only), which
    never re-stamps the target — leaving the operator refused forever unless they discovered
    rebuild=True. A stamped-target change now defeats the skip exactly like member-id drift."""
    spark, client = fresh
    run_generate(make_dep(spark, client, "generate"))  # stamps ws-guid / lh-guid
    other = FakeFabricClient([], workspace_id="other-ws-guid", item_id="other-lh-guid")
    res = run_generate(make_dep(spark, other, "generate"))  # NO rebuild — must not skip
    assert res["changed"], "target drift must defeat the idempotency skip"
    rows = spark._store[MAPPING_TABLE]
    assert all(r["workspace_id"] == "other-ws-guid" for r in rows)  # re-stamped...
    assert all(r["lakehouse_id"] == "other-lh-guid" for r in rows)
    assert make_dep(spark, other, "plan").plan()["changed"]  # ...and the new target plans


def test_plan_survives_a_reordered_config_table(fresh):
    # External security audit (2026-08-16), issue #13: the same logical config rows in a
    # different collect order (a compaction, a table rewrite) must NOT read as STALE — the
    # hash is content-keyed, not order-keyed.
    spark, client = fresh
    run_generate(make_dep(spark, client, "generate"))
    spark._store[CONFIG_TABLE] = list(reversed(spark._store[CONFIG_TABLE]))
    res = make_dep(spark, client, "plan").plan()
    assert res["changed"]  # planned normally — no STALE rejection


def test_apply_threads_the_drift_gate_reads_etag_to_the_real_put(fresh):
    """External security audit (2026-08-16), issue #10: the If-Match token the real PUT
    carries must come from the SAME list_roles read that feeds the drift gate — the last
    read before the write — and the zero-write dryRun must stay unconditional."""
    spark, client = fresh
    run_generate(make_dep(spark, client, "generate"))
    make_dep(spark, client, "plan").plan()
    approved_etag = client.roles_etag
    run_apply(make_dep(spark, client, "apply"))
    dry = [c for c in client.put_calls if c["dry_run"]]
    real = [c for c in client.put_calls if not c["dry_run"]]
    assert dry and all(c["etag"] is None for c in dry)  # dryRun: never conditional
    assert len(real) == 1
    # The write rotates the service ETag and the mandatory post-read captures that newer token;
    # the PUT itself must still carry the immutable pre-write snapshot token.
    assert real[0]["etag"] == approved_etag is not None
    assert client.roles_etag != approved_etag


def test_a_concurrent_edit_between_read_and_put_is_a_blocked_conflict():
    """A 412 has ZERO blast radius — the service refused the write, nothing landed — so it
    must NOT produce the mid-push forensic record (live re-read, per-role PRESENT/ABSENT,
    the restore-point pointer): that record documents an incident that did not happen and
    points at a restore that would clobber the very edit the 412 protected. Its changed DAR
    snapshot also blocks every following audit write: only the already-safe prepared intent
    remains, and the incident sentinel persists for reviewed clearance."""

    class _Racey(FakeFabricClient):
        # the concurrent actor's write lands in the window between apply's own live read
        # and its PUT — the exact seconds the drift gate cannot see
        def put_roles(self, roles, dry_run=False, etag=None, *, allow_unconditional=False):
            if not dry_run:
                self.simulate_external_edit()
            return super().put_roles(
                roles,
                dry_run=dry_run,
                etag=etag,
                allow_unconditional=allow_unconditional,
            )

    spark = build_spark()
    client = _Racey([], enforce_etag=True)
    run_runtime_blackbox("setup", spark)
    spark._store[CONFIG_TABLE] = sample_config_rows()
    seed_sample_members(spark)
    run_runtime_blackbox("generate", spark, client=client)
    run_runtime_blackbox("plan", spark, client=client)
    outcome = run_runtime_blackbox("apply", spark, client=client)
    res = outcome.envelope
    assert res["status"] == "blocked"  # a concurrency guard outcome, not an error+traceback
    assert "re-run mode=plan" in res["error"]
    real = [c for c in client.put_calls if not c["dry_run"]]
    assert len(real) == 1  # exactly one attempt — no retry on a precondition that cannot heal
    assert client._roles == []  # nothing landed
    log = spark._store[LOG_TABLE]
    apply_rows = [row for row in log if row.get("mode") == "apply"]
    assert [(row["action"], row["status"]) for row in apply_rows] == [("push", "prepared")]
    # The post-conflict audit write is correctly refused rather than becoming stale control data.
    # (Dedicated boundary tests assert the retained incident marker without this black-box test's
    # in-memory lakehouse-write seam.) The mid-push forensic record was NOT produced.
    assert not [r for r in log if "PRESENT" in str(r.get("message"))]
    assert not [r for r in log if "ABSENT" in str(r.get("message"))]
    assert not [r for r in log if r.get("action") == "push" and r.get("status") == "failed"]
    # No per-grant rows can claim a post-conflict result.
    stamped = [r for r in log if r.get("mode") == "apply" and r.get("action") == "validate"]
    assert not stamped


def test_a_412_after_a_retried_transient_is_ambiguous_not_a_clean_conflict():
    """External security audit (2026-08-18), R-01: a gateway 502 can arrive AFTER the
    origin committed the PUT; the retry re-sends the stale If-Match and draws a 412.
    Recorded as "nothing was written", that is a false negative in the audit trail of a
    security deployment — provenance would permanently deny a deploy that actually landed.
    The ambiguous conflict routes into the mid-push forensics instead (honest "NOT
    confirmed pushed" wording, live re-read, restore-point pointer) and surfaces as an
    error envelope demanding investigation, not a clean blocked one."""

    class _AmbiguousConflict(FakeFabricClient):
        # the client-layer verdict for retried-then-412 (unit-proven in
        # test_mock_fabric_seams); here the fake asserts what Deployment DOES with it
        def put_roles(self, roles, dry_run=False, etag=None, *, allow_unconditional=False):
            self.put_calls.append(
                {
                    "dry_run": dry_run,
                    "roles": len(roles),
                    "etag": etag,
                    "allow_unconditional": allow_unconditional,
                }
            )
            if not dry_run:
                raise DARConflictError(
                    "PUT .../dataAccessRoles -> 412 on a RETRIED attempt: an earlier "
                    "attempt of this same PUT was answered with a transient status "
                    "after possibly committing",
                    ambiguous=True,
                )
            return 200

    spark = build_spark()
    client = _AmbiguousConflict([])
    run_runtime_blackbox("setup", spark)
    spark._store[CONFIG_TABLE] = sample_config_rows()
    seed_sample_members(spark)
    run_runtime_blackbox("generate", spark, client=client)
    run_runtime_blackbox("plan", spark, client=client)
    outcome = run_runtime_blackbox("apply", spark, client=client)
    res = outcome.envelope
    assert res["status"] == "error"  # demands investigation — NOT a clean guard refusal
    assert "RETRIED attempt" in res["error"]
    log = spark._store[LOG_TABLE]
    # no row may claim certainty that nothing was written
    assert not [r for r in log if "nothing was written" in str(r.get("message"))]
    assert not [r for r in log if r.get("action") == "push" and r.get("status") == "rejected"]
    unknown_push = [r for r in log if r.get("action") == "push" and r.get("status") == "unknown"]
    assert len(unknown_push) == 1
    forensic = json.loads(unknown_push[0]["message"])
    assert "backup_path" in forensic  # the restore point is NAMED — write state unknown
    assert forensic["live_roles_after"] == []  # the re-read RAN (fake live state is empty)
    assert forensic["rolled_back"] is False
    stamped = [r for r in log if r.get("mode") == "apply" and r.get("action") == "validate"]
    assert stamped and all(r["status"] == "failed" for r in stamped)
    assert all("NOT confirmed pushed" in r["message"] for r in stamped)


def test_the_incremental_payload_is_bounded_by_the_platform_ceiling():
    # generate's ceiling bounds the CONFIG's roles; the incremental payload is live ∪
    # desired, so unmanaged live roles count too — refused before the push instead of dying
    # at the Fabric API mid-apply.
    live = [fake_role(f"Unmanaged{i:03d}", ["/Tables/x/y"], [GRP_READERS]) for i in range(249)]
    spark, client = build_spark(), FakeFabricClient(live)
    make_dep(spark, client, "setup").setup()
    spark._store[CONFIG_TABLE] = sample_config_rows()
    seed_sample_members(spark)
    run_generate(make_dep(spark, client, "generate"))
    make_dep(spark, client, "plan").plan()
    with pytest.raises(SystemExit) as excinfo:
        run_apply(make_dep(spark, client, "apply"), keep_unmanaged=True)
    assert "merged incremental payload exceed" in str(excinfo.value)
    assert "keep_unmanaged" in str(excinfo.value)
    assert client.put_calls == []  # refused before even the dryRun went out


def test_generate_rejects_guid_in_member_name_column():
    # A GUID authored in a *_names column is rejected at generate (config takes display names).
    spark, client = ready([dict(sample_config_rows()[0], include_group_names=GRP_READERS)])
    with pytest.raises(SystemExit) as excinfo:
        run_generate(make_dep(spark, client, "generate"))
    assert "generate blocked" in str(excinfo.value)
    assert "looks like an objectId" in str(excinfo.value)
    assert spark._store[MAPPING_TABLE] == []  # all-or-nothing — nothing written


def test_generate_rejects_member_not_found():
    # No-Graph gate: a member absent from onelake_security_member is a hard error naming it,
    # plus a forensic 'rejected' audit row — no Graph fallback.
    spark, client = ready([dict(sample_config_rows()[0], include_group_names=MISSING_NAME)])
    with pytest.raises(SystemExit) as excinfo:
        run_generate(make_dep(spark, client, "generate"))
    assert "generate blocked" in str(excinfo.value)
    assert MISSING_NAME in str(excinfo.value)  # the error NAMES the missing member
    assert "onelake_security_member" in str(excinfo.value)
    assert spark._store[MAPPING_TABLE] == []
    rejected = [r for r in spark._store[LOG_TABLE] if r.get("status") == "rejected"]
    assert rejected
    assert MISSING_NAME in rejected[-1]["message"]  # rejected row names it too


def test_generate_resolution_failure_is_all_or_nothing(fresh):
    # A good generate populates the mapping; a later generate with an unresolvable member must
    # block WITHOUT touching the already-written mapping (no partial/stale resolution).
    spark, client = fresh
    run_generate(make_dep(spark, client, "generate"))
    good = [dict(r) for r in spark._store[MAPPING_TABLE]]
    assert good
    spark._store[CONFIG_TABLE] = [dict(sample_config_rows()[0], include_group_names=MISSING_NAME)]
    with pytest.raises(SystemExit) as excinfo:
        run_generate(make_dep(spark, client, "generate"))
    assert "generate blocked" in str(excinfo.value)
    assert spark._store[MAPPING_TABLE] == good  # mapping untouched


def test_staleness_guard_blocks_and_logs_rejection(fresh):
    # R3-flagged gap: config edited after generate -> mapping.config_hash != live config_hash.
    spark, client = fresh
    run_generate(make_dep(spark, client, "generate"))
    for r in spark._store[MAPPING_TABLE]:
        r["config_hash"] = "0000000000000000"
    with pytest.raises(SystemExit) as excinfo:
        make_dep(spark, client, "plan").plan()
    assert "STALE" in str(excinfo.value)
    rejected = [r for r in spark._store[LOG_TABLE] if r.get("status") == "rejected"]
    assert rejected, "a blocked attempt must leave a forensic 'rejected' trace"
    assert "STALE" in rejected[-1]["message"]
    # provenance is stamped BEFORE the guards, so even a denial row carries the mapping chain
    assert rejected[-1]["mapping_hash"] == Hash.mapping_content(spark._store[MAPPING_TABLE])


def test_empty_config_refuses_generate():
    spark, client = ready(rows=[])
    with pytest.raises(SystemExit) as excinfo:
        run_generate(make_dep(spark, client, "generate"))
    assert "empty security config" in str(excinfo.value)


def test_the_mapping_write_reshapes_its_own_table():
    """The mapping is a DERIVED lock-file that generate rewrites in full, and its schema is the
    framework's, so the write carries `overwriteSchema`. Without it, an existing table whose schema
    disagrees — an older generation's column set, a hand-created table, a load with inferred types —
    makes Delta refuse the write (DELTA_FAILED_TO_MERGE_FIELDS) and blocks generate permanently.
    Reported from a live run.

    A wiring assertion, deliberately: the fake store has no schema, so it cannot reproduce Delta's
    enforcement — dropping the option would change nothing here and everything on a lakehouse."""
    spark, client = ready()
    run_generate(make_dep(spark, client, "generate"))
    mapping_writes = [w for w in spark._writes if w["table"] == MAPPING_TABLE]
    assert len(mapping_writes) == 1
    assert mapping_writes[0]["mode"] == "overwrite"
    assert mapping_writes[0]["options"].get("overwriteSchema") == "true"


def test_the_log_and_member_writes_never_reshape_their_tables():
    """The mirror, and the reason the option is not simply applied everywhere: the log is
    append-only history and the member cache is author-loaded. A write that reshaped either would
    drop columns nobody asked it to drop."""
    spark, client = ready()
    run_generate(make_dep(spark, client, "generate"))
    for write in spark._writes:
        if write["table"] == MAPPING_TABLE:
            continue
        assert "overwriteSchema" not in write["options"], write


def test_a_failed_mapping_write_names_the_table_and_the_remedy():
    """The raw Delta message names a column pair and nothing else — not the table, not what to do.
    The re-raise keeps the exception CLASS (run_mode's envelope leads with it) and adds both."""
    spark, client = ready()
    boom = RuntimeError("[DELTA_FAILED_TO_MERGE_FIELDS] Failed to merge fields 'generated_at'")

    def _explode(self, name):
        if name == MAPPING_TABLE:
            raise boom
        return None

    with mock.patch.object(FakeWriter, "saveAsTable", _explode):
        with pytest.raises(RuntimeError) as excinfo:
            run_generate(make_dep(spark, client, "generate"))
    message = str(excinfo.value)
    assert type(excinfo.value) is RuntimeError  # class preserved, so error_category is unchanged
    assert f"mapping write failed for {MAPPING_TABLE}" in message
    assert f"DROP TABLE {MAPPING_TABLE}" in message  # the remedy, named
    assert "3 row(s)" in message  # what it was trying to write
    assert "DELTA_FAILED_TO_MERGE_FIELDS" in message  # the original, never swallowed


def test_duplicate_row_skipped_in_generate():
    base = sample_config_rows()
    spark, client = ready([base[0], dict(base[0]), base[1]])  # SalesReaders authored twice
    res = run_generate(make_dep(spark, client, "generate"))
    assert res["data"]["grants"] == 3  # dedup -> same as the non-duplicated config
    assert res["data"]["warnings"] >= 1


# ---------------------------------------------------------------------------------------------
# MockShowEnrichment — live DAR state joined with onelake_security_log on the explicit key
# (role_name, scope_path, member) -> first/last_applied + first/last_granted_by / config_version,
# out-of-band flagging.
# ---------------------------------------------------------------------------------------------

ORDERS_PATH = "/Tables/sales/orders"


def log_spark(rows):
    return build_spark(store={LOG_TABLE: rows})


def test_enriches_grants_and_flags_out_of_band():
    logged, unlogged = GRP_READERS, "99999999-9999-9999-9999-999999999999"
    spark = log_spark(
        [
            seed_validate_row(
                "SalesReaders",
                ORDERS_PATH,
                logged,
                "2026-03-01T09:00:00+00:00",
                "alice@example.com",
                "5",
            )
        ]
    )
    client = FakeFabricClient([fake_role("SalesReaders", [ORDERS_PATH], [logged, unlogged])])
    d = make_dep(spark, client, "show").show("role", "SalesReaders")["data"]
    assert d["by"] == "role"  # result echoes the queried axis
    by_member = {g["member"]: g for g in d["grants"]}
    assert by_member[logged]["provenance"] == "framework"
    assert by_member[logged]["first_applied"] == "2026-03-01T09:00:00+00:00"
    assert by_member[logged]["first_granted_by"] == "alice@example.com"
    assert by_member[logged]["last_applied"] == "2026-03-01T09:00:00+00:00"
    assert by_member[logged]["last_granted_by"] == "alice@example.com"
    assert by_member[logged]["config_version"] == "5"
    assert by_member[logged]["permission"] == "Read"  # surfaced from the Action rule
    assert by_member[logged]["member_name"] == "sg-seeded"  # display name surfaced
    assert by_member[unlogged]["provenance"] == "out-of-band — no framework provenance"
    assert by_member[unlogged]["first_applied"] is None
    assert by_member[unlogged]["last_applied"] is None
    assert by_member[unlogged]["first_granted_by"] is None
    assert by_member[unlogged]["last_granted_by"] is None
    assert by_member[unlogged]["member_name"] is None  # unknown for an out-of-band grant
    assert d["out_of_band"] == 1


def test_a_rollback_deployed_grant_is_established_provenance_not_out_of_band():
    """External security audit (2026-08-16), issue #16: a rollback chain's apply stamps its
    per-grant success rows mode=rollback; the provenance consumers must count them exactly
    like apply's own — the plan-record loader was widened for rollback stamping long ago,
    and the three provenance consumers now match it."""
    spark, client = setup_run()
    spark._store[LOG_TABLE] = [
        seed_validate_row(
            "RestoredRole",
            "/Tables/sales/orders",
            GRP_READERS,
            run_at="2026-08-01T00:00:00",
            run_by="pipeline",
            config_version=7,
            mode="rollback",
        )
    ]
    audit = make_dep(spark, client, "show").audit
    prov = audit.grant_provenance()
    key = ("restoredrole", "/tables/sales/orders", GRP_READERS.lower())
    assert key in prov  # the rollback push established the grant
    # one row, so both ends name the same push -- what matters here is that a rollback-stamped
    # row establishes provenance at all, on either end.
    assert prov[key]["first_granted_by"] == "pipeline"
    assert prov[key]["last_granted_by"] == "pipeline"


def test_grant_provenance_pins_each_end_to_its_own_push_whatever_order_rows_arrive_in():
    """Log.grant_provenance is a SECOND implementation of the grain Audit.grants() also computes,
    and it does not get the pre-sorted rows grants() does -- it walks the log in store order. So
    the two ends have to be established by comparison, not by position, and each end has to keep
    the principal from ITS OWN row rather than from whichever row happened to arrive last."""
    spark, client = setup_run()
    spark._store[LOG_TABLE] = [
        # deliberately out of order: the LATEST push is seen first
        seed_validate_row(
            "R",
            "/Tables/sales/orders",
            GRP_READERS,
            run_at="2026-08-20T00:00:00",
            run_by="bob@example.com",
            config_version=9,
        ),
        seed_validate_row(
            "R",
            "/Tables/sales/orders",
            GRP_READERS,
            run_at="2026-08-10T00:00:00",
            run_by="alice@example.com",
            config_version=4,
        ),
        # a row with no run_at cannot be placed at either end, so it must not displace one --
        # and must not take 'ghost' along into granted_by on its way past.
        seed_validate_row(
            "R",
            "/Tables/sales/orders",
            GRP_READERS,
            run_at=None,
            run_by="ghost@example.com",
            config_version=99,
        ),
    ]
    prov = make_dep(spark, client, "show").audit.grant_provenance()
    p = prov[("r", "/tables/sales/orders", GRP_READERS.lower())]
    assert p["first_applied"] == "2026-08-10T00:00:00"
    assert p["first_granted_by"] == "alice@example.com"
    assert p["last_applied"] == "2026-08-20T00:00:00"
    assert p["last_granted_by"] == "bob@example.com"
    assert p["config_version"] == 9  # the version in effect now, from the latest push


def test_granted_by_carries_the_labelled_object_id_a_pipeline_run_logs():
    # granted_by is run_by copied verbatim (Audit.access_history / Audit.grants), so a pipeline
    # run's granted_by reads "name (objectId)" — the attested id is present in show's audit
    # enrichment too, not only on the raw log row. Every other granted_by assertion here uses a
    # UPN, which resolve_principal leaves untouched and so would not notice the format.
    member_spark = build_spark()
    seed_sample_members(member_spark)
    deploy_run_by = Log.resolve_principal(member_spark, MEMBER_TABLE, SVC_LOADER)
    spark = log_spark(
        [
            seed_validate_row(
                "SalesReaders",
                ORDERS_PATH,
                GRP_READERS,
                "2026-03-01T09:00:00+00:00",
                deploy_run_by,
                "5",
            )
        ]
    )
    client = FakeFabricClient([fake_role("SalesReaders", [ORDERS_PATH], [GRP_READERS])])
    res = make_dep(spark, client, "show").show("role", "SalesReaders")
    assert res["data"]["grants"][0]["last_granted_by"] == f"{SVC_LOADER_NAME} ({SVC_LOADER})"
    assert res["data"]["grants"][0]["first_granted_by"] == f"{SVC_LOADER_NAME} ({SVC_LOADER})"


def test_grant_entry_shape_is_locked():
    # Lock the full grant contract — catches a dropped key AND an extra undocumented one.
    spark = log_spark(
        [
            seed_validate_row(
                "SalesReaders",
                ORDERS_PATH,
                GRP_READERS,
                "2026-03-01T09:00:00+00:00",
                "alice@example.com",
                "5",
            )
        ]
    )
    client = FakeFabricClient(
        [fake_role("SalesReaders", [ORDERS_PATH], [GRP_READERS], permission="Read")]
    )
    grants = make_dep(spark, client, "show").show("table", "sales.orders")["data"]["grants"]
    assert grants
    for grant in grants:
        assert set(grant) == {
            "role_name",
            "scope_path",
            "member",
            "member_name",
            "permission",
            "provenance",
            "first_applied",
            "first_granted_by",
            "last_applied",
            "last_granted_by",
            "config_version",
        }
    assert grants[0]["permission"] == "Read"  # surfaced from the Action rule


def test_provenance_reports_both_deploys_and_ignores_plan_rows():
    spark = log_spark(
        [
            seed_validate_row(
                "SalesReaders",
                ORDERS_PATH,
                GRP_READERS,
                "2026-01-01T00:00:00+00:00",
                "bob@example.com",
                "3",
                mode="plan",
            ),
            seed_validate_row(
                "SalesReaders",
                ORDERS_PATH,
                GRP_READERS,
                "2026-02-01T00:00:00+00:00",
                "alice@example.com",
                "4",
                mode="apply",
            ),
            seed_validate_row(
                "SalesReaders",
                ORDERS_PATH,
                GRP_READERS,
                "2026-05-01T00:00:00+00:00",
                "carol@example.com",
                "7",
                mode="replace",
            ),
        ]
    )
    client = FakeFabricClient([fake_role("SalesReaders", [ORDERS_PATH], [GRP_READERS])])
    d = make_dep(spark, client, "show").show("member", GRP_READERS)["data"]
    assert d["by"] == "member"  # result echoes the queried axis
    g = d["grants"][0]
    # the plan row is excluded from BOTH ends -- it never pushed anything -- and the two ends
    # carry different principals, which is exactly why one `granted_by` could not serve both.
    assert g["first_applied"] == "2026-02-01T00:00:00+00:00"  # earliest apply, not the plan row
    assert g["first_granted_by"] == "alice@example.com"
    assert g["last_applied"] == "2026-05-01T00:00:00+00:00"
    assert g["last_granted_by"] == "carol@example.com"
    assert g["config_version"] == "7"  # follows the latest push, not the one that started it


def test_invalid_by_lists_axes():
    spark, client = log_spark([]), FakeFabricClient([])
    with pytest.raises(SystemExit) as excinfo:
        make_dep(spark, client, "show").show("column", "x")
    for axis in ("table", "role", "member"):
        assert axis in str(excinfo.value)


def test_missing_subject_fails():
    spark = log_spark([])
    client = FakeFabricClient([fake_role("R", ["/Tables/a/b"], [GRP_READERS])])
    with pytest.raises(SystemExit) as excinfo:
        make_dep(spark, client, "show").show("role", "")
    assert "subject" in str(excinfo.value)


# ---------------------------------------------------------------------------------------------
# MockSetupMigration — setup() audit + additive schema-drift migration (create / migrate /
# no_change), plus the setup/generate/show logging contract: setup & generate log, show writes
# nothing, and generate emits no per-grant 'validate' rows (which are exclusive to apply).
# ---------------------------------------------------------------------------------------------


def test_setup_first_run_creates_all_and_logs(bare):
    spark, client = bare
    res = make_dep(spark, client, "setup").setup()["data"]
    assert sorted(res["created"]) == sorted(CONTROL_TABLES)
    assert res["migrated"] == {}
    assert res["unchanged"] == []
    log = spark._store[LOG_TABLE]
    creates = [r for r in log if r["action"] == "create"]
    completes = [r for r in log if r["action"] == "complete"]
    assert len(creates) == 4  # one create row per created table
    assert len(completes) == 1  # exactly one run-level complete row
    assert all(r["mode"] == "setup" for r in log)  # context mode = setup
    assert all(r["run_by"] == "alice@example.com" for r in log)  # run_by populated
    assert all(r["config_hash"] is None for r in log)  # config_hash NULL on setup rows
    assert "created 4" in completes[0]["message"]


def test_setup_migrates_drifted_column(bare):
    spark, client = bare
    make_dep(spark, client, "setup").setup()
    drop_live_column(spark, LOG_TABLE, "config_version")  # old-schema table, pre config_version
    assert "config_version" not in spark.table(LOG_TABLE).columns
    res = make_dep(spark, client, "setup").setup()["data"]
    assert "config_version" in spark.table(LOG_TABLE).columns  # (a) column re-added
    assert res["created"] == []
    assert res["migrated"] == {LOG_TABLE: ["config_version"]}
    migrates = [r for r in spark._store[LOG_TABLE] if r["action"] == "migrate"]
    assert len(migrates) == 1  # (b) a migrate row names the column
    assert LOG_TABLE in migrates[0]["message"]
    assert "config_version" in migrates[0]["message"]
    last_complete = [r for r in spark._store[LOG_TABLE] if r["action"] == "complete"][-1]
    assert "migrated 1" in last_complete["message"]  # (c) summary reflects the migration


def test_setup_migrates_managed_identity_columns(bare):
    # An old-schema config/mapping (pre-ManagedIdentity) gains the MI columns on setup re-run —
    # additive schema-drift migration must cover the new member-type columns, not just provenance.
    spark, client = bare
    make_dep(spark, client, "setup").setup()
    drop_live_column(spark, CONFIG_TABLE, "include_mi_names")
    drop_live_column(spark, MAPPING_TABLE, "member_mi_ids")
    assert "include_mi_names" not in spark.table(CONFIG_TABLE).columns
    assert "member_mi_ids" not in spark.table(MAPPING_TABLE).columns
    res = make_dep(spark, client, "setup").setup()["data"]
    assert "include_mi_names" in spark.table(CONFIG_TABLE).columns  # config MI col re-added
    assert "member_mi_ids" in spark.table(MAPPING_TABLE).columns  # mapping MI col re-added
    assert res["migrated"] == {
        CONFIG_TABLE: ["include_mi_names"],
        MAPPING_TABLE: ["member_mi_ids"],
    }


def test_setup_migrates_provenance_log_columns(bare):
    # An old-schema log (pre target-ids / tenant / mapping provenance) gains the new columns on
    # a setup re-run — additive schema-drift migration must cover the log's provenance columns too.
    new_cols = [
        "workspace_id",
        "lakehouse_id",
        "tenant_id",
        "mapping_hash",
        "mapping_version",
        "framework_version",
    ]
    spark, client = bare
    make_dep(spark, client, "setup").setup()
    for col in new_cols:
        drop_live_column(spark, LOG_TABLE, col)
    for col in new_cols:
        assert col not in spark.table(LOG_TABLE).columns
    res = make_dep(spark, client, "setup").setup()["data"]
    for col in new_cols:
        assert col in spark.table(LOG_TABLE).columns  # log provenance col re-added
    assert set(res["migrated"][LOG_TABLE]) == set(new_cols)


def test_setup_warns_when_a_live_column_type_disagrees(bare, capsys):
    """setup ADDS a missing column but never RETYPES one, so a live column whose type disagrees
    with the framework's is a defect it can only REPORT — and it has to. Left silent, the failure
    surfaces much later and much further away: Delta refuses the write with
    DELTA_FAILED_TO_MERGE_FIELDS, naming a column pair and nothing else. Reported from a live run,
    where exactly this drift on the mapping's `generated_at` blocked generate while setup had just
    said the table was unchanged.

    The mapping heals itself (its write carries overwriteSchema); the config, log and member tables
    cannot, so for those this warning is the only notice anyone gets before the write fails."""
    spark, client = bare
    make_dep(spark, client, "setup").setup()
    spark._dtypes[LOG_TABLE.lower()] = {"config_hash": "timestamp"}  # an older table's type
    res = make_dep(spark, client, "setup").setup()["data"]
    warning = [ln for ln in capsys.readouterr().out.splitlines() if "⚠️" in ln]
    assert len(warning) == 1, warning
    assert f"{LOG_TABLE}.`config_hash` is timestamp" in warning[0]
    assert "the framework declares STRING" in warning[0]
    assert "rebuild=True" in warning[0]  # setup will not fix it by itself, and names the way out
    # the outcome is unchanged: a type it cannot repair is not a migration
    assert res["migrated"] == {}
    assert res["rebuilt"] == {}
    assert LOG_TABLE in res["unchanged"]


def test_setup_rebuild_drops_and_recreates_only_the_drifted_table(bare, capsys):
    """`ALTER` cannot retype a Delta column, so a drifted table stays broken however many times
    setup runs — rebuild=True is the only way out, and it is destructive by construction. It drops
    ONLY what is actually drifted: a table that agrees is left alone, so this is not a blunt
    "recreate everything"."""
    spark, client = bare
    make_dep(spark, client, "setup").setup()
    spark._store[LOG_TABLE] = [{"batch_id": "b1"}, {"batch_id": "b2"}]  # audit history, about to go
    spark._dtypes[LOG_TABLE.lower()] = {"config_hash": "timestamp"}
    res = make_dep(spark, client, "setup").setup(rebuild=True)["data"]

    assert res["rebuilt"] == {LOG_TABLE: ["config_hash"]}  # named, with the column that forced it
    assert LOG_TABLE not in res["unchanged"]
    assert sorted(res["unchanged"]) == sorted(t for t in CONTROL_TABLES if t != LOG_TABLE)
    assert res["created"] == []  # a rebuild is not a create
    # the drift is GONE, because the table is a new one built from the current DDL
    assert make_dep(spark, client, "setup")._type_drift(LOG_TABLE, ["config_hash"]) == []
    printed = capsys.readouterr().out
    assert f"💥 rebuilding {LOG_TABLE}" in printed
    assert "2 row(s) lost" in printed  # says what it costs BEFORE it costs it
    assert "config_hash: timestamp → STRING" in printed


def test_setup_rebuild_records_the_drop_in_the_new_log(bare):
    """The audit row for a rebuild lands in the table the rebuild just created — the old log may be
    exactly what was dropped, so the record of the destruction cannot live in it."""
    spark, client = bare
    make_dep(spark, client, "setup").setup()
    spark._dtypes[LOG_TABLE.lower()] = {"run_by": "bigint"}
    make_dep(spark, client, "setup").setup(rebuild=True)
    rebuild_rows = [r for r in spark._store[LOG_TABLE] if r["action"] == "rebuild"]
    assert len(rebuild_rows) == 1
    assert LOG_TABLE in rebuild_rows[0]["message"]
    assert "run_by" in rebuild_rows[0]["message"]


def test_setup_rebuild_proceeds_when_the_row_count_cannot_be_read(bare, capsys):
    """The count is there to tell the operator what the drop costs. A table too broken to count is
    exactly the one most likely to need rebuilding, so an unreadable count must not stop the fix —
    it is reported as unknown and the rebuild goes ahead."""
    spark, client = bare
    make_dep(spark, client, "setup").setup()
    spark._dtypes[LOG_TABLE.lower()] = {"config_hash": "timestamp"}

    real_count = FakeDataFrame.count

    def _uncountable(self):
        if self._table == LOG_TABLE:
            raise RuntimeError("table metadata unreadable")
        return real_count(self)

    with mock.patch.object(FakeDataFrame, "count", _uncountable):
        res = make_dep(spark, client, "setup").setup(rebuild=True)["data"]
    assert res["rebuilt"] == {LOG_TABLE: ["config_hash"]}  # rebuilt anyway
    assert "an unknown number of row(s) lost" in capsys.readouterr().out


def test_setup_rebuild_is_a_noop_when_nothing_has_drifted(bare, capsys):
    """The flag is not "drop everything": with every type in agreement, rebuild=True does exactly
    what rebuild=False does. An operator who leaves it on does not lose their tables each run."""
    spark, client = bare
    make_dep(spark, client, "setup").setup()
    spark._store[LOG_TABLE] = [{"batch_id": "keep-me"}]
    res = make_dep(spark, client, "setup").setup(rebuild=True)["data"]
    assert res["rebuilt"] == {}
    assert sorted(res["unchanged"]) == sorted(CONTROL_TABLES)
    assert any(r.get("batch_id") == "keep-me" for r in spark._store[LOG_TABLE])  # survived
    assert "💥" not in capsys.readouterr().out


def test_setup_rebuild_prefers_the_additive_path_for_a_merely_missing_column(bare):
    """A column the live table lacks is a MIGRATION, not a rebuild — dropping the table would throw
    away rows to add a column that ALTER can add in place."""
    spark, client = bare
    make_dep(spark, client, "setup").setup()
    spark._store[LOG_TABLE] = [{"batch_id": "keep-me"}]
    drop_live_column(spark, LOG_TABLE, "config_version")
    res = make_dep(spark, client, "setup").setup(rebuild=True)["data"]
    assert res["rebuilt"] == {}
    assert res["migrated"] == {LOG_TABLE: ["config_version"]}
    assert any(r.get("batch_id") == "keep-me" for r in spark._store[LOG_TABLE])


def test_setup_survives_a_table_that_reports_no_types(bare, capsys):
    """A table whose dtypes cannot be read (an engine that does not expose them, a permission
    quirk) must not fail setup — the drift check is diagnostic, never a gate."""
    spark, client = bare
    make_dep(spark, client, "setup").setup()

    def _no_dtypes(self):
        raise RuntimeError("dtypes unavailable on this table")

    with mock.patch.object(FakeDataFrame, "dtypes", property(_no_dtypes)):
        res = make_dep(spark, client, "setup").setup()["data"]
    assert sorted(res["unchanged"]) == sorted(CONTROL_TABLES)  # setup completed normally
    assert [ln for ln in capsys.readouterr().out.splitlines() if "⚠️" in ln] == []


def test_setup_noop_rerun_writes_only_complete(bare):
    spark, client = bare
    make_dep(spark, client, "setup").setup()
    before = len(spark._store[LOG_TABLE])
    res = make_dep(spark, client, "setup").setup()["data"]  # no drift -> nothing to do
    assert res["created"] == []
    assert res["migrated"] == {}
    assert sorted(res["unchanged"]) == sorted(CONTROL_TABLES)
    added = spark._store[LOG_TABLE][before:]
    assert len(added) == 1  # ONLY the complete row — no create/migrate rows
    assert added[0]["action"] == "complete"
    assert "no schema changes" in added[0]["message"]


def test_setup_never_drops_unexpected_column(bare):
    spark, client = bare
    make_dep(spark, client, "setup").setup()
    spark._columns[LOG_TABLE].append("legacy_extra")  # a column the framework no longer expects
    res = make_dep(spark, client, "setup").setup()["data"]  # must not raise
    assert "legacy_extra" in spark.table(LOG_TABLE).columns  # kept, never ALTER DROPped
    assert res["migrated"] == {}  # an unexpected column triggers no migration
    assert LOG_TABLE in res["unchanged"]


def test_generate_logs_complete_without_validate_rows():
    spark, client = setup_run()
    run_generate(make_dep(spark, client, "generate"))
    generate_log_rows = [r for r in spark._store[LOG_TABLE] if r["mode"] == "generate"]
    completes = [r for r in generate_log_rows if r["action"] == "complete"]
    assert len(completes) == 1
    assert completes[0]["run_duration"] is not None  # run-level complete carries duration
    assert completes[0]["config_hash"] is not None  # config_hash NON-NULL on generate
    assert completes[0]["config_version"] == 3  # captured config version, as a BIGINT
    assert len([r for r in generate_log_rows if r["action"] == "start"]) == 1
    assert [r for r in generate_log_rows if r["action"] == "validate"] == []  # NO validate rows


def test_generate_does_not_pollute_show_enrichment():
    spark, client = setup_run()
    run_generate(make_dep(spark, client, "generate"))
    make_dep(spark, client, "plan").plan()
    run_apply(make_dep(spark, client, "apply"))
    res = make_dep(spark, client, "show").show("role", "Sales*")
    grants = res["data"]["grants"]
    assert grants
    # enrichment comes only from apply's validate rows -> every grant reads as framework
    assert all(g["provenance"] == "framework" for g in grants)
    assert res["data"]["out_of_band"] == 0
    generate_validate = [
        r for r in spark._store[LOG_TABLE] if r["mode"] == "generate" and r["action"] == "validate"
    ]
    assert generate_validate == []  # generate contributed no validate rows


def test_show_writes_no_log_rows():
    spark, client = setup_run()
    run_generate(make_dep(spark, client, "generate"))
    make_dep(spark, client, "plan").plan()
    run_apply(make_dep(spark, client, "apply"))
    before = len(spark._store[LOG_TABLE])
    make_dep(spark, client, "show").show("role", "Sales*")
    assert len(spark._store[LOG_TABLE]) == before  # show is strictly read-only


def test_generate_stores_resolved_target_ids():
    # generate resolves the config's declared target to Fabric ids and stores them in the mapping
    # (single source of truth); on the happy path they equal the attached target ids.
    spark, client = setup_run()
    run_generate(make_dep(spark, client, "generate"))
    rows = spark._store[MAPPING_TABLE]
    assert rows
    assert all(r["workspace_id"] == "ws-guid" and r["lakehouse_id"] == "lh-guid" for r in rows)
    assert all(r["tenant_id"] == TENANT for r in rows)  # tenant captured
    # generate stamps the ATTACHED lakehouse's labels onto every grant (simplification B4)
    assert all(r["workspace_name"] == "WS_Demo" for r in rows)
    assert all(r["lakehouse_name"] == "LH_Demo" for r in rows)


def applied_tenants(mapping_tenant):
    """Run the full pipeline with every mapping row's tenant_id rewritten, and report the set of
    tenantIds the live DAR ends up carrying."""
    spark, client = setup_run()
    run_generate(make_dep(spark, client, "generate"))
    for r in spark._store[MAPPING_TABLE]:
        r["tenant_id"] = mapping_tenant
    make_dep(spark, client, "plan").plan()
    run_apply(make_dep(spark, client, "apply"))
    return {
        m["tenantId"]
        for role in client.list_roles()
        for m in role["members"]["microsoftEntraMembers"]
    }


def test_apply_uses_mapping_tenant_as_source_of_truth():
    # The DAR payload's tenantId comes from the MAPPING, not the runtime deployment.tenant_id:
    # rewrite the mapping's tenant to a sentinel after generate; apply must push that sentinel.
    assert applied_tenants("sentinel-tenant") == {"sentinel-tenant"}  # mapping wins


def test_apply_falls_back_to_runtime_tenant_when_mapping_blank():
    # An old mapping row with a blank tenant_id falls back to the runtime deployment.tenant_id.
    assert applied_tenants("") == {TENANT}


def test_mapping_content_hash_is_order_independent():
    # config_hash sorts keys but not list order; collect() is unordered, and the docstring
    # promises a stable fingerprint — so shuffling rows must NOT change the hash, while changing
    # content MUST. Guards the generate(in-memory) == plan/apply(read-back) equality.
    rows = [
        {"role_name": "B", "scope_path": "/Tables/y", "member_group_ids": "g2"},
        {"role_name": "A", "scope_path": "/Tables/x", "member_group_ids": "g1"},
    ]
    assert Hash.mapping_content(rows) == Hash.mapping_content(list(reversed(rows)))
    changed = [dict(r, member_group_ids="different") for r in rows]
    assert Hash.mapping_content(rows) != Hash.mapping_content(changed)


def test_mapping_content_hash_is_stable_for_duplicate_grain_keys():
    # Two rows sharing (role_name, scope_path) but differing elsewhere — an imperfectly deduped
    # table. The full canonical serialization TIEBREAKER means read order cannot flip the
    # fingerprint; on the bare (role_name, scope_path) key the hash was order-dependent here,
    # and a coin-flip hash makes the plan gate reject intermittently (fail-closed but flaky).
    rows = [
        {"role_name": "A", "scope_path": "/Tables/x", "member_group_ids": "g1"},
        {"role_name": "A", "scope_path": "/Tables/x", "member_group_ids": "g2"},
    ]
    assert Hash.mapping_content(rows) == Hash.mapping_content(list(reversed(rows)))


def test_mapping_content_golden_value_is_pinned():
    # A LITERAL, not a recomputation: every stamped mapping_hash in every existing log row
    # keys the saved-plan gate and the CSV dedupe, so an edit to the digest algorithm must
    # fail THIS test loudly instead of silently moving every fingerprint while the
    # self-referential tests keep passing.
    rows = [
        {"role_name": "B", "scope_path": "/Tables/y", "member_group_ids": "g2"},
        {"role_name": "A", "scope_path": "/Tables/x", "member_group_ids": "g1"},
    ]
    assert Hash.mapping_content(rows) == "b5c537f37c681080"


def test_mapping_content_hash_value_matches_the_1_0_0_algorithm_for_unique_keys():
    """Backward compatibility of the hash VALUE, not merely its stability: the saved-plan gate
    compares this fingerprint against 1.0.0-stamped plan rows, so for a duplicate-free mapping
    (the normal case) it must equal what 1.0.0 computed — its algorithm, a sort on
    (role_name, scope_path) alone, reproduced inline here. The rows are chosen so role-name
    order and full-serialization order DISAGREE (the case where a serialization-primary sort
    changed hash values and silently voided every pre-upgrade plan); the full serialization is
    a tiebreaker only, engaged solely for duplicate grain keys."""
    rows = [  # scrambled input; member ids sort opposite to role names
        {"role_name": "B", "scope_path": "/Tables/y", "member_group_ids": "a-sorts-first"},
        {"role_name": "A", "scope_path": "/Tables/x", "member_group_ids": "z-sorts-last"},
        {"role_name": "A", "scope_path": "/Tables/w", "member_group_ids": "m-sorts-mid"},
    ]
    projected = [{c: r.get(c) for c in MAPPING_COLUMNS} for r in rows]
    projected.sort(key=lambda d: (str(d.get("role_name") or ""), str(d.get("scope_path") or "")))
    hash_1_0_0 = Hash._digest(projected)  # 1.0.0's mapping_content, inline (_digest IS its body)
    assert Hash.mapping_content(rows) == hash_1_0_0
    assert Hash.mapping_content(list(reversed(rows))) == hash_1_0_0


def test_log_rows_carry_full_provenance_chain():
    # Every log row carries the target ids + tenant + config/mapping provenance —
    # the grant <- config gen X -> mapping gen Y -> run Z chain.
    spark, client = setup_run()
    run_generate(make_dep(spark, client, "generate"))
    make_dep(spark, client, "plan").plan()
    run_apply(make_dep(spark, client, "apply"))
    log = spark._store[LOG_TABLE]
    apply_c = [r for r in log if r["mode"] == "apply" and r["action"] == "complete"][-1]
    gen_c = [r for r in log if r["mode"] == "generate" and r["action"] == "complete"][-1]
    expected_hash = Hash.mapping_content(spark._store[MAPPING_TABLE])
    assert apply_c["workspace_id"] == "ws-guid"
    assert apply_c["lakehouse_id"] == "lh-guid"
    assert apply_c["tenant_id"] == TENANT
    # mapping_hash is the REAL content fingerprint, and it is STABLE generate -> apply
    # (order-independent) — not merely "16 chars, present".
    assert apply_c["mapping_hash"] == expected_hash
    assert gen_c["mapping_hash"] == expected_hash
    assert apply_c["mapping_version"] == 3  # FakeSpark Delta history_version, as a BIGINT
    assert apply_c["framework_version"] == __version__  # run-time code version, always set
    assert apply_c["config_hash"]


def test_setup_splits_the_extra_column_message_by_table_ownership(bare, capsys):
    """One policy for a column the framework never declared (issue #2), split by who owns
    the table: on the author-owned config/member tables a foreign column is SUPPORTED
    coexistence (a note — config_hash ignores it, load_config preserves it); on the
    framework-owned mapping it is living on borrowed time (generate rewrites the schema);
    on the append-only log it is an unmanaged intrusion. Same facts, three messages."""
    spark, client = bare
    make_dep(spark, client, "setup").setup()
    spark._columns[CONFIG_TABLE.lower()].append("load_ts")
    spark._columns[MAPPING_TABLE.lower()].append("stray_col")
    spark._columns[LOG_TABLE.lower()].append("audit_extra")
    make_dep(spark, client, "setup").setup()
    out = capsys.readouterr().out
    info = [ln for ln in out.splitlines() if "coexisting column" in ln]
    assert len(info) == 1 and "load_ts" in info[0] and CONFIG_TABLE in info[0]
    assert "ignored by config_hash" in info[0] and "preserved by load_config" in info[0]
    warns = [ln for ln in out.splitlines() if ln.startswith("WARN:")]
    assert len(warns) == 2, warns
    mapping_warn = next(w for w in warns if MAPPING_TABLE in w)
    assert "stray_col" in mapping_warn and "DROP this column" in mapping_warn
    log_warn = next(w for w in warns if LOG_TABLE in w)
    assert "audit_extra" in log_warn and "append-only" in log_warn


def test_an_unlabelled_run_reads_the_log_rows_it_just_wrote():
    """`env` is optional — a deployment that does not split its log by environment leaves it
    unset — and a blank one is stored as NULL, like every other blank string column. SQL's
    `env = ''` never matches NULL, so the log reads have to ask for NULL instead: without that,
    an unlabelled apply would be refused by the saved-plan gate reading past the plan it had
    just written, and every grant it deployed would read back as out-of-band."""
    spark, client = build_spark(), FakeFabricClient([])
    make_dep(spark, client, "setup", env="").setup()
    spark._store[CONFIG_TABLE] = sample_config_rows()
    seed_sample_members(spark)
    run_generate(make_dep(spark, client, "generate", env=""))
    make_dep(spark, client, "plan", env="").plan()
    run_apply(make_dep(spark, client, "apply", env=""))  # the gate must see its own plan row
    log = spark._store[LOG_TABLE]
    assert all(r["env"] is None for r in log)  # blank is stored as NULL, not ''
    applied = [r for r in log if r.get("mode") == "apply" and r.get("action") == "complete"]
    assert applied and applied[0]["status"] == "success"  # the gate let the deploy through
    assert not [r for r in log if r.get("status") == "rejected"]
    # and the provenance read finds them again rather than reporting out-of-band
    provenance = make_dep(spark, client, "show", env="").audit.grant_provenance()
    assert provenance


def test_grants_and_grant_provenance_agree_on_both_ends_whatever_the_rows_look_like():
    """Audit.grants() and Log.grant_provenance() compute the SAME grain from the SAME rows, so
    they must not disagree about either end. grants()' docstring claims it mirrors that grain
    EXACTLY -- this is the test that makes the claim mean something, and it is the test the
    provenance rewrite shipped without: the Log side got one, the Audit side did not.

    Two shapes break an ordering that trusts str(run_at): a row with no run_at at all, whose
    str() is 'None' and sorts AFTER every ISO date ('N' > '2'), and two establishing rows that
    tie. Neither may become an end, because neither is later than anything."""
    rows = [
        seed_validate_row(
            "R", ORDERS_PATH, GRP_READERS, "2026-08-19T01:00:00+00:00", "alice@example.com", "12"
        ),
        seed_validate_row(
            "R", ORDERS_PATH, GRP_READERS, "2026-08-24T05:00:00+00:00", "bob@example.com", "15"
        ),
        # no run_at: cannot be placed at either end, so it must displace neither
        seed_validate_row("R", ORDERS_PATH, GRP_READERS, None, "ghost@example.com", "99"),
    ]
    spark, client = setup_run()
    spark._store[LOG_TABLE] = rows
    dep = make_dep(spark, client, "show")
    prov = dep.audit.grant_provenance()[("r", ORDERS_PATH.lower(), GRP_READERS.lower())]
    got = [
        r.asDict() for r in Audit(spark, CONFIG_TABLE, MAPPING_TABLE, LOG_TABLE).grants().collect()
    ]
    assert len(got) == 1
    g = got[0]
    for field in (
        "first_applied",
        "first_granted_by",
        "last_applied",
        "last_granted_by",
        "config_version",
    ):
        assert g[field] == prov[field], (
            f"grants() and grant_provenance() disagree on {field}: {g[field]!r} vs {prov[field]!r}"
        )
    assert g["last_applied"] == "2026-08-24T05:00:00+00:00"
    assert g["last_granted_by"] == "bob@example.com"
    assert g["config_version"] == "15"  # never the run_at-less row's 99
