"""Fail-closed control-data boundary and durable incident-sentinel contracts."""

from dataclasses import replace
from pathlib import Path
from unittest import mock

import pytest

import _olaf_runtime as rt
from _fakes import (
    BACKUP_DIR,
    CONFIG_TABLE,
    CTL_MAPPING_HISTORY_DIR,
    GRP_READERS,
    LOG_TABLE,
    MAPPING_TABLE,
    MEMBER_TABLE,
    FakeFabricClient,
    build_spark,
    fake_role,
    lakehouse_writes,
    make_dep,
    ols_env,
    run_runtime_blackbox,
    run_generate,
    sample_config_rows,
    seed_sample_members,
    seed_workbook,
)


ATTESTATION = "change-review/2026-08-22#42"


def _boundary(client=None, attestation=ATTESTATION):
    return rt.ControlBoundary(
        client or FakeFabricClient([]),
        {
            "config_table": CONFIG_TABLE,
            "mapping_table": MAPPING_TABLE,
            "log_table": LOG_TABLE,
            "member_table": MEMBER_TABLE,
        },
        CTL_MAPPING_HISTORY_DIR,
        rt.PARAM_DEFAULTS["role_backup_dir"],
        attestation,
    )


@pytest.mark.parametrize(
    ("path", "overlaps"),
    [
        ("/", True),
        ("/Tables", True),
        ("/tables/OLAF", True),
        ("/Tables/olaf/onelake_security_config", True),
        ("/Tables/olaf/onelake_security_config/child", True),
        ("/FILES", True),
        ("/Files/security/private", True),
        ("//Files//Security//private//", True),
        ("/Files/security2", False),
        ("/Tables/olaf_business/x", False),
    ],
)
def test_reserved_overlap_is_segment_aware_case_insensitive_and_slash_normalized(path, overlaps):
    """A string-prefix comparator would falsely allow parents and block sibling prefixes."""
    assert _boundary().overlaps_reserved(path) is overlaps


def test_unparseable_configured_control_table_is_unknown_not_ignored():
    """Dropping an ambiguous control table from the reserved set would expose it silently."""
    with pytest.raises(rt.ControlDataGuardError, match="two-part"):
        rt.ControlBoundary(
            FakeFabricClient([]),
            {
                "config_table": "three.part.name",
                "mapping_table": MAPPING_TABLE,
                "log_table": LOG_TABLE,
                "member_table": MEMBER_TABLE,
            },
            CTL_MAPPING_HISTORY_DIR,
            rt.PARAM_DEFAULTS["role_backup_dir"],
            ATTESTATION,
        )


def test_control_boundary_requires_a_client_and_security_descendant_directories():
    tables = {
        "config_table": CONFIG_TABLE,
        "mapping_table": MAPPING_TABLE,
        "log_table": LOG_TABLE,
        "member_table": MEMBER_TABLE,
    }
    with pytest.raises(rt.ControlDataGuardError, match="live Fabric DAR client"):
        rt.ControlBoundary(None, tables, CTL_MAPPING_HISTORY_DIR, BACKUP_DIR, ATTESTATION)
    with pytest.raises(rt.ControlDataGuardError, match="mapping_history_dir"):
        rt.ControlBoundary(
            FakeFabricClient([]),
            tables,
            "Files/not-security/history",
            BACKUP_DIR,
            ATTESTATION,
        )


@pytest.mark.parametrize("path", ["", "Files\\security", "Files/security\x00bad"])
def test_malformed_dar_paths_are_never_normalized_into_a_safe_scope(path):
    with pytest.raises(rt.ControlDataGuardError, match="missing or malformed"):
        _boundary().normalize_path(path)


def test_desired_grant_overlapping_control_data_is_refused():
    """Removing the desired-side check would let OLAF publish its own principal state."""
    with pytest.raises(rt.ControlDataGuardError, match="reserved control-data"):
        _boundary().require_desired_safe(
            [{"role_name": "Readers", "scope_path": "/Files/security/export"}]
        )


def test_desired_grant_without_a_known_scope_is_refused_before_sentinel_creation(
    tmp_path, monkeypatch
):
    sentinel = tmp_path / "sentinel"
    monkeypatch.setattr(rt.ControlBoundary, "SENTINEL_FULL_PATH", str(sentinel))
    dep = make_dep(build_spark(), FakeFabricClient([]), "generate")
    with pytest.raises(rt.ControlDataGuardError, match="unknown scope"):
        dep._begin_sensitive("generate", desired_grants=[{"role_name": "Unknown"}])
    assert not sentinel.exists()


def test_dar_shape_classifier_refuses_every_ambiguous_control_boundary_case():
    """Unknown live shapes cannot be interpreted as an empty reader set."""
    boundary = _boundary()

    def role(path="/Tables/business/orders"):
        return fake_role("ShapeReaders", [path], [])

    malformed = []
    malformed.append(({}, "expected a role list"))
    malformed.append(([{}], "decisionRules shape"))

    bad_effect = role()
    bad_effect["decisionRules"][0]["effect"] = "Deny"
    malformed.append(([bad_effect], "effect is unknown"))

    bad_permissions = role()
    bad_permissions["decisionRules"][0]["permission"] = {}
    malformed.append(([bad_permissions], "permission shape"))

    bad_entry = role()
    bad_entry["decisionRules"][0]["permission"] = [None]
    malformed.append(([bad_entry], "permission entry"))

    duplicate_attribute = role()
    duplicate_attribute["decisionRules"][0]["permission"].append(
        {"attributeName": "Path", "attributeValueIncludedIn": ["/Tables/business/orders"]}
    )
    malformed.append(([duplicate_attribute], "attribute is duplicated"))

    bad_action_type = role()
    bad_action_type["decisionRules"][0]["permission"][1]["attributeValueIncludedIn"] = [None]
    malformed.append(([bad_action_type], "Action permission shape"))

    bad_action_name = role()
    bad_action_name["decisionRules"][0]["permission"][1]["attributeValueIncludedIn"] = ["Delete"]
    malformed.append(([bad_action_name], "Action permission shape"))

    bad_members = role("/Tables")
    bad_members["members"] = []
    malformed.append(([bad_members], "unknown members"))

    unknown_member_container = role("/Tables")
    unknown_member_container["members"] = {"other": []}
    malformed.append(([unknown_member_container], "unknown member container"))

    bad_entra = role("/Tables")
    bad_entra["members"] = {"microsoftEntraMembers": None}
    malformed.append(([bad_entra], "unknown Entra members"))

    nonempty_entra = role("/Tables")
    nonempty_entra["members"]["microsoftEntraMembers"] = [{"objectId": "test-object"}]
    malformed.append(([nonempty_entra], "grants read access"))

    for roles, message in malformed:
        with pytest.raises(rt.ControlDataGuardError, match=message):
            boundary.snapshot_from(roles, '"safe-etag"')

    no_action_on_nonoverlap = role()
    no_action_on_nonoverlap["decisionRules"][0]["permission"] = [
        {"attributeName": "Path", "attributeValueIncludedIn": ["/Tables/business/orders"]}
    ]
    assert boundary.snapshot_from([no_action_on_nonoverlap], '"safe-etag"').etag == '"safe-etag"'


def test_multiple_safe_decision_rules_are_all_classified_before_a_snapshot_is_approved():
    role = fake_role("TwoSafeRules", ["/Tables/business/orders"], [])
    role["decisionRules"].append(
        {
            "effect": "Permit",
            "permission": [
                {
                    "attributeName": "Path",
                    "attributeValueIncludedIn": ["/Files/business/drop"],
                },
                {"attributeName": "Action", "attributeValueIncludedIn": ["Read"]},
            ],
        }
    )
    assert _boundary().snapshot_from([role], '"safe-etag"').roles[0]["name"] == "TwoSafeRules"


def test_multiple_overlapping_empty_static_rules_are_safe_after_each_is_classified():
    """A safe empty role can cover more than one reserved path without hiding another rule."""
    role = fake_role("EmptyControlAuditor", ["/Tables"], [])
    role["decisionRules"].append(
        {
            "effect": "Permit",
            "permission": [
                {
                    "attributeName": "Path",
                    "attributeValueIncludedIn": ["/Files/security"],
                },
                {"attributeName": "Action", "attributeValueIncludedIn": ["Read"]},
            ],
        }
    )
    snapshot = _boundary().snapshot_from([role], '"safe-etag"')
    assert snapshot.roles[0]["name"] == "EmptyControlAuditor"


def test_dynamic_fabric_membership_over_reserved_scope_blocks_even_when_returned_empty():
    """Treating an empty returned dynamic container as no readers is a fail-open."""
    role = fake_role("BroadReaders", ["/Files"], [])
    role["members"]["fabricItemMembers"] = []
    client = FakeFabricClient([role])
    with pytest.raises(rt.ControlDataGuardError, match="dynamic fabricItemMembers"):
        _boundary(client).snapshot()


def test_static_entra_member_on_root_tables_blocks_by_shape_not_role_name():
    role = fake_role("OrdinaryBusinessRole", ["/Tables"], [GRP_READERS])
    with pytest.raises(rt.ControlDataGuardError, match="grants read access"):
        _boundary(FakeFabricClient([role])).snapshot()


def test_nonoverlapping_sibling_prefix_and_empty_static_members_do_not_false_positive():
    role = fake_role("EmptyReaders", ["/Tables/olaf_business/x"], [])
    assert _boundary(FakeFabricClient([role])).snapshot().etag


def test_non_string_permission_path_is_unknown_and_blocks():
    role = fake_role("MalformedPath", ["/Tables/business/x"], [])
    role["decisionRules"][0]["permission"][0]["attributeValueIncludedIn"] = [
        {"unexpected": "/Tables"}
    ]
    with pytest.raises(rt.ControlDataGuardError, match="Path"):
        _boundary(FakeFabricClient([role])).snapshot()


def test_snapshot_without_target_ids_is_unknown_and_blocks():
    client = FakeFabricClient([], workspace_id="", item_id="")
    with pytest.raises(rt.ControlDataGuardError, match="target"):
        _boundary(client).snapshot()


def test_attestation_never_overrides_an_unsafe_dar_snapshot():
    role = fake_role("BroadReaders", ["/Files"], [GRP_READERS])
    with pytest.raises(rt.ControlDataGuardError, match="reserved control data"):
        _boundary(FakeFabricClient([role]), attestation="review/approved").begin("setup")


def test_unknown_permission_shape_on_overlapping_rule_blocks():
    """Skipping an unclassified action would certify a snapshot OLAF did not understand."""
    role = fake_role("BroadReaders", ["/Tables"], [])
    role["decisionRules"][0]["permission"] = [
        {"attributeName": "Path", "attributeValueIncludedIn": ["/Tables"]}
    ]
    with pytest.raises(rt.ControlDataGuardError, match="Action"):
        _boundary(FakeFabricClient([role])).snapshot()


def test_missing_etag_blocks_a_clean_snapshot():
    """A role list without its collection version cannot authorize a sensitive write."""

    class NoETag(FakeFabricClient):
        def list_roles_quick(self):
            roles = super().list_roles_quick()
            self.roles_etag = None
            return roles

    with pytest.raises(rt.ControlDataGuardError, match="ETag"):
        _boundary(NoETag([])).snapshot()


def test_a_run_without_attestation_proceeds_but_never_claims_isolation(tmp_path, monkeypatch):
    """The evidence reference is optional, and the record must stay honest about that.

    It used to be mandatory: an absent reference refused the operation outright. That gated
    nothing real — the value is never verified, so a single character satisfied it — while
    breaking every caller that had no reason to supply one. The check that matters is not whether
    the run was refused, it is whether the record claims something nobody established.

    So the run proceeds, and `isolation_state` answers `unknown` rather than `attested`. Never
    "safe": OLAF cannot verify same-lakehouse isolation, and the absence of a claim is not a
    denial of one."""
    sentinel = tmp_path / "sentinel"
    monkeypatch.setattr(rt.ControlBoundary, "SENTINEL_FULL_PATH", str(sentinel))

    boundary = _boundary(attestation="")
    assert boundary.isolation_state() == "unknown"
    boundary.begin("setup")
    assert sentinel.exists(), "the sentinel still guards the write itself"

    assert _boundary(attestation="review/approved").isolation_state() == "attested"
    assert _boundary(attestation="has spaces").isolation_state() == "unknown", (
        "a malformed reference is no more of a claim than an absent one"
    )


def test_snapshot_is_immutable_and_reconfirmation_does_not_refresh_after_change(
    tmp_path, monkeypatch
):
    """Refreshing the approved snapshot after a race would authorize an unreviewed state."""
    sentinel = tmp_path / "sentinel"
    monkeypatch.setattr(rt.ControlBoundary, "SENTINEL_FULL_PATH", str(sentinel))
    client = FakeFabricClient([], enforce_etag=True)
    boundary = _boundary(client)
    approved = boundary.snapshot()
    client.simulate_external_edit()
    with pytest.raises(rt.ControlDataGuardError, match="changed after the approved snapshot"):
        boundary.begin("generate", snapshot=approved)
    assert sentinel.read_text(encoding="utf-8") == rt.ControlBoundary.SENTINEL_CONTENT


def test_reserved_digest_mismatch_refuses_before_creating_a_sentinel(tmp_path, monkeypatch):
    sentinel = tmp_path / "sentinel"
    monkeypatch.setattr(rt.ControlBoundary, "SENTINEL_FULL_PATH", str(sentinel))
    boundary = _boundary()
    approved = boundary.snapshot()
    with pytest.raises(rt.ControlDataGuardError, match="reserved control-data set changed"):
        boundary.begin("setup", snapshot=replace(approved, reserved_digest="different"))
    assert not sentinel.exists()


def test_sentinel_is_exclusively_created_read_back_and_only_safe_owner_clears_it(
    tmp_path, monkeypatch
):
    """A non-exclusive or unverified marker cannot serialize sensitive writers."""
    sentinel = tmp_path / "sentinel"
    monkeypatch.setattr(rt.ControlBoundary, "SENTINEL_FULL_PATH", str(sentinel))
    boundary = _boundary()
    lease = boundary.begin("plan")
    assert sentinel.read_text(encoding="utf-8") == rt.ControlBoundary.SENTINEL_CONTENT
    with pytest.raises(rt.ControlDataGuardError, match="sentinel already exists"):
        _boundary().begin("apply")
    lease.postcheck()
    lease.clear()
    assert not sentinel.exists()


def test_lease_prewrite_refuses_a_dar_change_before_a_later_sensitive_write(tmp_path, monkeypatch):
    """Dropping the just-in-time check would let a stale lease write local control data."""
    sentinel = tmp_path / "sentinel"
    monkeypatch.setattr(rt.ControlBoundary, "SENTINEL_FULL_PATH", str(sentinel))
    client = FakeFabricClient([])
    lease = _boundary(client).begin("generate")
    client.simulate_external_edit()

    with pytest.raises(rt.ControlDataGuardError, match="changed after the approved snapshot"):
        lease.prewrite()

    assert sentinel.read_text(encoding="utf-8") == rt.ControlBoundary.SENTINEL_CONTENT


def test_allow_dar_change_postcheck_accepts_a_lease_that_did_not_write_dar(tmp_path, monkeypatch):
    """The postcheck may authorize a nested no-op without inventing a DAR mutation."""
    sentinel = tmp_path / "sentinel"
    monkeypatch.setattr(rt.ControlBoundary, "SENTINEL_FULL_PATH", str(sentinel))
    lease = _boundary().begin("plan")

    post = lease.postcheck(allow_dar_change=True)
    assert post.etag == lease.snapshot.etag
    assert post.roles_digest == lease.snapshot.roles_digest
    lease.clear()


def test_sensitive_lease_without_an_audit_writer_still_has_a_safe_lifecycle():
    """Pure control-boundary callers must not require an audit object to release safely."""
    dep = make_dep(build_spark(), FakeFabricClient([]), "setup")
    dep.audit = None
    lease = dep._begin_sensitive("setup")

    dep._finish_sensitive(lease)

    assert dep._active_leases == []
    assert dep._control_depth == 0


def test_sensitive_lease_refuses_out_of_order_release_and_retains_the_sentinel():
    """A corrupted stack must never clear the marker just because the DAR still matches."""
    dep = make_dep(build_spark(), FakeFabricClient([]), "setup")
    lease = dep._begin_sensitive("setup")
    dep._active_leases.clear()

    with pytest.raises(rt.ControlDataGuardError, match="lease ordering is invalid"):
        dep._finish_sensitive(lease)

    assert Path(rt.ControlBoundary.SENTINEL_FULL_PATH).exists()


def test_sentinel_readback_failure_blocks_and_retains_incident_marker(tmp_path, monkeypatch):
    sentinel = tmp_path / "sentinel"
    boundary = _boundary()
    monkeypatch.setattr(rt.ControlBoundary, "SENTINEL_FULL_PATH", str(sentinel))
    original = boundary._read_sentinel
    calls = 0

    def fail_first_readback():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise rt.ControlDataGuardError("simulated sentinel read-back failure")
        return original()

    monkeypatch.setattr(boundary, "_read_sentinel", fail_first_readback)
    with pytest.raises(rt.ControlDataGuardError, match="read-back failure"):
        boundary.begin("apply")
    assert sentinel.read_text(encoding="utf-8") == rt.ControlBoundary.SENTINEL_CONTENT


def test_sentinel_create_read_and_clear_io_failures_stay_fail_closed(tmp_path, monkeypatch):
    sentinel = tmp_path / "sentinel"
    monkeypatch.setattr(rt.ControlBoundary, "SENTINEL_FULL_PATH", str(sentinel))
    boundary = _boundary()
    with mock.patch("builtins.open", side_effect=PermissionError("write denied")):
        with pytest.raises(rt.ControlDataGuardError, match="could not be created"):
            boundary._create_sentinel()
    assert not sentinel.exists()

    with pytest.raises(rt.ControlDataGuardError, match="absent or unreadable"):
        boundary._read_sentinel()

    sentinel.write_text(rt.ControlBoundary.SENTINEL_CONTENT, encoding="utf-8")
    lease = rt.ControlBoundaryLease(boundary, "setup", boundary.snapshot(), owns_sentinel=True)
    with pytest.raises(rt.ControlDataGuardError, match="only the owning"):
        boundary.clear_owned(lease)
    lease.post_snapshot = boundary.snapshot()
    with mock.patch("os.remove", side_effect=PermissionError("remove denied")):
        with pytest.raises(rt.ControlDataGuardError, match="could not be cleared"):
            boundary.clear_owned(lease)
    with mock.patch("os.remove", side_effect=PermissionError("remove denied")):
        with pytest.raises(rt.ControlDataGuardError, match="could not be cleared"):
            boundary.clear_incident("access-review/sentinel-io")
    assert sentinel.exists()


def test_postwrite_boundary_error_exposes_recovery_facts():
    err = rt.PostWriteBoundaryError(
        "apply", "Files/security/role-backups/test.json", RuntimeError("race")
    )
    assert err.as_data() == {
        "operation": "apply",
        "backup_path": "Files/security/role-backups/test.json",
        "possible_exposure": True,
    }


def test_unknown_write_metadata_best_effort_never_replaces_the_original_exception():
    class AttributeRefusingError(Exception):
        def __setattr__(self, name, value):
            raise AttributeError(name)

    original = AttributeRefusingError("write outcome unknown")
    dep = make_dep(build_spark(), FakeFabricClient([]), "apply")
    assert (
        dep._mark_unknown_write(original, "apply", "Files/security/role-backups/test.json")
        is original
    )


def test_incident_clearance_refuses_when_no_durable_audit_log_exists():
    dep = make_dep(build_spark(), FakeFabricClient([]), "setup")
    dep.audit = None
    with pytest.raises(rt.UsageError, match="durable audit log"):
        dep.clear_incident("access-review/no-audit")


def test_incident_clearance_prewrite_blocks_audit_when_dar_changes(tmp_path, monkeypatch):
    """Clearance must not append its audit evidence after its DAR approval goes stale."""
    sentinel = tmp_path / "sentinel"
    sentinel.write_text(rt.ControlBoundary.SENTINEL_CONTENT, encoding="utf-8")
    monkeypatch.setattr(rt.ControlBoundary, "SENTINEL_FULL_PATH", str(sentinel))
    spark, client = build_spark(), FakeFabricClient([])
    dep = make_dep(spark, client, "setup")
    original_write = dep.audit.write

    def rotate_before_audit(rows):
        client.simulate_external_edit()
        return original_write(rows)

    monkeypatch.setattr(dep.audit, "write", rotate_before_audit)
    with pytest.raises(rt.ControlDataGuardError, match="changed after the approved snapshot"):
        dep.clear_incident("access-review/clearance-race")

    assert sentinel.exists()
    assert not [
        row for row in spark._store.get(LOG_TABLE, []) if row.get("action") == "sentinel_clearance"
    ]


def test_nested_sensitive_stage_re_reads_outer_owned_sentinel(tmp_path, monkeypatch):
    sentinel = tmp_path / "sentinel"
    monkeypatch.setattr(rt.ControlBoundary, "SENTINEL_FULL_PATH", str(sentinel))
    boundary = _boundary()
    outer = boundary.begin("rollback")
    sentinel.write_text("tampered\n", encoding="utf-8")
    with pytest.raises(rt.ControlDataGuardError, match="read-back content"):
        boundary.begin("apply", sentinel_already_owned=True)
    assert outer.owns_sentinel is True


def test_explicit_clearance_requires_new_safe_evidence_and_an_access_review(tmp_path, monkeypatch):
    """Generic cleanup or an unreviewed delete must not erase the cross-run incident marker."""
    sentinel = tmp_path / "sentinel"
    sentinel.write_text("OLAF_CONTROL_DATA_WRITE_IN_PROGRESS_V1\n", encoding="utf-8")
    monkeypatch.setattr(rt.ControlBoundary, "SENTINEL_FULL_PATH", str(sentinel))
    boundary = _boundary()
    with pytest.raises(rt.ControlDataGuardError, match="access review"):
        boundary.clear_incident("")
    result = boundary.clear_incident("access-review/2026-08-22#7")
    assert result["cleared"] is True
    assert result["exposure_remediated"] is False
    assert not sentinel.exists()


def test_sentinel_content_is_constant_and_contains_no_runtime_identifiers():
    """Adding operation or target data to the marker would create the exposure it guards."""
    content = rt.ControlBoundary.SENTINEL_CONTENT
    assert content == "OLAF_CONTROL_DATA_WRITE_IN_PROGRESS_V1\n"
    for prohibited in ("workspace", "lakehouse", "tenant", "role", "attestation", "run"):
        assert prohibited not in content.lower()


def test_first_setup_without_attestation_succeeds_and_records_unknown():
    """Bootstrapping without an evidence reference is allowed; overstating it is not."""
    spark = build_spark()
    outcome = run_runtime_blackbox(
        "setup",
        spark,
        params={"control_data_isolation_attestation": ""},
    )
    assert outcome.envelope["status"] != "blocked"
    assert outcome.envelope["data"]["workspace_isolation"] == "unknown"
    assert spark._store != {}, "setup still creates the control tables"


@pytest.mark.parametrize("rebuild", [False, True])
def test_existing_migrating_and_rebuild_setup_report_unknown_without_attestation(rebuild):
    """A rerun or rebuild records the same honest unknown a first setup does."""
    spark = build_spark()
    run_runtime_blackbox("setup", spark)
    outcome = run_runtime_blackbox(
        "setup",
        spark,
        params={
            "rebuild": rebuild,
            "control_data_isolation_attestation": "",
        },
    )
    assert outcome.envelope["status"] != "blocked"
    assert outcome.envelope["data"]["workspace_isolation"] == "unknown"


def _authored_runtime():
    spark = build_spark()
    client = FakeFabricClient([])
    assert run_runtime_blackbox("setup", spark, client=client).envelope["status"] == "success"
    spark._store[CONFIG_TABLE] = sample_config_rows()
    seed_sample_members(spark)
    return spark, client


def _simulate_existing_sentinel(monkeypatch):
    def refuse(_boundary):
        raise rt.ControlDataGuardError(
            "control-data incident sentinel already exists; explicit reviewed clearance is required"
        )

    monkeypatch.setattr(rt.ControlBoundary, "_create_sentinel", refuse)


def test_existing_sentinel_blocks_first_setup_before_schema_or_audit(monkeypatch):
    spark, client = build_spark(), FakeFabricClient([])
    _simulate_existing_sentinel(monkeypatch)

    outcome = run_runtime_blackbox("setup", spark, client=client)

    assert outcome.envelope["status"] == "blocked"
    assert "sentinel already exists" in outcome.envelope["message"]
    assert spark._store == {}
    assert spark._writes == []


def test_setup_prewrite_blocks_schema_creation_after_the_lease_becomes_stale(monkeypatch):
    """Without a just-in-time check, setup creates tables after its DAR authority changed."""
    spark, client = build_spark(), FakeFabricClient([])
    dep = make_dep(spark, client, "setup")
    begin = dep._begin_sensitive

    def begin_then_rotate(*args, **kwargs):
        lease = begin(*args, **kwargs)
        client.simulate_external_edit()
        return lease

    monkeypatch.setattr(dep, "_begin_sensitive", begin_then_rotate)
    with pytest.raises(rt.ControlDataGuardError, match="changed after the approved snapshot"):
        dep.setup()

    assert spark._store == {}
    assert spark._writes == []


def test_setup_prewrite_blocks_its_audit_append_after_the_schema_is_safe(monkeypatch):
    """A DAR edit between schema work and the audit append must not create a misleading row."""
    spark, client = build_spark(), FakeFabricClient([])
    make_dep(spark, client, "setup").setup()
    dep = make_dep(spark, client, "setup")
    before = [dict(row) for row in spark._store[LOG_TABLE]]
    write = dep.audit.write

    def rotate_then_write(rows):
        client.simulate_external_edit()
        return write(rows)

    monkeypatch.setattr(dep.audit, "write", rotate_then_write)
    with pytest.raises(rt.ControlDataGuardError, match="changed after the approved snapshot"):
        dep.setup()

    assert spark._store[LOG_TABLE] == before


def test_generate_prewrite_blocks_the_first_history_or_mapping_write_after_a_dar_edit(monkeypatch):
    """The history artifact is a sensitive write and cannot be the gap before mapping commit."""
    spark, client = _authored_runtime()
    dep = make_dep(spark, client, "generate")
    export = rt.Deployment._export_history_csv
    log_before = [dict(row) for row in spark._store[LOG_TABLE]]

    def rotate_then_export(self, *args, **kwargs):
        client.simulate_external_edit()
        return export(self, *args, **kwargs)

    monkeypatch.setattr(rt.Deployment, "_export_history_csv", rotate_then_export)
    with pytest.raises(rt.ControlDataGuardError, match="changed after the approved snapshot"):
        run_generate(dep)

    assert spark._store[MAPPING_TABLE] == []
    assert spark._store[LOG_TABLE] == log_before


def test_generate_prewrite_blocks_mapping_and_audit_after_a_safe_history_write(monkeypatch):
    """Later generate writes need their own check; an earlier artifact does not authorize them."""
    spark, client = _authored_runtime()
    dep = make_dep(spark, client, "generate")
    export = rt.Deployment._export_history_csv
    log_before = [dict(row) for row in spark._store[LOG_TABLE]]

    def export_then_rotate(self, *args, **kwargs):
        result = export(self, *args, **kwargs)
        client.simulate_external_edit()
        return result

    monkeypatch.setattr(rt.Deployment, "_export_history_csv", export_then_rotate)
    with pytest.raises(rt.ControlDataGuardError, match="changed after the approved snapshot"):
        run_generate(dep)

    assert spark._store[MAPPING_TABLE] == []
    assert spark._store[LOG_TABLE] == log_before


def test_plan_prewrite_blocks_its_audit_append_after_dar_changes(monkeypatch):
    """A reviewed read from plan entry cannot authorize the later durable plan record."""
    spark, client = _authored_runtime()
    run_generate(make_dep(spark, client, "generate"))
    dep = make_dep(spark, client, "plan")
    before = [dict(row) for row in spark._store[LOG_TABLE]]
    write = dep.audit.write

    def rotate_then_write(rows):
        client.simulate_external_edit()
        return write(rows)

    monkeypatch.setattr(dep.audit, "write", rotate_then_write)
    with pytest.raises(rt.ControlDataGuardError, match="changed after the approved snapshot"):
        dep.plan()

    assert spark._store[LOG_TABLE] == before


def test_setup_postwrite_etag_race_is_typed_changed_and_retains_sentinel():
    class RacingClient(FakeFabricClient):
        def __init__(self):
            super().__init__([])
            self.boundary_reads = 0

        def list_roles_quick(self):
            self.boundary_reads += 1
            if self.boundary_reads == 12:
                self.simulate_external_edit()
            return super().list_roles_quick()

    spark, client, lakehouse = build_spark(), RacingClient(), {}
    with lakehouse_writes(store=lakehouse), pytest.raises(rt.PostWriteBoundaryError) as excinfo:
        make_dep(spark, client, "setup").setup()

    err = excinfo.value
    assert err.changed is True and err.possible_exposure is True
    for table in (CONFIG_TABLE, MAPPING_TABLE, LOG_TABLE, MEMBER_TABLE):
        assert table in err.backup_path and table in spark._store
    assert lakehouse[rt.ControlBoundary.SENTINEL_FULL_PATH] == rt.ControlBoundary.SENTINEL_CONTENT


def test_runtime_envelope_preserves_postwrite_boundary_recovery_facts():
    """Pipeline callers receive an error, never a misleading blocked verdict, after setup writes."""

    class RacingClient(FakeFabricClient):
        def __init__(self):
            super().__init__([])
            self.boundary_reads = 0

        def list_roles_quick(self):
            self.boundary_reads += 1
            if self.boundary_reads == 12:
                self.simulate_external_edit()
            return super().list_roles_quick()

    spark, client = build_spark(), RacingClient()
    outcome = run_runtime_blackbox("setup", spark, client=client)

    assert outcome.envelope["status"] == "error"
    assert outcome.envelope["changed"] is True
    assert "PostWriteBoundaryError" in outcome.envelope["error"]
    assert outcome.envelope["data"]["possible_exposure"] is True
    assert outcome.raised is not None
    assert set(spark._store) == {CONFIG_TABLE, MAPPING_TABLE, LOG_TABLE, MEMBER_TABLE}


def test_generate_postwrite_etag_race_is_typed_and_names_export_artifact():
    class RacingClient(FakeFabricClient):
        def __init__(self):
            super().__init__([])
            self.race = False
            self.boundary_reads = 0

        def list_roles_quick(self):
            if self.race:
                self.boundary_reads += 1
                if self.boundary_reads == 8:
                    self.simulate_external_edit()
            return super().list_roles_quick()

    spark, client, lakehouse = build_spark(), RacingClient(), {}
    make_dep(spark, client, "setup").setup()
    spark._store[CONFIG_TABLE] = sample_config_rows()
    seed_sample_members(spark)
    client.race = True
    with lakehouse_writes(store=lakehouse), pytest.raises(rt.PostWriteBoundaryError) as excinfo:
        run_generate(make_dep(spark, client, "generate"))

    err = excinfo.value
    assert err.changed is True and "mapping-history" in err.backup_path
    assert spark._store[MAPPING_TABLE]
    assert lakehouse[rt.ControlBoundary.SENTINEL_FULL_PATH] == rt.ControlBoundary.SENTINEL_CONTENT


def test_plan_postwrite_etag_race_is_typed_and_names_affected_log_table():
    class RacingClient(FakeFabricClient):
        def __init__(self):
            super().__init__([])
            self.race = False
            self.boundary_reads = 0

        def list_roles_quick(self):
            if self.race:
                self.boundary_reads += 1
                if self.boundary_reads == 4:
                    self.simulate_external_edit()
            return super().list_roles_quick()

    spark, client, lakehouse = build_spark(), RacingClient(), {}
    make_dep(spark, client, "setup").setup()
    spark._store[CONFIG_TABLE] = sample_config_rows()
    seed_sample_members(spark)
    run_generate(make_dep(spark, client, "generate"))
    client.race = True
    with lakehouse_writes(store=lakehouse), pytest.raises(rt.PostWriteBoundaryError) as excinfo:
        make_dep(spark, client, "plan").plan()

    err = excinfo.value
    assert err.changed is True and LOG_TABLE in err.backup_path
    assert [row for row in spark._store[LOG_TABLE] if row.get("mode") == "plan"]
    assert lakehouse[rt.ControlBoundary.SENTINEL_FULL_PATH] == rt.ControlBoundary.SENTINEL_CONTENT


def test_existing_sentinel_blocks_reset_before_backup_intent_or_put(monkeypatch):
    spark = build_spark()
    client = FakeFabricClient(
        [fake_role("BusinessReaders", ["/Tables/sales/orders"], [GRP_READERS])]
    )
    lakehouse = {}
    with ols_env(spark, client, store=lakehouse):
        rt.OLAF.setup()
        log_before = [dict(row) for row in spark._store[LOG_TABLE]]
        _simulate_existing_sentinel(monkeypatch)
        with pytest.raises(rt.ControlDataGuardError, match="sentinel already exists"):
            rt.OLAF.reset()

    assert client.put_calls == []
    assert spark._store[LOG_TABLE] == log_before
    assert not [path for path in lakehouse if BACKUP_DIR in path]


def test_existing_sentinel_blocks_generate_rejection_audit_before_any_sensitive_write(monkeypatch):
    spark = build_spark()
    client = FakeFabricClient([])
    assert run_runtime_blackbox("setup", spark, client=client).envelope["status"] == "success"
    log_before = [dict(row) for row in spark._store[LOG_TABLE]]
    _simulate_existing_sentinel(monkeypatch)

    outcome = run_runtime_blackbox("generate", spark, client=client)

    assert outcome.envelope["status"] == "blocked"
    assert "sentinel already exists" in outcome.envelope["message"]
    assert spark._store[LOG_TABLE] == log_before
    assert spark._store[MAPPING_TABLE] == []


def test_existing_sentinel_blocks_stale_plan_rejection_audit_before_sensitive_write(monkeypatch):
    spark, client = _authored_runtime()
    assert run_runtime_blackbox("generate", spark, client=client).envelope["status"] == "success"
    spark._store[CONFIG_TABLE][0]["role_name"] = "ChangedReaders"
    log_before = [dict(row) for row in spark._store[LOG_TABLE]]
    _simulate_existing_sentinel(monkeypatch)

    outcome = run_runtime_blackbox("plan", spark, client=client)

    assert outcome.envelope["status"] == "blocked"
    assert "sentinel already exists" in outcome.envelope["message"]
    assert spark._store[LOG_TABLE] == log_before


def test_existing_sentinel_blocks_unplanned_apply_rejection_audit_and_live_write(monkeypatch):
    spark, client = _authored_runtime()
    assert run_runtime_blackbox("generate", spark, client=client).envelope["status"] == "success"
    log_before = [dict(row) for row in spark._store[LOG_TABLE]]
    _simulate_existing_sentinel(monkeypatch)

    outcome = run_runtime_blackbox("apply", spark, client=client)

    assert outcome.envelope["status"] == "blocked"
    assert "sentinel already exists" in outcome.envelope["message"]
    assert spark._store[LOG_TABLE] == log_before
    assert client.put_calls == []


def test_generate_without_attestation_still_records_unknown_isolation():
    """Generate exports principal ids and rewrites the lock file, and it may do so without an
    evidence reference — but the row it writes must not claim one was given."""
    spark, client = _authored_runtime()
    outcome = run_runtime_blackbox(
        "generate",
        spark,
        client=client,
        params={"control_data_isolation_attestation": ""},
    )
    assert outcome.envelope["status"] != "blocked"
    assert outcome.envelope["data"]["workspace_isolation"] == "unknown"
    assert spark._store[MAPPING_TABLE] != [], "generate still writes the mapping"


def test_plan_and_apply_each_record_isolation_per_run():
    """Each sensitive mode reports its OWN isolation state; a prior mode's evidence never carries
    into the next one, and its absence never reads as attested."""
    spark, client = _authored_runtime()
    assert run_runtime_blackbox("generate", spark, client=client).envelope["status"] == "success"

    plan = run_runtime_blackbox(
        "plan",
        spark,
        client=client,
        params={"control_data_isolation_attestation": ""},
    )
    assert plan.envelope["status"] != "blocked"
    assert plan.envelope["data"]["workspace_isolation"] == "unknown"

    # An attested plan does not make the following apply attested.
    assert run_runtime_blackbox("plan", spark, client=client).envelope["status"] == "success"
    apply = run_runtime_blackbox(
        "apply",
        spark,
        client=client,
        params={"control_data_isolation_attestation": ""},
    )
    assert apply.envelope["status"] != "blocked"
    assert apply.envelope["data"]["workspace_isolation"] == "unknown"
    assert client.put_calls, "apply still submits the request"


def test_reset_without_attestation_still_submits_and_records_unknown():
    """reset is the most destructive DAR call there is, and the evidence reference never stopped
    it: the value is unverified, so requiring it only taught callers to type something. What must
    hold is that the record of an unattested reset says so."""
    spark = build_spark()
    client = FakeFabricClient(
        [fake_role("BusinessReaders", ["/Tables/sales/orders"], [GRP_READERS])]
    )
    with ols_env(spark, client):
        rt.OLAF.setup()
        rt.OLAF._base_params["control_data_isolation_attestation"] = ""
        rt.OLAF.reset()
    assert client.put_calls, "reset still submits the empty payload"
    assert [row for row in spark._store[LOG_TABLE] if row.get("action") == "push"], (
        "the push is still recorded"
    )


def test_load_config_reads_the_workbook_without_an_attestation_and_records_unknown():
    """The evidence reference no longer gates the read. The ordering it protected still holds —
    the control-data boundary is established before any principal data is touched — but that is
    the sentinel and the DAR snapshot doing the work, which is what actually verifies something."""
    spark = build_spark()
    workbook_rows = [
        {column: row.get(column) for column in rt.CONFIG_AUTHOR_COLUMNS}
        for row in sample_config_rows()
    ]
    calls = seed_workbook(config=workbook_rows)
    with ols_env(spark):
        rt.OLAF.setup()
        rt.OLAF._base_params["control_data_isolation_attestation"] = ""
        rt.OLAF.load_config("config", "Files/security/config.xlsx", sheet="config")
    assert calls, "the workbook is read"


def test_a_caller_that_never_heard_of_the_attestation_still_completes_the_chain():
    """The whole point of making the evidence reference optional: an existing driver notebook must
    keep working with no code change at all.

    This is not covered by the rest of the suite, and the reason is a trap worth naming.
    `tests/_fakes.py` injects CONTROL_ATTESTATION into `run_runtime_blackbox`, `ols_env`, and
    `make_dep`, so every lifecycle and pipeline test runs WITH a reference even though not one of
    them mentions it. A suite that green-lights the optional path while silently supplying the
    value proves nothing about a caller that supplies none.

    So this drives setup -> generate -> plan -> apply the way a pipeline does, with the parameter
    explicitly blanked at every step, and asserts the chain completes and the DAR write lands.
    """
    spark, client = _authored_runtime()
    blank = {"control_data_isolation_attestation": ""}

    for mode in ("generate", "plan", "apply"):
        outcome = run_runtime_blackbox(mode, spark, client=client, params=blank)
        assert outcome.envelope["status"] == "success", (
            f"{mode} failed for a caller supplying no attestation: {outcome.envelope}"
        )
        assert outcome.envelope["data"]["workspace_isolation"] == "unknown", (
            f"{mode} claimed isolation nobody attested to"
        )

    assert client.put_calls, "apply never submitted the DAR request"


def test_rollback_without_an_attestation_is_never_blocked_for_the_missing_reference():
    """rollback keeps its own guards — a saved plan and no since-plan drift — and neither is the
    evidence reference. Chaining it after an apply would trip the DRIFT gate instead, which is a
    different refusal and a working one, so this asserts on the REASON rather than on success:
    whatever stops a rollback, it must not be the absence of an attestation."""
    spark, client = _authored_runtime()
    spark._history_rows = [{"version": 1}, {"version": 2}]
    assert run_runtime_blackbox("generate", spark, client=client).envelope["status"] == "success"

    rollback = run_runtime_blackbox(
        "rollback",
        spark,
        client=client,
        params={
            "rollback_reason": "reviewed rollback",
            "control_data_isolation_attestation": "",
        },
    )
    message = str(rollback.envelope.get("message", "")) + str(rollback.envelope.get("error", ""))
    assert "attestation" not in message.lower(), (
        f"rollback still refuses over the optional evidence reference: {message}"
    )


def test_a_validation_refusal_does_not_strand_the_next_operation():
    """A blocked outcome that never authorized a write leaves a KNOWN state — nothing happened —
    so it must not leave an incident marker stranding everything after it.

    Observed on live Fabric, then reproduced here: a `generate` refused for a config typo
    (`matched 0 tables`) held its lease, and the very next `plan` came back
    "control-data incident sentinel already exists; explicit reviewed clearance is required".
    A config typo is the most common failure there is, and an incident marker that fires on typos
    is one operators learn to clear without reading — which destroys the signal the sentinel
    exists to carry.

    Genuine uncertainty still holds it: a post-write race has no idea what landed, and its own
    tests pin that. The line is whether the lease ever authorized a write, not whether the
    operation succeeded.

    Asserts on the NEXT operation rather than on the sentinel file, because that is the behaviour
    an operator actually meets, and because the file alone did not tell the whole story — the
    in-session lease does.
    """
    spark, client = _authored_runtime()
    for row in spark._store[CONFIG_TABLE]:
        row["include_tables"] = "nope.does_not_exist"

    with ols_env(spark, client):
        rt.OLAF.generate()
        blocked = rt.OLAF.last_result
        assert blocked["status"] == "blocked"
        assert "matched 0 tables" in blocked["message"], blocked["message"][:160]

        rt.OLAF.plan()
        after = rt.OLAF.last_result

    assert "sentinel already exists" not in str(after.get("message", "")), (
        "a refusal that never authorized a write stranded the next operation behind an "
        f"incident marker: {after.get('message')}"
    )


def test_release_unwritten_refuses_any_lease_that_reached_a_write():
    """The release exists for refusals that changed nothing. A lease that authorized a write kept
    its marker before this change and must keep it after: that state is the uncertain one."""
    boundary = _boundary()
    lease = boundary.begin("apply")
    lease.prewrite()  # authorizes the write
    assert boundary.release_unwritten(lease) is False

    # ...and a lease belonging to a different boundary is never this one's to release
    other = _boundary()
    fresh = _boundary().begin.__self__  # the boundary object itself
    assert other.release_unwritten(lease) is False
    assert fresh is not None


def test_release_unwritten_leaves_the_marker_when_removal_fails(monkeypatch):
    """An unremovable sentinel is reported by leaving it in place. Guessing that it went away
    would hand the next run a clean slate the filesystem never confirmed."""
    import os as _os

    boundary = _boundary()
    lease = boundary.begin("generate")

    def boom(_path):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(_os, "remove", boom)
    assert boundary.release_unwritten(lease) is False


def test_release_stops_at_the_first_lease_that_wrote(tmp_path, monkeypatch):
    """Leases nest. The unwinding stops at the first one that authorized a write, so an outer
    operation mid-write never has its marker taken away by an inner refusal."""
    spark, client = _authored_runtime()
    dep = make_dep(spark, client, "apply")
    outer = dep._begin_sensitive("apply")
    outer.prewrite()  # the outer operation is mid-write
    inner = dep._begin_sensitive("generate")  # nested, refuses without writing

    dep.release_unwritten_leases()

    assert dep._active_leases == [outer], "the writing lease must survive the unwind"
    assert inner not in dep._active_leases


def test_release_handles_a_deployment_with_no_audit():
    """The unwind rewires the audit's prewrite hook to whatever lease is left. A deployment
    without an audit has no hook to rewire, and must not fall over trying."""
    spark, client = _authored_runtime()
    dep = make_dep(spark, client, "generate")
    lease = dep._begin_sensitive("generate")
    dep.audit = None

    dep.release_unwritten_leases()

    assert dep._active_leases == []
    assert lease not in dep._active_leases


def test_a_failing_release_never_masks_the_refusal(monkeypatch):
    """Cleanup is best-effort by construction. If handing the lease back fails, the caller must
    still receive the refusal it was given — the message is what an operator acts on, and losing
    it to a bookkeeping error would be strictly worse than a stale marker."""

    def boom(self):
        raise RuntimeError("release exploded")

    monkeypatch.setattr(rt.Deployment, "release_unwritten_leases", boom)

    spark, client = _authored_runtime()
    for row in spark._store[CONFIG_TABLE]:
        row["include_tables"] = "nope.does_not_exist"

    outcome = run_runtime_blackbox("generate", spark, client=client)

    assert outcome.envelope["status"] == "blocked"
    assert "matched 0 tables" in outcome.envelope["message"], (
        "the refusal survived the failing cleanup intact"
    )
