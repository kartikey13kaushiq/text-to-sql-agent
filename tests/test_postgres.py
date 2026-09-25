"""Postgres sandbox: SELECT-only role + read-only session. Skipped unless TEST_DATABASE_URL is set.

TEST_DATABASE_URL=postgresql://postgres@localhost:5432/t2s pytest tests/test_postgres.py
"""

import os
import subprocess
import sys
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from text2sql.db import PostgresDatabase
from text2sql.graph import Text2SQLAgent
from text2sql.llm import ScriptedGenerator

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")
ROOT = Path(__file__).parent.parent


@pytest.fixture(scope="module")
def ro_dsn():
    subprocess.run([sys.executable, str(ROOT / "sql" / "load_retail_postgres.py"), URL], check=True)
    params = conninfo_to_dict(URL)
    role_sql = (ROOT / "sql" / "readonly_role.sql").read_text()
    role_sql = (
        role_sql.replace(":password", "'ro-test'")
        .replace(":db", params["dbname"])
        .replace(":schema", "public")
    )
    with psycopg.connect(URL, autocommit=True) as conn:
        conn.execute(role_sql)
    return make_conninfo(URL, user="text2sql_ro", password="ro-test")


def test_select_only_role_cannot_write_even_without_the_guard(ro_dsn):
    db = PostgresDatabase(ro_dsn)
    assert db.execute("SELECT COUNT(*) FROM customers").rows == [[240]]
    for sql in ["DELETE FROM customers", "CREATE TABLE evil (x int)", "UPDATE orders SET status = 'X'"]:
        result = db.execute(sql)
        assert not result.ok and result.error_type == "permission", result.error
    assert db.execute("SELECT COUNT(*) FROM customers").rows == [[240]]


def test_timeout_is_enforced(ro_dsn):
    result = PostgresDatabase(ro_dsn, timeout_ms=200).execute("SELECT pg_sleep(2)")
    assert not result.ok and result.error_type == "timeout"


def test_agent_repairs_against_postgres(ro_dsn):
    db = PostgresDatabase(ro_dsn)
    gen = ScriptedGenerator({"q": ["SELECT COUNT(*) FROM order_items", "SELECT COUNT(*) FROM order_line"]})
    out = Text2SQLAgent(db, gen).ask("q")
    assert out["status"] == "succeeded" and out["repairs_used"] == 1
    assert "FOREIGN KEY (customer_id) REFERENCES customers(customer_id)" in db.describe_schema()
