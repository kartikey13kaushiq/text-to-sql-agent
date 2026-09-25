"""Sandboxed, read-only database access for generated SQL.

Defence in depth - a generated query has to get past three independent layers:

1. ``guard.check_sql`` rejects anything that is not a single read-only query (static).
2. The connection itself is read-only:
   * SQLite: opened with ``mode=ro`` and an authorizer that denies every action except
     reads, selects, functions and recursive CTEs;
   * Postgres: the session is ``default_transaction_read_only`` and every statement runs in a
     transaction that is rolled back. Connect as the SELECT-only role in
     ``sql/readonly_role.sql`` so that even a guard bypass cannot write.
3. Resource limits: a statement timeout and a row cap on what is fetched.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel

ErrorType = Literal["guard", "syntax", "unknown_identifier", "type", "timeout", "permission", "runtime"]


class ExecutionResult(BaseModel):
    ok: bool
    columns: list[str] = []
    rows: list[list[Any]] = []
    truncated: bool = False
    error_type: ErrorType | None = None
    error: str | None = None
    elapsed_ms: float = 0.0


class Database(Protocol):
    dialect: str

    def table_names(self) -> list[str]: ...

    def describe_schema(self, sample_rows: int = 3) -> str: ...

    def execute(self, sql: str, max_rows: int = 200) -> ExecutionResult: ...


def _classify_error(message: str) -> ErrorType:
    m = message.lower()
    if "syntax" in m or "incomplete input" in m:
        return "syntax"
    if "no such" in m or "does not exist" in m or "ambiguous" in m or "unknown" in m:
        return "unknown_identifier"
    if "not authorized" in m or "permission denied" in m or "read-only" in m or "readonly" in m:
        return "permission"
    if "interrupted" in m or "timeout" in m or "canceling statement" in m:
        return "timeout"
    if "operator does not exist" in m or "invalid input syntax" in m or "datatype" in m:
        return "type"
    return "runtime"


# ------------------------------------------------------------------------------ SQLite

_SQLITE_ALLOWED = {
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION,
    sqlite3.SQLITE_RECURSIVE,
}


def _sqlite_authorizer(action: int, *_: Any) -> int:
    return sqlite3.SQLITE_OK if action in _SQLITE_ALLOWED else sqlite3.SQLITE_DENY


class SQLiteDatabase:
    dialect = "sqlite"

    def __init__(self, path: str | Path, timeout_s: float = 5.0):
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(self.path)
        self.timeout_s = timeout_s
        self.conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, check_same_thread=False)
        self.conn.text_factory = lambda b: b.decode(errors="replace")

    def table_names(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        return [r[0] for r in rows]

    def describe_schema(self, sample_rows: int = 3) -> str:
        parts = []
        for table in self.table_names():
            cols = self.conn.execute(f'PRAGMA table_info("{table}")').fetchall()
            fks = self.conn.execute(f'PRAGMA foreign_key_list("{table}")').fetchall()
            lines = [f"  {c[1]} {c[2] or 'ANY'}{' PRIMARY KEY' if c[5] else ''}" for c in cols]
            lines += [f"  FOREIGN KEY ({fk[3]}) REFERENCES {fk[2]}({fk[4]})" for fk in fks]
            block = f"CREATE TABLE {table} (\n" + ",\n".join(lines) + "\n);"
            if sample_rows:
                sample = self.conn.execute(f'SELECT * FROM "{table}" LIMIT {int(sample_rows)}').fetchall()
                if sample:
                    block += f"\n/* sample rows from {table}:\n" + "\n".join(map(repr, sample)) + "\n*/"
            parts.append(block)
        return "\n\n".join(parts)

    def execute(self, sql: str, max_rows: int = 200) -> ExecutionResult:
        deadline = time.monotonic() + self.timeout_s
        started = time.perf_counter()
        self.conn.set_authorizer(_sqlite_authorizer)
        self.conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
        try:
            cur = self.conn.execute(sql)
            rows = cur.fetchmany(max_rows + 1)
            columns = [d[0] for d in cur.description or []]
            return ExecutionResult(
                ok=True,
                columns=columns,
                rows=[list(r) for r in rows[:max_rows]],
                truncated=len(rows) > max_rows,
                elapsed_ms=(time.perf_counter() - started) * 1000,
            )
        except (sqlite3.Error, sqlite3.Warning) as exc:
            return ExecutionResult(
                ok=False,
                error_type=_classify_error(str(exc)),
                error=str(exc),
                elapsed_ms=(time.perf_counter() - started) * 1000,
            )
        finally:
            self.conn.set_authorizer(None)
            self.conn.set_progress_handler(None, 0)


# ---------------------------------------------------------------------------- Postgres


class PostgresDatabase:
    dialect = "postgres"

    def __init__(self, dsn: str, schema: str = "public", timeout_ms: int = 5000):
        import psycopg

        self.schema = schema
        self.conn = psycopg.connect(dsn, autocommit=False)
        with self.conn.cursor() as cur:
            cur.execute("SET default_transaction_read_only = on")
            cur.execute(f"SET statement_timeout = {int(timeout_ms)}")
        self.conn.commit()

    def _query(self, sql: str, params: tuple = ()) -> list[tuple]:
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        self.conn.rollback()
        return rows

    def table_names(self) -> list[str]:
        rows = self._query(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = %s AND table_type = 'BASE TABLE' ORDER BY table_name",
            (self.schema,),
        )
        return [r[0] for r in rows]

    def describe_schema(self, sample_rows: int = 3) -> str:
        from psycopg import sql as pgsql

        parts = []
        for table in self.table_names():
            cols = self._query(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
                (self.schema, table),
            )
            # pg_catalog, not information_schema: the latter hides constraints from non-owners,
            # and the agent's SELECT-only role owns nothing.
            keys = self._query(
                """
                SELECT c.contype, a.attname, cf.relname, af.attname
                FROM pg_constraint c
                JOIN pg_class cl ON cl.oid = c.conrelid
                JOIN pg_namespace n ON n.oid = cl.relnamespace
                CROSS JOIN LATERAL unnest(c.conkey, coalesce(c.confkey, c.conkey)) AS k(col, fcol)
                JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.col
                LEFT JOIN pg_class cf ON cf.oid = c.confrelid
                LEFT JOIN pg_attribute af ON af.attrelid = c.confrelid AND af.attnum = k.fcol
                WHERE c.contype IN ('p', 'f') AND n.nspname = %s AND cl.relname = %s
                ORDER BY c.conname, k.col
                """,
                (self.schema, table),
            )
            pk = {col for kind, col, _, _ in keys if kind == "p"}
            fks = [(col, ref_table, ref_col) for kind, col, ref_table, ref_col in keys if kind == "f"]
            lines = [f"  {c} {t}{' PRIMARY KEY' if c in pk else ''}" for c, t in cols] + [
                f"  FOREIGN KEY ({c}) REFERENCES {t}({rc})" for c, t, rc in fks
            ]
            block = f"CREATE TABLE {table} (\n" + ",\n".join(lines) + "\n);"
            if sample_rows:
                with self.conn.cursor() as cur:
                    cur.execute(
                        pgsql.SQL("SELECT * FROM {}.{} LIMIT %s").format(
                            pgsql.Identifier(self.schema), pgsql.Identifier(table)
                        ),
                        (sample_rows,),
                    )
                    sample = cur.fetchall()
                self.conn.rollback()
                if sample:
                    block += f"\n/* sample rows from {table}:\n" + "\n".join(map(repr, sample)) + "\n*/"
            parts.append(block)
        return "\n\n".join(parts)

    def execute(self, sql: str, max_rows: int = 200) -> ExecutionResult:
        import psycopg

        started = time.perf_counter()
        try:
            with self.conn.cursor() as cur:
                cur.execute(sql)
                rows = cur.fetchmany(max_rows + 1)
                columns = [d.name for d in cur.description or []]
            return ExecutionResult(
                ok=True,
                columns=columns,
                rows=[list(r) for r in rows[:max_rows]],
                truncated=len(rows) > max_rows,
                elapsed_ms=(time.perf_counter() - started) * 1000,
            )
        except psycopg.Error as exc:
            return ExecutionResult(
                ok=False,
                error_type=_classify_error(str(exc)),
                error=str(exc).strip(),
                elapsed_ms=(time.perf_counter() - started) * 1000,
            )
        finally:
            self.conn.rollback()  # nothing a generated statement does is ever committed


def connect(target: str) -> Database:
    if target.startswith(("postgres://", "postgresql://")):
        return PostgresDatabase(target)
    return SQLiteDatabase(target)
