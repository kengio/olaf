"""config_hash covers CONFIG_AUTHOR_COLUMNS only — OLAF's own column contract.

A control table may carry columns another framework added (a load timestamp, a file
hash, a loader identity). Those are not OLAF's contract: hashed, they would fire the
STALE guard on a config nobody edited — every source-workbook reload would force a
needless regenerate — and the same logical config would stop hashing identically across
environments. So the active-config read projects to CONFIG_AUTHOR_COLUMNS before
hashing, through ONE shared reader (`Catalog.active_config_rows`) behind both
`Deployment.short_rows` and `Audit.is_stale`, and a MISSING declared column is refused,
never silently projected past.
"""

import pytest

from _olaf_runtime import CONFIG_AUTHOR_COLUMNS, Audit, Catalog, Hash, UsageError
from _fakes import (
    CONFIG_TABLE,
    LOG_TABLE,
    MAPPING_TABLE,
    FakeFabricClient,
    build_spark,
    make_dep,
    run_generate,
    sample_config_rows,
    seed_sample_members,
)

# Computed BEFORE the projection change shipped: Hash.config over Parse.trim_row of ROWS
# below (rows carrying exactly the declared columns). The projection must be invisible for
# a table that has exactly those columns — existing mappings and log provenance stay
# valid, no false STALE — so this literal is the regression anchor for that promise.
PRE_CHANGE_DIGEST = "c9e5141039a5fdda"


def _row(**over):
    base = {c: None for c in CONFIG_AUTHOR_COLUMNS}
    base.update(
        role_name="SalesReaders",
        lakehouse_name="LH_Demo",
        include_tables="sales.*",
        permission="Read",
        include_group_names="sg-readers ",
        active=True,
        notes="  n1  ",
    )
    base.update(over)
    return base


ROWS = [_row(), _row(role_name="FinanceReaders", include_tables="finance.*", notes=None)]


def _read(rows):
    spark = build_spark()
    spark._store[CONFIG_TABLE] = [dict(r) for r in rows]
    return Catalog.active_config_rows(spark, CONFIG_TABLE)


def test_the_digest_is_unchanged_for_a_table_with_exactly_the_declared_columns():
    # invariant 1 — the whole reason the projection is safe to ship
    assert Hash.config(_read(ROWS)) == PRE_CHANGE_DIGEST


def test_foreign_columns_do_not_enter_the_hash():
    extra = [
        dict(r, load_ts="2026-01-01T00:00:00", file_hash="abc123", loaded_by="job-7") for r in ROWS
    ]
    assert Hash.config(_read(extra)) == PRE_CHANGE_DIGEST


def test_a_foreign_column_changing_value_does_not_move_the_hash():
    # the actual bug: a reload that rewrites only provenance columns another framework owns
    a = [dict(r, load_ts="2026-01-01T00:00:00") for r in ROWS]
    b = [dict(r, load_ts="2026-02-02T09:30:00") for r in ROWS]
    assert Hash.config(_read(a)) == Hash.config(_read(b)) == PRE_CHANGE_DIGEST


def test_an_authored_column_edit_still_moves_the_hash():
    # the projection must not blunt the guard it feeds
    edited = [dict(ROWS[0], include_tables="changed.*"), ROWS[1]]
    assert Hash.config(_read(edited)) != PRE_CHANGE_DIGEST


def test_a_missing_declared_column_is_refused_naming_it():
    broken = [{k: v for k, v in r.items() if k != "notes"} for r in ROWS]
    with pytest.raises(UsageError) as excinfo:
        _read(broken)
    assert "notes" in str(excinfo.value)
    assert CONFIG_TABLE in str(excinfo.value)


def test_deployment_config_hash_goes_through_the_projection():
    spark = build_spark()
    spark._store[CONFIG_TABLE] = [dict(r, foreign_col="x") for r in ROWS]
    dep = make_dep(spark, FakeFabricClient([]), "plan")
    assert dep.config_hash == PRE_CHANGE_DIGEST


def test_is_stale_mirrors_the_generator_through_the_same_reader():
    # invariant 5: rows with extra columns → is_stale False while the authored columns
    # match the stored generation, True the moment an authored column changes
    spark, client = build_spark(), FakeFabricClient([])
    make_dep(spark, client, "setup").setup()
    spark._store[CONFIG_TABLE] = sample_config_rows()
    seed_sample_members(spark)
    run_generate(make_dep(spark, client, "generate"))
    audit = Audit(spark, CONFIG_TABLE, MAPPING_TABLE, LOG_TABLE)
    spark._store[CONFIG_TABLE] = [
        dict(r, load_ts="2026-02-02T09:30:00") for r in spark._store[CONFIG_TABLE]
    ]
    assert audit.is_stale() is False  # foreign churn is not staleness
    rows = [dict(r) for r in spark._store[CONFIG_TABLE]]
    rows[0]["include_tables"] = "changed.*"
    spark._store[CONFIG_TABLE] = rows
    assert audit.is_stale() is True  # an authored edit still is


def test_a_case_variant_column_spelling_projects_like_the_contract():
    """Spark and Delta resolve `Role_Name` and `role_name` to one column, and setup()/health()
    already fold case — so a case-sensitive projection would refuse a table those two just
    declared healthy, with no way for the operator to reconcile the two answers."""
    variant = [{("Role_Name" if k == "role_name" else k): v for k, v in r.items()} for r in ROWS]
    projected = _read(variant)
    assert all("role_name" in r for r in projected)  # contract spelling in the output
    assert Hash.config(projected) == PRE_CHANGE_DIGEST  # and the hash does not move
