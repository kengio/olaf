"""OLAF.load_config — loading an author-owned control table from a workbook on the lakehouse.

Two tables are loadable and two are not, and the refusal is the interesting half: the mapping is
derived by generate and the log is append-only audit history, so a load into either would fabricate
a record nobody authored.

The workbook seam is faked (`seed_workbook`), so everything else runs for real — the parsing, the
column validation, the type coercion and the write.
"""

import pytest

import _olaf_runtime as rt
from _olaf_runtime import OLAF, CONFIG_AUTHOR_COLUMNS, MEMBER_CACHE_COLUMNS, UsageError
from _fakes import (
    CONFIG_TABLE,
    GRP_READERS,
    MEMBER_TABLE,
    FakeFabricClient,
    build_spark,
    fake_role,
    ols_env,
    ols_rows,
    seed_workbook,
)

CONFIG_SHEET_ROW = {
    **{c: None for c in CONFIG_AUTHOR_COLUMNS},
    "role_name": "SalesReaders",
    "lakehouse_name": "LH_Demo",
    "include_tables": "sales.*",
    "permission": "Read",
    "include_group_names": "sg-readers",
    "active": True,
}
MEMBER_SHEET_ROW = {
    "member_type": "Group",
    "member_name": "sg-readers",
    "member_id": "e0000000-0000-0000-0000-000000000001",
}


def test_load_config_without_attestation_still_loads():
    """An unverified evidence reference never protected this path; the sentinel and the DAR
    snapshot do. Absent one, the load proceeds and the record carries `unknown`."""
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        OLAF.setup()
        calls = seed_workbook(**{"": [CONFIG_SHEET_ROW]})
        writes_before = list(spark._writes)
        OLAF._base_params["control_data_isolation_attestation"] = ""
        OLAF.load_config("config", "Files/security/config.xlsx")
    assert calls, "the workbook is read"
    assert spark._writes != writes_before, "the config table is written"


def test_load_config_unsafe_or_etag_less_dar_reads_no_workbook_and_writes_no_table(monkeypatch):
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        OLAF.setup()
        calls = seed_workbook(**{"": [CONFIG_SHEET_ROW]})
        writes_before = list(spark._writes)
        client._roles = [fake_role("BroadReaders", ["/Files"], [GRP_READERS])]
        with pytest.raises(rt.ControlDataGuardError, match="reserved control data"):
            OLAF.load_config("config", "Files/security/config.xlsx")
        assert calls == [] and spark._writes == writes_before

        client._roles = []
        real_list = client.list_roles_quick

        def no_etag():
            roles = real_list()
            client.roles_etag = None
            return roles

        monkeypatch.setattr(client, "list_roles_quick", no_etag)
        with pytest.raises(rt.ControlDataGuardError, match="ETag"):
            OLAF.load_config("config", "Files/security/config.xlsx")
    assert calls == []
    assert spark._writes == writes_before


def test_existing_sentinel_blocks_load_config_before_workbook_or_table_write(monkeypatch):
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        OLAF.setup()
        calls = seed_workbook(**{"": [CONFIG_SHEET_ROW]})
        writes_before = list(spark._writes)

        def refuse(_boundary):
            raise rt.ControlDataGuardError("control-data incident sentinel already exists")

        monkeypatch.setattr(rt.ControlBoundary, "_create_sentinel", refuse)
        with pytest.raises(rt.ControlDataGuardError, match="sentinel already exists"):
            OLAF.load_config("config", "Files/security/config.xlsx")
    assert calls == []
    assert spark._writes == writes_before


def test_load_config_postwrite_etag_race_is_typed_changed_and_retains_sentinel():
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
    with ols_env(spark, client, store=lakehouse):
        OLAF.setup()
        seed_workbook(**{"": [CONFIG_SHEET_ROW]})
        client.race = True
        client.boundary_reads = 0
        with pytest.raises(rt.PostWriteBoundaryError) as excinfo:
            OLAF.load_config("config", "Files/security/config.xlsx")

    err = excinfo.value
    assert err.changed is True and err.possible_exposure is True
    assert CONFIG_TABLE in err.backup_path
    assert "Files/security/config.xlsx" in err.backup_path
    assert spark._store[CONFIG_TABLE][0]["role_name"] == "SalesReaders"
    assert lakehouse[rt.ControlBoundary.SENTINEL_FULL_PATH] == rt.ControlBoundary.SENTINEL_CONTENT


def test_load_config_prewrite_blocks_overwrite_after_dar_changes(monkeypatch):
    """Workbook parsing cannot authorize an overwrite after the approved DAR snapshot changes."""
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        OLAF.setup()
        spark._store[CONFIG_TABLE] = [dict(CONFIG_SHEET_ROW, role_name="BeforeRace")]
        seed_workbook(**{"": [CONFIG_SHEET_ROW]})
        create_frame = spark.createDataFrame

        def create_then_rotate(*args, **kwargs):
            frame = create_frame(*args, **kwargs)
            client.simulate_external_edit()
            return frame

        monkeypatch.setattr(spark, "createDataFrame", create_then_rotate)
        with pytest.raises(rt.ControlDataGuardError, match="changed after the approved snapshot"):
            OLAF.load_config("config", "Files/security/config.xlsx")

    assert [row["role_name"] for row in spark._store[CONFIG_TABLE]] == ["BeforeRace"]


@pytest.mark.parametrize("path", ["Files/config.xlsx", "Files/security2/config.xlsx"])
def test_load_config_accepts_only_files_security_descendants(path):
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        OLAF.setup()
        calls = seed_workbook(**{"": [CONFIG_SHEET_ROW]})
        with pytest.raises(UsageError, match="Files/security"):
            OLAF.load_config("config", path)
    assert calls == []


def test_a_config_workbook_replaces_the_config_table():
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        OLAF.setup()
        spark._store[CONFIG_TABLE] = [dict(CONFIG_SHEET_ROW, role_name="AboutToBeReplaced")]
        seed_workbook(**{"": [CONFIG_SHEET_ROW]})
        df = OLAF.load_config("config", "Files/security/config.xlsx")
    # a REPLACE, not an append: a row deleted in the workbook is deleted here
    assert [r["role_name"] for r in spark._store[CONFIG_TABLE]] == ["SalesReaders"]
    summary = ols_rows(df)[0]
    assert summary["table"] == CONFIG_TABLE
    assert summary["rows"] == "1"
    assert summary["source"] == "Files/security/config.xlsx"


def test_the_load_says_what_it_wrote_and_where_it_came_from(capsys):
    """It used to print a 📥 line and hand back a frame with no mention that it had — the same
    silent return every other method on the facade had already been fixed for."""
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        OLAF.setup()
        capsys.readouterr()
        seed_workbook(config=[CONFIG_SHEET_ROW])
        frame = OLAF.load_config("config", "Files/security/config.xlsx", sheet="config")
    printed = capsys.readouterr().out.splitlines()
    assert printed[0] == (
        f"✅ load_config · 1 row(s) into {CONFIG_TABLE} · from Files/security/config.xlsx · config"
    )
    assert printed[-1] == f"→  DataFrame[{', '.join(frame.columns)}]"


def test_a_named_sheet_is_read_from_the_named_workbook():
    """The path is resolved under the lakehouse mount and the sheet is passed through — both are
    what the caller wrote, not a guess."""
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        OLAF.setup()
        calls = seed_workbook(member=[MEMBER_SHEET_ROW])
        OLAF.load_config("member", "Files/security/member.xlsx", sheet="member")
    assert calls == [("/lakehouse/default/Files/security/member.xlsx", "member")]
    assert spark._store[MEMBER_TABLE] == [MEMBER_SHEET_ROW]


def test_a_leading_slash_in_the_path_still_resolves_under_the_mount():
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        OLAF.setup()
        calls = seed_workbook(**{"": [MEMBER_SHEET_ROW]})
        OLAF.load_config("member", "/Files/security/member.xlsx")
    assert calls == [("/lakehouse/default/Files/security/member.xlsx", None)]


def test_the_load_writes_real_types_not_strings():
    """`active` is a BOOLEAN column. A workbook load that wrote it as text would put the config
    table straight back into the schema conflict this framework just spent a release fixing."""
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        OLAF.setup()
        seed_workbook(**{"": [dict(CONFIG_SHEET_ROW, active="true")]})
        OLAF.load_config("config", "Files/security/config.xlsx")
    assert spark._store[CONFIG_TABLE][0]["active"] is True


@pytest.mark.parametrize("table", ["mapping", "log"])
def test_the_derived_and_the_append_only_tables_are_refused(table):
    """The whole reason this takes a logical name rather than any table name."""
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client), pytest.raises(UsageError) as excinfo:
        OLAF.load_config(table, "Files/security/whatever.xlsx")
    message = str(excinfo.value)
    assert table in message
    assert "config" in message and "member" in message  # names what IS loadable


def test_an_unknown_table_name_is_refused():
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client), pytest.raises(UsageError) as excinfo:
        OLAF.load_config("olaf.onelake_security_config", "Files/x.xlsx")
    assert "not loadable" in str(excinfo.value)


def test_a_missing_column_is_refused_and_names_it():
    """A sheet short a column is a config that silently means something else than the one the
    author edited — and this is the last point before it becomes deployed access."""
    spark, client = build_spark(), FakeFabricClient([])
    short = {k: v for k, v in CONFIG_SHEET_ROW.items() if k != "rls_condition"}
    with ols_env(spark, client):
        OLAF.setup()
        seed_workbook(**{"": [short]})
        with pytest.raises(UsageError) as excinfo:
            OLAF.load_config("config", "Files/security/config.xlsx")
    assert "missing" in str(excinfo.value)
    assert "rls_condition" in str(excinfo.value)
    assert spark._store[CONFIG_TABLE] == []  # nothing written — the check is BEFORE the write


def test_an_unexpected_column_is_refused_and_names_it():
    """Usually a sheet from a newer or older template, or the wrong sheet entirely."""
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        OLAF.setup()
        seed_workbook(**{"": [dict(CONFIG_SHEET_ROW, owner_email="a@b.c")]})
        with pytest.raises(UsageError) as excinfo:
            OLAF.load_config("config", "Files/security/config.xlsx")
    assert "unexpected" in str(excinfo.value)
    assert "owner_email" in str(excinfo.value)


def test_both_problems_are_reported_together():
    """One trip to the workbook, not two: an author fixing a template wants the whole list."""
    spark, client = build_spark(), FakeFabricClient([])
    wrong = {k: v for k, v in CONFIG_SHEET_ROW.items() if k != "notes"}
    wrong["note"] = "typo in the header"
    with ols_env(spark, client):
        OLAF.setup()
        seed_workbook(**{"": [wrong]})
        with pytest.raises(UsageError) as excinfo:
            OLAF.load_config("config", "Files/security/config.xlsx")
    message = str(excinfo.value)
    assert "missing ['notes']" in message
    assert "unexpected ['note']" in message


def test_an_empty_sheet_is_refused_rather_than_emptying_the_table():
    """A REPLACE from an empty sheet would silently delete the whole security config. generate
    already refuses an empty config; this refuses it a step earlier, where it is recoverable."""
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        OLAF.setup()
        spark._store[CONFIG_TABLE] = [dict(CONFIG_SHEET_ROW)]
        seed_workbook(**{"": []})
        with pytest.raises(UsageError) as excinfo:
            OLAF.load_config("config", "Files/security/config.xlsx")
    assert "no rows" in str(excinfo.value)
    assert len(spark._store[CONFIG_TABLE]) == 1  # untouched


def test_a_missing_sheet_surfaces_the_readers_own_error():
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        OLAF.setup()
        seed_workbook(config=[CONFIG_SHEET_ROW])
        with pytest.raises(ValueError, match="no sheet"):
            OLAF.load_config("config", "Files/security/config.xlsx", sheet="Sheet1")


def test_a_blank_cell_loads_as_null_not_the_text_nan():
    """pandas reads an empty cell as NaN. Left alone it reaches Delta as the string "nan" and every
    downstream `if not value` check silently reads it as set."""
    spark, client = build_spark(), FakeFabricClient([])
    nan = float("nan")
    with ols_env(spark, client):
        OLAF.setup()
        seed_workbook(**{"": [dict(CONFIG_SHEET_ROW, notes=nan, exclude_tables=nan)]})
        OLAF.load_config("config", "Files/security/config.xlsx")
    loaded = spark._store[CONFIG_TABLE][0]
    assert loaded["notes"] is None
    assert loaded["exclude_tables"] is None


def test_the_loaded_table_is_usable_by_generate_immediately():
    """The end-to-end point of the feature: load the workbook, then deploy from it — including
    after a setup(rebuild=True) that dropped the config table."""
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        OLAF.setup()
        seed_workbook(config=[CONFIG_SHEET_ROW], member=[MEMBER_SHEET_ROW])
        OLAF.load_config("member", "Files/security/member.xlsx", sheet="member")
        OLAF.load_config("config", "Files/security/config.xlsx", sheet="config")
        df = OLAF.generate()
    row = ols_rows(df)[0]
    assert (row["mode"], row["status"]) == ("generate", "success")
    assert row["grants"] == "3"  # sales.* over the sample catalog


def test_the_member_table_columns_are_the_member_ones():
    """Each loadable table validates against ITS OWN column list — a config sheet loaded as a
    member sheet is caught, not written."""
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        OLAF.setup()
        seed_workbook(**{"": [CONFIG_SHEET_ROW]})
        with pytest.raises(UsageError) as excinfo:
            OLAF.load_config("member", "Files/security/member.xlsx")
    assert "missing" in str(excinfo.value)
    assert all(c in str(excinfo.value) for c in MEMBER_CACHE_COLUMNS)


@pytest.mark.parametrize(
    "bad",
    [
        "Files/../secrets/config.xlsx",  # traversal escaping Files/ into a sibling
        "../../etc/config.xlsx",  # traversal escaping the mount entirely
        "/tmp/config.xlsx",  # absolute path outside Files/
        "Tables/config.xlsx",  # inside the mount, outside the file area
        "Files\\security\\config.xlsx",  # backslash
        "Files/se\x00cret.xlsx",  # NUL
        "",  # empty
        "   ",  # whitespace only
    ],
)
def test_a_path_outside_files_is_refused_not_read(bad):
    """What load_config reads becomes a control table — the last point before deployed
    access — so the workbook path follows the same Files/ containment the folder
    parameters do: refused, not coerced, and the workbook is never opened."""
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        OLAF.setup()
        calls = seed_workbook(**{"": [CONFIG_SHEET_ROW]})
        with pytest.raises(UsageError) as excinfo:
            OLAF.load_config("config", bad)
    assert "Files/security" in str(excinfo.value)
    assert calls == []  # refused before the read — the workbook was never opened


def test_a_case_variant_files_segment_is_canonicalized_like_the_folder_params():
    # the config-side folder rule: any letter case of the Files segment (and an optional
    # leading '/') canonicalizes — the spellings the config columns accept keep working here
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        OLAF.setup()
        calls = seed_workbook(**{"": [MEMBER_SHEET_ROW]})
        OLAF.load_config("member", "files/security/member.xlsx")
    assert calls == [("/lakehouse/default/Files/security/member.xlsx", None)]


FOREIGN = {"load_ts": "2026-01-01T00:00:00", "file_hash": "abc123", "loaded_by": "job-7"}


def test_foreign_columns_survive_a_reload_with_their_values():
    """Another framework may own provenance columns on the same table. A reload of OLAF's
    workbook must not drop those columns (the old overwriteSchema write did) nor null the
    surviving rows' values — update OLAF's columns, carry everything else by row key."""
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        OLAF.setup()
        spark._store[CONFIG_TABLE] = [
            dict(CONFIG_SHEET_ROW, **FOREIGN),
            dict(
                CONFIG_SHEET_ROW,
                role_name="OtherRole",
                load_ts="2025-12-31T00:00:00",
                file_hash="zzz",
                loaded_by="job-1",
            ),
        ]
        seed_workbook(
            **{
                "": [
                    CONFIG_SHEET_ROW,  # comes back unchanged -> keeps its provenance
                    dict(CONFIG_SHEET_ROW, role_name="BrandNewRole"),  # no history yet
                    # OtherRole omitted -> deleted: the replace semantic is intact
                ]
            }
        )
        OLAF.load_config("config", "Files/security/config.xlsx")
    rows = {r["role_name"]: r for r in spark._store[CONFIG_TABLE]}
    assert set(rows) == {"SalesReaders", "BrandNewRole"}  # rows still replaced
    for c, v in FOREIGN.items():
        assert rows["SalesReaders"][c] == v  # foreign values carried over
        assert rows["BrandNewRole"][c] is None  # new row: nothing to carry
    write = [w for w in spark._writes if w["table"] == CONFIG_TABLE][-1]
    assert write["mode"] == "overwrite"
    assert "overwriteSchema" not in write["options"]  # the schema is never rewritten
    assert write["options"].get("mergeSchema") == "true"  # additive only


def test_member_reload_carries_foreign_columns_by_its_composite_key():
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        OLAF.setup()
        spark._store[MEMBER_TABLE] = [dict(MEMBER_SHEET_ROW, sync_batch="b-42")]
        seed_workbook(**{"": [MEMBER_SHEET_ROW]})
        OLAF.load_config("member", "Files/security/member.xlsx")
    assert spark._store[MEMBER_TABLE][0]["sync_batch"] == "b-42"
    assert spark._store[MEMBER_TABLE][0]["member_id"] == MEMBER_SHEET_ROW["member_id"]


def test_load_into_a_missing_table_still_works():
    # a fresh environment where setup has not created the target yet: nothing to preserve,
    # and the load creates the table from the contract exactly as it always did
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        seed_workbook(**{"": [CONFIG_SHEET_ROW]})
        OLAF.load_config("config", "Files/security/config.xlsx")
    assert [r["role_name"] for r in spark._store[CONFIG_TABLE]] == ["SalesReaders"]


def test_each_row_of_a_multi_row_role_keeps_its_own_foreign_values():
    """A role spans as many rows as it has policy statements (data-model.md: role_name is
    "not unique — dup detection is by full-row hash"), so role_name is NOT a row identity.
    Keyed on it, the carry would collapse every row of a role onto whichever one the engine
    returned last and hand that row's provenance to all the others."""
    spark, client = build_spark(), FakeFabricClient([])
    sales = dict(CONFIG_SHEET_ROW, include_tables="sales.*")
    hr = dict(CONFIG_SHEET_ROW, include_tables="hr.*")  # same role, second policy statement
    with ols_env(spark, client):
        OLAF.setup()
        spark._store[CONFIG_TABLE] = [
            dict(sales, load_ts="2026-01-01T00:00:00", loaded_by="job-sales"),
            dict(hr, load_ts="2026-02-02T00:00:00", loaded_by="job-hr"),
        ]
        seed_workbook(**{"": [sales, hr]})
        OLAF.load_config("config", "Files/security/config.xlsx")
    rows = {r["include_tables"]: r for r in spark._store[CONFIG_TABLE]}
    assert rows["sales.*"]["loaded_by"] == "job-sales"
    assert rows["sales.*"]["load_ts"] == "2026-01-01T00:00:00"
    assert rows["hr.*"]["loaded_by"] == "job-hr"  # not the other row's provenance
    assert rows["hr.*"]["load_ts"] == "2026-02-02T00:00:00"


def test_an_edited_row_starts_with_null_provenance():
    """Identity is the authored row itself, so an edit makes a new row. NULL is the honest
    answer: what the other framework recorded, it recorded about the old content."""
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        OLAF.setup()
        spark._store[CONFIG_TABLE] = [dict(CONFIG_SHEET_ROW, **FOREIGN)]
        seed_workbook(**{"": [dict(CONFIG_SHEET_ROW, include_tables="edited.*")]})
        OLAF.load_config("config", "Files/security/config.xlsx")
    row = spark._store[CONFIG_TABLE][0]
    assert row["include_tables"] == "edited.*"
    assert all(row[c] is None for c in FOREIGN)


def test_conflicting_duplicate_rows_carry_nothing_and_say_so(capsys):
    """Two prior rows identical in every authored column but disagreeing about a coexisting
    column: the incoming row is equally 'the same row' as either, so guessing is the one
    thing that must not happen."""
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        OLAF.setup()
        spark._store[CONFIG_TABLE] = [
            dict(CONFIG_SHEET_ROW, load_ts="2026-01-01T00:00:00"),
            dict(CONFIG_SHEET_ROW, load_ts="2026-09-09T00:00:00"),
        ]
        seed_workbook(**{"": [CONFIG_SHEET_ROW]})
        capsys.readouterr()
        OLAF.load_config("config", "Files/security/config.xlsx")
    warning = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("WARN:")]
    assert len(warning) == 1
    assert "more than one row with role_name='SalesReaders'" in warning[0]
    assert "load_ts" in warning[0]
    assert spark._store[CONFIG_TABLE][0]["load_ts"] is None  # never guessed


def test_a_case_variant_contract_column_is_not_treated_as_foreign():
    """Delta resolves column names case-insensitively, so a table spelling a contract
    column `Role_Name` holds OLAF's column, not a coexisting one. Read as foreign, the
    write would ask Delta for two columns differing only by case."""
    spark, client = build_spark(), FakeFabricClient([])
    with ols_env(spark, client):
        OLAF.setup()
        cols = spark._columns[CONFIG_TABLE.lower()]
        cols[cols.index("role_name")] = "Role_Name"
        spark._store[CONFIG_TABLE] = [
            {
                ("Role_Name" if k == "role_name" else k): v
                for k, v in dict(CONFIG_SHEET_ROW, **FOREIGN).items()
            }
        ]
        seed_workbook(**{"": [CONFIG_SHEET_ROW]})
        OLAF.load_config("config", "Files/security/config.xlsx")
    row = spark._store[CONFIG_TABLE][0]
    assert row["role_name"] == "SalesReaders"  # written under the contract spelling
    assert "Role_Name" not in row  # and NOT alongside a case-variant duplicate
    for c, v in FOREIGN.items():
        assert row[c] == v  # the variant spelling still matched the row for the carry
