"""Copy the bundled SQLite retail benchmark into Postgres (for the Postgres sandbox tests).

python sql/load_retail_postgres.py postgresql://postgres@localhost:5432/t2s
"""

import sqlite3
import sys
from pathlib import Path

import psycopg

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "evals" / "bench"))
from build_db import DEFAULT_PATH, SCHEMA, build  # noqa: E402

TABLES = ["regions", "stores", "customers", "products", "orders", "order_line", "employees"]


def load(dsn: str) -> None:
    if not DEFAULT_PATH.exists():
        build(DEFAULT_PATH)
    src = sqlite3.connect(DEFAULT_PATH)
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS " + ", ".join(reversed(TABLES)) + " CASCADE")
        cur.execute(SCHEMA)
        for table in TABLES:
            rows = src.execute(f"SELECT * FROM {table}").fetchall()
            if rows:
                marks = ", ".join(["%s"] * len(rows[0]))
                cur.executemany(f"INSERT INTO {table} VALUES ({marks})", rows)
    print(f"loaded {len(TABLES)} tables into {dsn}")


if __name__ == "__main__":
    load(sys.argv[1])
