"""Load the three Superstore extracts into SQLite and fail loudly if the data
is not the shape definitions.yaml assumes.

Usage:  python load_data.py            (reads ./data/*.xlsx, writes ./superstore.db)
Exit code 0 = loaded and all checks passed; 1 = a check failed, no DB left behind.
"""
import sqlite3, sys
from pathlib import Path
import pandas as pd

DATA, DB = Path("data"), Path("superstore.db")

SCHEMA = """
CREATE TABLE ss_products (
    product_id   TEXT PRIMARY KEY,
    category     TEXT NOT NULL,
    sub_category TEXT NOT NULL,
    product_name TEXT NOT NULL);
CREATE TABLE ss_customers (
    customer_id   TEXT PRIMARY KEY,
    customer_name TEXT NOT NULL,
    segment       TEXT NOT NULL,
    country       TEXT NOT NULL,   -- source column 'Country/Region' holds the COUNTRY
    city          TEXT, state TEXT,
    postal_code   TEXT,            -- TEXT on purpose: integers drop leading zeros
    region        TEXT NOT NULL);  -- US sales territory, not a country region
-- GRAIN: one row per order LINE, not per order. Key = (order_id, product_id).
CREATE TABLE ss_orders_products (
    row_id      INTEGER NOT NULL,
    order_id    TEXT NOT NULL,
    order_date  TEXT NOT NULL,     -- ISO 'YYYY-MM-DD'
    ship_date   TEXT NOT NULL,
    ship_mode   TEXT NOT NULL,
    customer_id TEXT NOT NULL REFERENCES ss_customers(customer_id),
    product_id  TEXT NOT NULL REFERENCES ss_products(product_id),
    sales       REAL NOT NULL,     -- line total AFTER discount (not unit price)
    quantity    INTEGER NOT NULL,
    discount    REAL NOT NULL,     -- a RATE 0-1, not dollars
    profit      REAL NOT NULL,
    PRIMARY KEY (order_id, product_id));
"""

def read_sources():
    orders_file = DATA / "SS_Orders_Products.xlsx"
    orders = pd.read_excel(orders_file, sheet_name="Orders")
    embedded_products = pd.read_excel(orders_file, sheet_name="Products")
    products = pd.read_excel(DATA / "SS_Products.xlsx")
    customers = pd.read_excel(DATA / "SS_Customers.xlsx", dtype={"Postal Code": str})
    return orders, embedded_products, products, customers

def check(failures, ok, msg):
    print(("  PASS  " if ok else "  FAIL  ") + msg)
    if not ok:
        failures.append(msg)

def validate_sources(orders, embedded_products, products, customers):
    f = []
    print("Source checks:")
    check(f, embedded_products.equals(products),
          "Products sheet inside SS_Orders_Products.xlsx == SS_Products.xlsx "
          "(SS_Products.xlsx is the source of truth)")
    check(f, not orders.duplicated(["Order ID", "Product ID"]).any(),
          "(order_id, product_id) is unique -> table is at order-LINE grain")
    per_order = orders.groupby("Order ID")[
        ["Order Date", "Ship Date", "Ship Mode", "Customer ID"]].nunique()
    check(f, (per_order == 1).all().all(),
          "order date, ship date, ship mode, customer constant within each order")
    check(f, orders["Discount"].between(0, 1, inclusive="left").all(),
          "discount is a rate in [0, 1)")
    check(f, (orders["Sales"] > 0).all() and (orders["Quantity"] > 0).all(),
          "sales and quantity strictly positive")
    check(f, (orders["Ship Date"] >= orders["Order Date"]).all(),
          "ship date on or after order date")
    check(f, products["Product ID"].is_unique and customers["Customer ID"].is_unique,
          "product_id and customer_id unique in their dimension tables")
    check(f, set(orders["Product ID"]) <= set(products["Product ID"]),
          "every order product_id exists in ss_products")
    check(f, set(orders["Customer ID"]) <= set(customers["Customer ID"]),
          "every order customer_id exists in ss_customers")
    prefix = {"FUR": "Furniture", "OFF": "Office Supplies", "TEC": "Technology"}
    check(f, (products["Product ID"].str[:3].map(prefix) == products["Category"]).all(),
          "product_id prefix (FUR/OFF/TEC) agrees with category")
    dirty = customers[~customers["Customer Name"].str.fullmatch(r"[A-Za-z' .-]+")]
    for _, r in dirty.iterrows():   # documented caveat, not a load failure
        print(f"  WARN  suspicious customer name: {r['Customer ID']} -> {r['Customer Name']!r}")
    return f

def write_db(orders, products, customers):
    con = sqlite3.connect(DB)
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(SCHEMA)
    con.executemany("INSERT INTO ss_products VALUES (?,?,?,?)",
                    products[["Product ID", "Category", "Sub-Category", "Product Name"]]
                    .itertuples(index=False))
    con.executemany("INSERT INTO ss_customers VALUES (?,?,?,?,?,?,?,?)",
                    customers[["Customer ID", "Customer Name", "Segment", "Country/Region",
                               "City", "State/Province", "Postal Code", "Region"]]
                    .itertuples(index=False))
    o = orders.copy()
    for c in ("Order Date", "Ship Date"):
        o[c] = o[c].dt.strftime("%Y-%m-%d")
    con.executemany("INSERT INTO ss_orders_products VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    [(int(r[0]), r[1], r[2], r[3], r[4], r[5], r[6],
                      float(r[7]), int(r[8]), float(r[9]), float(r[10]))
                     for r in o.itertuples(index=False)])
    con.commit()
    return con

def validate_db(con, orders):
    f = []
    q = lambda sql: con.execute(sql).fetchone()[0]
    print("Database checks:")
    check(f, q("SELECT COUNT(*) FROM ss_orders_products") == len(orders),
          f"all {len(orders)} source lines loaded")
    joined = q("""SELECT COUNT(*) FROM ss_orders_products o
                  JOIN ss_products p  ON o.product_id  = p.product_id
                  JOIN ss_customers c ON o.customer_id = c.customer_id""")
    check(f, joined == len(orders), "joining products + customers does not add or drop rows")
    check(f, abs(q("SELECT SUM(sales) FROM ss_orders_products") - orders["Sales"].sum()) < 0.005,
          "SQL total sales == spreadsheet total sales")
    check(f, abs(q("SELECT SUM(profit) FROM ss_orders_products") - orders["Profit"].sum()) < 0.005,
          "SQL total profit == spreadsheet total profit")
    return f

def main():
    DB.unlink(missing_ok=True)
    orders, embedded, products, customers = read_sources()
    failures = validate_sources(orders, embedded, products, customers)
    if not failures:
        con = write_db(orders, products, customers)
        failures += validate_db(con, orders)
        con.close()
    if failures:
        DB.unlink(missing_ok=True)
        print(f"\nLOAD FAILED: {len(failures)} check(s). No database written.")
        sys.exit(1)
    print(f"\nLoaded {DB}: {len(orders)} lines, {orders['Order ID'].nunique()} orders, "
          f"{len(customers)} customers, {len(products)} products, "
          f"{orders['Order Date'].min():%Y-%m-%d} to {orders['Order Date'].max():%Y-%m-%d}")

if __name__ == "__main__":
    main()
