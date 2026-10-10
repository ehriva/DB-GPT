"""One command for the weekly sales-amount ranking: by customer and by product.

    python -B weekly_rank.py

Read-only against data/erp_trial.sqlite. Writes the three ranking CSVs into
--out-dir (default data/) and prints the period totals with a top-N ranking, so
the console output is enough to answer the question without opening a file.

Re-running replaces those CSVs: they are derived artifacts, reproducible from
the database, not something to edit by hand. The date range defaults to
everything in sales_outbound, so the bare command always covers the whole
sample. Ranks are computed per week and currency and are never combined across
currencies; README.md "销售额口径与周排名" holds the amount definition and its
limits (unpriced rows, missing document status, no returns).
"""

import argparse
import csv
from datetime import date, timedelta
from pathlib import Path

from query_db import connect_readonly, report


def write_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def totals_by_currency(rows: list[dict], field: str) -> dict[str, float]:
    totals: dict[str, float] = {}
    for row in rows:
        if row[field]:
            totals[row["currency_code"]] = (totals.get(row["currency_code"], 0.0)
                                            + float(row[field]))
    return totals


def overall(rows: list[dict], key) -> dict:
    """Sum the weekly rows into a period total per entity, skipping unpriced ones."""
    totals: dict = {}
    for row in rows:
        if not row["amount_excl_tax"]:
            continue
        totals[key(row)] = totals.get(key(row), 0.0) + float(row["amount_excl_tax"])
    return totals


def print_ranking(title: str, totals: dict, label, top: int) -> None:
    if top <= 0 or not totals:
        return
    print(f"  {title}")
    for rank, (entity, total) in enumerate(
            sorted(totals.items(), key=lambda item: -item[1])[:top], start=1):
        print(f"    {rank:>3}  {label(entity):<52} {total:>14,.2f}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    base = Path(__file__).parent
    parser.add_argument("--db", type=Path,
                        default=base / "data" / "erp_trial.sqlite")
    parser.add_argument("--out-dir", type=Path, default=base / "data",
                        help="CSV 输出目录（默认 data/，同名文件会被覆盖）")
    parser.add_argument("--start", help="默认取库中最早的出货日期")
    parser.add_argument("--end", help="不含当天；默认取库中最后出货日期的次日")
    parser.add_argument("--currency", help="只保留一种币种；默认两种分别排名")
    parser.add_argument("--top", type=int, default=10, help="控制台打印前几名（默认 10）")
    args = parser.parse_args()

    conn = connect_readonly(args.db)
    try:
        start, end = args.start, args.end
        if start is None or end is None:
            first, last = conn.execute(
                "SELECT MIN(bill_date), MAX(bill_date) FROM sales_outbound"
            ).fetchone()
            if first is None:
                raise SystemExit("sales_outbound 没有数据")
            start = start or first
            end = end or (date.fromisoformat(last) + timedelta(days=1)).isoformat()
        customers = report(conn, "amount-rank", start, end,
                           currency=args.currency, group="customer")
        materials = report(conn, "amount-rank", start, end,
                           currency=args.currency, group="material")
        summary = report(conn, "weekly-summary", start, end,
                         currency=args.currency)
    finally:
        conn.close()
    if not summary:
        raise SystemExit(f"{start} 至 {end} 没有出货记录")

    week_starts = sorted({row["week_start"] for row in summary})
    span = f"{week_starts[0].replace('-', '')}_{week_starts[-1].replace('-', '')}"
    args.out_dir.mkdir(parents=True, exist_ok=True)
    outputs = [
        (args.out_dir / f"销售额周排名_客户_{span}.csv", customers),
        (args.out_dir / f"销售额周排名_产品_{span}.csv", materials),
        (args.out_dir / f"销售额周汇总_{span}.csv", summary),
    ]
    for path, rows in outputs:
        write_csv(path, rows)

    detail_rows = sum(int(row["detail_count"]) for row in summary)
    missing_rows = sum(int(row["missing_price_count"]) for row in summary)
    week_end = max(row["week_end"] for row in summary)
    print(f"周期 {week_starts[0]} 至 {week_end}（{len(week_starts)} 周，周一至周日），"
          f"明细 {detail_rows:,} 行，其中缺价 {missing_rows} 行不计金额")
    print("口径：出库数量 × 单价推算的未税金额；PRE001 与 PRE007 不跨币种相加；"
          "排名在“周 × 币种”内计算")
    excl = totals_by_currency(summary, "amount_excl_tax")
    incl = totals_by_currency(summary, "amount_incl_tax")
    for currency in sorted(excl):
        print(f"\n{currency}  未税 {excl[currency]:,.2f}（含税 {incl.get(currency, 0):,.2f}）")
        print_ranking("分客户", overall(
            [r for r in customers if r["currency_code"] == currency],
            lambda r: r["customer_code"]),
            lambda code: f"{code}", args.top)
        print_ranking("分产品（物料 + 长度 + 品牌 + 单位）", overall(
            [r for r in materials if r["currency_code"] == currency],
            lambda r: (r["material_code"], r["length_value"], r["brand_code"],
                       r["unit"])),
            lambda key: f"{key[0]}  长={key[1] or '-'}  {key[2] or '-'}  {key[3]}",
            args.top)
    print("\n已写出：")
    for path, rows in outputs:
        print(f"  {path}  ({len(rows)} 行)")
    print("核对数据请运行 python -B validate_rank.py")


if __name__ == "__main__":
    main()
