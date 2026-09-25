import pytest

from text2sql.guard import check_sql

TABLES = {"customers", "orders", "order_line"}


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT COUNT(*) FROM customers",
        "WITH t AS (SELECT customer_id FROM orders) SELECT COUNT(*) FROM t",
        "SELECT a.customer_id FROM customers a UNION SELECT customer_id FROM orders",
        "SELECT * FROM (SELECT customer_id FROM orders) sub",
    ],
)
def test_read_only_queries_pass(sql):
    assert check_sql(sql, "sqlite", TABLES).ok


@pytest.mark.parametrize(
    "sql, reason",
    [
        ("DELETE FROM customers", "only read-only"),
        ("UPDATE customers SET city = 'x'", "only read-only"),
        ("DROP TABLE customers", "only read-only"),
        ("INSERT INTO customers (customer_id) VALUES (1)", "only read-only"),
        ("SELECT 1; DELETE FROM customers", "exactly one statement"),
        ("PRAGMA writable_schema = 1", "only read-only"),
        ("ATTACH DATABASE '/tmp/x.db' AS x", "only read-only"),
        ("WITH d AS (DELETE FROM orders RETURNING *) SELECT * FROM d", ""),
        ("SELECT * INTO backup FROM customers", ""),
        ("SELECT * FROM order_items", "unknown table(s): order_items"),
        ("SELEC * FROM customers", ""),
    ],
)
def test_unsafe_or_invalid_sql_is_rejected(sql, reason):
    verdict = check_sql(sql, "postgres" if "RETURNING" in sql or "INTO" in sql else "sqlite", TABLES)
    assert not verdict.ok
    assert reason in verdict.reason
