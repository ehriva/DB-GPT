"""Read-only reports for the isolated ERP sample database.

Reports:
  shipments       top-10 shipped quantity for one unit inside a date range
  details         every outbound detail row inside a date range
  issues          data issue summary
  amount-rank     weekly sales-amount ranking, --group customer or material
  usage-rank      weekly shipped-quantity ranking of products for one unit
  salesperson-rank  weekly sales-amount ranking by salesperson (--owner)
  weekly-summary  weekly totals and coverage per currency

Every query is a fixed SQL literal with named parameters: user input only ever
travels through the parameter mapping, never through SQL text.

Amount reports never sum across currencies. The export contains PRE001 and
PRE007 and no exchange rate is available, so every amount row carries its
currency_code and ranks restart per week and currency; --currency only narrows
the output to one currency. Amounts are derived as quantity times unit price;
rows without a price stay unpriced and are reported through
missing_price_count instead of being counted as zero.
"""

import argparse
import csv
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
import json
from pathlib import Path
import sqlite3


def connect_readonly(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    conn.execute("PRAGMA query_only = ON")
    conn.row_factory = sqlite3.Row
    return conn


def _week_label(week_start: str) -> str:
    iso = date.fromisoformat(week_start).isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _round_amount(value: float | None) -> float | None:
    """Round to cents the way SQLite ROUND and Excel do: halves away from zero.

    Python's round() would use banker's rounding instead, so a half-cent amount
    such as 522.125 would show as 522.12 here but 522.13 in Excel.
    """
    if value is None:
        return None
    return float(Decimal(repr(value)).quantize(Decimal("0.01"),
                                               rounding=ROUND_HALF_UP))


def _clean_quantity(value: float) -> float:
    rounded = round(value, 6)
    return int(rounded) if rounded == int(rounded) else rounded


def _rank_by_group(rows: list[dict], key_fields: tuple[str, ...]) -> list[dict]:
    """Collapse per-unit rows into one ranked row per key and week."""
    units = sorted({row["unit"] for row in rows})
    merged: dict[tuple, dict] = {}
    for row in rows:
        key = ((row["week_start"], row["currency_code"])
               + tuple(row[field] for field in key_fields))
        entry = merged.get(key)
        if entry is None:
            entry = {
                "week_start": row["week_start"],
                "week_end": row["week_end"],
                "currency_code": row["currency_code"],
                **{field: row[field] for field in key_fields},
                "amount_excl_tax": None,
                "amount_incl_tax": None,
                "units": set(),
                "detail_count": 0,
                "missing_price_count": 0,
            }
            for unit in units:
                entry["quantity_" + unit] = 0.0
            merged[key] = entry
        for field in ("amount_excl_tax", "amount_incl_tax"):
            if row[field] is not None:
                entry[field] = (entry[field] or 0.0) + row[field]
        entry["quantity_" + row["unit"]] += row["quantity"]
        entry["units"].add(row["unit"])
        entry["detail_count"] += row["detail_count"]
        entry["missing_price_count"] += row["missing_price_count"]
    ranked = []
    periods = sorted({(entry["week_start"], entry["currency_code"])
                      for entry in merged.values()})
    for period in periods:
        group = [entry for entry in merged.values()
                 if (entry["week_start"], entry["currency_code"]) == period]
        group.sort(key=lambda entry: (-(entry["amount_excl_tax"] or 0.0),
                                      *(entry[field] or "" for field in key_fields)))
        for rank, entry in enumerate(group, start=1):
            output = {
                "week_start": entry["week_start"],
                "week_end": entry["week_end"],
                "week": _week_label(entry["week_start"]),
                "currency_code": entry["currency_code"],
                "rank": rank,
            }
            for field in key_fields:
                output[field] = entry[field]
            output["amount_excl_tax"] = _round_amount(entry["amount_excl_tax"])
            output["amount_incl_tax"] = _round_amount(entry["amount_incl_tax"])
            for unit in units:
                output["quantity_" + unit] = _clean_quantity(entry["quantity_" + unit])
            output["units"] = ";".join(sorted(entry["units"]))
            output["detail_count"] = entry["detail_count"]
            output["missing_price_count"] = entry["missing_price_count"]
            ranked.append(output)
    return ranked


def _rank_by_material(rows: list[dict]) -> list[dict]:
    """Rank material/length/brand/unit variants, one row per variant and week."""
    ranked = []
    periods = sorted({(row["week_start"], row["currency_code"]) for row in rows})
    for period in periods:
        group = [row for row in rows
                 if (row["week_start"], row["currency_code"]) == period]
        group.sort(key=lambda row: (-(row["amount_excl_tax"] or 0.0),
                                    row["material_code"], row["length_value"] or "",
                                    row["brand_code"] or "", row["unit"]))
        for rank, row in enumerate(group, start=1):
            ranked.append({
                "week_start": row["week_start"],
                "week_end": row["week_end"],
                "week": _week_label(row["week_start"]),
                "currency_code": row["currency_code"],
                "rank": rank,
                "material_code": row["material_code"],
                "material_name": row["material_name"],
                "length_value": row["length_value"],
                "brand_code": row["brand_code"],
                "brand_name": row["brand_name"],
                "unit": row["unit"],
                "quantity": _clean_quantity(row["quantity"]),
                "amount_excl_tax": _round_amount(row["amount_excl_tax"]),
                "amount_incl_tax": _round_amount(row["amount_incl_tax"]),
                "detail_count": row["detail_count"],
                "missing_price_count": row["missing_price_count"],
            })
    return ranked


def _rank_by_quantity(rows: list[dict]) -> list[dict]:
    """Rank variants per week by shipped quantity.

    Quantity is physical, so it carries no currency dimension and is never
    split by currency; --unit keeps Pcs and m apart.
    """
    ranked = []
    for week in sorted({row["week_start"] for row in rows}):
        group = [row for row in rows if row["week_start"] == week]
        group.sort(key=lambda row: (-row["quantity"], row["material_code"],
                                    row["length_value"] or "",
                                    row["brand_code"] or "", row["unit"]))
        for rank, row in enumerate(group, start=1):
            ranked.append({
                "week_start": row["week_start"],
                "week_end": row["week_end"],
                "week": _week_label(row["week_start"]),
                "rank": rank,
                "material_code": row["material_code"],
                "material_name": row["material_name"],
                "length_value": row["length_value"],
                "brand_code": row["brand_code"],
                "brand_name": row["brand_name"],
                "unit": row["unit"],
                "quantity": _clean_quantity(row["quantity"]),
                "detail_count": row["detail_count"],
            })
    return ranked


# The outbound export's recorded column labels are shifted one column left of
# the values (the column labelled 销售员#编码 holds the person's name and the one
# labelled 销售员#名称 holds a code), so the owner columns are pinned by index
# for this export instead of being looked up by label.
OWNER_COLUMNS = {"saler": 23, "follow": 64, "inner": 66}
MISSING_OWNER = "（缺业务员）"


def _owner_by_row(conn: sqlite3.Connection, column: int) -> dict[tuple, str]:
    """Map every outbound (sheet_id, excel_row) to its document's owner name.

    The salesperson exists only in the raw export layer: sales_outbound has no
    such column. It is a document-header field, so it is present on the first
    row of a document and empty on the continuation rows; carrying the last
    non-empty value forward therefore attributes every line of the document.
    """
    owners: dict[tuple, str] = {}
    sheet_ids = [row["sheet_id"] for row in conn.execute(
        "SELECT DISTINCT sheet_id FROM sales_outbound")]
    for sheet_id in sheet_ids:
        current = None
        for row in conn.execute("""
            SELECT excel_row, values_json FROM raw_rows
            WHERE sheet_id = ? ORDER BY excel_row
        """, (sheet_id,)):
            values = json.loads(row["values_json"])
            value = values[column] if column < len(values) else None
            if value is not None and str(value).strip():
                current = str(value).strip()
            if current is not None:
                owners[(sheet_id, row["excel_row"])] = current
    return owners


def _weekly_summary(rows: list[dict], quantity_rows: list[dict]) -> list[dict]:
    """Attach one quantity column per unit; quantity is never summed across units."""
    units = sorted({row["unit"] for row in quantity_rows})
    quantities: dict[tuple, dict[str, float]] = {}
    for row in quantity_rows:
        key = (row["week_start"], row["currency_code"])
        entry = quantities.setdefault(key, {unit: 0.0 for unit in units})
        entry[row["unit"]] += row["quantity"]
    summary = []
    for row in rows:
        key = (row["week_start"], row["currency_code"])
        output = {
            "week_start": row["week_start"],
            "week_end": row["week_end"],
            "week": _week_label(row["week_start"]),
            "currency_code": row["currency_code"],
            "detail_count": row["detail_count"],
            "days_with_data": row["days_with_data"],
            "documents": row["documents"],
            "customers": row["customers"],
            "materials": row["materials"],
        }
        for unit in units:
            output["quantity_" + unit] = _clean_quantity(quantities[key][unit])
        output["amount_excl_tax"] = row["amount_excl_tax"]
        output["amount_incl_tax"] = row["amount_incl_tax"]
        output["missing_price_count"] = row["missing_price_count"]
        summary.append(output)
    return summary


def report(conn: sqlite3.Connection, name: str, start: str | None = None,
           end: str | None = None, unit: str | None = None,
           customer: str | None = None, material: str | None = None,
           currency: str | None = None, group: str | None = None,
           top: int | None = None, owner: str | None = None) -> list[dict]:
    if name == "issues":
        return [dict(row) for row in conn.execute(
            "SELECT * FROM v_data_issue_summary ORDER BY business_table,issue_type"
        )]
    if start is None or end is None:
        raise ValueError("出货查询必须提供 --start 和 --end（结束日期不包含）")
    if date.fromisoformat(start) >= date.fromisoformat(end):
        raise ValueError("起始日期必须早于结束日期")
    if name in ("shipments", "usage-rank") and not unit:
        raise ValueError("排名必须指定 --unit，不能混合 Pcs 与 m")
    if name == "amount-rank" and group not in ("customer", "material"):
        raise ValueError("amount-rank 必须指定 --group customer 或 material")
    if name == "salesperson-rank" and (owner or "saler") not in OWNER_COLUMNS:
        raise ValueError("--owner 只能是 " + "/".join(OWNER_COLUMNS))
    if top is not None and top < 1:
        raise ValueError("--top 必须大于 0")
    params = {
        "start": start,
        "end": end,
        "unit": unit,
        "customer": customer,
        "material": material,
        "currency": currency,
    }
    if name == "shipments":
        rows = [dict(row) for row in conn.execute("""
            SELECT s.material_code, s.material_name, s.length_value, s.brand_code,
                   s.brand_name, s.unit, SUM(s.quantity) AS shipped_quantity,
                   COUNT(*) AS detail_count
            FROM sales_outbound s
            WHERE s.bill_date >= :start AND s.bill_date < :end
              AND (:unit IS NULL OR s.unit = :unit)
              AND (:customer IS NULL OR s.customer_code = :customer)
              AND (:material IS NULL OR s.material_code = :material)
              AND (:currency IS NULL OR s.currency_code = :currency)
            GROUP BY s.material_code,s.material_name,s.length_value,s.brand_code,
                     s.brand_name,s.unit
            ORDER BY shipped_quantity DESC,s.material_code,s.length_value,s.brand_code
            LIMIT 10
        """, params)]
    elif name == "details":
        rows = [dict(row) for row in conn.execute("""
            SELECT s.bill_date,s.bill_no,s.document_id,s.entry_id,
                   s.customer_code,s.customer_name,s.material_code,s.material_name,
                   s.length_value,s.brand_code,s.brand_name,s.unit,s.quantity,
                   s.warehouse_name,s.source_order_entry_id,
                   ss.workbook,ss.sheet,s.excel_row,s.document_status
            FROM sales_outbound s JOIN source_sheets ss ON ss.id=s.sheet_id
            WHERE s.bill_date >= :start AND s.bill_date < :end
              AND (:unit IS NULL OR s.unit = :unit)
              AND (:customer IS NULL OR s.customer_code = :customer)
              AND (:material IS NULL OR s.material_code = :material)
              AND (:currency IS NULL OR s.currency_code = :currency)
            ORDER BY s.bill_date,s.document_id,s.entry_id
        """, params)]
    elif name == "salesperson-rank":
        detail = [dict(row) for row in conn.execute("""
            SELECT s.sheet_id,s.excel_row,s.unit,
                   date(s.bill_date,'weekday 0','-6 days') AS week_start,
                   date(s.bill_date,'weekday 0') AS week_end,
                   s.currency_code,s.quantity,
                   CASE WHEN s.unit_price IS NOT NULL
                        THEN s.quantity*s.unit_price END AS amount_excl_tax,
                   CASE WHEN s.tax_unit_price IS NOT NULL
                        THEN s.quantity*s.tax_unit_price END AS amount_incl_tax,
                   (s.unit_price IS NULL) AS missing_price_count
            FROM sales_outbound s
            WHERE s.bill_date >= :start AND s.bill_date < :end
              AND (:unit IS NULL OR s.unit = :unit)
              AND (:customer IS NULL OR s.customer_code = :customer)
              AND (:material IS NULL OR s.material_code = :material)
              AND (:currency IS NULL OR s.currency_code = :currency)
            ORDER BY s.bill_date,s.document_id,s.entry_id
        """, params)]
        owners = _owner_by_row(conn, OWNER_COLUMNS[owner or "saler"])
        merged: dict[tuple, dict] = {}
        for row in detail:
            name_of_owner = owners.get((row["sheet_id"], row["excel_row"]),
                                       MISSING_OWNER)
            key = (row["week_start"], row["currency_code"], name_of_owner,
                   row["unit"])
            entry = merged.get(key)
            if entry is None:
                entry = {
                    "week_start": row["week_start"],
                    "week_end": row["week_end"],
                    "currency_code": row["currency_code"],
                    "salesperson": name_of_owner,
                    "unit": row["unit"],
                    "quantity": 0.0,
                    "amount_excl_tax": None,
                    "amount_incl_tax": None,
                    "detail_count": 0,
                    "missing_price_count": 0,
                }
                merged[key] = entry
            for field in ("amount_excl_tax", "amount_incl_tax"):
                if row[field] is not None:
                    entry[field] = (entry[field] or 0.0) + row[field]
            entry["quantity"] += row["quantity"]
            entry["detail_count"] += 1
            entry["missing_price_count"] += row["missing_price_count"]
        rows = list(merged.values())
    elif name == "amount-rank" and group == "customer":
        rows = [dict(row) for row in conn.execute("""
            SELECT date(s.bill_date,'weekday 0','-6 days') AS week_start,
                   date(s.bill_date,'weekday 0') AS week_end,
                   s.currency_code,s.customer_code,s.customer_name,s.unit,
                   SUM(s.quantity) AS quantity,
                   SUM(CASE WHEN s.unit_price IS NOT NULL
                            THEN s.quantity*s.unit_price END) AS amount_excl_tax,
                   SUM(CASE WHEN s.tax_unit_price IS NOT NULL
                            THEN s.quantity*s.tax_unit_price END) AS amount_incl_tax,
                   COUNT(*) AS detail_count,
                   SUM(CASE WHEN s.unit_price IS NULL THEN 1 ELSE 0 END)
                       AS missing_price_count
            FROM sales_outbound s
            WHERE s.bill_date >= :start AND s.bill_date < :end
              AND (:unit IS NULL OR s.unit = :unit)
              AND (:customer IS NULL OR s.customer_code = :customer)
              AND (:material IS NULL OR s.material_code = :material)
              AND (:currency IS NULL OR s.currency_code = :currency)
            GROUP BY week_start,week_end,s.currency_code,s.customer_code,
                     s.customer_name,s.unit
            ORDER BY week_start,s.currency_code,s.customer_code,s.unit
        """, params)]
    elif name == "usage-rank":
        rows = [dict(row) for row in conn.execute("""
            SELECT date(s.bill_date,'weekday 0','-6 days') AS week_start,
                   date(s.bill_date,'weekday 0') AS week_end,
                   s.material_code,s.material_name,s.length_value,s.brand_code,
                   s.brand_name,s.unit,
                   SUM(s.quantity) AS quantity,
                   COUNT(*) AS detail_count
            FROM sales_outbound s
            WHERE s.bill_date >= :start AND s.bill_date < :end
              AND (:unit IS NULL OR s.unit = :unit)
              AND (:customer IS NULL OR s.customer_code = :customer)
              AND (:material IS NULL OR s.material_code = :material)
              AND (:currency IS NULL OR s.currency_code = :currency)
            GROUP BY week_start,week_end,s.material_code,s.material_name,
                     s.length_value,s.brand_code,s.brand_name,s.unit
            ORDER BY week_start,s.material_code,s.length_value,s.brand_code,s.unit
        """, params)]
    elif name == "amount-rank":
        rows = [dict(row) for row in conn.execute("""
            SELECT date(s.bill_date,'weekday 0','-6 days') AS week_start,
                   date(s.bill_date,'weekday 0') AS week_end,
                   s.currency_code,s.material_code,s.material_name,s.length_value,
                   s.brand_code,s.brand_name,s.unit,
                   SUM(s.quantity) AS quantity,
                   SUM(CASE WHEN s.unit_price IS NOT NULL
                            THEN s.quantity*s.unit_price END) AS amount_excl_tax,
                   SUM(CASE WHEN s.tax_unit_price IS NOT NULL
                            THEN s.quantity*s.tax_unit_price END) AS amount_incl_tax,
                   COUNT(*) AS detail_count,
                   SUM(CASE WHEN s.unit_price IS NULL THEN 1 ELSE 0 END)
                       AS missing_price_count
            FROM sales_outbound s
            WHERE s.bill_date >= :start AND s.bill_date < :end
              AND (:unit IS NULL OR s.unit = :unit)
              AND (:customer IS NULL OR s.customer_code = :customer)
              AND (:material IS NULL OR s.material_code = :material)
              AND (:currency IS NULL OR s.currency_code = :currency)
            GROUP BY week_start,week_end,s.currency_code,s.material_code,
                     s.material_name,s.length_value,s.brand_code,s.brand_name,s.unit
            ORDER BY week_start,s.currency_code,s.material_code,s.length_value,
                     s.brand_code,s.unit
        """, params)]
    elif name == "weekly-summary":
        rows = [dict(row) for row in conn.execute("""
            SELECT date(s.bill_date,'weekday 0','-6 days') AS week_start,
                   date(s.bill_date,'weekday 0') AS week_end,
                   s.currency_code,
                   COUNT(*) AS detail_count,
                   COUNT(DISTINCT s.bill_date) AS days_with_data,
                   COUNT(DISTINCT s.document_id) AS documents,
                   COUNT(DISTINCT s.customer_code) AS customers,
                   COUNT(DISTINCT s.material_code) AS materials,
                   ROUND(SUM(CASE WHEN s.unit_price IS NOT NULL
                                  THEN s.quantity*s.unit_price END),2)
                       AS amount_excl_tax,
                   ROUND(SUM(CASE WHEN s.tax_unit_price IS NOT NULL
                                  THEN s.quantity*s.tax_unit_price END),2)
                       AS amount_incl_tax,
                   SUM(CASE WHEN s.unit_price IS NULL THEN 1 ELSE 0 END)
                       AS missing_price_count
            FROM sales_outbound s
            WHERE s.bill_date >= :start AND s.bill_date < :end
              AND (:unit IS NULL OR s.unit = :unit)
              AND (:customer IS NULL OR s.customer_code = :customer)
              AND (:material IS NULL OR s.material_code = :material)
              AND (:currency IS NULL OR s.currency_code = :currency)
            GROUP BY week_start,week_end,s.currency_code
            ORDER BY week_start,s.currency_code
        """, params)]
        quantity_rows = [dict(row) for row in conn.execute("""
            SELECT date(s.bill_date,'weekday 0','-6 days') AS week_start,
                   s.currency_code, s.unit, SUM(s.quantity) AS quantity
            FROM sales_outbound s
            WHERE s.bill_date >= :start AND s.bill_date < :end
              AND (:unit IS NULL OR s.unit = :unit)
              AND (:customer IS NULL OR s.customer_code = :customer)
              AND (:material IS NULL OR s.material_code = :material)
              AND (:currency IS NULL OR s.currency_code = :currency)
            GROUP BY week_start,s.currency_code,s.unit
            ORDER BY week_start,s.currency_code,s.unit
        """, params)]
        rows = _weekly_summary(rows, quantity_rows)
    else:
        raise ValueError(f"Unknown report: {name}")
    if name == "amount-rank" and group == "customer":
        rows = _rank_by_group(rows, ("customer_code", "customer_name"))
    elif name == "amount-rank":
        rows = _rank_by_material(rows)
    elif name == "usage-rank":
        rows = _rank_by_quantity(rows)
    elif name == "salesperson-rank":
        rows = _rank_by_group(rows, ("salesperson",))
    if top is not None and name in ("amount-rank", "usage-rank",
                                    "salesperson-rank"):
        rows = [row for row in rows if row["rank"] <= top]
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path,
                        default=Path(__file__).parent / "data" / "erp_trial.sqlite")
    parser.add_argument("--report",
                        choices=["shipments", "details", "issues", "amount-rank",
                                 "usage-rank", "salesperson-rank",
                                 "weekly-summary"],
                        required=True)
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--unit")
    parser.add_argument("--customer")
    parser.add_argument("--material")
    parser.add_argument("--currency",
                        help="只保留一种币种；省略时按币种分别排名，金额不跨币种相加")
    parser.add_argument("--group", choices=["customer", "material"],
                        help="amount-rank 的排名维度")
    parser.add_argument("--top", type=int,
                        help="只保留每周前 N 名（amount-rank / usage-rank / salesperson-rank）")
    parser.add_argument("--owner", choices=["saler", "follow", "inner"],
                        help="salesperson-rank 用谁：saler 销售员（默认）、follow 业务跟单、inner 内部跟单")
    parser.add_argument("--csv", type=Path)
    args = parser.parse_args()
    conn = connect_readonly(args.db)
    try:
        rows = report(conn, args.report, args.start, args.end, args.unit,
                      args.customer, args.material, args.currency, args.group,
                      args.top, args.owner)
    finally:
        conn.close()
    if args.csv:
        if not rows:
            raise ValueError("没有匹配记录；未写出 CSV")
        with args.csv.open("x", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        print(f"Exported {len(rows)} rows")
    else:
        print(json.dumps(rows, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
