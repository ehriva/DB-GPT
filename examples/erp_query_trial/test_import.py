import json
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from build_db import (
    bill_entries,
    iso_date,
    number,
    production_entries,
    text,
    unpack_rows,
)
from query_db import MISSING_OWNER, connect_readonly, report


class ImportTests(unittest.TestCase):
    def test_continuation_and_payment_rows_do_not_duplicate_items(self):
        keys = ["head", "FBillNo", "FDate", "FCustId", "*Split*2", "entry"]
        rows = [
            ["1", "same-number", "2025-07-01", "C1", None, "11"],
            [None, None, None, None, None, None],  # Payment-only row.
            [None, None, None, None, None, "12"],
            ["2", "same-number", "2025-07-02", "C2", None, "21"],
        ]
        entries = bill_entries(keys, rows, "entry")
        self.assertEqual([r[0] for r in entries], [3, 5, 6])
        self.assertEqual([r[1]["head"] for r in entries], ["1", "1", "2"])
        self.assertEqual(entries[-1][1]["FCustId"], "C2")

    def test_new_header_clears_previous_optional_fields(self):
        keys = ["head", "currency", "*Split*2", "entry"]
        entries = bill_entries(
            keys, [["1", "RMB", None, "11"], ["2", None, None, "21"]], "entry"
        )
        self.assertIsNone(entries[-1][1]["currency"])

    def test_orphan_continuation_fails(self):
        with self.assertRaises(ValueError):
            bill_entries(["head", "*Split*2", "entry"], [[None, None, "1"]], "entry")

    def test_production_continuation_preserves_item_and_resets_customer(self):
        keys = ["单据编号", "日期", "客户", "单据状态", "终端客户", "物料编码"]
        rows = [
            ["P1", "2025/07/01", "C1", "已审核", None, "M1"],
            [None, "2025/07/01", None, None, None, "M2"],
            ["P2", "2025/07/02", None, "已审核", None, "M3"],
        ]
        entries = production_entries(keys, rows)
        self.assertEqual(entries[1][1]["单据编号"], "P1")
        self.assertEqual(entries[1][1]["客户"], "C1")
        self.assertEqual(entries[1][1]["物料编码"], "M2")
        self.assertIsNone(entries[2][1]["客户"])

    def test_missing_quantity_is_not_zero_and_codes_preserve_zeroes(self):
        self.assertIsNone(
            unpack_rows(["code", "optional"], [["M1"]], 2)[0][1]["optional"]
        )
        self.assertIsNone(number(None))
        self.assertEqual(number("0"), 0)
        self.assertEqual(text("001.02"), "001.02")
        self.assertEqual(text(123.0), "123")
        self.assertEqual(iso_date("2025/07/01"), "2025-07-01")
        self.assertEqual(iso_date("2025/12/8"), "2025-12-08")
        with self.assertRaises(ValueError):
            number("NaN")

    def test_readonly_connection_rejects_writes(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "trial.sqlite"
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE sample(id INTEGER)")
            conn.close()
            readonly = connect_readonly(path)
            try:
                with self.assertRaises(sqlite3.OperationalError):
                    readonly.execute("INSERT INTO sample VALUES (1)")
                with self.assertRaises(ValueError):
                    report(readonly, "shipments", "2025-07-01", "2025-07-08")
                with self.assertRaises(ValueError):
                    report(readonly, "details", "2025-07-08", "2025-07-01")
            finally:
                readonly.close()

    def test_ranking_keeps_units_and_variants_separate_and_end_exclusive(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(Path(__file__).with_name("schema.sql").read_text("utf-8"))
        conn.execute("INSERT INTO source_sheets VALUES (1,'sample','Sheet1',1)")
        samples = [
            ("2025-08-18", "Pcs", "1", 10),
            ("2025-08-24", "Pcs", "2", 20),
            ("2025-08-25", "Pcs", "1", 100),
            ("2025-08-20", "m", "1", 999),
        ]
        try:
            for i, (day, unit, length, qty) in enumerate(samples, 2):
                conn.execute("INSERT INTO raw_rows VALUES (1,?,'[]','{}')", (i,))
                conn.execute(
                    "INSERT INTO sales_outbound(entry_id,document_id,bill_no,bill_date,"
                    "customer_code,material_code,length_value,unit,quantity,sheet_id,"
                    "excel_row) VALUES (?,?,'B',?,'C','M',?,?,?,1,?)",
                    (str(i), str(i), day, length, unit, qty, i),
                )
            ranked = report(conn, "shipments", "2025-08-18", "2025-08-25", "Pcs")
            self.assertEqual([r["shipped_quantity"] for r in ranked], [20, 10])
            self.assertEqual([r["length_value"] for r in ranked], ["2", "1"])
        finally:
            conn.close()

    def test_amount_rank_is_per_currency_week_and_keeps_units_apart(self):
        conn = self._sample_outbound_db(
            [
                ("2025-08-18", "PRE001", "C1", "M1", "Pcs", 10, 100.0, 113.0),
                ("2025-08-19", "PRE001", "C1", "M1", "Pcs", 5, 200.0, 226.0),
                ("2025-08-20", "PRE001", "C2", "M2", "Pcs", 100, 1.0, 1.13),
                ("2025-08-19", "PRE001", "C1", "M3", "m", 3, 10.0, 11.3),
                ("2025-08-25", "PRE001", "C1", "M1", "Pcs", 999, 999.0, 999.0),
                ("2025-08-18", "PRE007", "C3", "M4", "Pcs", 1, 50.0, 50.0),
                ("2025-08-19", "PRE001", "C2", "M2", "Pcs", 7, None, None),
                ("2025-08-20", "PRE001", "C4", "M5", "Pcs", 9, None, None),
            ]
        )
        try:
            with self.assertRaises(ValueError):
                report(
                    conn, "amount-rank", "2025-08-18", "2025-08-25", currency="PRE001"
                )
            both = report(
                conn, "amount-rank", "2025-08-18", "2025-08-25", group="customer"
            )
            self.assertEqual(
                [(r["currency_code"], r["customer_code"], r["rank"]) for r in both],
                [
                    ("PRE001", "C1", 1),
                    ("PRE001", "C2", 2),
                    ("PRE001", "C4", 3),
                    ("PRE007", "C3", 1),
                ],
            )
            ranked = report(
                conn,
                "amount-rank",
                "2025-08-18",
                "2025-09-01",
                currency="PRE001",
                group="customer",
            )
            self.assertEqual(
                [(r["week"], r["customer_code"], r["rank"]) for r in ranked],
                [
                    ("2025-W34", "C1", 1),
                    ("2025-W34", "C2", 2),
                    ("2025-W34", "C4", 3),
                    ("2025-W35", "C1", 1),
                ],
            )
            top = ranked[0]
            self.assertEqual(top["customer_code"], "C1")
            self.assertEqual(top["amount_excl_tax"], 2030.0)
            self.assertEqual(top["amount_incl_tax"], 2293.9)
            self.assertEqual(top["quantity_Pcs"], 15)
            self.assertEqual(top["quantity_m"], 3)
            self.assertEqual(top["units"], "Pcs;m")
            second = ranked[1]
            self.assertEqual(second["amount_excl_tax"], 100.0)
            self.assertEqual(second["quantity_Pcs"], 107)  # unpriced row counted
            self.assertEqual(second["missing_price_count"], 1)
            unpriced = ranked[2]
            self.assertIsNone(unpriced["amount_excl_tax"])  # unknown, not zero
            self.assertEqual(unpriced["quantity_Pcs"], 9)
            self.assertEqual(unpriced["missing_price_count"], 1)
            self.assertEqual(ranked[3]["amount_excl_tax"], 999.0 * 999)
            other = report(
                conn,
                "amount-rank",
                "2025-08-18",
                "2025-08-25",
                currency="PRE007",
                group="customer",
            )
            self.assertEqual([r["customer_code"] for r in other], ["C3"])
            materials = report(
                conn,
                "amount-rank",
                "2025-08-18",
                "2025-08-25",
                currency="PRE001",
                group="material",
            )
            self.assertEqual(
                [r["material_code"] for r in materials[:3]], ["M1", "M2", "M3"]
            )
            self.assertEqual([r["rank"] for r in materials[:3]], [1, 2, 3])
            self.assertEqual(materials[0]["unit"], "Pcs")
        finally:
            conn.close()

    def test_weekly_summary_counts_coverage_per_currency(self):
        conn = self._sample_outbound_db(
            [
                ("2025-08-18", "PRE001", "C1", "M1", "Pcs", 10, 100.0, 113.0),
                ("2025-08-19", "PRE001", "C2", "M2", "Pcs", 100, 1.0, 1.13),
                ("2025-08-19", "PRE001", "C2", "M2", "Pcs", 7, None, None),
                ("2025-08-18", "PRE007", "C3", "M4", "Pcs", 1, 50.0, 50.0),
            ]
        )
        try:
            summary = report(
                conn, "weekly-summary", "2025-08-18", "2025-08-25", currency="PRE001"
            )
            self.assertEqual(len(summary), 1)
            week = summary[0]
            self.assertEqual(week["week"], "2025-W34")
            self.assertEqual(week["week_start"], "2025-08-18")
            self.assertEqual(week["week_end"], "2025-08-24")
            self.assertEqual(week["detail_count"], 3)
            self.assertEqual(week["days_with_data"], 2)
            self.assertEqual(week["customers"], 2)
            self.assertEqual(week["quantity_Pcs"], 117)  # unpriced row included
            self.assertNotIn("quantity", week)  # never one quantity across units
            self.assertEqual(week["amount_excl_tax"], 1100.0)
            self.assertEqual(week["missing_price_count"], 1)
        finally:
            conn.close()

    def test_half_cent_amounts_round_up_like_excel(self):
        conn = self._sample_outbound_db(
            [
                ("2025-08-18", "PRE001", "C1", "M1", "Pcs", 50, 10.4425, 10.4425),
            ]
        )
        try:
            ranked = report(
                conn,
                "amount-rank",
                "2025-08-18",
                "2025-08-25",
                currency="PRE001",
                group="customer",
            )
            # 50 * 10.4425 is exactly 522.125; banker's rounding would say 522.12.
            self.assertEqual(ranked[0]["amount_excl_tax"], 522.13)
            self.assertEqual(ranked[0]["amount_incl_tax"], 522.13)
        finally:
            conn.close()

    def test_usage_rank_is_per_week_and_needs_a_unit(self):
        conn = self._sample_outbound_db(
            [
                ("2025-08-18", "PRE001", "C1", "M1", "Pcs", 100, 1.0, 1.0),
                ("2025-08-19", "PRE001", "C2", "M2", "Pcs", 900, 1.0, 1.0),
                ("2025-08-20", "PRE001", "C1", "M1", "Pcs", 50, 1.0, 1.0),
                ("2025-08-25", "PRE001", "C1", "M2", "Pcs", 7, 1.0, 1.0),
                ("2025-08-20", "PRE001", "C1", "M3", "m", 9999, 1.0, 1.0),
            ]
        )
        try:
            with self.assertRaises(ValueError):
                report(conn, "usage-rank", "2025-08-18", "2025-09-01")
            ranked = report(conn, "usage-rank", "2025-08-18", "2025-09-01", "Pcs")
            self.assertEqual(
                [
                    (r["week"], r["material_code"], r["quantity"], r["rank"])
                    for r in ranked
                ],
                [
                    ("2025-W34", "M2", 900, 1),
                    ("2025-W34", "M1", 150, 2),
                    ("2025-W35", "M2", 7, 1),
                ],
            )
            # The 9,999 m row must not enter a Pcs ranking.
            self.assertNotIn("M3", [row["material_code"] for row in ranked])
            top = report(conn, "usage-rank", "2025-08-18", "2025-09-01", "Pcs", top=1)
            self.assertEqual(
                [(r["week"], r["material_code"]) for r in top],
                [("2025-W34", "M2"), ("2025-W35", "M2")],
            )
            self.assertEqual(set(top[0]), set(ranked[0]))
        finally:
            conn.close()

    def test_salesperson_rank_reads_the_owner_from_the_document_header(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(Path(__file__).with_name("schema.sql").read_text("utf-8"))
        conn.execute("INSERT INTO source_sheets VALUES (1,'sample','Sheet1',1)")
        # The owner sits on the document's first raw row only (column 23);
        # column 4 is the bill number that marks a new document.
        for excel_row, bill, owner in (
            (2, "B1", "张三"),
            (3, None, None),
            (4, "B2", "李四"),
        ):
            values = [None] * 70
            values[4] = bill
            values[23] = owner
            conn.execute(
                "INSERT INTO raw_rows VALUES (1,?,?,'{}')",
                (excel_row, json.dumps(values)),
            )
        for excel_row, document, day, quantity in (
            (2, "B1", "2025-08-18", 100.0),
            (3, "B1", "2025-08-19", 50.0),
            (4, "B2", "2025-08-18", 900.0),
        ):
            conn.execute(
                "INSERT INTO sales_outbound(entry_id,document_id,bill_no,bill_date,"
                "customer_code,material_code,unit,quantity,currency_code,"
                "unit_price,sheet_id,excel_row)"
                " VALUES (?,?,'B',?,'C','M','Pcs',?,'PRE001',1.0,1,?)",
                (str(excel_row), document, day, quantity, excel_row),
            )
        try:
            ranked = report(conn, "salesperson-rank", "2025-08-18", "2025-09-01", "Pcs")
            # The second line of B1 has no owner cell and must inherit 张三.
            self.assertEqual(
                [(r["salesperson"], r["quantity_Pcs"], r["rank"]) for r in ranked],
                [("李四", 900, 1), ("张三", 150, 2)],
            )
            self.assertEqual(ranked[1]["amount_excl_tax"], 150.0)
            # An unpopulated owner column must not drop lines silently.
            follow = report(
                conn,
                "salesperson-rank",
                "2025-08-18",
                "2025-09-01",
                "Pcs",
                owner="follow",
            )
            self.assertEqual([r["salesperson"] for r in follow], [MISSING_OWNER])
            self.assertEqual(follow[0]["quantity_Pcs"], 1050)
        finally:
            conn.close()

    @staticmethod
    def _sample_outbound_db(rows: list[tuple]) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(Path(__file__).with_name("schema.sql").read_text("utf-8"))
        conn.execute("INSERT INTO source_sheets VALUES (1,'sample','Sheet1',1)")
        for i, (day, currency, customer, material, unit, qty, price, tax) in enumerate(
            rows, 2
        ):
            conn.execute("INSERT INTO raw_rows VALUES (1,?,'[]','{}')", (i,))
            conn.execute(
                "INSERT INTO sales_outbound(entry_id,document_id,bill_no,bill_date,"
                "customer_code,material_code,unit,quantity,currency_code,"
                "unit_price,tax_unit_price,sheet_id,excel_row)"
                " VALUES (?,?,'B',?,?,?,?,?,?,?,?,1,?)",
                (
                    str(i),
                    str(i),
                    day,
                    customer,
                    material,
                    unit,
                    qty,
                    currency,
                    price,
                    tax,
                    i,
                ),
            )
        return conn


if __name__ == "__main__":
    unittest.main()
