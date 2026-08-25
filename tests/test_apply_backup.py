"""apply's pre-push role backup.

The default config payload can omit prior-live roles. OLAF records those as omission candidates,
not confirmed deletion or access outcomes. The pre-push snapshot is a reviewed recovery input for
a future request; it does not guarantee service acceptance or exact platform-state recovery.

Ported from `olaf_test_integration.ipynb` class `ApplyRoleBackup` (scope "mock").
"""

import datetime
import json
import os
import re
from unittest import mock

import pytest

import _olaf_runtime as rt
from _olaf_runtime import OLAF
from _fakes import (
    BACKUP_DIR,
    CONFIG_TABLE,
    GRP_READERS,
    LOG_TABLE,
    FakeFabricClient,
    build_spark,
    fake_role,
    lakehouse_writes,
    make_dep,
    ols_env,
    ols_seed,
    role_backup_of,
    role_backups_of,
    run_apply,
    run_generate,
    sample_config_rows,
    seed_sample_members,
)


def seeded(live_extra=()):
    """setup -> generate -> plan on fakes, with `live_extra` already live and NOT declared in
    config. Returns (spark, client) ready for one apply request."""
    spark = build_spark()
    client = FakeFabricClient(list(live_extra))
    make_dep(spark, client, "setup").setup()
    spark._store[CONFIG_TABLE] = sample_config_rows()
    seed_sample_members(spark)
    run_generate(make_dep(spark, client, "generate"))
    make_dep(spark, client, "plan").plan()  # the plan record that unlocks apply
    return spark, client


def undeclared_role():
    """A live Default* reader carrying an RLS predicate, absent from the configuration payload."""
    return fake_role(
        "DefaultReader",
        ["/Tables/sales/orders"],
        [GRP_READERS],
        rls={"/Tables/sales/orders": "region = 'north'"},
    )


def apply_complete_rows(spark):
    return [
        r for r in spark._store[LOG_TABLE] if r["action"] == "complete" and r["mode"] == "apply"
    ]


def test_private_backup_refuses_without_an_owned_sensitive_operation_sentinel():
    spark, client = seeded([undeclared_role()])
    dep = make_dep(spark, client, "apply")
    dep._live = client.list_roles()
    captured = {}

    with (
        lakehouse_writes(store=captured),
        pytest.raises(rt.ControlDataGuardError, match="active sensitive-operation sentinel"),
    ):
        dep._backup_live_roles("replace")

    assert captured == {}


def test_existing_sentinel_blocks_a_valid_planned_apply_before_backup_or_put(monkeypatch):
    spark, client = seeded([undeclared_role()])
    log_before = [dict(row) for row in spark._store[LOG_TABLE]]
    captured = {}

    def refuse(_boundary):
        raise rt.ControlDataGuardError("control-data incident sentinel already exists")

    monkeypatch.setattr(rt.ControlBoundary, "_create_sentinel", refuse)
    with pytest.raises(rt.ControlDataGuardError, match="sentinel already exists"):
        run_apply(make_dep(spark, client, "apply"), captured=captured)

    assert captured == {}
    assert client.put_calls == []
    assert spark._store[LOG_TABLE] == log_before


def test_backup_dry_run_prepared_intent_and_real_put_are_strictly_ordered():
    """The durable write protocol is structural: backup -> dry-run -> prepared -> real PUT."""
    spark, client = seeded([undeclared_role()])
    pushes = []
    real_put = client.put_roles

    with lakehouse_writes() as writes:

        def spy(roles, dry_run=False, etag=None, *, allow_unconditional=False):
            prepared = [
                r
                for r in spark._store[LOG_TABLE]
                if r.get("action") == "push" and r.get("status") == "prepared"
            ]
            pushes.append(
                {
                    "dry_run": dry_run,
                    "backups": sorted(p for p in writes if BACKUP_DIR in p),
                    "prepared": prepared,
                }
            )
            return real_put(
                roles,
                dry_run=dry_run,
                etag=etag,
                allow_unconditional=allow_unconditional,
            )

        client.put_roles = spy
        make_dep(spark, client, "apply").apply()

    assert len(pushes) == 2  # dryRun + the real bulk PUT
    assert all(len(event["backups"]) == 1 for event in pushes)
    assert pushes[0]["dry_run"] is True and pushes[0]["prepared"] == []
    assert pushes[1]["dry_run"] is False and len(pushes[1]["prepared"]) == 1
    intent = json.loads(pushes[1]["prepared"][0]["message"])
    expected = {
        "schema": 1,
        "operation": "apply",
        "phase": "prepared",
        "payload_hash": intent["payload_hash"],
        "intended_roles": ["RawReaders", "SalesReaders"],
        "omitted_role_candidates": ["DefaultReader"],
        "post_state_review_required": True,
        "keep_unmanaged": False,
        "backup_path": intent["backup_path"],
        "conditional": True,
        "etag": '"fake-etag-0"',
        "isolation_attestation": "test-access-review/fixture",
    }
    assert {key: intent[key] for key in expected} == expected
    assert re.fullmatch(r"[0-9a-f]{64}", intent["payload_hash"])
    assert re.fullmatch(r"[0-9a-f]{64}", intent["reserved_digest"])
    assert re.fullmatch(r"[0-9a-f]{64}", intent["dar_roles_digest"])
    assert intent["backup_path"].startswith(BACKUP_DIR + "/")


def test_prepared_intent_append_failure_aborts_before_real_put_and_leaves_incident_marker():
    """No durable intent means no real request is authorized, even after backup and dry-run."""
    spark, client = seeded([undeclared_role()])
    dep = make_dep(spark, client, "apply")
    real_write = dep.audit.write

    def fail_prepared(rows):
        if any(r.get("action") == "push" and r.get("status") == "prepared" for r in rows):
            raise RuntimeError("intent append unavailable")
        return real_write(rows)

    dep.audit.write = fail_prepared
    with lakehouse_writes() as writes:
        with pytest.raises(RuntimeError, match="intent append unavailable"):
            dep.apply()
        assert any(BACKUP_DIR in path for path in writes)
        assert any(".olaf-sensitive-write.sentinel" in path for path in writes)

    assert [call["dry_run"] for call in client.put_calls] == [True]
    assert not [
        r
        for r in spark._store[LOG_TABLE]
        if r.get("action") == "push" and r.get("status") == "prepared"
    ]


def test_apply_prewrite_blocks_backup_when_dar_changes_after_apply_entry(monkeypatch):
    """A stale apply must not create a recovery artifact, prepared intent, or real DAR write."""
    spark, client = seeded([undeclared_role()])
    dep = make_dep(spark, client, "apply")
    backup = dep._backup_live_roles
    log_before = [dict(row) for row in spark._store[LOG_TABLE]]

    def rotate_then_backup(*args, **kwargs):
        client.simulate_external_edit()
        return backup(*args, **kwargs)

    monkeypatch.setattr(dep, "_backup_live_roles", rotate_then_backup)
    with lakehouse_writes() as writes:
        with pytest.raises(rt.ControlDataGuardError, match="changed after the approved snapshot"):
            dep.apply()

    assert not [path for path in writes if BACKUP_DIR in path]
    assert client.put_calls == []
    assert spark._store[LOG_TABLE] == log_before


def test_apply_prewrite_blocks_real_put_after_a_safe_prepared_intent(monkeypatch):
    """Prepared intent is not a blanket authorization for a later real DAR write."""
    spark, client = seeded([undeclared_role()])
    dep = make_dep(spark, client, "apply")
    prepared = dep._prepared_intent

    def prepare_then_rotate(*args, **kwargs):
        result = prepared(*args, **kwargs)
        client.simulate_external_edit()
        return result

    monkeypatch.setattr(dep, "_prepared_intent", prepare_then_rotate)
    with lakehouse_writes() as writes:
        with pytest.raises(rt.ControlDataGuardError, match="changed after the approved snapshot"):
            dep.apply()

    assert any(BACKUP_DIR in path for path in writes)
    assert [call["dry_run"] for call in client.put_calls] == [True]
    assert [
        row
        for row in spark._store[LOG_TABLE]
        if row.get("action") == "push" and row.get("status") == "prepared"
    ]


@pytest.mark.parametrize(("keep_unmanaged", "word"), [(False, "replace"), (True, "incremental")])
def test_both_branches_back_up_and_name_the_file_for_their_mode(keep_unmanaged, word):
    """Both payload-construction branches capture a recovery input before submission. The file name
    carries the branch word so an operator can locate and review the matching artifact."""
    spark, client = seeded([undeclared_role()])
    dep = make_dep(spark, client, "apply")
    captured = {}
    res = run_apply(dep, keep_unmanaged=keep_unmanaged, captured=captured)
    path, _ = role_backup_of(captured)
    name = path.rsplit("/", 1)[-1]
    assert path == f"/lakehouse/default/{BACKUP_DIR}/{name}"
    assert re.match(
        r"^onelake_security_roles_\d{8}-\d{6}_" + word + r"_[0-9a-f]{16}_[0-9A-Za-z]{1,12}\.json$",
        name,
    ), name
    assert f"_{dep.config_hash}_" in name
    # the timestamp still LEADS, so a name sort is a time sort for the operator scanning the
    # folder for the last `replace`
    assert name.startswith("onelake_security_roles_"), name
    assert res["data"]["backup_path"] == f"{BACKUP_DIR}/{name}"


def test_backup_content_is_a_reviewed_input_to_a_fake_put_request():
    """The stored bytes are a bare role list suitable for a reviewed recovery request.

    The final fake-client call exercises OLAF's serialization seam only; it does not assert that a
    Preview service accepts the request or restores an exact prior platform state.
    """
    spark, client = seeded([undeclared_role()])
    before = client.list_roles()
    captured = {}
    res = run_apply(make_dep(spark, client, "apply"), captured=captured)
    assert "DefaultReader" in res["data"]["omitted_role_candidates"]
    assert res["data"]["post_state_review_required"] is True

    _, roles = role_backup_of(captured)
    assert isinstance(roles, list)
    assert roles == before
    client.list_roles()  # fake conditional request starts with a fresh collection snapshot
    client.put_roles(roles, etag=client.roles_etag)
    assert client.put_calls[-1]["roles"] == len(before)


def test_backup_omits_the_server_assigned_id_and_etag():
    """The backup removes server-assigned id/etag fields from the captured role representation.
    Stripping keeps the artifact suitable as reviewed input to a future request, without claiming
    an exact restore outcome. The live set itself is untouched by this serialization step."""
    stamped = dict(
        undeclared_role(),
        id="7c9f2e41" + "-0000-4a11-9d31-6b0e2f8a5c13",
        etag="W/\"datetime'2026-07-20T16%3A38%3A43.1Z'\"",
    )
    spark, client = seeded([stamped])
    before = client.list_roles()
    # NON-VACUITY: the fixture really does emit both keys on the live role being backed up.
    assert [(r.get("id"), r.get("etag")) for r in before if r["name"] == "DefaultReader"] == [
        (stamped["id"], stamped["etag"])
    ]

    pushed = []
    real_put = client.put_roles

    def spy(roles, dry_run=False, etag=None, *, allow_unconditional=False):
        pushed.append(json.loads(json.dumps(roles)))
        return real_put(
            roles,
            dry_run=dry_run,
            etag=etag,
            allow_unconditional=allow_unconditional,
        )

    client.put_roles = spy
    captured = {}
    # INCREMENTAL: the branch that feeds self._live straight into DAR.merge_upsert, so the
    # pushed payload is the direct witness that the strip did not mutate self._live.
    run_apply(make_dep(spark, client, "apply"), keep_unmanaged=True, captured=captured)

    _, roles = role_backup_of(captured)
    assert [k for r in roles for k in ("id", "etag") if k in r] == []
    # nothing ELSE was dropped — the strip removes exactly those two keys
    assert roles == [{k: v for k, v in r.items() if k not in ("id", "etag")} for r in before]
    # self._live UNMUTATED: every push still carried the id/etag verbatim
    for payload in pushed:
        assert [(r.get("id"), r.get("etag")) for r in payload if r["name"] == "DefaultReader"] == [
            (stamped["id"], stamped["etag"])
        ]
    # and the file is restore-ready as written — RUNBOOK §3c, verbatim, with no hand-edit
    client.put_roles = real_put
    client.list_roles()
    client.put_roles(roles, etag=client.roles_etag)
    assert {r["name"] for r in client.list_roles()} == {r["name"] for r in before}


@pytest.mark.parametrize("keep_unmanaged", [False, True])
def test_backup_path_reaches_the_envelope_and_the_log_row(keep_unmanaged):
    """backup_path REPLACES the old backup_roles count (which backed nothing up), and rides
    into the apply's complete row so history points at the artifact months later."""
    spark, client = seeded()
    captured = {}
    res = run_apply(
        make_dep(spark, client, "apply"), keep_unmanaged=keep_unmanaged, captured=captured
    )
    path, _ = role_backup_of(captured)
    relpath = path.replace("/lakehouse/default/", "")
    assert res["data"]["backup_path"] == relpath
    assert "backup_roles" not in res["data"]
    complete = apply_complete_rows(spark)
    assert json.loads(complete[-1]["message"])["backup_path"] == relpath


def test_role_backup_dir_is_honoured_end_to_end():
    """The parameter is load-bearing, not decorative: a configured folder must reach BOTH the
    directory apply writes into and the `backup_path` the envelope hands back — the operator
    restores from that string (RUNBOOK 3c), so a path disagreeing with where the bytes went would
    send a break-glass restore to an empty folder."""
    custom = "Files/security/olaf-restore-points"
    spark, client = seeded([undeclared_role()])
    dep = make_dep(spark, client, "apply", role_backup_dir=custom)
    captured = {}
    res = run_apply(dep, captured=captured)
    written = [p for p in captured if custom in p]
    assert len(written) == 1, captured  # the bytes went to the configured folder
    assert written[0].startswith(f"/lakehouse/default/{custom}/")
    assert res["data"]["backup_path"].startswith(f"{custom}/")  # ...and the envelope agrees
    assert BACKUP_DIR not in written[0]  # nothing fell back to the default
    assert BACKUP_DIR not in res["data"]["backup_path"]


def test_a_failed_backup_aborts_the_apply_and_pushes_nothing():
    """A backup that fails open is WORSE than none — the envelope would advertise a restore
    point that does not exist. The write failure must propagate (run_mode turns it into a
    failed envelope) with the live roles untouched and not one call made to the client."""
    spark, client = seeded([undeclared_role()])
    before = client.list_roles()
    with pytest.raises(OSError):
        run_apply(make_dep(spark, client, "apply"), fail_on=BACKUP_DIR)
    assert client.put_calls == []  # nothing pushed — not even the dryRun
    assert client.list_roles() == before
    assert apply_complete_rows(spark) == []


def test_two_applies_never_share_a_filename_and_both_snapshots_survive():
    """The collision this naming scheme exists to make IMPOSSIBLE — reproduced, then closed.
    `ts` is second-granularity and mode_word/config_hash repeat, so two applies (a retry, or two
    pipelines on one lakehouse) reached ONE name and the second snapshot overwrote the first.
    On REPLACE the overwritten file is precisely the one holding the roles the first apply had
    just deleted: the exact loss the feature exists to prevent.

    Every name component that is NOT a collision guard is pinned identical here — frozen clock
    (same `ts` to the second), same config (same config_hash), same batch token (make_dep's
    default "B"), same mode word — so ONLY the exclusive-create claim can keep the two files
    apart. Both snapshots must survive, and the second must not be the first's content."""
    real_datetime = datetime.datetime

    class _FrozenUTC(real_datetime):
        """Second-granularity `ts` is the collision's precondition; freeze it so this test
        reproduces it on every run rather than once in a while."""

        @classmethod
        def now(cls, tz=None):
            return real_datetime(2026, 7, 20, 17, 12, 13, tzinfo=tz)

    spark, client = seeded([undeclared_role()])
    live_before_first = client.list_roles()
    captured = {}
    # INCREMENTAL both times: it never deletes, so the SECOND apply still runs against a live
    # set the first one changed — two genuinely different snapshots under one stem. The clock is
    # frozen ONLY around each apply (it returns the same instant either way, which is the point);
    # the re-plan in between keeps its real timestamp so the apply gate reads the LATEST record.
    with mock.patch("datetime.datetime", _FrozenUTC):
        run_apply(make_dep(spark, client, "apply"), keep_unmanaged=True, captured=captured)
    live_before_second = client.list_roles()
    make_dep(spark, client, "plan").plan()  # the re-plan the apply gate requires
    with mock.patch("datetime.datetime", _FrozenUTC):
        run_apply(make_dep(spark, client, "apply"), keep_unmanaged=True, captured=captured)

    backups = role_backups_of(captured)
    assert len(backups) == 2, [p for p, _ in backups]
    (first_path, first_roles), (second_path, second_roles) = backups
    assert first_path != second_path
    # PRECONDITION, asserted not assumed: the two names really are one disambiguating suffix
    # apart — same timestamp, same mode word, same config_hash, same batch token — and the
    # suffix sorts AFTER the unsuffixed name, so a name sort is still a time sort.
    assert first_path.replace(".json", "_2.json") == second_path
    assert [p for p, _ in backups] == sorted(p for p, _ in backups)
    # BOTH restore points intact, and the second is NOT the first
    assert first_roles != second_roles
    assert first_roles == live_before_first

    def canonical(roles):
        return sorted(json.dumps(role, sort_keys=True) for role in roles)

    assert canonical(second_roles) == canonical(
        [{k: v for k, v in role.items() if k not in ("id", "etag")} for role in live_before_second]
    )
    # the first apply's restore point still holds what the first apply saw
    assert "DefaultReader" in {r["name"] for r in first_roles}
    # and the single-backup helper AGREES: it checks the count against the applies the caller
    # drove, so one file where two belong is a helper FAILURE.
    assert role_backup_of(captured, applies=2) == (second_path, second_roles)


def test_a_short_write_leaves_no_file_under_the_advertised_name():
    """open(path,"w") + json.dump writes INCREMENTALLY, so a crash or short write left a
    TRUNCATED file at the very name data.backup_path and the log row advertise as a valid
    restore point. It does fail loudly — a truncated snapshot always raises JSONDecodeError —
    but only at RESTORE time, during the incident. The content now goes to a temp file beside
    the claimed name and is swapped in with os.replace, so a mid-write failure surfaces HERE and
    aborts the apply per the fail-closed contract, leaving neither a truncated backup at the
    advertised name nor the temp file behind."""
    spark, client = seeded([undeclared_role()])
    before = client.list_roles()
    partial = []

    def short_dump(obj, fh):
        """A REAL short write: some bytes land, then the device fills up."""
        fh.write(json.dumps(obj)[:12])
        partial.append(fh.getvalue())
        raise OSError(28, "No space left on device")

    captured = {}
    with mock.patch.object(json, "dump", short_dump), pytest.raises(OSError) as excinfo:
        run_apply(make_dep(spark, client, "apply"), captured=captured)

    # NON-VACUITY: the fake really did write parse-breaking bytes before failing
    assert partial[0]
    with pytest.raises(json.JSONDecodeError):
        json.loads(partial[0])
    # nothing survives under the backup dir — not the truncated content, not the temp file
    assert [p for p in captured if BACKUP_DIR in p] == []
    assert "role backup write failed" in str(excinfo.value)
    # and the apply still aborted fail-closed: no push, live untouched, no complete row
    assert client.put_calls == []
    assert client.list_roles() == before
    assert apply_complete_rows(spark) == []


@pytest.mark.parametrize("boundary", ["dar", "sentinel"])
def test_backup_cleanup_preserves_recovery_artifacts_when_the_boundary_turns_unsafe(boundary):
    """Removing the temp or claim after a new DAR/sentinel failure destroys incident evidence."""
    spark, client = seeded([undeclared_role()])
    dep = make_dep(spark, client, "apply")
    captured, removed = {}, []

    def fail_after_artifact_creation(_roles, _handle):
        if boundary == "dar":
            client.simulate_external_edit()
        else:
            captured[rt.ControlBoundary.SENTINEL_FULL_PATH] = "tampered\n"
        raise OSError("backup medium failed")

    with lakehouse_writes(store=captured):
        real_remove = os.remove

        def record_remove(path):
            removed.append(path)
            return real_remove(path)

        with (
            mock.patch.object(json, "dump", fail_after_artifact_creation),
            mock.patch("os.remove", record_remove),
            pytest.raises(OSError) as excinfo,
        ):
            dep.apply()

    artifacts = [path for path in captured if path.startswith(f"/lakehouse/default/{BACKUP_DIR}/")]
    assert len(artifacts) == 2
    assert any(path.endswith(".tmp") for path in artifacts)
    assert any(path.endswith(".json") for path in artifacts)
    assert removed == []
    assert "backup medium failed" in str(excinfo.value)
    assert excinfo.value.cleanup_boundary["artifacts_retained"] is True
    assert excinfo.value.cleanup_boundary["operation"] == "apply"
    assert excinfo.value.cleanup_boundary["reason"].startswith("ControlDataGuardError:")
    assert client.put_calls == []


def test_backup_cleanup_boundary_fact_does_not_mask_an_attribute_refusing_write_error():
    """An exotic original error must still name both the failed write and blocked cleanup."""

    class AttributeRefusingOSError(OSError):
        def __setattr__(self, name, value):
            if name == "cleanup_boundary":
                raise AttributeError("custom errors reject metadata")
            return super().__setattr__(name, value)

    spark, client = seeded([undeclared_role()])
    dep = make_dep(spark, client, "apply")
    captured, removed = {}, []

    def fail_after_artifact_creation(_roles, _handle):
        client.simulate_external_edit()
        raise AttributeRefusingOSError("backup medium failed")

    with lakehouse_writes(store=captured):
        real_remove = os.remove

        def record_remove(path):
            removed.append(path)
            return real_remove(path)

        with (
            mock.patch.object(json, "dump", fail_after_artifact_creation),
            mock.patch("os.remove", record_remove),
            pytest.raises(AttributeRefusingOSError) as excinfo,
        ):
            dep.apply()

    artifacts = [path for path in captured if path.startswith(f"/lakehouse/default/{BACKUP_DIR}/")]
    assert len(artifacts) == 2
    assert removed == []
    assert "backup medium failed" in str(excinfo.value)
    assert "cleanup boundary:" in str(excinfo.value)
    assert client.put_calls == []


def test_a_backup_failure_names_the_artifact_and_the_path_in_the_envelope():
    """Live-reproduced before this fix, through this same dispatch: forcing OSError on the
    backup write produced envelope.message = "OSError: [Errno 28] No space left on device" with
    a log row of action="run", error_category="unexpected" — nothing naming the backup or the
    path, so an on-call engineer could not tell it from any other crash. The abort is
    deliberately UNCHANGED (it must still abort, and it must still be `unexpected`); only the
    reason is now legible."""
    spark = build_spark()
    client = FakeFabricClient([undeclared_role()])
    with ols_env(spark, client, fail_on=BACKUP_DIR):
        ols_seed(spark, upto="plan")
        OLAF.apply()  # the facade returns a view; the raw envelope is stashed
        envelope = OLAF.last_result

    assert envelope["status"] == "error"
    assert "role backup write failed" in envelope["message"]
    assert f"{BACKUP_DIR}/onelake_security_roles_" in envelope["message"]
    # unchanged: still an abort, still the same failure channel and category
    assert client.put_calls == []
    run_rows = [r for r in spark._store[LOG_TABLE] if r["action"] == "run"]
    assert run_rows[-1]["error_category"] == "unexpected"


def test_a_poisoned_backup_directory_reads_as_one():
    """os.makedirs(p, exist_ok=True) still raises FileExistsError when a FILE sits at p — so
    anyone who can write Files/security/ (a LOWER privilege than the workspace Member apply
    itself requires) can `touch Files/security/role-backups` and block every apply and every
    rollback, permanently. Failing closed stays right; what was missing was any way to see WHY.
    The re-raise keeps the exception CLASS — run_mode's envelope message leads with it, and
    FileExistsError is the tell that separates a poisoned directory from a full disk."""
    spark, client = seeded([undeclared_role()])
    # exactly what makedirs raises over a file
    poison = FileExistsError(17, "File exists", f"/lakehouse/default/{BACKUP_DIR}")
    with pytest.raises(FileExistsError) as excinfo:
        run_apply(make_dep(spark, client, "apply"), fail_makedirs=poison)

    message = f"{type(excinfo.value).__name__}: {excinfo.value}"  # the envelope's own format
    assert "role backup write failed" in message
    assert f"{BACKUP_DIR}/onelake_security_roles_" in message
    assert "File exists" in message
    assert f"/lakehouse/default/{BACKUP_DIR}" in message
    assert client.put_calls == []
