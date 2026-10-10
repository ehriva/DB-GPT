"""Independent verification of the weekly sales-amount ranking files.

Recomputes every figure from the database with separate SQL and compares it
against the CSV files written by `query_db.py --report amount-rank` and
`--report weekly-summary`. Run it after regenerating those files:

    python -B query_db.py --report amount-rank --group customer \
        --start 2025-07-01 --end 2026-01-01 --csv data/销售额周排名_客户_20250707_20251229.csv
    python -B query_db.py --report amount-rank --group material \
        --start 2025-07-01 --end 2026-01-01 --csv data/销售额周排名_产品_20250707_20251229.csv
    python -B query_db.py --report weekly-summary \
        --start 2025-07-01 --end 2026-01-01 --csv data/销售额周汇总_20250707_20251229.csv
    python -B validate_rank.py

Rounding policy under test: a reported amount is the exact group sum rounded
once to the nearest cent, halves away from zero (the Excel and SQLite
convention), so every reported amount must sit within half a cent of the raw
SQL group sum. Amounts rounded at different group granularities therefore
differ by cents when summed; the per-currency bound accounts for that.
"""

import argparse
import csv
import sqlite3
from datetime import date
from pathlib import Path

HALF_CENT = 0.005
FLOAT_SLACK = 1e-6
WEEKLY_TOLERANCE = 0.10


def load_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def amount(text: str) -> float:
    return float(text) if text else 0.0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    base = Path(__file__).parent
    parser.add_argument("--db", type=Path, default=base / "data" / "erp_trial.sqlite")
    parser.add_argument("--customer-csv", type=Path,
                        default=base / "data" / "销售额周排名_客户_20250707_20251229.csv")
    parser.add_argument("--material-csv", type=Path,
                        default=base / "data" / "销售额周排名_产品_20250707_20251229.csv")
    parser.add_argument("--summary-csv", type=Path,
                        default=base / "data" / "销售额周汇总_20250707_20251229.csv")
    parser.add_argument("--start", default="2025-07-01")
    parser.add_argument("--end", default="2026-01-01")
    args = parser.parse_args()
    window = (args.start, args.end)

    problems: list[str] = []

    def check(condition: bool, message: str) -> None:
        if not condition:
            problems.append(message)
            print("FAIL:", message)

    conn = sqlite3.connect(args.db.resolve().as_uri() + "?mode=ro", uri=True)
    conn.execute("PRAGMA query_only = ON")
    conn.row_factory = sqlite3.Row
    try:
        customers = load_csv(args.customer_csv)
        materials = load_csv(args.material_csv)
        summary = load_csv(args.summary_csv)
        week_currency = {(r["week_start"], r["currency_code"]) for r in summary}

        # 1. no detail row is lost: the summary covers the whole table.
        window_rows = conn.execute(
            "SELECT COUNT(*) FROM sales_outbound"
            " WHERE bill_date >= ? AND bill_date < ?", window).fetchone()[0]
        table_rows = conn.execute(
            "SELECT COUNT(*) FROM sales_outbound").fetchone()[0]
        file_rows = sum(int(r["detail_count"]) for r in summary)
        check(file_rows == window_rows == table_rows,
              f"detail rows: file {file_rows}, window {window_rows}, table {table_rows}")
        print(f"detail rows: table {table_rows} = window {window_rows} = file {file_rows}")

        # 2. week buckets are Mondays spanning seven days with ISO labels.
        print(f"weeks: {len({r['week_start'] for r in summary})}, "
              f"week/currency rows: {len(summary)}")
        for row in summary:
            start = date.fromisoformat(row["week_start"])
            iso = start.isocalendar()
            check(start.weekday() == 0,
                  f"week start {row['week_start']} is not a Monday")
            check(row["week"] == f"{iso.year}-W{iso.week:02d}",
                  f"week label {row['week']} does not match {row['week_start']}")
            check((date.fromisoformat(row["week_end"]) - start).days == 6,
                  f"week {row['week']} does not span seven days")

        # 3. every reported amount is within half a cent of the exact group sum.
        customer_raw = {}
        for row in conn.execute("""
            SELECT date(bill_date,'weekday 0','-6 days') AS week, currency_code,
                   customer_code, SUM(quantity*unit_price) AS amount
            FROM sales_outbound
            WHERE unit_price IS NOT NULL AND bill_date >= ? AND bill_date < ?
            GROUP BY week, currency_code, customer_code
        """, window):
            customer_raw[(row["week"], row["currency_code"],
                          row["customer_code"])] = row["amount"]
        # NULL in the database is an empty field in the CSV, so compare '' to ''.
        material_raw = {}
        for row in conn.execute("""
            SELECT date(bill_date,'weekday 0','-6 days') AS week, currency_code,
                   material_code, length_value, brand_code, unit,
                   SUM(quantity*unit_price) AS amount
            FROM sales_outbound
            WHERE unit_price IS NOT NULL AND bill_date >= ? AND bill_date < ?
            GROUP BY week, currency_code, material_code, length_value,
                     brand_code, unit
        """, window):
            material_raw[(row["week"], row["currency_code"], row["material_code"],
                          row["length_value"] or "", row["brand_code"] or "",
                          row["unit"])] = row["amount"]

        worst = 0.0
        for row in customers:
            key = (row["week_start"], row["currency_code"], row["customer_code"])
            if key not in customer_raw:
                check(not row["amount_excl_tax"],
                      f"customer group {key}: file reports {row['amount_excl_tax']}"
                      " but the database has no priced row")
                continue
            gap = abs(amount(row["amount_excl_tax"]) - customer_raw[key])
            worst = max(worst, gap)
            check(gap <= HALF_CENT + FLOAT_SLACK,
                  f"customer group {key}: {row['amount_excl_tax']}"
                  f" vs raw {customer_raw[key]}")
        for row in materials:
            key = (row["week_start"], row["currency_code"], row["material_code"],
                   row["length_value"], row["brand_code"], row["unit"])
            if key not in material_raw:
                check(not row["amount_excl_tax"],
                      f"material group {key}: file reports {row['amount_excl_tax']}"
                      " but the database has no priced row")
                continue
            gap = abs(amount(row["amount_excl_tax"]) - material_raw[key])
            worst = max(worst, gap)
            check(gap <= HALF_CENT + FLOAT_SLACK,
                  f"material group {key}: {row['amount_excl_tax']}"
                  f" vs raw {material_raw[key]}")
        print(f"largest deviation of a reported amount from the exact group sum: "
              f"{round(worst, 6)} (must stay at or below {HALF_CENT})")

        # 4. per-currency totals agree within the accumulated rounding bound.
        customer_totals: dict[str, float] = {}
        for row in customers:
            customer_totals[row["currency_code"]] = (
                customer_totals.get(row["currency_code"], 0.0)
                + amount(row["amount_excl_tax"]))
        material_totals: dict[str, float] = {}
        for row in materials:
            material_totals[row["currency_code"]] = (
                material_totals.get(row["currency_code"], 0.0)
                + amount(row["amount_excl_tax"]))
        for currency, raw in conn.execute("""
            SELECT currency_code, SUM(quantity*unit_price)
            FROM sales_outbound WHERE unit_price IS NOT NULL
              AND bill_date >= ? AND bill_date < ?
            GROUP BY currency_code
        """, window):
            bound = HALF_CENT * (len(customer_raw) + 1) + FLOAT_SLACK
            for label, totals in (("customer", customer_totals),
                                  ("material", material_totals)):
                gap = abs(totals[currency] - raw)
                check(gap <= bound,
                      f"{label} total {currency}: {totals[currency]} vs raw {raw}")
                print(f"{label} {currency}: reported {round(totals[currency], 2)},"
                      f" raw {round(raw, 2)}, deviation {round(gap, 4)}")

        # 5. the three files agree per week and currency.
        weekly = {"customer": {}, "material": {}}
        for label, rows in (("customer", customers), ("material", materials)):
            for row in rows:
                key = (row["week_start"], row["currency_code"])
                weekly[label][key] = (weekly[label].get(key, 0.0)
                                      + amount(row["amount_excl_tax"]))
        worst_week = 0.0
        for row in summary:
            key = (row["week_start"], row["currency_code"])
            for label in ("customer", "material"):
                gap = abs(weekly[label][key] - amount(row["amount_excl_tax"]))
                worst_week = max(worst_week, gap)
                check(gap <= WEEKLY_TOLERANCE,
                      f"week {key}: {label} file {weekly[label][key]}"
                      f" != summary {row['amount_excl_tax']}")
        print(f"largest weekly difference between a ranking file and the summary:"
              f" {round(worst_week, 4)}")

        # 6. ranks run 1..n per week and currency, amounts descend.
        for label, rows in (("customer", customers), ("material", materials)):
            groups: dict[tuple, list[dict]] = {}
            for row in rows:
                groups.setdefault((row["week_start"], row["currency_code"]),
                                  []).append(row)
            for key, group in groups.items():
                group.sort(key=lambda r: int(r["rank"]))
                check([int(r["rank"]) for r in group] == list(range(1, len(group) + 1)),
                      f"{label} {key}: ranks not 1..n")
                amounts = [amount(r["amount_excl_tax"]) for r in group]
                check(amounts == sorted(amounts, reverse=True),
                      f"{label} {key}: amounts not descending")
            check(set(groups) == week_currency,
                  f"{label}: week/currency sets differ from the summary")

        # 7. every rank-1 row is the true maximum, recomputed independently.
        maxima = {"customer": {}, "material": {}}
        for row in conn.execute("""
            SELECT week, currency_code, MAX(amount) FROM (
                SELECT date(bill_date,'weekday 0','-6 days') AS week,
                       currency_code, SUM(quantity*unit_price) AS amount
                FROM sales_outbound WHERE unit_price IS NOT NULL
                  AND bill_date >= ? AND bill_date < ?
                GROUP BY week, currency_code, customer_code
            ) GROUP BY week, currency_code
        """, window):
            maxima["customer"][(row[0], row[1])] = row[2]
        for row in conn.execute("""
            SELECT week, currency_code, MAX(amount) FROM (
                SELECT date(bill_date,'weekday 0','-6 days') AS week,
                       currency_code, SUM(quantity*unit_price) AS amount
                FROM sales_outbound WHERE unit_price IS NOT NULL
                  AND bill_date >= ? AND bill_date < ?
                GROUP BY week, currency_code, material_code, length_value,
                         brand_code, unit
            ) GROUP BY week, currency_code
        """, window):
            maxima["material"][(row[0], row[1])] = row[2]
        for label, rows in (("customer", customers), ("material", materials)):
            for row in rows:
                if row["rank"] != "1":
                    continue
                key = (row["week_start"], row["currency_code"])
                check(abs(amount(row["amount_excl_tax"]) - maxima[label][key])
                      <= HALF_CENT + FLOAT_SLACK,
                      f"{label} rank1 {key}: {row['amount_excl_tax']}"
                      f" != max {maxima[label][key]}")

        # 8. missing prices stay counts, never zero amounts.
        missing = sum(int(r["missing_price_count"]) for r in summary)
        in_table = conn.execute(
            "SELECT COUNT(*) FROM sales_outbound"
            " WHERE unit_price IS NULL").fetchone()[0]
        check(missing == in_table,
              f"missing price count {missing} != table {in_table}")
        print(f"missing-price rows carried through: {missing} (table {in_table})")

        print(f"\ncustomer rows {len(customers)}, material rows {len(materials)},"
              f" summary rows {len(summary)}")
    finally:
        conn.close()

    if problems:
        print(f"RESULT: {len(problems)} PROBLEMS")
        return 1
    print("RESULT: ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
