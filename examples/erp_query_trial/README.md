# ERP 独立试验查询数据库

数据库：`data/erp_trial.sqlite`。SQLite 是一个本地文件，无需启动数据库服务。
本目录是独立试验，不改动 DB-GPT 服务，不连接 ERP，不修改源 Excel。
所有业务数据、报告和查询结果位于 Git 忽略的 `data/` 下。

## 已导入的数据

| 主要业务表 | 内容 | 行数 | 日期范围 |
|---|---|---:|---|
| `customers` | 客户主档 | 369 | — |
| `materials` | 物料主档 | 1,875 | — |
| `sales_orders` | 销售订单产品明细，含所属单据信息 | 770 | 2025-07-01 至 2025-08-30 |
| `sales_outbound` | 实际销售出库明细，含所属单据信息 | 1,624 | 2025-07-08 至 2025-12-29 |
| `production_receipts` | 生产入库明细，含所属单据信息 | 5,382 | 2025-01-04 至 2025-12-31 |

生产文件的第二个工作表另导入 `production_standards`（1,012 条有物料编码的工时记录）。
230 条无编码工时行只保留在原始层；一条工时记录的四个 `#N/A` 值设为 NULL，并记录问题。

`source_sheets` 保存文件和工作表名称，`source_columns` 保存每列原始字段键及中文说明。
`raw_rows` 保存全部 11,958 个原始数据行的值和公式，不含表头。
每条业务记录有 `sheet_id` 和 `excel_row`，可定位源文件及 Excel 行号。

## 导入规则与字段含义

- 客户、物料以编码关联，名称只用于显示。生产入库仅有客户名称，只有主档名称精确且唯一匹配时才补客户编码。
- 订单使用内部 `document_id` 和 `entry_id`，不能用订单号作为唯一键：源数据有 468 个订单内部 ID，但仅 455 个不同订单号。
- 订单、出库单据头在明细续行中补齐，新单据重新开始；收款计划等行不算产品订单。订单文件的 1,466 行中只有 770 行产品明细。
- 生产入库只补齐同一单据的前五个单据头字段，不把上一单据的客户带到下一单据。
- `sales_orders.quantity` 对应 `FQty`（销售数量）；`sales_outbound.quantity` 对应 `FRealQty`（实发数量）；生产入库对应“实收数量”。
- `bill_date` 是单据日期，不是审核日期；`requested_delivery_at` 是订单明细的“要货日期”，其是否等于业务所说的交期需确认。
- `length_value`、`brand_code`、`unit` 保留原值，排名按物料、长度、品牌、单位区分。不同单位不相加，生产的“米”也未自动改成出库的 `m`。
- 所有 3,389 个 Excel 公式的原文及缓存值已保留，缓存值已按原始引用/工时公式核对。
- 库存、退补类型和销售单据审核/作废状态缺失时保留 NULL，不表示零或“已审核”。价格保留，未推算销售金额。

## 查询方法

在 PowerShell 中切换到本目录。使用已经可用的 Python（包含 openpyxl）：

```powershell
Set-Location 'D:\code\DB-ERP\examples\erp_query_trial'
$erpPython = 'C:\Users\tongshuo\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'

# 2025-08-18 至 2025-08-24 的 Pcs 出货前十名；--end 不包含当天
& $erpPython -B query_db.py --report shipments --start 2025-08-18 --end 2025-08-25 --unit Pcs

# 同一周所有出货明细；可附加 --customer 客户编码 或 --material 物料编码
& $erpPython -B query_db.py --report details --start 2025-08-18 --end 2025-08-25 --unit Pcs

# 销售额周排名：--group customer 分客户，--group material 分产品
& $erpPython -B query_db.py --report amount-rank --group customer --start 2025-07-01 --end 2026-01-01

# 每周金额、明细行数、天数和缺价行数
& $erpPython -B query_db.py --report weekly-summary --start 2025-07-01 --end 2026-01-01

# 查看数据问题汇总
& $erpPython -B query_db.py --report issues

# 每周用量（出库数量）前五的产品；用量必须指定 --unit，Pcs 与 m 不相加
& $erpPython -B query_db.py --report usage-rank --unit Pcs --start 2025-07-01 --end 2026-01-01 --top 5

# 每周按业务员的销售排名；--owner 可选 saler（默认销售员）/ follow 业务跟单 / inner 内部跟单
& $erpPython -B query_db.py --report salesperson-rank --start 2025-07-01 --end 2026-01-01
```

以上查询以 SQLite `mode=ro` 和 `query_only` 打开数据库，禁止写入。
附加 `--csv data/新的结果文件.csv` 可导出 UTF-8 BOM CSV；已有文件不会覆盖。
数据库文件本身可由本机文件拥有者修改，后续 AI 必须使用只读连接；这不是服务器级用户权限隔离。

## 销售额口径与周排名

要重新出结果，只跑一条命令（零参数：自动覆盖库中全部出货日期，写出三个 CSV，并在控制台直接打印排名）：

```powershell
Set-Location 'D:\code\DB-ERP\examples\erp_query_trial'
& $erpPython -B weekly_rank.py
```

可选 `--start` / `--end`（不含当天）/ `--currency` / `--top N` / `--out-dir`。
它读只读连接，只覆盖自己生成的三个 CSV；跑完可用 `python -B validate_rank.py` 复核。
可复用工作流 `erp-weekly-sales-rank` 已保存到 `.zcode/workflows/`：它按同样参数调用
`weekly_rank.py`，再跑 `validate_rank.py`，然后把三个 CSV 与要点报告作为产物发布出来。

`amount-rank` 和 `weekly-summary` 从 `sales_outbound` 推算销售额：`金额 = 数量 × 单价`，
不依赖订单表。口径如下，使用前必须与业务确认：

- 金额是**按出货明细推算**的，不是开票额、不是财务确认的销售收入。
- `amount_excl_tax = 数量 × unit_price`（未税），`amount_incl_tax = 数量 × tax_unit_price`（含税）。
  排名按未税金额降序；两者都在 CSV 中给出，因为 PRE001 中有 83 行含税单价等于未税单价，按含税排名可能与按未税排名不同。
- **金额保留到分：四舍五入（半分进位），与 Excel 一致。**
- **两种币种 PRE001 与 PRE007 绝不相加**：没有汇率数据。每个金额行都带 `currency_code`，
  排名在"周 × 币种"内独立计算；`--currency` 只是把输出收窄到一种币种。
  PRE007 只有 4 个客户（其中 CUST0160 占绝大部分），客户排名信息量有限，产品排名仍有意义。
- **周为周一至周日**（ISO 周，`week` 列形如 `2025-W34`）。跨年的那一周是 `2026-W01`（2025-12-29 起）。
- **产品排名的粒度是"物料 + 长度 + 品牌 + 单位"**，与 `shipments` 报表一致；同一物料的不同长度分别排名。
- 缺价的 124 行**不记为 0**：它们计入 `quantity_*` 和 `missing_price_count`，但不计入金额；
  若某客户/产品在某一周全部行都缺价，`amount_*` 留空而不是 0。
- 单据审核/作废状态在导出中缺失，因此这是"导出记录的出货金额"，不是已确认有效销售，也没有退货冲减。
- 客户排名与周汇总都按单位拆分列出 `quantity_Pcs`、`quantity_m` 等列，**任何数量都不跨单位相加**。

已生成的结果文件（范围 2025-07-07 至 2026-01-04，共 26 周）：

| 文件 | 内容 | 行数 |
|---|---|---:|
| `data/销售额周排名_客户_20250707_20251229.csv` | 每周分客户排名，含未税/含税金额 | 266 |
| `data/销售额周排名_产品_20250707_20251229.csv` | 每周分产品（物料+长度+品牌+单位）排名 | 950 |
| `data/销售额周汇总_20250707_20251229.csv` | 每周每币种金额、分单位数量、明细行数、天数、客户/产品数、缺价行数 | 47 |

精确合计（未税，不跨币种相加）：PRE001 19,646,786.55，PRE007 5,530,817.95。
每个文件在自己的粒度上把金额保留到分，所以同一笔金额相加会差几分：
周汇总文件为 19,646,786.56 / 5,530,817.96，客户或产品排名文件为 19,646,786.58 / 5,530,817.98。
`validate_rank.py` 用独立 SQL 重算逐组核对：每个金额与精确值相差不超过半分，排名连续且金额降序，
每周第一名与重算一致，明细行数 1,624 行全部落入周桶。重新生成 CSV 后请再跑一次：

```powershell
& $erpPython -B validate_rank.py
```

若与 ERP 对账出现差异，先检查上面的舍入与币种口径。

`usage-rank` 的"用量"口径是**出库数量**（生产领料/消耗不在导出里），必须指定 `--unit`，
`--top N` 只保留每周前 N 名；数量是物理量、与币种无关，所以不按币种拆分。
注意按数量排名会由低价辅料主导：防尘帽单周可达 20 万 Pcs，而主产品里 `20.01.06.5057` 在 26 周中有 19 周居首。
需要"用量"按金额或按生产入库口径时，说明后另算。

`salesperson-rank` 的**业务员不在已导入的业务表里**，只在原始导出层，查询时按
`(sheet_id, excel_row)` 回原始行取单据头字段，再按单据头带值下延到该单据的每一行。
`--owner` 决定用哪个人：`saler` 销售员（默认）、`follow` 业务跟单、`inner` 内部跟单。
**销售员是客户归属人**：订单表与出库表两份独立单据对 83 个客户中有 76 个的销售员完全一致，
且每个客户基本只对应一人（83 个客户里只有 3 个跨人）；业务跟单/内部跟单则更常按单据变化。
全期结果高度集中——肖燕和名下 27 个客户、占 PRE001 未税 91.7%（8 月峰值单周 189 万），
因为几个最大的客户都挂在他名下；贺志名下有 26 个客户但只有 1.8 万金额，是几乎没有出货的客户。
这是客户归属的分布，不等于个人销售工作量。

**导出文件的表头标签与数据错位一列**：标注为"销售员#编码"的列里是姓名，标注为"销售员#名称"的列里是编码，
"客户#编码"列里是客户名称。所以销售员这几个未导入字段只能**按列号定位**（出库单第 23/64/66 列）而不是按标签，
换导出模板后必须重新核对。导入后的业务数据本身已核对正确。

也可使用 SQLite 客户端打开数据库。示例 SQL：

```sql
-- 分单位汇总，避免把件与米相加
SELECT unit, SUM(quantity) AS quantity FROM sales_outbound GROUP BY unit;

-- 通过问题行号追溯原始数据
SELECT d.issue_type, s.workbook, s.sheet, d.excel_row, d.detail
FROM data_issues d LEFT JOIN source_sheets s ON s.id=d.sheet_id;
```

`v_sales_order_shipments` 展示本次导出范围内、客户/物料/单位/辅助属性一致的订单关联出货累计。
它不代表完整履约、真实欠交或可用库存。尚未提供独立退货表。

## 已发现的数据限制

- 出库有 216 条明细找不到本次订单导出的关联记录；两个导出日期范围不同，不能直接判定为 ERP 错误。
- 3 条出库的关联订单存在，但客户、物料、单位或辅助属性不一致，已记录供业务确认。
- 订单 20 条、出库 34 条、生产入库 64 条明细的物料编码不在物料主档中；仍保留原明细与原名称，不丢弃、不虚构主档。
- 3 条生产入库客户名称无法唯一匹配主档；2 条有效工时记录的物料不在主档。
- 销售订单和出库未导出审核/作废状态，目前排名是“导出记录的出货数量”，不是已确认有效销售或净销售。
- 出库金额自 2025-10（W40）起明显下降：周金额从 7–9 月的 100–226 万降到 3–77 万，每周覆盖客户数从约 20 家降到 1–6 家。
  这是导出数据的实际内容，是真实下降还是导出范围不完整，需业务确认后才能解读趋势。
- 样本是历史静态数据，尚不能回答今天/昨天、退货量、交期满足情况。
- **库存完全没有数据**：`sales_orders.current_inventory`、`sales_orders.available_inventory`、`sales_outbound.current_inventory`
  全为 NULL（0/770、0/770、0/1,624）。源文件里"当前库存""可用库存"两列既没有值也没有公式，是空列；
  订单表的"库存数量""可发库存"两列虽有 770 个值，但**逐行等于订单数量**（770/770 相同），不是库存。
  全库没有库存表、没有出入库流水、没有期初结存。要回答库存余额（含每周结存），需从 ERP 另导
  "即时库存"或"库存收发存汇总表"，维度为物料 + 仓库 + 日期。
- 销售订单和出库单的表头标签与数据错位一列，且业务员（销售员/业务跟单/内部跟单）没有导入业务表，
  只能按列号从原始层取；客户主档的"销售员""业务跟单"两列是空的，无法用作客户归属字典。

## 重建与验证

导入需要 Python 3.10+ 和 openpyxl；查询仅依赖 Python 标准库。
导入会创建本地文件，不会覆盖已有数据库。更新数据时指定新的 `--output`。

```powershell
& $erpPython -B build_db.py --source-dir '导出文件所在目录' --output data/erp_trial_new.sqlite
& $erpPython -B validate_db.py --source-dir '导出文件所在目录' --db data/erp_trial_new.sqlite
& $erpPython -B -m unittest -v test_import
& $erpPython -B validate_rank.py
```

导入报告：`data/erp_trial.import.json`；源表核对报告：`data/erp_trial.validation.json`。
已逐行核对原始值/公式、业务编码/单据/日期/数量，分单位合计一致，一周前十名与源表独立计算一致。
人工验收仍需在 ERP 中按同样日期、单位及单据状态查询并对账。

## 本地嵌入模型配置

`configs/dbgpt-proxy-deepseek.toml` 的嵌入部分使用 `Qwen/Qwen3-Embedding-0.6B`。
本地目录为 `models/Qwen3-Embedding-0.6B`，指定 CPU、向量归一化和本地文件加载。
模型使用 1,024 维向量；对话模型仍使用原配置中的 API，嵌入模型不需要 API Key。
权重来自 [Qwen 官方 ModelScope 仓库](https://modelscope.cn/models/Qwen/Qwen3-Embedding-0.6B)，与官方 Hugging Face 仓库的权重标识一致。
已核对官方文件大小和校验值，并通过 DB-GPT 接口离线验证：CPU 加载、1,024 维文档/查询向量、归一化及中文出货查询匹配均通过。

启动系统时，从仓库根目录使用该配置：

Docker 启动方式见 [ERP Docker 部署说明](../../docker/erp/README.md)：

```powershell
docker compose -f docker-compose.erp.yml up -d --build
```

原本地虚拟环境启动方式仍可使用：

```powershell
& .\.venv\Scripts\dbgpt.exe start webserver --config configs/dbgpt-proxy-deepseek.toml
```

对话 API 的真实平台、模型名称和密钥仍需另行配置，不能用模板占位值启动对话。

## DB-GPT 只读数据源

已在本机 DB-GPT 注册 `erp_trial_readonly`，类型为 SQLite。
连接设置保存在 DB-GPT 的元数据库中，不需要修改模型配置或源码。
网页入口：`http://localhost:5670/construct/database/`，点击 Sqlite；描述为“ERP试验库，只读”。
当前页面以文件路径显示卡片标题，查询时的数据源名称为 `erp_trial_readonly`。

连接路径使用 SQLite URI：

```text
file:D%3A%2Fcode%2FDB-ERP%2Fexamples%2Ferp_query_trial%2Fdata%2Ferp_trial.sqlite?mode=ro&uri=true
```

`mode=ro` 由 SQLite 限制对该数据库的写入；`uri=true` 让 SQLAlchemy 按 URI 解析。
路径分隔符经过编码，避免当前连接器将 URI 误当作需创建的文件目录。
编辑连接时保留这两个参数；不要替换成普通文件路径，否则会变成普通可写连接。

已通过 DB-GPT SQL 编辑器核对五张业务表数量：369、1,875、770、1,624、5,382。
连接器及服务端执行 `UPDATE sales_outbound SET quantity=quantity WHERE 0` 均被 SQLite 拒绝。
SQL 编辑器会把数据库错误包装在成功响应内，验证时须检查 `data.result_info`，不能只看 `success`。
本次只读接入未将业务记录发送至对话模型 API，尚未验证自然语言业务问数。

下一步：由业务人员用 `data/销售额周排名_*.csv` 与 ERP 报表按同样周、币种和单据状态核对，
重点确认未税/含税口径与 10 月后的金额下降是否为真实趋势；库存、退货和单据状态仍需补充。
