"""apply's mid-push failure forensics — what the record SAYS, and what it must never say.

The bulk full-set PUT is the ONLY write surface OLAF uses, and Microsoft labels that endpoint
Preview: there is no delete verb on it (a prior-live role left out of the body is an OMISSION
CANDIDATE, not a proven deletion; the Preview granular DELETE is unconditional and unused), and
the published contract does not promise stable role ids across a write. So when it fails MID-PUSH
nobody knows what landed — an unknown subset of the submitted roles may or may not be in effect,
and the default apply is REPLACE. Before this fix the exception propagated out of apply() BEFORE
any audit.write ran, so the only trace was run_mode's generic handler: ONE row, mode=apply
action=run status=failed, no role_name, data={}.

Two things are pinned by their ABSENCE, both deliberate:
  * NO automatic rollback — a PUT that timed out may well have SUCCEEDED, so restoring the backup
    would silently undo an intended deployment. The restore point is named, never replayed.
  * NO masking — neither a failed re-read nor a failed forensic write may replace the original
    push failure, which is the one thing the operator needs to see.

Ported from `olaf_test_integration.ipynb` classes `ApplyPushFailureForensics` and
`ApplyPushFailureRecordFidelity` (scope "mock").
"""

import json
from unittest import mock

import pytest

import _olaf_runtime as rt
from _olaf_runtime import Audit, DARHTTPError
from _fakes import (
    BACKUP_DIR,
    CONFIG_TABLE,
    GRP_READERS,
    LOG_TABLE,
    MAPPING_TABLE,
    FailingPushFabricClient,
    FakeFabricClient,
    build_spark,
    fake_role,
    lakehouse_writes,
    make_dep,
    run_apply,
    run_generate,
    run_runtime_blackbox,
    sample_config_rows,
    seed_sample_members,
)


def seeded(client):
    """setup -> generate -> plan on fakes against `client`, so ONE apply is unlocked. plan
    performs no PUT, so a push-failing client gets that far untouched."""
    spark = build_spark()
    make_dep(spark, client, "setup").setup()
    spark._store[CONFIG_TABLE] = sample_config_rows()
    seed_sample_members(spark)
    run_generate(make_dep(spark, client, "generate"))
    make_dep(spark, client, "plan").plan()  # the plan record that unlocks apply
    return spark


def per_role(rows):
    """The per-role failure rows: every row naming a role that is NOT one of the pre-push
    `validate` header rows."""
    return {r["role_name"]: r for r in rows if r["role_name"] and r["action"] != "validate"}


def legacy_role():
    """A live role absent from config, used to exercise omission-candidate failure forensics."""
    return fake_role("LegacyManual", ["/Tables/x/y"], [GRP_READERS])


def hand_made_role():
    """A grant made OUT-OF-BAND in the Fabric UI. It names a (role, scope, member) triple the
    config also declares — so it is exactly the triple the pre-push `validate` rows name — but
    the framework never wrote it, so it has no framework provenance and out_of_band() must go
    on reporting it however an apply ends."""
    return fake_role("SalesReaders", ["/Tables/sales/orders"], [GRP_READERS])


def failed_apply(client, keep_unmanaged=False, expected=DARHTTPError):
    """Drive ONE failing apply and return (spark, deployment, the rows it appended)."""
    spark = seeded(client)
    dep = make_dep(spark, client, "apply")
    before = len(spark._store[LOG_TABLE])
    with pytest.raises(expected):
        run_apply(dep, keep_unmanaged=keep_unmanaged)
    return spark, dep, spark._store[LOG_TABLE][before:]


def audit_over(spark, client):
    return Audit(spark, CONFIG_TABLE, MAPPING_TABLE, LOG_TABLE, client=client)


def push_summary(rows):
    return next(r for r in reversed(rows) if r["action"] == "push" and r["status"] != "prepared")


def test_real_put_without_authoritative_response_is_unknown_not_failed_or_unchanged():
    """Once the request starts, failure is an unknown mutation outcome, never changed=false."""
    client = FailingPushFabricClient([legacy_role()])
    spark = seeded(client)
    dep = make_dep(spark, client, "apply")
    before = len(spark._store[LOG_TABLE])

    with pytest.raises(DARHTTPError) as excinfo:
        run_apply(dep)

    summary = push_summary(spark._store[LOG_TABLE][before:])
    assert summary["status"] == "unknown"
    assert getattr(excinfo.value, "changed") is None
    assert excinfo.value.operation == "apply"
    assert excinfo.value.backup_path
    assert excinfo.value.possible_exposure is True


def test_confirmed_put_then_completion_append_failure_raises_typed_changed_true_error():
    """A 2xx is authoritative even when the success audit cannot be confirmed afterwards."""
    client = FakeFabricClient([legacy_role()])
    spark = seeded(client)
    dep = make_dep(spark, client, "apply")
    real_write = dep.audit.write

    def fail_completion(rows):
        if any(r.get("action") == "complete" for r in rows):
            raise RuntimeError("audit append unavailable")
        return real_write(rows)

    dep.audit.write = fail_completion
    with lakehouse_writes():
        with pytest.raises(rt.PostWriteAuditError) as excinfo:
            dep.apply()

    exc = excinfo.value
    assert exc.changed is True
    assert exc.operation == "apply"
    assert exc.push_status == 200
    assert exc.backup_path
    assert exc.batch_id == "B" and exc.run_id == "R"
    assert "DAR PUT returned 2xx; audit completion was not confirmed" in str(exc)
    assert {role["name"] for role in client.list_roles()} == {"RawReaders", "SalesReaders"}
    assert [
        r
        for r in spark._store[LOG_TABLE]
        if r.get("action") == "push" and r.get("status") == "prepared"
    ]


def test_runtime_envelope_uses_null_for_unknown_real_put_outcome():
    client = FailingPushFabricClient([legacy_role()])
    spark = build_spark()
    assert run_runtime_blackbox("setup", spark, client=client).envelope["status"] == "success"
    spark._store[CONFIG_TABLE] = sample_config_rows()
    seed_sample_members(spark)
    assert run_runtime_blackbox("generate", spark, client=client).envelope["status"] == "success"
    assert run_runtime_blackbox("plan", spark, client=client).envelope["status"] == "success"

    outcome = run_runtime_blackbox("apply", spark, client=client)

    assert outcome.envelope["status"] == "error"
    assert outcome.envelope["changed"] is None
    assert outcome.envelope["data"]["operation"] == "apply"
    assert outcome.envelope["data"]["backup_path"]
    assert outcome.envelope["data"]["possible_exposure"] is True


def test_runtime_envelope_preserves_changed_true_for_post_write_audit_error():
    spark = build_spark()
    client = FakeFabricClient([])
    run_runtime_blackbox("setup", spark, client=client)
    spark._store[CONFIG_TABLE] = sample_config_rows()
    seed_sample_members(spark)
    run_runtime_blackbox("generate", spark, client=client)
    run_runtime_blackbox("plan", spark, client=client)
    error = rt.PostWriteAuditError(
        "apply",
        200,
        "Files/security/role-backups/recovery.json",
        "batch-1",
        "run-1",
        RuntimeError("append unknown"),
    )

    with mock.patch.object(rt.Deployment, "apply", side_effect=error):
        outcome = run_runtime_blackbox("apply", spark, client=client)

    assert outcome.envelope["status"] == "error"
    assert outcome.envelope["changed"] is True
    assert outcome.envelope["data"] == error.as_data()
    assert "DAR PUT returned 2xx" in outcome.envelope["error"]


# ---------------------------------------------------------------------------------------------
# ApplyPushFailureForensics — what the record SAYS
# ---------------------------------------------------------------------------------------------


def test_push_failure_records_request_candidates_vs_live_per_role_then_reraises():
    """Failure forensics distinguish a submitted request from an omission candidate; they do
    not turn an absent prior-live role into a deletion claim."""
    client = FailingPushFabricClient([legacy_role()])
    spark = seeded(client)
    before = len(spark._store[LOG_TABLE])

    with pytest.raises(DARHTTPError) as excinfo:
        run_apply(make_dep(spark, client, "apply"))
    assert "504" in str(excinfo.value)  # the ORIGINAL push failure, re-raised unchanged

    rows = spark._store[LOG_TABLE][before:]
    by_role = per_role(rows)
    assert {n: r["action"] for n, r in by_role.items()} == {
        "SalesReaders": "create",
        "RawReaders": "create",
        "LegacyManual": "omission_candidate",
    }
    assert {r["status"] for r in by_role.values()} == {"failed"}
    assert {r["error_category"] for r in by_role.values()} == {"http"}
    # Requested role data versus the best-effort post-request read; neither is a deletion fact.
    assert "ABSENT" in by_role["SalesReaders"]["message"]
    assert "ABSENT" in by_role["RawReaders"]["message"]
    assert "PRESENT" in by_role["LegacyManual"]["message"]

    summary = [r for r in rows if r["action"] == "push" and r["status"] != "prepared"]
    assert len(summary) == 1
    assert (summary[0]["status"], summary[0]["error_category"]) == ("unknown", "http")
    forensics = json.loads(summary[0]["message"])
    assert forensics["intended_roles"] == ["RawReaders", "SalesReaders"]
    assert forensics["omitted_role_candidates"] == ["LegacyManual"]
    assert forensics["post_state_review_required"] is True
    assert forensics["live_roles_after"] == ["LegacyManual"]
    assert forensics["live_read_error"] is None
    assert BACKUP_DIR in forensics["backup_path"]
    assert "504" in forensics["error"]
    # NO automatic rollback: exactly ONE real PUT was attempted and nothing was restored
    assert not forensics["rolled_back"]
    real_puts = [c for c in client.put_calls if not c["dry_run"]]
    assert [(c["dry_run"], c["roles"]) for c in real_puts] == [(False, 2)]
    assert real_puts[0]["etag"]  # the If-Match token rode along on the one real PUT


def test_a_failed_re_read_blocks_forensic_audit_and_never_masks_the_original_failure():
    """The outage that broke the PUT is usually still in force a second later, so the re-read
    can fail too. That means a fresh DAR snapshot cannot authorize the forensic audit write;
    preserve only the already-safe prepared intent and never mask the original push failure."""
    client = FailingPushFabricClient(
        [legacy_role()], list_roles_error=ConnectionError("DAR API unreachable")
    )
    spark = seeded(client)
    before = len(spark._store[LOG_TABLE])

    with pytest.raises(DARHTTPError) as excinfo:
        run_apply(make_dep(spark, client, "apply"))
    assert "504" in str(excinfo.value)  # the PUSH failure — NOT the re-read failure
    assert "unreachable" not in str(excinfo.value)

    rows = spark._store[LOG_TABLE][before:]
    assert [(row["action"], row["status"]) for row in rows] == [("push", "prepared")]
    # The unavailable DAR is not turned into a misleading empty live-state claim, nor used to
    # authorize a follow-up audit append.
    assert not [row for row in rows if row["status"] in {"unknown", "failed"}]


def test_a_failed_forensic_write_still_surfaces_the_original_failure():
    """The log table can be the thing that is down. Writing the forensic rows is best-effort
    for the same reason _reject's denial row is: a logging failure must never replace the
    failure it was trying to describe."""
    client = FailingPushFabricClient([legacy_role()])
    spark = seeded(client)
    dep = make_dep(spark, client, "apply")
    attempts = []

    def flaky(rows):
        attempts.append(rows)
        if any(r.get("action") == "push" and r.get("status") == "prepared" for r in rows):
            return real_write(rows)
        raise RuntimeError("onelake_security_log unavailable")

    real_write = dep.audit.write
    with mock.patch.object(dep.audit, "write", flaky):
        with pytest.raises(DARHTTPError) as excinfo:
            run_apply(dep)
    assert "504" in str(excinfo.value)
    assert "onelake_security_log unavailable" not in str(excinfo.value)
    assert len(attempts) == 2  # prepared intent succeeded; unknown-outcome forensics were attempted


# ---------------------------------------------------------------------------------------------
# ApplyPushFailureRecordFidelity — what the record must NEVER say.
#
# The record is written into the SAME table the audit-provenance surface reads: the pre-push header
# rows carry action='validate' status='success' mode='apply' — EXACTLY the predicate
# Log.grant_provenance / Audit.grants / Audit.trace use to mean "this grant was actually pushed".
# Emitted unchanged on the failure path, a push that provably wrote NOTHING claims it wrote
# everything — permanently, because those three take the EARLIEST run_at per key. Worst of all it
# ADOPTS a grant made out-of-band in the Fabric UI.
# ---------------------------------------------------------------------------------------------


def test_a_failed_apply_establishes_no_grants():
    """A push that wrote nothing establishes nothing: grants()/trace() read the deploying
    'validate' + 'success' rows, and a failed apply must contribute none."""
    client = FailingPushFabricClient([legacy_role()])
    spark, _dep, _rows = failed_apply(client)
    audit = audit_over(spark, client)
    assert [r.asDict() for r in audit.grants().collect()] == []
    assert [r.asDict() for r in audit.trace().collect()] == []


def test_a_failed_apply_leaves_no_framework_provenance():
    """A failed push writes no establishing row at all, so grant_provenance has nothing to
    report -- not a first_applied end, not a last_applied one. This matters more now that the
    LATEST push supplies granted_by and config_version: if a failure could seed a key, it would
    also be free to overwrite a real grant's current-state fields on the way past."""
    client = FailingPushFabricClient([legacy_role()])
    _spark, dep, _rows = failed_apply(client)
    assert dep.audit.grant_provenance() == {}


def test_a_genuine_out_of_band_grant_survives_a_failed_apply():
    """The operator-facing consequence, and the one that matters most: a grant the framework
    never wrote must not be adopted as its own by a push that failed."""
    client = FailingPushFabricClient([legacy_role(), hand_made_role()])
    spark, _dep, _rows = failed_apply(client)
    oob = [r.asDict() for r in audit_over(spark, client).out_of_band().collect()]
    assert sorted((r["role_name"], r["scope_path"]) for r in oob) == [
        ("LegacyManual", "/Tables/x/y"),
        ("SalesReaders", "/Tables/sales/orders"),
    ]


def test_the_pre_push_header_rows_are_kept_but_not_as_pushed_grants():
    """The grant-grain facts stay in the trail — validation DID happen — but they are recorded
    as failed, not success, so no provenance query counts them as a deploy."""
    client = FailingPushFabricClient([legacy_role()])
    _spark, _dep, rows = failed_apply(client)
    assert len([r for r in rows if r["action"] == "start"]) == 1
    validate = [r for r in rows if r["action"] == "validate"]
    assert len(validate) == 4  # one per grant (role x scope x member)
    assert {r["status"] for r in validate} == {"failed"}
    assert {r["error_category"] for r in validate} == {"http"}
    assert {r["member_id"] is not None for r in validate} == {True}


def test_a_dry_run_rejection_leaves_no_mid_push_record():
    """The dryRun writes nothing, so a rejection there must produce NO forensic record at all —
    not one structurally identical to a failure that may have half-written the live set."""
    client = FailingPushFabricClient(
        [legacy_role()],
        dry_run_error=DARHTTPError(
            "PUT .../dataAccessRoles?dryRun=true -> 400: policy validation failed"
        ),
    )
    _spark, _dep, rows = failed_apply(client)
    assert [c for c in client.put_calls if not c["dry_run"]] == []
    assert rows == []


def test_an_incremental_failure_records_no_omission_candidate():
    """The incremental payload carries unmanaged roles, so its failure record names no
    omission candidate."""
    client = FailingPushFabricClient([legacy_role()])
    _spark, _dep, rows = failed_apply(client, keep_unmanaged=True)
    legacy = per_role(rows)["LegacyManual"]
    assert "not submitted as omission candidate" in legacy["message"]
    assert "delete" not in legacy["message"]
    assert "PRESENT" in legacy["message"]
    forensics = json.loads(push_summary(rows)["message"])
    assert forensics["keep_unmanaged"] is True
    assert forensics["omitted_role_candidates"] == []
    assert forensics["post_state_review_required"] is True


def test_a_config_payload_failure_records_an_omission_candidate():
    """The config payload distinguishes its omission candidate from an incremental payload
    without claiming what the service did with that candidate."""
    client = FailingPushFabricClient([legacy_role()])
    _spark, _dep, rows = failed_apply(client)
    legacy = per_role(rows)["LegacyManual"]
    assert legacy["action"] == "omission_candidate"
    assert "submitted payload omission candidate" in legacy["message"]
    assert "post-state review required" in legacy["message"]
    forensics = json.loads(push_summary(rows)["message"])
    assert forensics["keep_unmanaged"] is False
    assert forensics["omitted_role_candidates"] == ["LegacyManual"]
    assert forensics["post_state_review_required"] is True


def test_a_cancelled_cell_mid_push_is_still_recorded():
    """An operator hitting stop on a hanging PUT is exactly the 'it may have landed' case.
    KeyboardInterrupt is a BaseException, so `except Exception` never saw it and the whole
    record was skipped."""
    client = FailingPushFabricClient([legacy_role()], push_error=KeyboardInterrupt())
    _spark, _dep, rows = failed_apply(client, expected=KeyboardInterrupt)
    summary = [r for r in rows if r["action"] == "push" and r["status"] != "prepared"]
    assert len(summary) == 1
    assert summary[0]["error_category"] == "unexpected"
    assert json.loads(summary[0]["message"])["live_roles_after"] == ["LegacyManual"]
    assert len(per_role(rows)) == 3


def test_widening_to_base_exception_still_lets_system_exit_out():
    """SystemExit is the load-bearing pipeline-fail signal: the record is written and then the
    ORIGINAL SystemExit propagates unchanged — never swallowed, never re-wrapped."""
    client = FailingPushFabricClient([legacy_role()], push_error=SystemExit("pipeline fail"))
    spark = seeded(client)
    dep = make_dep(spark, client, "apply")
    before = len(spark._store[LOG_TABLE])
    with pytest.raises(SystemExit) as excinfo:
        run_apply(dep)
    assert str(excinfo.value) == "pipeline fail"
    rows = spark._store[LOG_TABLE][before:]
    summary = [r for r in rows if r["action"] == "push" and r["status"] != "prepared"]
    assert len(summary) == 1
    # SystemExit keeps its own error_category — the vocabulary is unchanged by the widening
    assert summary[0]["error_category"] == "guard"
    assert "pipeline fail" in json.loads(summary[0]["message"])["error"]
