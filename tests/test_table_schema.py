"""TableSchema's type map — the single source of truth the CREATE DDL, setup's ALTER/type-drift
check, and both write paths all read.

Every control table is STRING except five columns, and those five are what docs/data-model.md
documents per column. A DataFrame whose types
disagreed with the table's would be refused by Delta, so `frame_schema` derives the write schema
from `ddl_type` rather than restating it.
"""

import datetime

import pytest

from _olaf_runtime import (
    CONFIG_AUTHOR_COLUMNS,
    LOG_COLUMNS,
    MAPPING_COLUMNS,
    MAPPING_PROVENANCE_COLUMNS,
    MEMBER_CACHE_COLUMNS,
    TableSchema,
)

TYPED = {
    "active": "BOOLEAN",
    "generated_at": "TIMESTAMP",
    "run_at": "TIMESTAMP",
    "config_version": "BIGINT",
    "mapping_version": "BIGINT",
}
ALL_COLUMNS = sorted(
    set(CONFIG_AUTHOR_COLUMNS)
    | set(MAPPING_COLUMNS)
    | set(MAPPING_PROVENANCE_COLUMNS)
    | set(LOG_COLUMNS)
    | set(MEMBER_CACHE_COLUMNS)
)


@pytest.mark.parametrize("column", ALL_COLUMNS)
def test_every_control_table_column_has_the_documented_type(column):
    """Pinned column by column, not as "the map equals this dict": a column ADDED to a control
    table without a considered type silently becomes STRING, and this is where that shows up."""
    assert TableSchema.ddl_type(column) == TYPED.get(column, "STRING")


def test_run_duration_is_deliberately_a_string():
    """The one that looks like it should be typed and is not: `run_duration` holds a rounded float
    of elapsed seconds and is read as a label, never arithmetic. Pinned so a later "obvious" retype
    has to be a decision about the data model, not a tidy-up."""
    assert TableSchema.ddl_type("run_duration") == "STRING"


def test_both_delta_commit_versions_are_bigint():
    """config_version and mapping_version are the same kind of value — a Delta commit version of a
    control table — and were documented as different types. They agree now, so a numeric sort of
    either is correct by construction rather than by luck."""
    assert TableSchema.ddl_type("config_version") == "BIGINT"
    assert TableSchema.ddl_type("mapping_version") == "BIGINT"


def test_frame_schema_field_types_follow_the_ddl():
    columns = ["role_name", "generated_at", "config_version", "active"]
    schema, _to_row = TableSchema.frame_schema(columns)
    assert [f.name for f in schema.fields] == columns
    assert [type(f.dataType).__name__ for f in schema.fields] == [
        "StringType",
        "TimestampType",
        "LongType",
        "BooleanType",
    ]
    assert all(f.nullable for f in schema.fields)


def coerced(column, value):
    """The single coerced cell `frame_schema` would write for one column."""
    _schema, to_row = TableSchema.frame_schema([column])
    return to_row({column: value})[0]


@pytest.mark.parametrize("column", ALL_COLUMNS)
@pytest.mark.parametrize("empty", [None, ""], ids=["None", "blank"])
def test_an_absent_value_is_null_whatever_the_column_type(column, empty):
    """A blank must never become the string "None", the int 0, or a parse error — every optional
    column is None on some row (member_name on a 'start' row, rls_condition on a folder grant)."""
    assert coerced(column, empty) is None


def test_a_string_column_stringifies_whatever_it_is_given():
    assert coerced("role_name", "SalesReaders") == "SalesReaders"
    assert coerced("run_duration", 1.25) == "1.25"  # a float, declared STRING, written as one


@pytest.mark.parametrize(
    ("value", "expected"),
    [(True, True), (False, False), ("true", True), ("TRUE", True), ("false", False)],
)
def test_a_boolean_column_accepts_a_bool_or_its_spelling(value, expected):
    """`active` arrives as a real bool from a config read and as text from a spreadsheet load;
    both have to reach Delta as a BOOLEAN."""
    assert coerced("active", value) is expected


@pytest.mark.parametrize("value", [7, "7"])
def test_a_bigint_column_accepts_an_int_or_its_spelling(value):
    """config_version is an int from Catalog.config_version and a string once it has been read
    back out of a table."""
    assert coerced("config_version", value) == 7


def test_a_timestamp_column_accepts_a_datetime_unchanged():
    moment = datetime.datetime(2026, 7, 22, 8, 38, 18, tzinfo=datetime.timezone.utc)
    assert coerced("generated_at", moment) is moment


@pytest.mark.parametrize(
    "spelling", ["2026-07-22T08:38:18+00:00", "2026-07-22T08:38:18Z"], ids=["offset", "Z"]
)
def test_a_timestamp_column_parses_the_iso_spellings_the_runtime_writes(spelling):
    """The runtime builds `datetime.now(utc).isoformat()` (offset form), and a value read back from
    a table or a JSON round-trip can carry the `Z` spelling instead — both are the same instant."""
    assert coerced("run_at", spelling) == datetime.datetime(
        2026, 7, 22, 8, 38, 18, tzinfo=datetime.timezone.utc
    )
