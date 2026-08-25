"""Test fakes + the runtime black-box driver, ported from olaf_test_fixtures.ipynb.
In Fabric the fixtures notebook did `%run olaf`; here the runtime is the extracted
`_olaf_runtime` module (see conftest.py). `run_runtime_blackbox` execs the notebook's
▶️ Run dispatch cell in a namespace seeded with the traced runtime globals so the
entrypoints it drives are the SAME traced objects (mock_integration coverage path)."""

from __future__ import annotations

import builtins as _builtins
import contextlib as _contextlib
import io as _io
import json
import os as _os
import sys as _sys
import types as _t
import types as _types

import _olaf_runtime
from _olaf_runtime import *  # noqa: F403 — the runtime library surface under test

# ---------------------------------------------------------------------------------------------
# The DEFAULT pre-apply role-backup folder. It stopped being a runtime constant when
# role_backup_dir became a parameter — tests that exercise the default path read it from
# PARAM_DEFAULTS so they follow the default rather than pinning a second copy of the literal.
BACKUP_DIR = PARAM_DEFAULTS["role_backup_dir"]
CONTROL_ATTESTATION = "test-access-review/fixture"


# Shared data fixtures (ported from olaf_test_fixtures.ipynb cell 2).
# ---------------------------------------------------------------------------------------------

# Mock catalog from the documented worked examples: sales/hr/ref tables + per-table columns,
# /Files/raw folders, sg-* members. Canonical case is lowercase (matches the doc's scope_paths).
CANON = {
    "tables": {
        "sales.orders": "sales.orders",
        "sales.leads": "sales.leads",
        "sales.returns": "sales.returns",
        "hr.employees": "hr.employees",
        "hr.payroll": "hr.payroll",
        "ref.calendar": "ref.calendar",
    },
    "columns": {
        "sales.orders": ["order_id", "region", "type", "amount", "customer_id"],
        "sales.leads": ["lead_id", "region", "source", "created_at"],
        "sales.returns": ["return_id", "region", "reason", "amount"],
        "hr.employees": ["employee_id", "name", "department", "manager_id"],
        "hr.payroll": ["employee_id", "name", "department", "salary", "bank_account"],
        "ref.calendar": ["date_key", "year", "month", "quarter"],
    },
    "folders": {"/Files": ["raw"], "/Files/raw": ["region_a", "region_b", "temp"]},
}

# A full config row; worked-example cases override only the columns they exercise.
BASE = {
    "role_name": "SalesReaders",
    "workspace_name": "W",
    "lakehouse_name": "L",
    "include_tables": "sales.*",
    "exclude_tables": None,
    "include_folders": None,
    "exclude_folders": None,
    "permission": "Read",
    "rls_condition": "region NOT IN ('north') OR region IS NULL",
    "include_columns": None,
    "exclude_columns": None,
    "include_group_names": "sg-sales-readers",
    "exclude_group_names": None,
    "include_user_names": None,
    "exclude_user_names": None,
    "include_sp_names": None,
    "exclude_sp_names": None,
    "active": True,
    "notes": None,
}


def make_row(**overrides):
    """A blank config row (all scope/member columns empty) with just role_name/permission/active;
    each case fills only the columns it exercises. Built from CONFIG_AUTHOR_COLUMNS — the read
    seam projects to (and requires) exactly that contract, so a seeded table must carry it."""
    row = {k: None for k in CONFIG_AUTHOR_COLUMNS}
    row.update({"role_name": "R", "permission": "Read", "active": True, "lakehouse_name": "L"})
    row.update(overrides)
    return row


def index_grants(grants):
    return {(a["role_name"], a["scope_path"]): a for a in grants}


# --- Entra directory for the member-resolution cache (No-Graph gate) ---
# generate resolves the config's member display names to objectIds ONLY from onelake_security_member
# (no Graph). DIRECTORY_* is the seed the DAR/log tests resolve against; MISSING_NAME is a name absent
# from it, for the member-gate error-path tests.
DIRECTORY_GROUPS = {
    "sg-sales-readers": "a0000000-0000-0000-0000-000000000001",
    "sg-analysts": "a0000000-0000-0000-0000-000000000002",
    "sg-contractors": "a0000000-0000-0000-0000-000000000003",
}
DIRECTORY_USERS = {"alice@example.com": "b0000000-0000-0000-0000-000000000001"}
DIRECTORY_SPS = {"svc-etl": "c0000000-0000-0000-0000-000000000001"}
DIRECTORY_MIS = {"mi-loader": "d0000000-0000-0000-0000-000000000001"}
MISSING_NAME = "sg-missing"  # absent from DIRECTORY_* -> the No-Graph member gate blocks


def directory_cache():
    """The DIRECTORY_* seeds as a {(member_type, name.lower()): objectId} cache — the form
    Member.resolve_ids consumes (mirrors _load_member_cache reading onelake_security_member)."""
    cache = {}
    for mtype, d in (
        ("Group", DIRECTORY_GROUPS),
        ("User", DIRECTORY_USERS),
        ("ServicePrincipal", DIRECTORY_SPS),
        ("ManagedIdentity", DIRECTORY_MIS),
    ):
        for name, oid in d.items():
            cache[(mtype, name.lower())] = oid
    return cache


def resolve_grants(grants, rows):
    """Run generate's member-resolution step on grants built directly from Generate.rows, so DAR/log
    tests get populated member_*_ids. Resolves against DIRECTORY_* (the No-Graph member cache) and
    asserts a clean resolution (no errors). `rows` is REQUIRED (not defaulted) to match
    Member.resolve_ids' own required third parameter -- a default here would reintroduce,
    one level down, exactly the fail-open the guard exists to prevent."""
    errors = Member.resolve_ids(grants, directory_cache(), rows)
    assert errors == [], errors
    return grants


# The four ways the suites call Generate.rows. In the notebook these were per-class `_errors` /
# `_warnings` / `_generate` helpers repeated in each TestCase; as pytest modules they share one copy.


def generate_ok(rows, canon=CANON):
    """Generate.rows asserting a clean run; returns (grants, warnings, summary)."""
    grants, errors, warnings, summary = Generate.rows(rows, canon)
    assert errors == [], f"unexpected errors: {errors}"
    return grants, warnings, summary


def generate_errors(rows, canon=CANON):
    return Generate.rows(rows, canon)[1]


def generate_warnings(rows, canon=CANON):
    """(errors, warnings) — the warning-path helper; the caller asserts on both."""
    _grants, errors, warnings, _summary = Generate.rows(rows, canon)
    return errors, warnings


def resolved_grants(rows, canon=CANON):
    """generate_ok + generate's member-resolution step — grants carrying member_*_ids."""
    grants, _warnings, _summary = generate_ok(rows, canon)
    return resolve_grants(grants, rows)


class _FakeHistorySpark:
    """spark.sql("DESCRIBE HISTORY ...").select("version").collect() stand-in for
    Catalog.config_version."""

    def __init__(self, version, raises=False):
        self._version, self._raises = version, raises

    def sql(self, query):
        assert "DESCRIBE HISTORY" in query, query
        if self._raises:
            raise RuntimeError("not a Delta table / DESCRIBE HISTORY unavailable")
        return _HistoryFrame([{"version": self._version}])


class _FakeLogSpark:
    """spark.table(name).where(...).where(...).orderBy(...).collect() stand-in for
    Log.find_plan_record; where() interprets the "col = 'val'" (AND-joined) predicates."""

    def __init__(self, rows):
        self._rows = rows

    def table(self, name):
        return _FakeLogDataFrame(self._rows)


class _FakeLogDataFrame:
    def __init__(self, rows):
        self._rows = rows

    def where(self, condition):
        # The runtime's pushed-down filters lean on this fake as their test oracle, so it
        # must not silently mangle a predicate. Quoted spans are MASKED (same length, so
        # indices line up) before any syntax scan — AND or IN inside a string literal is
        # data, not syntax; the old splitter turned `member_name = 'A AND B'` into a
        # never-match, silently. A clause the parser cannot honor RAISES: a fake that drops
        # a predicate silently is how a dropped filter passes review (the same principle as
        # select() raising on unknown columns). Every comparison is NULL-aware like the
        # engine: a NULL cell never matches an equality or an IN list.
        import re

        cond = str(condition)
        masked = re.sub(r"'[^']*'", lambda m: "\x01" * len(m.group(0)), cond)
        kept = self._rows
        pos, spans = 0, []
        for m in re.finditer(r"(?i)\bAND\b", masked):
            spans.append((pos, m.start()))
            pos = m.end()
        spans.append((pos, len(cond)))
        for start, end in spans:
            clause, clause_masked = cond[start:end], masked[start:end]
            if re.search(r"(?i)\bNOT\s+IN\b", clause_masked):
                raise ValueError(f"fake where(): NOT IN is not supported: {clause.strip()!r}")
            m_in = re.search(r"(\w+)\s+IN\s+\(", clause_masked, re.I)
            if m_in:
                close = clause_masked.find(")", m_in.end())
                if close == -1:
                    raise ValueError(f"fake where(): unterminated IN list: {clause.strip()!r}")
                col = m_in.group(1)
                vals = re.findall(r"'([^']*)'", clause[m_in.end() : close])
                kept = [r for r in kept if r.get(col) is not None and str(r.get(col)) in vals]
                continue
            m_null = re.search(r"(\w+)\s+IS\s+NULL\b", clause_masked, re.I)
            if m_null:
                # the engine's own semantic, and the one an unlabelled env run depends on:
                # a blank env is stored as NULL, so its reads ask for NULL (see Log._env_scope)
                kept = [r for r in kept if r.get(m_null.group(1)) is None]
                continue
            m_eq = re.search(r"(\w+)\s*=\s*(\x01+)", clause_masked)
            if not m_eq:
                raise ValueError(f"fake where(): cannot parse clause: {clause.strip()!r}")
            col = m_eq.group(1)
            val = clause[m_eq.start(2) + 1 : m_eq.end(2) - 1]  # the ORIGINAL quoted span
            kept = [r for r in kept if r.get(col) is not None and str(r.get(col)) == val]
        return _FakeLogDataFrame(kept)

    def orderBy(self, *args, **kwargs):
        return self

    def collect(self):
        import types

        return [types.SimpleNamespace(**r) for r in self._rows]


# ---- Mock-Fabric harness: lightweight fakes so Deployment runs end-to-end
# ---- WITHOUT a live workspace. CI has no pyspark, so provide a
# ---- minimal pyspark.sql.types ONLY when absent (Fabric keeps its real one).
try:
    import pyspark.sql.types  # noqa: F401  (real Spark on a Fabric image — keep it; never shadow)
except ImportError:
    _pys = _types.ModuleType("pyspark")
    _pysql = _types.ModuleType("pyspark.sql")
    _pytypes = _types.ModuleType("pyspark.sql.types")

    # Markers only — the fake store keeps whatever Python object it is handed, so these carry no
    # behaviour. They exist because TableSchema.frame_schema names them: the runtime builds a real
    # typed schema from its own DDL type map, and a stub missing one of these would fail the import
    # rather than the assertion, hiding what actually broke.
    class StringType:
        pass

    class BooleanType:
        pass

    class LongType:
        pass

    class TimestampType:
        pass

    class StructField:
        def __init__(self, name, dataType=None, nullable=True):
            self.name, self.dataType, self.nullable = name, dataType, nullable

    class StructType:
        def __init__(self, fields=None):
            self.fields = list(fields or [])

        def fieldNames(self):
            return [f.name for f in self.fields]

        @property
        def names(self):
            return [f.name for f in self.fields]

        def __iter__(self):
            return iter(self.fields)

    (
        _pytypes.StringType,
        _pytypes.BooleanType,
        _pytypes.LongType,
        _pytypes.TimestampType,
        _pytypes.StructField,
        _pytypes.StructType,
    ) = (StringType, BooleanType, LongType, TimestampType, StructField, StructType)
    _pysql.types = _pytypes
    _pys.sql = _pysql
    _sys.modules["pyspark"], _sys.modules["pyspark.sql"], _sys.modules["pyspark.sql.types"] = (
        _pys,
        _pysql,
        _pytypes,
    )


# ---- control-table names + generic (neutral) sample world ----
CONFIG_TABLE, MAPPING_TABLE, LOG_TABLE, MEMBER_TABLE = (
    "olaf.onelake_security_config",
    "olaf.onelake_security_mapping",
    "olaf.onelake_security_log",
    "olaf.onelake_security_member",
)
CTL_MAPPING_HISTORY_DIR = "Files/security/mapping-history"
TENANT = "00000000-0000-0000-0000-000000000000"
# Member DISPLAY NAMES the sample config authors, and the objectIds they resolve to (what the live
# DAR + the log's member_id carry). generate resolves name -> id from onelake_security_member
# (seed it with seed_sample_members).
GRP_READERS_NAME, GRP_READERS = "sg-readers", "11111111-1111-1111-1111-111111111111"
SVC_LOADER_NAME, SVC_LOADER = "svc-loader", "22222222-2222-2222-2222-222222222222"
# One objectId in two letter cases. Entra objectIds are case-INSENSITIVE hex, so these two strings
# name ONE principal — the shape the digit-only ids above cannot express (upper() is a no-op on them).
SVC_HEX = "a0000000-0000-0000-0000-0000000000aa"
SVC_HEX_UPPER = "A0000000-0000-0000-0000-0000000000AA"

SAMPLE_SCHEMAS_TABLES = {
    "sales": ["orders", "leads", "returns"],
    "hr": ["payroll"],
    "ref": ["calendar"],
}
SAMPLE_COLUMNS = {
    "sales.orders": ["order_id", "region", "amount"],
    "sales.leads": ["lead_id", "region", "source"],
    "sales.returns": ["return_id", "region", "reason"],
    "hr.payroll": ["employee_id", "name", "salary"],
    "ref.calendar": ["date_key", "year"],
}
SAMPLE_FOLDERS = {"/Files": ["raw"], "/Files/raw": ["region_a", "region_b", "temp"]}


class FakeRow:
    """Spark Row stand-in: index access (s[0]), attribute access (r.tableName), .asDict(), .get()."""

    def __init__(self, d):
        object.__setattr__(self, "_d", dict(d))

    def __getitem__(self, k):
        return list(self._d.values())[k] if isinstance(k, int) else self._d[k]

    def __getattr__(self, name):
        d = object.__getattribute__(self, "_d")
        if name in d:
            return d[name]
        raise AttributeError(name)

    def asDict(self):
        return dict(self._d)

    def get(self, k, default=None):
        return self._d.get(k, default)


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def collect(self):
        return self._rows

    def head(self):
        return self._rows[0] if self._rows else None


class _HistoryFrame:
    """DESCRIBE HISTORY result: supports .collect() (authored_by / rollback read r["version"] +
    .asDict()) and .select("version").collect() (Catalog.config_version)."""

    def __init__(self, rows):
        self._rows = rows

    def select(self, col):
        return _HistoryFrame([{col: r.get(col)} for r in self._rows])

    def collect(self):
        return [FakeRow(r) for r in self._rows]


def _cell_eq(cell, val):
    if isinstance(cell, bool):
        return str(cell).lower() == str(val).lower()
    return str(cell) == str(val)


class FakeWriter:
    """DataFrameWriter stand-in. It records the writer OPTIONS as well as the rows, because the
    fake store has no schema and therefore cannot reproduce Delta's schema enforcement — a
    write-option regression (dropping `overwriteSchema` from the mapping write, say) would be
    invisible here and would surface only on a live lakehouse. `spark._writes` lets a test assert
    the wiring instead."""

    def __init__(self, spark, df):
        self._spark, self._df, self._mode = spark, df, "append"
        self._options = {}

    def mode(self, m):
        self._mode = m
        return self

    def option(self, key, value):
        self._options[key] = value
        return self

    def saveAsTable(self, name):
        rows = [dict(r) for r in self._df._rows]
        self._spark._writes.append(
            {"table": name, "mode": self._mode, "options": dict(self._options)}
        )
        if self._mode == "overwrite":
            self._spark._store[name] = rows
        else:
            self._spark._store.setdefault(name, []).extend(rows)


class _FakePandas:
    def to_csv(self, *a, **k):
        return None


class _FakeExcelFrame:
    """What pandas.read_excel hands back, reduced to the one method the runtime calls."""

    def __init__(self, rows):
        self._rows = rows

    def to_dict(self, orient):
        assert orient == "records", orient
        return [dict(r) for r in self._rows]


def seed_workbook(**sheets):
    """Install a fake `pandas` whose read_excel serves the given sheets, so OLAF.load_config runs
    for REAL — its own parsing, validation and write — with no Excel file and no openpyxl in CI.
    `sheets` maps a sheet name to its rows; the key "" is the workbook's first sheet (what
    read_excel returns when no sheet is named). Returns the recorded (path, sheet) calls."""
    calls = []

    def read_excel(path, sheet=None):
        calls.append((path, sheet))
        key = sheet if sheet is not None else ""
        if key not in sheets:
            raise ValueError(f"no sheet {sheet!r} in {path}")
        return _FakeExcelFrame(sheets[key])

    module = _types.ModuleType("pandas")
    module.read_excel = read_excel
    _sys.modules["pandas"] = module
    return calls


class FakeDataFrame:
    def __init__(self, spark, rows, names, table=None):
        self._spark, self._rows, self._names, self._table = spark, rows, names, table
        self.write = FakeWriter(spark, self)

    @property
    def columns(self):
        """DataFrame.columns: the schema column names. Uses the explicit names passed by table()
        (from FakeSpark._columns, so it works on an empty table — what setup's drift check reads),
        else falls back to the keys of the first row."""
        if self._names is not None:
            return list(self._names)
        return list(self._rows[0].keys()) if self._rows else []

    @property
    def schema(self):
        """DataFrame.schema as a minimal StructType (stub or real pyspark, whichever is
        importable): field names from `columns` plus any row-only keys (store rows may
        carry columns the tracked DDL never declared), types inferred from the first
        non-None value per column — StringType when nothing better is known. Exists for
        runtime code that extends a LIVE table's schema (load_config's foreign-column
        carry), so that path exercises the same shape here as on a lakehouse."""
        import datetime as _dt

        from pyspark.sql.types import (
            BooleanType,
            LongType,
            StringType,
            StructField,
            StructType,
            TimestampType,
        )

        names = list(self.columns)
        for r in self._rows:
            for k in r:
                if k not in names:
                    names.append(k)

        def _typ(name):
            for r in self._rows:
                v = r.get(name)
                if isinstance(v, bool):
                    return BooleanType()
                if isinstance(v, int):
                    return LongType()
                if isinstance(v, _dt.datetime):
                    return TimestampType()
                if v is not None:
                    return StringType()
            return StringType()

        return StructType([StructField(n, _typ(n), True) for n in names])

    @property
    def dtypes(self):
        """DataFrame.dtypes: (name, type) per column, as Spark spells them (lowercase 'string' /
        'boolean'). The store holds no types, so this reports what the framework's OWN DDL declares
        — i.e. a table this framework created, which is the no-drift case. `spark._dtypes[table]`
        overrides a column, to seed the drift setup() must warn about."""
        override = self._spark._dtypes.get((self._table or "").lower(), {})
        return [(c, override.get(c.lower(), TableSchema.ddl_type(c).lower())) for c in self.columns]

    def toPandas(self):
        return _FakePandas()

    def collect(self):
        return [FakeRow(r) for r in self._rows]

    def orderBy(self, col=None, ascending=True, **k):
        if col is None:
            return self
        rows = sorted(self._rows, key=lambda r: str(r.get(col)), reverse=not ascending)
        return FakeDataFrame(self._spark, rows, self._names, self._table)

    def where(self, cond):
        # Same contract as _FakeLogDataFrame.where (see the comment there): quoted spans are
        # masked before any syntax scan, unparseable clauses RAISE, and every comparison is
        # NULL-aware. This fake additionally honors an unquoted token (`active = true`, the
        # short-rows filter), compared through _cell_eq's bool handling.
        import re

        cond = str(cond)
        masked = re.sub(r"'[^']*'", lambda m: "\x01" * len(m.group(0)), cond)
        kept = self._rows
        pos, spans = 0, []
        for m in re.finditer(r"(?i)\bAND\b", masked):
            spans.append((pos, m.start()))
            pos = m.end()
        spans.append((pos, len(cond)))
        for start, end in spans:
            clause, clause_masked = cond[start:end], masked[start:end]
            if re.search(r"(?i)\bNOT\s+IN\b", clause_masked):
                raise ValueError(f"fake where(): NOT IN is not supported: {clause.strip()!r}")
            m_in = re.search(r"([\w.]+)\s+IN\s+\(", clause_masked, re.I)
            if m_in:
                close = clause_masked.find(")", m_in.end())
                if close == -1:
                    raise ValueError(f"fake where(): unterminated IN list: {clause.strip()!r}")
                col = m_in.group(1)
                vals = re.findall(r"'([^']*)'", clause[m_in.end() : close])
                kept = [r for r in kept if r.get(col) is not None and str(r.get(col)) in vals]
                continue
            m_null = re.search(r"([\w.]+)\s+IS\s+NULL\b", clause_masked, re.I)
            if m_null:
                # the engine's own semantic, and what an unlabelled env run depends on: a blank
                # env is stored as NULL, so its reads ask for NULL (see Log._env_scope)
                kept = [r for r in kept if r.get(m_null.group(1)) is None]
                continue
            m_eq = re.search(r"([\w.]+)\s*=\s*(\x01+|\S+)", clause_masked)
            if not m_eq:
                raise ValueError(f"fake where(): cannot parse clause: {clause.strip()!r}")
            col = m_eq.group(1)
            token = m_eq.group(2)
            if token.startswith("\x01"):
                val = clause[m_eq.start(2) + 1 : m_eq.end(2) - 1]  # the ORIGINAL quoted span
            else:
                val = token
            kept = [r for r in kept if r.get(col) is not None and _cell_eq(r.get(col), val)]
        return FakeDataFrame(self._spark, kept, self._names, self._table)

    def select(self, *cols):
        flat = []
        for c in cols:
            flat += list(c) if isinstance(c, (list, tuple)) else [c]
        # Real PySpark raises AnalysisException for a column the schema does not hold; the
        # fake used to fill it with None, which HID every fail-closed path keyed on a
        # missing column from the suite. Raise like the engine (ValueError stands in for
        # AnalysisException, which the no-pyspark CI cannot import).
        known = self.columns
        missing = [c for c in flat if c not in known]
        if missing:
            raise ValueError(f"cannot resolve column(s) {missing} — schema has {sorted(known)}")
        rows = [{c: r.get(c) for c in flat} for r in self._rows]
        return FakeDataFrame(self._spark, rows, flat, self._table)

    def limit(self, n):
        return FakeDataFrame(self._spark, self._rows[:n], self._names)

    def count(self):
        return len(self._rows)


class FakeCatalog:
    def __init__(self, spark):
        self._spark = spark

    def tableExists(self, name):
        return name in self._spark._store

    def listColumns(self, name):
        return [_types.SimpleNamespace(name=c) for c in self._spark._columns.get(name.lower(), [])]


class FakeSpark:
    """Dict-backed catalog + in-memory table store. Handles the exact SQL the lib issues:
    DESCRIBE HISTORY -> version · SHOW SCHEMAS / SHOW TABLES -> catalog · CREATE TABLE -> register ·
    current_user(). table()/createDataFrame() read/write the store."""

    def __init__(self, schemas_tables, columns, store=None, history_version=3, history_rows=None):
        self._schemas_tables = schemas_tables
        self._columns = {k.lower(): v for k, v in columns.items()}
        self._store = dict(store or {})
        self._history_version = history_version
        self._history_rows = history_rows
        self._version_store = {}
        self._sql_calls = []
        self._writes = []  # one {table, mode, options} per saveAsTable — see FakeWriter
        self._dtypes = {}  # {table.lower(): {column.lower(): type}} — seeds setup()'s type drift
        self.version = "3.5.0"  # supported Spark baseline unless a prerequisite test overrides it
        self.catalog = FakeCatalog(self)

    def table(self, name):
        if name not in self._store:
            raise ValueError(f"table not found: {name}")
        # expose .columns from the tracked schema (set by CREATE TABLE / ALTER ... ADD COLUMNS) so
        # DataFrame.columns works even on an empty table — exactly what setup's drift check reads.
        return FakeDataFrame(
            self, [dict(r) for r in self._store[name]], self._columns.get(name.lower()), name
        )

    def createDataFrame(self, data, schema=None):
        names = None
        if schema is not None:
            try:
                names = list(schema.fieldNames())
            except Exception:
                try:
                    names = [f.name for f in schema.fields]
                except Exception:
                    names = list(getattr(schema, "names", []) or [])
        rows = [dict(zip(names, row)) for row in data] if names else [dict(r) for r in data]
        return FakeDataFrame(self, rows, names)

    def sql(self, query):
        import re

        q = " ".join(str(query).split())
        self._last_sql = q  # last emitted SQL, spark-level (table_history's capture seam)
        self._sql_calls.append(q)
        U = q.upper()
        if U.startswith("RESTORE TABLE"):
            m = re.search(r"RESTORE TABLE\s+([^\s]+)\s+TO VERSION AS OF\s+(\d+)", q, re.I)
            if m:
                name, version = m.group(1), int(m.group(2))
                source = self._version_store.get(version, self._store.get(name, []))
                self._store[name] = [dict(row) for row in source]
            return _Result([])
        if "DESCRIBE HISTORY" in U:
            # authored_by/rollback read explicit history rows; otherwise a single-commit history at
            # _history_version (None -> no history, so Catalog.config_version returns None).
            if self._history_rows is not None:
                return _HistoryFrame(self._history_rows)
            if self._history_version is None:
                return _HistoryFrame([])
            return _HistoryFrame([{"version": self._history_version}])
        if U.startswith("SHOW SCHEMAS"):
            return _Result([FakeRow({"namespace": s}) for s in self._schemas_tables])
        if U.startswith("SHOW TABLES"):
            m = re.search(r"IN\s+`?(\w+)`?", q, re.I)
            schema = m.group(1) if m else None
            return _Result(
                [FakeRow({"tableName": t}) for t in self._schemas_tables.get(schema, [])]
            )
        if U.startswith("CREATE SCHEMA"):
            return _Result([])
        if U.startswith("DROP TABLE"):
            # setup(rebuild=True)'s only destructive statement. Drop the ROWS, the tracked schema
            # AND any seeded dtype override — a rebuilt table is a brand-new one, so leaving the
            # override behind would make the drift it was seeded to model survive its own fix.
            m = re.search(r"DROP TABLE(?:\s+IF EXISTS)?\s+([^\s;]+)", q, re.I)
            if m:
                name = m.group(1)
                self._store.pop(name, None)
                self._columns.pop(name.lower(), None)
                self._dtypes.pop(name.lower(), None)
            return _Result([])
        if U.startswith("CREATE TABLE"):
            m = re.search(r"CREATE TABLE IF NOT EXISTS\s+([^\s(]+)", q, re.I)
            if m:
                name = m.group(1)
                self._store.setdefault(name, [])
                # register the schema too: the backticked column names in the DDL parentheses, so a
                # later table().columns / drift check sees the created schema (CREATE populates _columns).
                cols = re.findall(r"`([^`]+)`", q)
                if cols:
                    self._columns[name.lower()] = cols
            return _Result([])
        if U.startswith("ALTER TABLE"):
            # ALTER TABLE <name> ADD COLUMNS (`col` TYPE[, ...]) -> append the new name(s) to _columns.
            m = re.search(r"ALTER TABLE\s+([^\s(]+)\s+ADD COLUMNS\s*\((.*)\)", q, re.I)
            if m:
                name = m.group(1)
                new_cols = re.findall(r"`([^`]+)`", m.group(2))
                self._columns.setdefault(name.lower(), []).extend(new_cols)
            return _Result([])
        if "VERSION AS OF" in U or "TIMESTAMP AS OF" in U:
            # Runtime rollback materializes a historical config before RESTORE. Use an explicitly
            # seeded version when present; otherwise mirror the current table for legacy tests.
            match = re.search(r"FROM\s+([^\s]+)\s+VERSION AS OF\s+(\d+)", q, re.I)
            rows = []
            if match:
                name, version = match.group(1), int(match.group(2))
                rows = self._version_store.get(version, self._store.get(name, []))
            result = _Result([FakeRow(dict(row)) for row in rows])
            result._captured_sql = q
            return result
        if "CURRENT_USER" in U:
            return _Result([["tester@example.com"]])
        return _Result([])


class FakeFabricClient:
    """Stand-in for FabricClient: list_roles() returns the in-memory live set; put_roles() records
    the call and (non-dry-run) commits the new set. Deep-copies to mimic REST round-trips."""

    def __init__(
        self,
        roles=None,
        # These MUST match _RT_DEFAULT_CONTEXT's currentWorkspaceId/defaultLakehouseId. In
        # production the client is built FROM that context (Target.resolve -> FabricClient), so the
        # two can never disagree; a fake that disagrees encodes an impossible state -- and health()'s
        # table_location check compares exactly these two things.
        workspace_id="ws-guid",
        item_id="lh-guid",
        lakehouses=None,
        lakehouse_name="LH_Demo",
        enforce_etag=False,
    ):
        self._roles = [json.loads(json.dumps(r)) for r in (roles or [])]
        self.put_calls = []
        self.workspace_id, self.item_id = workspace_id, item_id
        # Mirrors a collection ETag: repeated reads of unchanged state return the same token;
        # an external edit or real PUT rotates it.
        self.roles_etag = None
        self._etag_serial = 0
        # enforce_etag=True makes the fake behave like a conditional-update service: a real
        # PUT whose etag no longer matches the server-side collection state raises the same
        # DARConflictError the real client maps a 412 to (see simulate_external_edit).
        self._enforce_etag = enforce_etag
        self._server_etag = '"fake-etag-0"'
        # the workspace's lakehouse listing the generate target-guard resolves config.lakehouse_name
        # against; default = one lakehouse whose id == item_id, so the attached-match guard passes.
        self._lakehouses = (
            lakehouses
            if lakehouses is not None
            else [{"displayName": lakehouse_name, "id": item_id}]
        )

    def resolve_lakehouse(self, name):
        return Target._single_named(self._lakehouses, name, "lakehouse", self.workspace_id)

    def list_roles(self, timeout=None):
        self.roles_etag = self._server_etag
        return [json.loads(json.dumps(r)) for r in self._roles]

    def simulate_external_edit(self):
        """Another actor's role write lands on the service: the server-side collection state
        moves, so a conditional PUT carrying a token from an EARLIER read now conflicts
        (with enforce_etag=True) — the read→PUT race the drift gate cannot see."""
        self._etag_serial += 1
        self._server_etag = f'"fake-etag-{self._etag_serial}"'

    def list_roles_quick(self):
        # mirrors the real client's failure-path seam; the bound itself is FabricClient's business
        return self.list_roles()

    def put_roles(self, roles, dry_run=False, etag=None, *, allow_unconditional=False):
        self.put_calls.append(
            {
                "dry_run": dry_run,
                "roles": len(roles),
                "etag": etag,
                "allow_unconditional": allow_unconditional,
            }
        )
        if not dry_run and self._enforce_etag and etag is not None and etag != self._server_etag:
            raise DARConflictError(
                "PUT .../dataAccessRoles -> 412: live OneLake security changed since read "
                "(the collection ETag no longer matches) — re-run mode=plan and review "
                "the new diff"
            )
        if not dry_run:
            self._roles = [json.loads(json.dumps(r)) for r in roles]
            self._etag_serial += 1  # every real write rotates the collection state
            self._server_etag = f'"fake-etag-{self._etag_serial}"'
        return 200


class FailingPushFabricClient(FakeFabricClient):
    """FakeFabricClient whose REAL (non-dry-run) bulk PUT raises — the mid-push failure the
    forensics exist for. The dryRun call still succeeds by default, so apply reaches the real push exactly the
    way it does live, and `put_calls` still records both attempts. `push_error` takes ANY
    exception, including a BaseException (a cancelled Fabric cell is a KeyboardInterrupt, and the
    pipeline-fail signal is a SystemExit — both bypass an `except Exception`).
    `list_roles_error` additionally makes the post-failure RE-READ raise (and only that one: the
    pre-push read apply's drift gate already did must still succeed), modelling the outage that
    broke the PUT still being in force when apply tries to establish what actually landed.
    `dry_run_error` instead fails the ZERO-WRITE dryRun validation call — a 400 policy-validation
    rejection, the likeliest failure of the two — so the real PUT is never reached at all."""

    DEFAULT_PUSH_ERROR = "PUT .../dataAccessRoles -> 504: Gateway Timeout"

    def __init__(
        self, roles=None, push_error=None, list_roles_error=None, dry_run_error=None, **kw
    ):
        super().__init__(roles, **kw)
        self._push_error = push_error or DARHTTPError(self.DEFAULT_PUSH_ERROR)
        self._list_roles_error = list_roles_error
        self._dry_run_error = dry_run_error
        self._pushed = False

    def list_roles(self):
        if self._list_roles_error is not None and self._pushed:
            raise self._list_roles_error
        return super().list_roles()

    def put_roles(self, roles, dry_run=False, etag=None, *, allow_unconditional=False):
        self.put_calls.append(
            {
                "dry_run": dry_run,
                "roles": len(roles),
                "etag": etag,
                "allow_unconditional": allow_unconditional,
            }
        )
        if dry_run:
            if self._dry_run_error is not None:
                raise self._dry_run_error
            return 200
        self._pushed = True  # the PUT was ATTEMPTED — what it did to the live set is unknowable
        raise self._push_error


def build_spark(store=None):
    return FakeSpark(SAMPLE_SCHEMAS_TABLES, SAMPLE_COLUMNS, store=store)


def drop_live_column(spark, table, column):
    """Simulate schema drift on an existing control table: drop a column from its live schema (as if
    the table were authored by an older framework version that predates the column), so the next
    setup() sees it missing and ADD COLUMNs it back. Minimal by design — mutates the tracked schema."""
    cols = spark._columns.get(table.lower())
    if cols and column in cols:
        cols.remove(column)


def sample_config_rows():
    """Two neutral roles: a tables role (include/exclude + RLS) and a folders role (include/exclude
    + typed group & service-principal members). Members are display names; generate resolves them to
    objectIds from onelake_security_member (seed via seed_sample_members)."""
    base = {c: None for c in CONFIG_AUTHOR_COLUMNS}
    base.update(
        {
            "permission": "Read",
            "active": True,
            "workspace_name": "WS_Demo",
            "lakehouse_name": "LH_Demo",
        }
    )
    sales = {
        **base,
        "role_name": "SalesReaders",
        "include_tables": "sales.*",
        "exclude_tables": "sales.returns",
        "rls_condition": "region = 'north'",
        "include_group_names": GRP_READERS_NAME,
    }
    raw = {
        **base,
        "role_name": "RawReaders",
        "include_folders": "/Files/raw/region_*",
        "exclude_folders": "/Files/raw/region_b",
        "include_group_names": GRP_READERS_NAME,
        "include_sp_names": SVC_LOADER_NAME,
    }
    return [sales, raw]


def carve_config_row(
    role_name="CarveReaders", include_folders="/Files/raw", exclude_folders="/Files/raw/region_b"
):
    """A folder role that grants a parent and excludes a child -> a clean subtree-carve WARNING
    (mode=validate's clean-config case: RuntimeBlackBoxValidate and ValidateDryRun both exercise
    the same warning against this exact row)."""
    base = {c: None for c in CONFIG_AUTHOR_COLUMNS}
    base.update(
        {
            "permission": "Read",
            "active": True,
            "workspace_name": "WS_Demo",
            "lakehouse_name": "LH_Demo",
        }
    )
    return dict(
        base,
        role_name=role_name,
        include_folders=include_folders,
        exclude_folders=exclude_folders,
        include_group_names=GRP_READERS_NAME,
    )


def seed_sample_members(spark):
    """Preload onelake_security_member with the sample config's members. The No-Graph gate resolves
    member names ONLY from this cache, so a successful generate needs every config member seeded."""
    spark._store[MEMBER_TABLE] = [
        member_cache_row("Group", GRP_READERS_NAME, GRP_READERS),
        member_cache_row("ServicePrincipal", SVC_LOADER_NAME, SVC_LOADER),
    ]


def member_cache_row(member_type, member_name, member_id):
    """One onelake_security_member row (the name->objectId resolution grain; 3 columns, Excel-preload)."""
    return {
        "member_type": member_type,
        "member_name": member_name,
        "member_id": member_id,
    }


def make_dep(
    spark,
    client,
    mode,
    ws="WS_Demo",
    lh="LH_Demo",
    env="dev",
    batch="B",
    run="R",
    run_by="alice@example.com",
    role_backup_dir=None,
    control_data_isolation_attestation=CONTROL_ATTESTATION,
):
    audit = Log(
        spark,
        LOG_TABLE,
        batch,
        run,
        env,
        mode,
        ws,
        lh,
        run_by=run_by,
        workspace_id=client.workspace_id,
        lakehouse_id=client.item_id,
        tenant_id=TENANT,
    )
    return Deployment(
        spark,
        client,
        audit,
        TENANT,
        CONFIG_TABLE,
        MAPPING_TABLE,
        CTL_MAPPING_HISTORY_DIR,
        ws,
        lh,
        member_table=MEMBER_TABLE,
        role_backup_dir=role_backup_dir or BACKUP_DIR,
        control_data_isolation_attestation=control_data_isolation_attestation,
    )


def run_generate(dep, history=None, rebuild=False):
    """Drive generate() on fakes: neutralize the real /lakehouse CSV write (os.makedirs) and route the
    folder listing to the sample catalog, so a folder grant needs no live OneLake — in CI and Fabric.
    `history` seeds the simulated CSV history-dir listing (Catalog._export_lister) that drives the
    per-generation dedupe check; default empty (no prior export -> a fresh versioned name is written)."""
    from unittest import mock

    def _sample_lister(base):
        return SAMPLE_FOLDERS.get(base, [])

    def _sample_export_lister(history_fullpath):
        return list(history or [])

    with (
        mock.patch("os.makedirs"),
        # the CSV export's atomic swap: _FakePandas writes no real temp file, so the swap is
        # neutralized here exactly like makedirs
        mock.patch("os.replace"),
        mock.patch.object(Catalog, "fs_folder_lister", lambda *_ids: _sample_lister),
        mock.patch.object(Catalog, "_export_lister", _sample_export_lister),
    ):
        return dep.generate(rebuild=rebuild)


def run_validate(dep):
    """Drive validate() on fakes: route the folder listing to the sample catalog so a folder grant
    needs no live OneLake. Unlike run_generate, validate writes NO CSV, so it needs no os.makedirs /
    _export_lister patch — only the folder lister the config-side folder resolution reads."""
    from unittest import mock

    def _sample_lister(base):
        return SAMPLE_FOLDERS.get(base, [])

    with mock.patch.object(Catalog, "fs_folder_lister", lambda *_ids: _sample_lister):
        return dep.validate()


@_contextlib.contextmanager
def lakehouse_writes(fail_on=None, fail_makedirs=None, store=None):
    """Capture the /lakehouse/default writes a Deployment makes (apply's pre-push role backup)
    without touching a real filesystem: os.makedirs is neutralized (exactly as run_generate already
    does for the mapping-history CSV) and text-mode CREATE opens are redirected into an in-memory
    {path: text} dict, yielded live so a spy can observe it MID-apply. Reads pass straight through
    to the real open, so helpers that read the runtime .ipynb keep working inside the window.

    The in-memory filesystem models the four operations the backup's collision-proof, all-or-nothing write
    actually performs, because a fake that models only "w" could not observe either property:
      * a path is CLAIMED at OPEN time, not at close — a real open() creates the file immediately,
        which is what makes the exclusive claim a claim;
      * mode "x" raises FileExistsError when the path is already claimed — the exclusive create the
        sequence-suffix retry keys off;
      * os.replace(src, dst) moves the entry — the temp file's content appearing under the final
        name in ONE step is the whole all-or-nothing guarantee;
      * os.remove(path) deletes it, raising FileNotFoundError when absent — so "the failure path
        leaves no temp file behind" is an assertion about this dict, not an act of faith.
    `fail_on` is a path substring whose create raises OSError (the backup-failure abort path);
    `fail_makedirs` is an exception instance os.makedirs raises instead of succeeding — e.g. the
    FileExistsError a poisoned backup directory (a FILE where the directory belongs) produces.
    `store` adopts a caller-owned dict AS the filesystem instead of starting empty, so a SECOND
    apply opens on the files the first one left — without that the two applies would each run on a
    private empty disk and could never collide, which is exactly the case under test."""
    from unittest import mock

    captured = {} if store is None else store
    real_open = _builtins.open

    class _Captured(_io.StringIO):
        def __init__(self, path):
            super().__init__()
            self._path = path

        def close(self):
            if not self.closed:
                captured[self._path] = self.getvalue()
            super().close()

    def _open(file, mode="r", *args, **kwargs):
        mode_text = str(mode)
        path = str(file)
        if "w" in mode_text or "x" in mode_text:
            if fail_on is not None and fail_on in path:
                raise OSError(f"simulated write failure: {path}")
            if "x" in mode_text and path in captured:
                raise FileExistsError(17, "File exists", path)
            captured[path] = ""  # the name is taken from the moment it is opened
            return _Captured(path)
        if path in captured:
            return _io.StringIO(captured[path])
        return real_open(file, mode, *args, **kwargs)

    def _makedirs(path, *args, **kwargs):
        # A poisoned role-backup directory must not also poison the distinct sentinel parent;
        # production creates both during apply and the test seam models the named target only.
        if fail_makedirs is not None and BACKUP_DIR in str(path):
            raise fail_makedirs

    def _replace(src, dst):
        # The role backup's temp file was OPENED through the captured-open seam above, so the
        # swap moves its content — and a backup temp that was never created still raises,
        # keeping "the failure path leaves no temp file" an assertion. The mapping-history
        # CSV's temp, by contrast, is written by _FakePandas (which writes nothing real), so
        # its swap registers the final name with empty content instead of raising.
        if src in captured or BACKUP_DIR in str(src):
            captured[dst] = captured.pop(src)
        else:
            captured[dst] = ""

    def _remove(path):
        if path not in captured:
            raise FileNotFoundError(2, "No such file or directory", path)
        del captured[path]

    with (
        mock.patch("os.makedirs", _makedirs),
        mock.patch("os.replace", _replace),
        mock.patch("os.remove", _remove),
        mock.patch.object(_builtins, "open", _open),
    ):
        yield captured


def run_apply(dep, keep_unmanaged=False, captured=None, fail_on=None, fail_makedirs=None):
    """Drive apply() on fakes: apply snapshots the live roles to /lakehouse/default BEFORE it
    pushes, so route that write into memory (see lakehouse_writes) instead of a real path. Pass a
    dict as `captured` to receive {path: text}; it is filled even when apply raises, so the
    backup-failure abort path can assert on it too. Pass the SAME dict across two applies to see
    both restore points — the collision case (see role_backup_of)."""
    with lakehouse_writes(fail_on=fail_on, fail_makedirs=fail_makedirs, store=captured) as writes:
        try:
            return dep.apply(keep_unmanaged=keep_unmanaged)
        finally:
            if captured is not None:
                captured.update(writes)


def role_backups_of(captured):
    """EVERY role-backup write in a captured {path: text} map -> [(path, parsed roles), ...] sorted
    by path, which (the timestamp leads) is also by time. Two applies MUST show up as two entries;
    a helper that could only ever return one is how an overwrite hides."""
    return [(p, json.loads(captured[p])) for p in sorted(captured) if BACKUP_DIR in p]


def role_backup_of(captured, applies=1):
    """The role backup of the LAST of `applies` applies -> (path, parsed roles list). The count is
    asserted against the number of applies the CALLER drove, not against a hardcoded 1: a fixed
    `== 1` cannot tell a second apply's backup from a first apply's backup silently OVERWRITTEN —
    both leave exactly one file on disk — so it masked the collision instead of exposing it, and
    made the two-applies test unwritable in the first place. A stray temp file also shows up here
    (it shares BACKUP_DIR), so a leaked temp is a count mismatch too, not a silent pass."""
    found = role_backups_of(captured)
    assert len(found) == applies, (
        f"expected {applies} role backup file(s) after {applies} apply(s), "
        f"got {len(found)}: {[p for p, _ in found]}"
    )
    return found[-1]


def fake_role(
    name, paths, members, mtype="Group", permission="Read", rls=None, visible_columns=None
):
    """Live DAR role fixture. `rls` is an optional {tablePath: condition} map -> a
    constraints.rows entry per path (wrapped via RLS.to_predicate, the exact shape a real pushed
    role carries). `visible_columns` is an optional {tablePath: [cols]} map -> a
    constraints.columns allow-list entry per path. Both default to None (no constraints) --
    existing callers that only pass name/paths/members/mtype/permission are unaffected."""
    rule = {
        "effect": "Permit",
        "permission": [
            {"attributeName": "Path", "attributeValueIncludedIn": list(paths)},
            {"attributeName": "Action", "attributeValueIncludedIn": [permission]},
        ],
    }
    constraints = {}
    if rls:
        constraints["rows"] = [
            {"tablePath": p, "value": RLS.to_predicate(ScopePath.to_table(p), cond)}
            for p, cond in rls.items()
        ]
    # matches real Fabric DAR + DAR.to_role (columnEffect/columnAction required by the platform; verified live on the lab)
    if visible_columns:
        constraints["columns"] = [
            {
                "tablePath": p,
                "columnNames": list(cols),
                "columnEffect": "Permit",
                "columnAction": ["Read"],
            }
            for p, cols in visible_columns.items()
        ]
    if constraints:
        rule["constraints"] = constraints
    return {
        "name": name,
        "kind": "Policy",
        "decisionRules": [rule],
        "members": {
            "microsoftEntraMembers": [
                {"objectId": m, "objectType": mtype, "tenantId": TENANT} for m in members
            ]
        },
    }


def seed_validate_row(
    role,
    path,
    member_id,
    run_at,
    run_by,
    config_version,
    mode="apply",
    member_type="Group",
    member_name="sg-seeded",
):
    # member_id is the join key show enriches on (the live DAR exposes objectIds); member_name is the
    # human-facing display name that rides along.
    row = {c: None for c in LOG_COLUMNS}
    row.update(
        {
            "action": "validate",
            "status": "success",
            "mode": mode,
            "env": "dev",
            "role_name": role,
            "scope_path": path,
            "member_name": member_name,
            "member_id": member_id,
            "member_type": member_type,
            "run_at": run_at,
            "run_by": run_by,
            "config_version": config_version,
        }
    )
    return row


# --------------------------------------------------------------------------
# CELL C — runtime black-box driver + integration scenarios (scope = "mock_integration")
# --------------------------------------------------------------------------


def _rt_find_ipynb():
    fname = "olaf.ipynb"
    d = _os.getcwd()
    while True:
        for cand in (
            _os.path.join(d, fname),
            _os.path.join(d, "notebooks", fname),
        ):
            if _os.path.exists(cand):
                return cand
        parent = _os.path.dirname(d)
        if parent == d:
            raise FileNotFoundError(f"{fname} not found from cwd or notebooks/")
        d = parent


def _rt_code():
    """Compiled Run dispatch cell of olaf.ipynb (one self-contained
    notebook, no per-mode template routing). The black-box execs it in a seeded namespace to drive
    run_and_exit/run_mode, which are defined ONCE in that namespace — by the traced
    `tests/_olaf_runtime.py` conftest regenerates from the notebook, or by `%run olaf` when a suite
    runs standalone. Only the run-tagged dispatch cell is taken: re-defining the library here would
    shadow those traced entrypoints and lose their coverage."""
    ipynb = _rt_find_ipynb()
    nb = json.loads(open(ipynb, encoding="utf-8").read())
    parts = [
        "".join(c["source"])
        for c in nb["cells"]
        if c["cell_type"] == "code" and "run" in c.get("metadata", {}).get("tags", [])
    ]
    return compile("\n".join(parts), ipynb, "exec")


def _rt_sample_lister(base):
    return SAMPLE_FOLDERS.get(base, [])


_RT_DEFAULT_CONTEXT = {
    "currentWorkspaceId": "ws-guid",
    "defaultLakehouseId": "lh-guid",
    "currentWorkspaceName": "WS_Demo",
    "defaultLakehouseName": "LH_Demo",
    "userName": "alice@example.com",
    "activityId": "act-123",
}


def run_runtime_blackbox(
    mode,
    spark,
    client=None,
    params=None,
    with_notebookutils=True,
    context="__default__",
):
    """Drive the runtime notebook black-box against fakes. Seeds the papermill params + fake spark
    + a fake-FabricClient factory + fake notebookutils, execs the runtime's Run dispatch cell
    (which calls run_and_exit), and returns (result_dict, exit_value). This exercises every
    dispatch/param/result branch of run_mode/run_and_exit."""
    from unittest import mock

    params = params or {}
    exit_sink = []

    g = dict(globals())
    g.update(
        {
            "mode": mode,
            "spark": spark,
            "workspace_id": "",
            "item_id": "",
            "tenant_id": "TENANT-GUID",
            "config_table": CONFIG_TABLE,
            "mapping_table": MAPPING_TABLE,
            "log_table": LOG_TABLE,
            "member_table": MEMBER_TABLE,
            "mapping_history_dir": CTL_MAPPING_HISTORY_DIR,
            "role_backup_dir": BACKUP_DIR,
            # the suite pins today's full output, which IS the `verbose` level; the level ladder
            # itself is exercised by tests/test_verbosity.py against the real default.
            "verbosity": "verbose",
            "env": "dev",
            "workspace_name": "WS_Demo",
            "lakehouse_name": "LH_Demo",
            "batch_id": "B",
            "keep_unmanaged": False,
            "rebuild": False,
            "if_match": True,
            "control_data_isolation_attestation": CONTROL_ATTESTATION,
            "by": "table",
            "subject": "",
            "rollback_to_version": "",
            "rollback_reason": "",
        }
    )
    g.update(params)

    holder = client if client is not None else FakeFabricClient([])
    g["FabricClient"] = lambda *a, **k: holder

    ctx = _RT_DEFAULT_CONTEXT if context == "__default__" else context
    nu = _t.ModuleType("notebookutils")
    nu.runtime = _t.SimpleNamespace(context=dict(ctx or {}))
    nu.notebook = _t.SimpleNamespace(exit=lambda v: exit_sink.append(v))
    nu.credentials = _t.SimpleNamespace(getToken=lambda *a, **k: "tok")
    saved_nu = _sys.modules.get("notebookutils")
    if with_notebookutils:
        _sys.modules["notebookutils"] = nu
    else:
        _sys.modules.pop("notebookutils", None)
    # In Fabric the runtime, the fixtures and the tests all share ONE namespace, so the notebook
    # read/wrote these through its own globals(). Here the runtime is a separate module, so every
    # "the runtime namespace" access below must target `_olaf_runtime.__dict__` — `globals()` is
    # this fixtures module and would silently miss (leaving the fake FabricClient uninjected and
    # the envelope uncaptured).
    runtime_ns = _olaf_runtime.__dict__
    runtime_ns.pop("envelope", None)
    raised = None
    envelope = None
    try:
        with (
            # lakehouse_writes covers os.makedirs AND apply's pre-push role-backup file write
            lakehouse_writes(),
            mock.patch.object(Catalog, "fs_folder_lister", lambda *_ids: _rt_sample_lister),
            mock.patch.object(Catalog, "_export_lister", lambda p: []),
            mock.patch.dict(
                runtime_ns,
                {
                    # run_mode resolves FabricClient from the runtime namespace (its
                    # __globals__), not the per-exec `g`, so inject the fake here too.
                    "FabricClient": lambda *a, **k: holder,
                },
            ),
        ):
            try:
                exec(_rt_code(), g)
            finally:
                # run_and_exit stashes the full envelope into the runtime namespace, not the
                # per-exec `g`. Capture it HERE, inside the mock.patch.dict block, before that
                # context manager reverts the namespace on exit (which would drop the freshly
                # written `envelope`).
                envelope = runtime_ns.get("envelope")
    except SystemExit as _se:
        # native-failure outcome: blocked/error raise SystemExit carrying the envelope JSON payload
        raised = _se.code if isinstance(_se.code, str) else str(_se.code)
    finally:
        if saved_nu is not None:
            _sys.modules["notebookutils"] = saved_nu
        else:
            _sys.modules.pop("notebookutils", None)

    exit_value = json.loads(exit_sink[0]) if exit_sink else None
    return _t.SimpleNamespace(envelope=envelope, exit_value=exit_value, raised=raised)


def prepare_runtime_with_undeclared_role(role_name="LegacyManual"):
    """Fresh fakes driven through the runtime (setup -> generate -> plan) whose LIVE side already
    carries `role_name` — a role that is NOT in the short config. The next apply is the
    `keep_unmanaged` fork: the default apply (keep_unmanaged=False) OMITS it from the submitted
    payload — an omission candidate, not a proven platform deletion — while
    apply(keep_unmanaged=True) carries it INTO the payload and KEEPS it, still listing it under
    drift_omission_candidates. Returns (spark, client) ready for that one apply."""
    spark = build_spark()
    client = FakeFabricClient([fake_role(role_name, ["/Tables/x/y"], [GRP_READERS])])
    run_runtime_blackbox("setup", spark)
    spark._store[CONFIG_TABLE] = sample_config_rows()
    seed_sample_members(spark)
    run_runtime_blackbox("generate", spark, client=client)
    run_runtime_blackbox("plan", spark, client=client)  # records the plan that unlocks apply
    return spark, client


# ---------------------------------------------------------------------------------------------
# The OLAF interactive facade's environment (ported from olaf_test_integration.ipynb cell D).
# OLAF calls run_mode directly (interactive path — no notebook.exit / no raise), binding the
# ambient `spark` from the RUNTIME namespace. `ols_env` seeds the same fakes run_runtime_blackbox
# uses, but for direct OLAF.<action>() calls.
# ---------------------------------------------------------------------------------------------


@_contextlib.contextmanager
def ols_env(spark, client=None, **writes_kw):
    """Bind the ambient runtime environment OLAF reads (the runtime module's `spark`, a fake
    FabricClient, fake notebookutils, the folder/export listers) for a direct OLAF call, then
    restore it. `writes_kw` is forwarded to lakehouse_writes (fail_on / fail_makedirs), so a
    backup-write failure can be driven all the way through the real dispatch to the ENVELOPE the
    operator actually reads."""
    from unittest import mock

    # As in run_runtime_blackbox: the notebook shared ONE namespace, so this patched globals().
    # Here OLAF lives in the runtime module, so `spark`/`FabricClient` must be patched THERE.
    runtime_ns = _olaf_runtime.__dict__
    holder = client if client is not None else FakeFabricClient([])
    nu = _t.ModuleType("notebookutils")
    nu.runtime = _t.SimpleNamespace(context=dict(_RT_DEFAULT_CONTEXT))
    nu.notebook = _t.SimpleNamespace(exit=lambda v: None)
    nu.credentials = _t.SimpleNamespace(getToken=lambda *a, **k: "tok")
    saved_nu = _sys.modules.get("notebookutils")
    _sys.modules["notebookutils"] = nu
    # lakehouse_name: setup asserts it against _RT_DEFAULT_CONTEXT above (mode=setup is the only
    # mode that reads it). Sticky like tenant_id, via the same OLAF.configure() path.
    OLAF._base_params = {
        "tenant_id": TENANT,
        "lakehouse_name": "LH_Demo",
        "verbosity": "verbose",
        "control_data_isolation_attestation": CONTROL_ATTESTATION,
    }
    OLAF.last_result = None
    try:
        with (
            # lakehouse_writes covers os.makedirs AND apply's pre-push role-backup file write
            lakehouse_writes(**writes_kw),
            mock.patch.object(Catalog, "fs_folder_lister", lambda *_ids: _rt_sample_lister),
            mock.patch.object(Catalog, "_export_lister", lambda p: []),
            mock.patch.dict(runtime_ns, {"spark": spark, "FabricClient": lambda *a, **k: holder}),
        ):
            yield holder
    finally:
        if saved_nu is not None:
            _sys.modules["notebookutils"] = saved_nu
        else:
            _sys.modules.pop("notebookutils", None)


def ols_rows(df):
    """A DataFrame (real Spark or the fake) as a list of dicts."""
    return [r.asDict() for r in df.collect()]


def ols_seed(spark, upto="apply"):
    """Drive the OLAF chain (inside the caller's ols_env) to a checkpoint so the query/view tests
    have real control-table + log state to read."""
    OLAF.setup()
    spark._store[CONFIG_TABLE] = sample_config_rows()
    seed_sample_members(spark)
    OLAF.generate()
    if upto in ("plan", "apply"):
        OLAF.plan()
    if upto == "apply":
        OLAF.apply()


def ols_run_cell_source():
    """The runtime's Run (run-tagged) dispatch cell source — raw, to exec under a spy."""
    nb = json.loads(open(_rt_find_ipynb(), encoding="utf-8").read())
    return "\n".join(
        "".join(c["source"])
        for c in nb["cells"]
        if c["cell_type"] == "code" and "run" in c.get("metadata", {}).get("tags", [])
    )
