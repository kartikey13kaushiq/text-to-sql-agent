"""Build the deterministic ``retail`` benchmark database (SQLite).

The schema has the kind of naming the model cannot guess (``order_line`` rather than
``order_items``, prices in integer cents, upper-case status codes, a self-referencing
manager column), so first-shot queries fail in realistic ways and the repair loop has
something to repair.

    python evals/bench/build_db.py [out.sqlite]
"""

from __future__ import annotations

import random
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

DEFAULT_PATH = Path(__file__).parent / "retail.sqlite"

SCHEMA = """
CREATE TABLE regions (
    region_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL
);
CREATE TABLE stores (
    store_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    city TEXT NOT NULL,
    region_id INTEGER NOT NULL REFERENCES regions(region_id),
    opened_on TEXT NOT NULL
);
CREATE TABLE customers (
    customer_id INTEGER PRIMARY KEY,
    full_name TEXT NOT NULL,
    email TEXT NOT NULL,
    city TEXT NOT NULL,
    signup_date TEXT NOT NULL,
    loyalty_tier TEXT NOT NULL CHECK (loyalty_tier IN ('bronze', 'silver', 'gold'))
);
CREATE TABLE products (
    sku TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    unit_price_cents INTEGER NOT NULL
);
CREATE TABLE orders (
    order_id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(customer_id),
    store_id INTEGER NOT NULL REFERENCES stores(store_id),
    order_date TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('PLACED', 'SHIPPED', 'CANCELLED', 'RETURNED'))
);
CREATE TABLE order_line (
    order_id INTEGER NOT NULL REFERENCES orders(order_id),
    line_no INTEGER NOT NULL,
    sku TEXT NOT NULL REFERENCES products(sku),
    qty INTEGER NOT NULL,
    unit_price_cents INTEGER NOT NULL,
    PRIMARY KEY (order_id, line_no)
);
CREATE TABLE employees (
    employee_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    store_id INTEGER NOT NULL REFERENCES stores(store_id),
    manager_id INTEGER REFERENCES employees(employee_id),
    hired_on TEXT NOT NULL,
    salary INTEGER NOT NULL
);
"""

REGIONS = ["North", "South", "East", "West", "Central"]
CITIES = [
    "Delhi",
    "Noida",
    "Gurugram",
    "Mumbai",
    "Pune",
    "Bengaluru",
    "Chennai",
    "Hyderabad",
    "Kolkata",
    "Jaipur",
    "Lucknow",
    "Ahmedabad",
]
CATEGORIES = {
    "grocery": [
        "Basmati Rice 5kg",
        "Atta 10kg",
        "Olive Oil 1L",
        "Green Tea",
        "Almonds 500g",
        "Honey 1kg",
        "Coffee Beans",
        "Oats 1kg",
    ],
    "electronics": [
        "USB-C Charger",
        "Bluetooth Speaker",
        "Wireless Mouse",
        "Mechanical Keyboard",
        "Noise Cancelling Headphones",
        "Smart Watch",
        "Power Bank 20000mAh",
        "LED Monitor 24in",
    ],
    "apparel": [
        "Cotton Kurta",
        "Denim Jeans",
        "Running Shoes",
        "Rain Jacket",
        "Wool Scarf",
        "Linen Shirt",
        "Sports Socks",
        "Leather Belt",
    ],
    "home": [
        "Pressure Cooker",
        "Bedsheet Set",
        "Table Lamp",
        "Water Bottle",
        "Cast Iron Pan",
        "Wall Clock",
        "Storage Box",
        "Doormat",
    ],
    "books": [
        "Python Crash Course",
        "Designing Data-Intensive Applications",
        "The Pragmatic Programmer",
        "Clean Code",
        "Atomic Habits",
        "Sapiens",
        "Deep Work",
        "The Gene",
    ],
}
FIRST = [
    "Aarav",
    "Diya",
    "Kabir",
    "Isha",
    "Rohan",
    "Meera",
    "Arjun",
    "Anaya",
    "Vivaan",
    "Saanvi",
    "Kiran",
    "Neha",
    "Rahul",
    "Priya",
    "Aditya",
    "Tara",
]
LAST = [
    "Sharma",
    "Verma",
    "Iyer",
    "Nair",
    "Gupta",
    "Kapoor",
    "Reddy",
    "Singh",
    "Das",
    "Mehta",
    "Joshi",
    "Bose",
]


def build(path: Path = DEFAULT_PATH) -> Path:
    rng = random.Random(20260924)
    path.unlink(missing_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)

    conn.executemany("INSERT INTO regions VALUES (?, ?)", list(enumerate(REGIONS, start=1)))
    stores = []
    for sid in range(1, 13):
        city = CITIES[sid - 1]
        opened = date(2018, 1, 1) + timedelta(days=rng.randint(0, 2000))
        stores.append(
            (
                sid,
                f"{city} Flagship" if sid <= 3 else f"{city} Store",
                city,
                (sid - 1) % len(REGIONS) + 1,
                opened.isoformat(),
            )
        )
    conn.executemany("INSERT INTO stores VALUES (?, ?, ?, ?, ?)", stores)

    customers = []
    for cid in range(1, 241):
        first, last = rng.choice(FIRST), rng.choice(LAST)
        signup = date(2022, 1, 1) + timedelta(days=rng.randint(0, 1300))
        tier = rng.choices(["bronze", "silver", "gold"], weights=[6, 3, 1])[0]
        customers.append(
            (
                cid,
                f"{first} {last}",
                f"{first.lower()}.{last.lower()}{cid}@example.com",
                rng.choice(CITIES),
                signup.isoformat(),
                tier,
            )
        )
    conn.executemany("INSERT INTO customers VALUES (?, ?, ?, ?, ?, ?)", customers)

    products = []
    for cat, names in CATEGORIES.items():
        for n, name in enumerate(names, start=1):
            base = {"grocery": 300, "electronics": 2500, "apparel": 1200, "home": 900, "books": 500}[cat]
            products.append((f"{cat[:3].upper()}-{n:03d}", name, cat, base * 100 + rng.randint(0, 150) * 100))
    conn.executemany("INSERT INTO products VALUES (?, ?, ?, ?)", products)

    orders, lines = [], []
    for oid in range(1, 1601):
        day = date(2025, 1, 1) + timedelta(days=rng.randint(0, 600))
        status = rng.choices(["PLACED", "SHIPPED", "CANCELLED", "RETURNED"], weights=[1, 14, 2, 1])[0]
        orders.append((oid, rng.randint(1, 240), rng.randint(1, 12), day.isoformat(), status))
        for line_no in range(1, rng.randint(1, 4) + 1):
            sku, _, _, price = rng.choice(products)
            lines.append((oid, line_no, sku, rng.randint(1, 5), price))
    conn.executemany("INSERT INTO orders VALUES (?, ?, ?, ?, ?)", orders)
    conn.executemany("INSERT INTO order_line VALUES (?, ?, ?, ?, ?)", lines)

    employees, eid = [], 1
    for sid in range(1, 13):
        manager = eid
        employees.append(
            (
                eid,
                f"{rng.choice(FIRST)} {rng.choice(LAST)}",
                sid,
                None,
                (date(2018, 1, 1) + timedelta(days=rng.randint(0, 900))).isoformat(),
                rng.randint(90, 140) * 10_000,
            )
        )
        eid += 1
        for _ in range(rng.randint(3, 6)):
            employees.append(
                (
                    eid,
                    f"{rng.choice(FIRST)} {rng.choice(LAST)}",
                    sid,
                    manager,
                    (date(2019, 1, 1) + timedelta(days=rng.randint(0, 2000))).isoformat(),
                    rng.randint(30, 80) * 10_000,
                )
            )
            eid += 1
    conn.executemany("INSERT INTO employees VALUES (?, ?, ?, ?, ?, ?)", employees)
    conn.commit()
    conn.close()
    return path


if __name__ == "__main__":
    out = build(Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PATH)
    print(f"built {out}")
