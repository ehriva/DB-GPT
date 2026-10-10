PRAGMA foreign_keys = ON;

CREATE TABLE source_sheets (
    id INTEGER PRIMARY KEY,
    workbook TEXT NOT NULL,
    sheet TEXT NOT NULL,
    header_rows INTEGER NOT NULL,
    UNIQUE(workbook, sheet)
);
CREATE TABLE source_columns (
    sheet_id INTEGER NOT NULL REFERENCES source_sheets(id),
    column_number INTEGER NOT NULL,
    field_key TEXT,
    field_label TEXT,
    PRIMARY KEY(sheet_id, column_number)
);
CREATE TABLE raw_rows (
    sheet_id INTEGER NOT NULL REFERENCES source_sheets(id),
    excel_row INTEGER NOT NULL,
    values_json TEXT NOT NULL,
    formulas_json TEXT NOT NULL,
    PRIMARY KEY(sheet_id, excel_row)
);
CREATE TABLE customers (
    customer_code TEXT PRIMARY KEY NOT NULL,
    customer_name TEXT NOT NULL,
    short_name TEXT,
    organization TEXT,
    customer_group TEXT,
    document_status TEXT,
    disabled TEXT,
    sheet_id INTEGER NOT NULL,
    excel_row INTEGER NOT NULL,
    FOREIGN KEY(sheet_id, excel_row) REFERENCES raw_rows(sheet_id, excel_row)
);
CREATE TABLE materials (
    material_code TEXT PRIMARY KEY NOT NULL,
    material_name TEXT NOT NULL,
    specification TEXT,
    organization TEXT,
    base_unit TEXT,
    material_group TEXT,
    inventory_category TEXT,
    document_status TEXT,
    disabled TEXT,
    sheet_id INTEGER NOT NULL,
    excel_row INTEGER NOT NULL,
    FOREIGN KEY(sheet_id, excel_row) REFERENCES raw_rows(sheet_id, excel_row)
);
CREATE TABLE sales_orders (
    entry_id TEXT PRIMARY KEY NOT NULL,
    document_id TEXT NOT NULL,
    bill_no TEXT NOT NULL,
    bill_date TEXT NOT NULL,
    customer_code TEXT NOT NULL,
    customer_name TEXT,
    sales_org_code TEXT,
    currency_code TEXT,
    material_code TEXT NOT NULL,
    material_name TEXT,
    customer_material_code TEXT,
    length_value TEXT,
    brand_code TEXT,
    brand_name TEXT,
    unit TEXT NOT NULL,
    quantity REAL NOT NULL,
    requested_delivery_at TEXT,
    unit_price REAL,
    tax_unit_price REAL,
    current_inventory REAL,
    available_inventory REAL,
    return_type TEXT,
    document_status TEXT,
    sheet_id INTEGER NOT NULL,
    excel_row INTEGER NOT NULL,
    FOREIGN KEY(sheet_id, excel_row) REFERENCES raw_rows(sheet_id, excel_row)
);
CREATE TABLE sales_outbound (
    entry_id TEXT PRIMARY KEY NOT NULL,
    document_id TEXT NOT NULL,
    bill_no TEXT NOT NULL,
    bill_date TEXT NOT NULL,
    customer_code TEXT NOT NULL,
    customer_name TEXT,
    sales_org_code TEXT,
    currency_code TEXT,
    material_code TEXT NOT NULL,
    material_name TEXT,
    customer_material_code TEXT,
    length_value TEXT,
    brand_code TEXT,
    brand_name TEXT,
    unit TEXT NOT NULL,
    quantity REAL NOT NULL,
    warehouse_code TEXT,
    warehouse_name TEXT,
    source_order_entry_id TEXT,
    unit_price REAL,
    tax_unit_price REAL,
    current_inventory REAL,
    document_status TEXT,
    sheet_id INTEGER NOT NULL,
    excel_row INTEGER NOT NULL,
    FOREIGN KEY(sheet_id, excel_row) REFERENCES raw_rows(sheet_id, excel_row)
);
CREATE TABLE production_receipts (
    id INTEGER PRIMARY KEY,
    bill_no TEXT NOT NULL,
    bill_date TEXT NOT NULL,
    customer_name TEXT,
    customer_code TEXT,
    terminal_customer_name TEXT,
    material_code TEXT NOT NULL,
    material_name TEXT,
    product_type TEXT,
    unit TEXT NOT NULL,
    quantity REAL NOT NULL,
    warehouse_name TEXT,
    length_value TEXT,
    brand_code TEXT,
    brand_name TEXT,
    document_status TEXT,
    sheet_id INTEGER NOT NULL,
    excel_row INTEGER NOT NULL,
    UNIQUE(sheet_id, excel_row),
    FOREIGN KEY(sheet_id, excel_row) REFERENCES raw_rows(sheet_id, excel_row)
);
CREATE TABLE production_standards (
    material_code TEXT PRIMARY KEY NOT NULL,
    material_name TEXT,
    assembly_value REAL,
    coating_value REAL,
    total_labor_cost REAL,
    total_labor_minutes REAL,
    sheet_id INTEGER NOT NULL,
    excel_row INTEGER NOT NULL,
    FOREIGN KEY(sheet_id, excel_row) REFERENCES raw_rows(sheet_id, excel_row)
);
CREATE TABLE data_issues (
    id INTEGER PRIMARY KEY,
    issue_type TEXT NOT NULL,
    business_table TEXT NOT NULL,
    sheet_id INTEGER,
    excel_row INTEGER,
    detail TEXT NOT NULL
);

CREATE INDEX orders_document ON sales_orders(document_id);
CREATE INDEX orders_date_customer ON sales_orders(bill_date, customer_code);
CREATE INDEX orders_material_delivery ON sales_orders(material_code, requested_delivery_at);
CREATE INDEX outbound_date_customer ON sales_outbound(bill_date, customer_code);
CREATE INDEX outbound_material ON sales_outbound(material_code);
CREATE INDEX outbound_order_entry ON sales_outbound(source_order_entry_id);
CREATE INDEX receipts_date_material ON production_receipts(bill_date, material_code);

CREATE VIEW v_sales_order_shipments AS
SELECT o.entry_id, o.bill_no, o.customer_code, o.material_code,
       o.length_value, o.brand_code, o.unit, o.quantity AS ordered_quantity,
       o.requested_delivery_at,
       COALESCE(s.shipped_quantity, 0) AS shipped_quantity_in_export,
       '仅为本次导出范围内的出货累计，不代表完整履约或当前欠交' AS scope_note
FROM sales_orders o LEFT JOIN (
    SELECT source_order_entry_id, customer_code, material_code, unit, length_value, brand_code,
           SUM(quantity) AS shipped_quantity
    FROM sales_outbound
    GROUP BY source_order_entry_id, customer_code, material_code, unit, length_value, brand_code
) s ON s.source_order_entry_id = o.entry_id
    AND s.customer_code = o.customer_code
    AND s.material_code = o.material_code AND s.unit = o.unit
    AND s.length_value IS o.length_value AND s.brand_code IS o.brand_code;

CREATE VIEW v_data_issue_summary AS
SELECT issue_type, business_table, COUNT(*) AS affected_count
FROM data_issues GROUP BY issue_type, business_table;
