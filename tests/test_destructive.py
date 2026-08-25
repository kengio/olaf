"""`reset()` and `cleanup()` — the two irreversible utilities.

They are deliberately NOT modes: a pipeline cannot select them by setting a string parameter, and
run_mode refuses them BY NAME so the absence reads as a decision rather than a typo. These tests
pin that, and pin what each one destroys — because for these two, "what it deletes" IS the contract.
"""

import contextlib
import json
import os
import re
from unittest import mock

import pytest

import _olaf_runtime as rt
from _olaf_runtime import (
    DARHTTPError,
    DEFAULT_CONTROL_TABLES,
    INTERACTIVE_ONLY,
    OLAF,
    Deployment,
    UsageError,
)
from _fakes import (
    BACKUP_DIR,
    CONFIG_TABLE,
    CTL_MAPPING_HISTORY_DIR,
    LOG_TABLE,
    MAPPING_TABLE,
    MEMBER_TABLE,
    FakeFabricClient,
    build_spark,
    fake_role,
    lakehouse_writes,
    make_dep,
    ols_env,
    ols_rows,
    ols_seed,
    run_runtime_blackbox,
)


# --------------------------------------------------------------------------------------------
# the guard that makes them safe: not modes
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("mode", sorted(INTERACTIVE_ONLY))
def test_a_pipeline_cannot_select_them_by_mode(mode):
    """The primary guard. A scheduled run passing mode="reset" must not submit an empty DAR payload -- and
    the refusal names the mode instead of saying "unknown", so the reader learns it EXISTS and is
    withheld on purpose."""
    outcome = run_runtime_blackbox(mode, build_spark(), params={"lakehouse_name": "LH_Demo"})
    err = (outcome.raised or "") + str(outcome.envelope or "")
    assert "interactive-only" in err and mode in err
    assert "destructive" in err.lower()


def test_they_are_absent_from_the_mode_list_not_just_refused():
    """Belt and braces: the refusal above is a message, this is the structural fact behind it --
    neither name appears in the KNOWN_MODES literal, so even if the refusal were deleted the
    dispatch could not reach them."""
    import inspect

    from _olaf_runtime import run_mode

    known = re.search(r"KNOWN_MODES = \{(.*?)\}", inspect.getsource(run_mode), re.S).group(1)
    for name in INTERACTIVE_ONLY:
        assert f'"{name}"' not in known


# --------------------------------------------------------------------------------------------
# reset
# --------------------------------------------------------------------------------------------


def _direct_reset(client):
    spark = build_spark()
    make_dep(spark, client, "setup").setup()
    return spark, make_dep(spark, client, "reset")


def test_reset_writes_prepared_intent_after_backup_before_real_put():
    client = FakeFabricClient(
        [fake_role("BusinessReaders", ["/Tables/sales/orders"], ["group-id"])]
    )
    spark, dep = _direct_reset(client)
    observed = []
    real_put = client.put_roles

    with lakehouse_writes() as writes:

        def spy(roles, dry_run=False, etag=None, *, allow_unconditional=False):
            observed.append(
                {
                    "backups": [path for path in writes if BACKUP_DIR in path],
                    "prepared": [
                        row
                        for row in spark._store[LOG_TABLE]
                        if row.get("action") == "push" and row.get("status") == "prepared"
                    ],
                }
            )
            return real_put(
                roles,
                dry_run=dry_run,
                etag=etag,
                allow_unconditional=allow_unconditional,
            )

        client.put_roles = spy
        dep.reset()

    assert len(observed) == 1
    assert len(observed[0]["backups"]) == 1
    assert len(observed[0]["prepared"]) == 1
    intent = json.loads(observed[0]["prepared"][0]["message"])
    assert intent["schema"] == 1
    assert intent["operation"] == "reset" and intent["phase"] == "prepared"
    assert intent["intended_roles"] == []
    assert intent["omitted_role_candidates"] == ["BusinessReaders"]
    assert intent["post_state_review_required"] is True
    assert intent["keep_unmanaged"] is None
    assert intent["conditional"] is True and intent["etag"] == '"fake-etag-0"'
    assert intent["isolation_attestation"] == "test-access-review/fixture"
    assert re.fullmatch(r"[0-9a-f]{64}", intent["payload_hash"])


def test_reset_publicly_reports_an_empty_request_and_prior_role_candidates():
    """A successful PUT response confirms the submitted empty body, not role deletion.

    Removing the request/candidate distinction would let OLAF's own frame and durable audit turn
    a Preview request into an unsupported platform-state claim. The fake is used only to expose
    OLAF's submitted payload and recorded labels; this test intentionally makes no assertion about
    the platform's deletion-by-omission behavior.
    """
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        ols_seed(spark, upto="apply")
        client._roles.append(fake_role("ManualReader", ["/Tables/sales/orders"], ["group-id"]))
        client.put_calls.clear()  # isolate the reset request from seed's apply request
        rows = ols_rows(OLAF.reset())

    assert {row["prior_live_role_candidate"] for row in rows} >= {
        "ManualReader",
        "RawReaders",
        "SalesReaders",
    }
    assert all(row["request"] == "empty_payload" for row in rows)
    assert all(row["post_state_review_required"] == "True" for row in rows)
    assert all("deleted_role" not in row for row in rows)
    assert client.put_calls == [
        {
            "dry_run": False,
            "roles": 0,
            "etag": '"fake-etag-1"',
            "allow_unconditional": False,
        }
    ]

    candidate_rows = [
        row
        for row in spark._store[LOG_TABLE]
        if row.get("action") == "omission_candidate" and row.get("mode") == "reset"
    ]
    assert {row["role_name"] for row in candidate_rows} >= {
        "ManualReader",
        "RawReaders",
        "SalesReaders",
    }
    assert {row["status"] for row in candidate_rows} == {"submitted"}
    assert all("post-state review" in row["message"] for row in candidate_rows)


def test_reset_missing_snapshot_etag_blocks_before_backup_intent_or_put():
    class NoETag(FakeFabricClient):
        def list_roles_quick(self):
            roles = super().list_roles_quick()
            self.roles_etag = None
            return roles

    client = NoETag([])
    spark = build_spark()
    dep = make_dep(spark, client, "reset")
    with lakehouse_writes() as writes:
        with pytest.raises(rt.ControlDataGuardError, match="ETag"):
            dep.reset()
    assert client.put_calls == []
    assert not [path for path in writes if BACKUP_DIR in path]
    assert not [
        row
        for row in spark._store.get(LOG_TABLE, [])
        if row.get("action") == "push" and row.get("status") == "prepared"
    ]


def test_reset_prewrite_blocks_backup_when_dar_changes_after_reset_entry(monkeypatch):
    """Reset must not create a recovery artifact from stale DAR authority."""
    client = FakeFabricClient(
        [fake_role("BusinessReaders", ["/Tables/sales/orders"], ["group-id"])]
    )
    spark, dep = _direct_reset(client)
    backup = dep._backup_live_roles
    log_before = [dict(row) for row in spark._store[LOG_TABLE]]

    def rotate_then_backup(*args, **kwargs):
        client.simulate_external_edit()
        return backup(*args, **kwargs)

    monkeypatch.setattr(dep, "_backup_live_roles", rotate_then_backup)
    with lakehouse_writes() as writes:
        with pytest.raises(rt.ControlDataGuardError, match="changed after the approved snapshot"):
            dep.reset()

    assert not [path for path in writes if BACKUP_DIR in path]
    assert client.put_calls == []
    assert spark._store[LOG_TABLE] == log_before


def test_reset_prewrite_blocks_real_put_after_a_safe_prepared_intent(monkeypatch):
    """The reset intent records history but cannot authorize a later stale whole-set delete."""
    client = FakeFabricClient(
        [fake_role("BusinessReaders", ["/Tables/sales/orders"], ["group-id"])]
    )
    spark, dep = _direct_reset(client)
    prepared = dep._prepared_intent

    def prepare_then_rotate(*args, **kwargs):
        result = prepared(*args, **kwargs)
        client.simulate_external_edit()
        return result

    monkeypatch.setattr(dep, "_prepared_intent", prepare_then_rotate)
    with lakehouse_writes() as writes:
        with pytest.raises(rt.ControlDataGuardError, match="changed after the approved snapshot"):
            dep.reset()

    assert any(BACKUP_DIR in path for path in writes)
    assert client.put_calls == []
    assert [
        row
        for row in spark._store[LOG_TABLE]
        if row.get("action") == "push" and row.get("status") == "prepared"
    ]


def test_reset_explicit_unconditional_opt_out_still_uses_etag_bearing_privacy_gate():
    client = FakeFabricClient([])
    spark, dep = _direct_reset(client)
    dep.if_match = False
    with lakehouse_writes():
        dep.reset()
    assert client.put_calls == [
        {
            "dry_run": False,
            "roles": 0,
            "etag": None,
            "allow_unconditional": True,
        }
    ]


def test_reset_unknown_real_write_records_unknown_and_retains_recovery_pointer():
    class UnknownResetClient(FakeFabricClient):
        def put_roles(self, roles, dry_run=False, etag=None, *, allow_unconditional=False):
            self.put_calls.append(
                {
                    "dry_run": dry_run,
                    "roles": len(roles),
                    "etag": etag,
                    "allow_unconditional": allow_unconditional,
                }
            )
            raise DARHTTPError("reset response unavailable")

    client = UnknownResetClient([])
    spark, dep = _direct_reset(client)
    with lakehouse_writes():
        with pytest.raises(DARHTTPError) as excinfo:
            dep.reset()
    assert getattr(excinfo.value, "changed") is None
    assert excinfo.value.operation == "reset"
    unknown = [
        row
        for row in spark._store[LOG_TABLE]
        if row.get("action") == "push" and row.get("status") == "unknown"
    ]
    assert len(unknown) == 1
    recovery = json.loads(unknown[0]["message"])
    assert recovery["backup_path"] and recovery["omitted_role_candidates"] == []
    assert recovery["post_state_review_required"] is True


@pytest.mark.parametrize(
    ("ambiguous", "status", "changed"), [(False, "rejected", False), (True, "unknown", None)]
)
def test_reset_conflicts_record_their_distinct_recovery_outcomes(ambiguous, status, changed):
    class ConflictResetClient(FakeFabricClient):
        def put_roles(self, roles, dry_run=False, etag=None, *, allow_unconditional=False):
            self.put_calls.append(
                {
                    "dry_run": dry_run,
                    "roles": len(roles),
                    "etag": etag,
                    "allow_unconditional": allow_unconditional,
                }
            )
            raise rt.DARConflictError("reset conditional write conflicted", ambiguous=ambiguous)

    client = ConflictResetClient([])
    spark, dep = _direct_reset(client)
    with lakehouse_writes(), pytest.raises(rt.DARConflictError) as excinfo:
        dep.reset()

    assert excinfo.value.changed is changed
    rows = [
        row
        for row in spark._store[LOG_TABLE]
        if row.get("action") == "push" and row.get("status") == status
    ]
    assert len(rows) == 1


def test_reset_forensics_tolerate_a_failed_reread_or_audit_append():
    client = FakeFabricClient([])
    spark, dep = _direct_reset(client)

    def reread_fails():
        raise RuntimeError("DAR reread unavailable")

    client.list_roles_quick = reread_fails
    dep.audit.write = mock.Mock(side_effect=RuntimeError("audit unavailable"))
    dep._record_reset_unknown(
        RuntimeError("write outcome unknown"), [], "Files/security/backup.json"
    )
    dep._record_reset_conflict(rt.DARConflictError("conflict"), "Files/security/backup.json")


def test_reset_confirmed_put_then_completion_audit_failure_is_changed_true():
    client = FakeFabricClient([])
    spark, dep = _direct_reset(client)
    real_write = dep.audit.write

    def fail_completion(rows):
        if any(row.get("action") == "complete" for row in rows):
            raise RuntimeError("reset completion unavailable")
        return real_write(rows)

    dep.audit.write = fail_completion
    with lakehouse_writes():
        with pytest.raises(rt.PostWriteAuditError) as excinfo:
            dep.reset()
    assert excinfo.value.changed is True
    assert excinfo.value.operation == "reset"
    assert excinfo.value.push_status == 200
    assert excinfo.value.backup_path


def test_reset_reports_every_prior_live_role_as_an_empty_request_candidate(capsys):
    """The frame reports every prior-live role as a request candidate and retains a backup input.

    The test verifies OLAF's payload and labels only; the fake's local role list is not treated as
    evidence of Preview deletion-by-omission behavior.
    """
    spark, client = build_spark(), FakeFabricClient([])
    captured = {}
    with ols_env(spark, client, store=captured):
        ols_seed(spark, upto="apply")  # deploys the config's own roles
        client._roles += [  # ...plus two this framework never authored
            fake_role(
                "DefaultReader", ["/Tables/sales/orders"], ["11111111-1111-1111-1111-111111111111"]
            ),
            fake_role(
                "SomeoneElses", ["/Tables/sales/orders"], ["11111111-1111-1111-1111-111111111111"]
            ),
        ]
        df = OLAF.reset()
    rows = ols_rows(df)
    candidates = {r["prior_live_role_candidate"] for r in rows}
    assert {"DefaultReader", "SomeoneElses"} <= candidates
    assert {"SalesReaders", "RawReaders"} <= candidates
    assert {row["request"] for row in rows} == {"empty_payload"}
    assert {row["post_state_review_required"] for row in rows} == {"True"}
    assert client.put_calls[-1]["roles"] == 0
    backup = {r["backup_path"] for r in rows}.pop()
    assert backup and any(backup in p for p in captured)  # the file really was written


def test_reset_on_an_empty_target_says_so_rather_than_returning_nothing():
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        ols_seed(spark, upto="generate")  # nothing deployed, so nothing live
        df = OLAF.reset()
    row = ols_rows(df)[0]
    assert "(none" in row["prior_live_role_candidate"]
    assert row["request"] == "empty_payload"


def test_reset_leaves_the_control_tables_alone():
    """The separation of duties between the two: reset clears the LIVE side only, so a redeploy is
    generate -> plan -> apply with the config still there."""
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        ols_seed(spark, upto="apply")
        before = {t: len(spark._store[t]) for t in (CONFIG_TABLE, MAPPING_TABLE, MEMBER_TABLE)}
        OLAF.reset()
    assert {t: len(spark._store[t]) for t in before} == before
    assert LOG_TABLE in spark._store  # and the reset is itself in the audit trail


def test_reset_refuses_without_a_live_client():
    spark = build_spark()
    with ols_env(spark, FakeFabricClient([])):
        ols_seed(spark, upto="apply")
        with mock_no_client():
            with pytest.raises(UsageError) as e:
                OLAF.reset()
    assert "FabricClient" in str(e.value)


# --------------------------------------------------------------------------------------------
# cleanup
# --------------------------------------------------------------------------------------------


def test_cleanup_drops_every_control_table_and_reports_each():
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        ols_seed(spark, upto="apply")
        df = OLAF.cleanup()
    rows = ols_rows(df)
    dropped = {r["name"] for r in rows if r["kind"] == "dropped table"}
    assert dropped == {CONFIG_TABLE, MAPPING_TABLE, LOG_TABLE, MEMBER_TABLE}
    for t in dropped:
        assert t not in spark._store  # the audit history included -- that is the point


def test_cleanup_names_the_live_roles_it_did_not_touch():
    """It clears the lakehouse, never the DAR. A role left deployed with its audit trail deleted is
    the state an operator most needs told about, so it is a row in the result, not a footnote."""
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        ols_seed(spark, upto="apply")
        df = OLAF.cleanup()
    left = [r["name"] for r in ols_rows(df) if r["kind"] == "LIVE ROLE LEFT BEHIND"]
    assert left == ["RawReaders", "SalesReaders"]  # every live role, sorted
    assert client._roles  # untouched


def test_cleanup_survives_an_unreadable_dar():
    """A lakehouse cleanup must not be blocked by the live side being unreachable."""

    class _Boom(FakeFabricClient):
        def list_roles(self, timeout=None):
            raise RuntimeError("no network")

    spark = build_spark()
    with ols_env(spark, _Boom([])):
        ols_seed(spark, upto="apply")
        df = OLAF.cleanup()
    note = [r["name"] for r in ols_rows(df) if r["kind"] == "LIVE ROLE LEFT BEHIND"]
    assert note and "could not read live roles" in note[0]
    assert CONFIG_TABLE not in spark._store  # and it still did the job


def test_cleanup_of_an_untouched_lakehouse_is_not_an_error():
    """Absent folders are a clean state, not a failure -- this is the FIRST-run case it exists for."""
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        OLAF.setup()
        df = OLAF.cleanup()
    assert [r for r in ols_rows(df) if r["kind"] == "dropped table"]


def test_cleanup_enumerates_four_tables_on_a_directly_built_deployment():
    """cleanup() reads self.log_table off the instance, and until Deployment.__init__ set it the
    attribute existed ONLY because the interactive facade patched it on after construction. Any
    Deployment built another way — explain(), a direct call, a test — therefore had a cleanup()
    that dropped the config and mapping tables and THEN raised AttributeError, halfway through the
    one operation whose whole contract is what it destroys.

    Built here with no facade in sight, so the fix has to be in the constructor for this to pass."""
    spark, client = build_spark(), FakeFabricClient([])
    dep = make_dep(spark, client, "setup")
    dep.setup()  # the four control tables now exist

    assert dep.log_table == LOG_TABLE  # taken from this Deployment's OWN audit Log, not guessed
    with mock.patch("os.listdir", side_effect=FileNotFoundError):  # no lakehouse folders here
        res = dep.cleanup()

    assert res["dropped"] == [CONFIG_TABLE, MAPPING_TABLE, LOG_TABLE, MEMBER_TABLE]
    for table in res["dropped"]:
        assert table not in spark._store


def test_a_deployment_without_an_audit_falls_back_to_the_default_log_table():
    """explain() builds a client-less, audit-less Deployment for a pure read. It never calls
    cleanup(), but the attribute must still name a TABLE: left as None the drop loop would issue
    `DROP TABLE IF EXISTS None`, which is a valid statement against a table nobody meant."""
    dep = Deployment(
        build_spark(), None, None, "", CONFIG_TABLE, MAPPING_TABLE, CTL_MAPPING_HISTORY_DIR
    )
    assert dep.log_table == DEFAULT_CONTROL_TABLES["log_table"]


# --------------------------------------------------------------------------------------------
# the folder-parameter path guard (external security audit 2026-08-16, A-04): cleanup()
# deletes every file under mapping_history_dir/role_backup_dir by raw f-string concatenation,
# so both parameters must be vetted ONCE, at construction, before any consumer reads them.
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "",  # empty -> the delete loop would target the lakehouse mount root
        "   ",  # whitespace-only is empty after the strip
        "/lakehouse/default/Files/x",  # absolute spelled as the full mount path
        "/tmp/x",  # absolute outside Files/ (a leading '/' alone is canonicalized, not refused)
        "Files\\history",  # backslash — not a posix lakehouse path
        "Files/se\x00cret",  # NUL — never a real lakehouse path
        "..",  # traversal escaping the mount entirely
        "Files/..",  # normalizes to '.' — the mount root again
        "Files/../secrets",  # traversal escaping Files/ into a sibling
        "Files",  # the WHOLE user file area, not a folder inside it
        ".",  # the mount root
        "data/x",  # outside Files/ — not a file area this framework owns
    ],
)
@pytest.mark.parametrize("param", ["mapping_history_dir", "role_backup_dir"])
def test_an_unsafe_folder_parameter_is_refused_at_construction(param, value):
    history = value if param == "mapping_history_dir" else CTL_MAPPING_HISTORY_DIR
    backup = value if param == "role_backup_dir" else BACKUP_DIR
    with pytest.raises(SystemExit) as excinfo:
        Deployment(
            build_spark(),
            FakeFabricClient([]),
            None,
            "",
            CONFIG_TABLE,
            MAPPING_TABLE,
            history,
            role_backup_dir=backup,
        )
    assert param in str(excinfo.value)  # the refusal names the offending parameter
    assert repr(value) in str(excinfo.value)  # ...and the offending value
    assert "refused, not coerced" in str(excinfo.value)


def test_the_default_folders_are_accepted_verbatim():
    dep = Deployment(
        build_spark(),
        None,
        None,
        "",
        CONFIG_TABLE,
        MAPPING_TABLE,
        CTL_MAPPING_HISTORY_DIR,
        role_backup_dir=BACKUP_DIR,
    )
    assert dep.mapping_history_dir == CTL_MAPPING_HISTORY_DIR
    assert dep.role_backup_dir == BACKUP_DIR


def test_a_messy_but_contained_folder_is_normalized_not_refused():
    # redundant separators and inner dots normalize away; a '..' that STAYS inside Files/ is
    # normalized too — only a path that ends up outside Files/ is refused.
    dep = Deployment(
        build_spark(),
        None,
        None,
        "",
        CONFIG_TABLE,
        MAPPING_TABLE,
        "Files//security/./history",
        role_backup_dir="Files/security/a/../backups",
    )
    assert dep.mapping_history_dir == "Files/security/history"
    assert dep.role_backup_dir == "Files/security/backups"


@pytest.mark.parametrize(
    ("value", "canonical"),
    [
        ("/Files/security/mapping-history", "Files/security/mapping-history"),  # leading slash
        ("files/security/history", "Files/security/history"),  # lowercase Files segment
        ("FILES/Security/History", "Files/Security/History"),  # any case; rest kept verbatim
    ],
)
def test_folder_spellings_the_config_columns_accept_are_canonicalized(value, canonical):
    # ScopePath.folder accepts these spellings for the config's folder columns; the folder
    # PARAMETERS follow the same rule rather than inventing a stricter one — they were valid
    # spellings before this guard existed, and refusing them would break working setups.
    dep = Deployment(
        build_spark(), None, None, "", CONFIG_TABLE, MAPPING_TABLE, value, role_backup_dir=value
    )
    assert dep.mapping_history_dir == canonical
    assert dep.role_backup_dir == canonical


def test_a_traversal_folder_parameter_is_refused_at_the_boundary():
    """Driven through the REAL dispatch: the folder parameters are refused beside the
    verbosity/env guards INSIDE the envelope boundary, so the refusal is the same
    structured 'blocked' envelope every parameter guard produces (no log context exists
    either way). Deployment.__init__ stays the backstop for constructions that bypass
    run_mode (explain(), the destructive utilities, direct use)."""
    outcome = run_runtime_blackbox(
        "generate", build_spark(), params={"mapping_history_dir": "Files/../secrets"}
    )
    res = outcome.envelope
    assert res["status"] == "blocked"
    assert "mapping_history_dir" in res["error"]
    assert "'Files/../secrets'" in res["error"]  # the offending value is named
    assert outcome.raised is not None  # native-failure: the activity FAILS
    assert outcome.exit_value is None  # blocked never reaches notebook.exit


def test_cleanup_refuses_a_folder_mutated_after_construction_before_dropping_anything():
    """__init__ is the primary guard; cleanup() re-checks the CONCRETE delete roots before its
    first DROP TABLE, so even an attribute mutated after construction cannot point the delete
    loop outside /lakehouse/default/Files — and the four tables survive the refusal. A real
    SystemExit, not an assert: `python -O` strips asserts, and a containment check that
    vanishes under an interpreter flag is no check at all."""
    spark, client = build_spark(), FakeFabricClient([])
    dep = make_dep(spark, client, "setup")
    dep.setup()  # the four control tables now exist
    dep.role_backup_dir = "Files/../../../etc"  # bypasses __init__ — a post-construction edit
    with pytest.raises(SystemExit) as excinfo:
        dep.cleanup()
    assert "cleanup refused" in str(excinfo.value)
    for table in (CONFIG_TABLE, MAPPING_TABLE, LOG_TABLE, MEMBER_TABLE):
        assert table in spark._store  # nothing was dropped


# --------------------------------------------------------------------------------------------


@contextlib.contextmanager
def mock_no_client():
    """Target.resolve failing is exactly how _build_client returns None off-Fabric."""
    from unittest import mock

    from _olaf_runtime import Target

    with mock.patch.object(Target, "resolve", side_effect=SystemExit("no lakehouse attached")):
        yield


def test_cleanup_deletes_the_files_and_reports_one_it_could_not():
    """The files half, including the failure it must not hide: a locked entry or a subfolder is
    reported as NOT removed rather than silently counted as deleted — after cleanup(), what the
    frame says is gone is the only record that anything was ever there."""
    spark, client = build_spark(), FakeFabricClient([])
    listed = {
        "/lakehouse/default/Files/security/mapping-history": ["a.csv", "b.csv"],
        "/lakehouse/default/Files/security/role-backups": ["roles.json", "locked"],
    }
    removed = []

    def fake_listdir(path):
        if path in listed:
            return listed[path]
        raise FileNotFoundError(path)

    def fake_remove(path):
        if path.endswith("locked"):
            raise PermissionError(path)
        removed.append(path)

    with ols_env(spark, client):
        ols_seed(spark, upto="apply")
        # Keep these replacements nested inside ols_env's filesystem seam. A pytest
        # monkeypatch finalizer runs after ols_env exits and would restore its stale
        # os.remove fake globally, corrupting the next test's sentinel cleanup.
        with (
            mock.patch.object(os, "listdir", fake_listdir),
            mock.patch.object(os, "remove", fake_remove),
        ):
            df = OLAF.cleanup()
    files = [r["name"] for r in ols_rows(df) if r["kind"] == "deleted file"]
    not_removed = [r["name"] for r in ols_rows(df) if r["kind"] == "NOT REMOVED"]
    assert "Files/security/mapping-history/a.csv" in files
    assert "Files/security/role-backups/roles.json" in files
    assert "Files/security/role-backups/locked" in not_removed  # named, not swallowed
    assert len(removed) == 3


def test_cleanup_names_failed_table_drops_and_preserves_an_enumerated_sentinel(monkeypatch):
    spark, client = build_spark(), FakeFabricClient([])
    dep = make_dep(spark, client, "cleanup")
    sentinel = "/lakehouse/default/Files/security/mapping-history/.olaf-sensitive-write.sentinel"
    monkeypatch.setattr(rt.ControlBoundary, "SENTINEL_FULL_PATH", sentinel)
    real_sql = spark.sql

    def drop_one_table(query):
        if f"DROP TABLE IF EXISTS {MAPPING_TABLE}" in str(query):
            raise RuntimeError("drop unavailable")
        return real_sql(query)

    def list_sentinel(path):
        if path.endswith("Files/security/mapping-history"):
            return [".olaf-sensitive-write.sentinel"]
        raise FileNotFoundError(path)

    spark.sql = drop_one_table
    with mock.patch.object(os, "listdir", list_sentinel):
        result = dep.cleanup()

    assert f"{MAPPING_TABLE} (RuntimeError)" in result["not_removed"]
    assert (
        "Files/security/mapping-history/.olaf-sensitive-write.sentinel (incident sentinel preserved)"
        in result["not_removed"]
    )


def test_cleanup_preserves_incident_sentinel_and_reports_containment_not_remediation():
    spark, client, lakehouse = build_spark(), FakeFabricClient([]), {}
    sentinel = rt.ControlBoundary.SENTINEL_FULL_PATH
    with ols_env(spark, client, store=lakehouse):
        OLAF.setup()
        lakehouse[sentinel] = rt.ControlBoundary.SENTINEL_CONTENT
        df = OLAF.cleanup()

    rows = ols_rows(df)
    assert lakehouse[sentinel] == rt.ControlBoundary.SENTINEL_CONTENT
    assert {r["kind"] for r in rows} >= {
        "INCIDENT SENTINEL PRESERVED",
        "EXPOSURE NOT REMEDIATED",
    }
    warning = next(r["name"] for r in rows if r["kind"] == "EXPOSURE NOT REMEDIATED")
    assert "exposure_remediated=false" in warning
    assert "prior reads" in warning and "Delta history" in warning


def test_reviewed_incident_clearance_is_audited_before_the_marker_is_removed():
    spark, client, lakehouse = build_spark(), FakeFabricClient([]), {}
    sentinel = rt.ControlBoundary.SENTINEL_FULL_PATH
    with ols_env(spark, client, store=lakehouse):
        OLAF.setup()
        lakehouse[sentinel] = rt.ControlBoundary.SENTINEL_CONTENT
        df = OLAF.clear_incident("access-review/2026-08-22#42")
        clearance = [
            dict(row)
            for row in spark._store[LOG_TABLE]
            if row.get("action") == "sentinel_clearance"
        ]

    assert sentinel not in lakehouse
    assert len(clearance) == 1 and clearance[0]["status"] == "reviewed"
    evidence = json.loads(clearance[0]["message"])
    assert evidence["access_review"] == "access-review/2026-08-22#42"
    assert evidence["exposure_remediated"] is False
    row = ols_rows(df)[0]
    assert row["cleared"] == "True"
    assert row["exposure_remediated"] == "False"


def test_run_mode_called_directly_also_refuses_them():
    """run_and_exit's `allowed` gate catches a pipeline first, so this pins the SECOND guard —
    the one that still holds if someone calls run_mode by hand from a notebook cell."""
    from _olaf_runtime import run_mode

    for mode in sorted(INTERACTIVE_ONLY):
        # run_mode never raises on outcome — a refused mode comes back as a blocked envelope
        env = run_mode(mode, {"lakehouse_name": "LH_Demo"}, build_spark())
        assert env["status"] == "blocked"
        assert "interactive-only" in env["error"] and mode in env["error"]


# ---------------------------------------------------------------------------------------------
# The interactive-only refusal sentence, pinned at BOTH entry points.
#
# It used to be two separate byte-identical literals differing only in where the source line
# wrapped -- one a `raise SystemExit` in run_mode, the other an envelope `_msg` in run_and_exit --
# so an expression-level sweep for duplicated `raise` text could not see the pair. The only
# assertion touching either was a SUBSTRING ("interactive-only", above), which would have stayed
# green while the two copies drifted into two different explanations of the same refusal.
#
# It matters more than a typical duplicated string: this is the guard keeping reset() and
# cleanup() -- the two destructive, irreversible operations -- off the `mode` string a scheduled
# pipeline can set. The absence of those from the mode table IS the guard; this sentence is how an
# operator learns the omission is deliberate rather than a bug to work around.
#
# Both tests below assert against `interactive_only_refusal` itself, so a future edit to the
# sentence updates both at once and neither can silently diverge.
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("mode", sorted(INTERACTIVE_ONLY))
def test_run_mode_refusal_is_the_shared_sentence_verbatim(mode):
    from _olaf_runtime import interactive_only_refusal, run_mode

    env = run_mode(mode, {"lakehouse_name": "LH_Demo"}, build_spark())

    assert env["status"] == "blocked"
    assert env["error"] == interactive_only_refusal(mode)
    assert env["message"] == interactive_only_refusal(mode)


@pytest.mark.parametrize("mode", sorted(INTERACTIVE_ONLY))
def test_run_and_exit_refusal_is_the_same_sentence(mode):
    """The second entry point, asserted by EQUALITY on what it actually hands the caller.

    run_and_exit raises SystemExit carrying the whole result envelope as JSON — not an exit status,
    which is what the first version of this test claimed and why it settled for a containment check
    on printed output. Review measured that mistake: appending to this site left the two entry
    points saying different things with the whole suite green, which is the exact failure this pair
    of tests exists to close. Containment cannot see an append; equality can.
    """
    import json

    from _olaf_runtime import interactive_only_refusal, run_and_exit

    with pytest.raises(SystemExit) as excinfo:
        run_and_exit(mode, ["setup"], {"lakehouse_name": "LH_Demo"}, build_spark())

    envelope = json.loads(str(excinfo.value))
    assert envelope["status"] == "blocked"
    assert envelope["error"] == interactive_only_refusal(mode)
    # `message` is deliberately NOT the sentence here: the SystemExit payload swaps it for a short
    # pointer at the pipeline's activity output ("blocked — full reason in onelake_security_log"),
    # so `error` is the field carrying the reason. The in-process envelope keeps the sentence in
    # both, and the drift test below reads `error` for exactly this reason.
    assert envelope["message"].startswith("blocked — full reason in onelake_security_log")


def test_both_entry_points_cannot_drift_apart():
    """The pair, stated as one assertion — the property that actually matters here.

    The first version of this never read run_and_exit's output at all: it called it, discarded the
    result, and compared run_mode against the shared function. That made it a strict duplicate of
    the test above it, green under every mutation of the OTHER entry point, while its name and
    docstring both promised the opposite. Both sides are now read from what each call produced.
    """
    import json

    from _olaf_runtime import interactive_only_refusal, run_and_exit, run_mode

    for mode in sorted(INTERACTIVE_ONLY):
        from_run_mode = run_mode(mode, {"lakehouse_name": "LH_Demo"}, build_spark())["error"]
        with pytest.raises(SystemExit) as excinfo:
            run_and_exit(mode, ["setup"], {"lakehouse_name": "LH_Demo"}, build_spark())
        from_run_and_exit = json.loads(str(excinfo.value))["error"]

        assert from_run_mode == from_run_and_exit == interactive_only_refusal(mode)
