"""The connection layer must hold even when the static guard is bypassed."""

import pytest


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM customers",
        "UPDATE customers SET city = 'x'",
        "CREATE TABLE evil (x INT)",
        "DROP TABLE customers",
        "ATTACH DATABASE ':memory:' AS other",
        "PRAGMA journal_mode = DELETE",
    ],
)
def test_writes_are_blocked_by_the_connection(db, sql):
    result = db.execute(sql)
    assert not result.ok
    assert result.error_type in {"permission", "runtime"}
    assert db.execute("SELECT COUNT(*) FROM customers").rows == [[240]]


def test_runaway_query_hits_the_timeout(db):
    result = db.execute("WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n) SELECT MAX(i) FROM n")
    assert not result.ok
    assert result.error_type == "timeout"


def test_row_cap(db):
    result = db.execute("SELECT * FROM orders", max_rows=10)
    assert result.ok and len(result.rows) == 10 and result.truncated


def test_error_classification(db):
    assert db.execute("SELECT nope FROM customers").error_type == "unknown_identifier"
    assert db.execute("SELECT FROM WHERE").error_type == "syntax"


def test_schema_description_has_keys_and_samples(db):
    schema = db.describe_schema()
    assert "CREATE TABLE order_line" in schema
    assert "FOREIGN KEY (sku) REFERENCES products(sku)" in schema
    assert "sample rows from orders" in schema
