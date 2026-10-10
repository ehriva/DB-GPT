"""Read-only queries with complete file-backed results for Python analysis."""

import asyncio
import json
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

import sqlparse
from sqlalchemy import inspect, text

from dbgpt.agent.resource.tool.base import tool

from .query_quality import (
    _ratio_definitions,
    _select_bindings,
    confirmed_price_rule,
    original_count_scope,
    query_result_catalog,
    sales_identity_column,
    select_sources,
    validate_cost_join,
    validate_explicit_ratios,
    visible_source_column,
)
from .report_rendering import record_validated_result

QUERY_TIMEOUT_SECONDS = 60


class AnalysisScopeChoice(ValueError):
    def __init__(self, value, codes, kind="customer"):
        super().__init__(
            f"客户“{value}”匹配多个实际客户，请选择。"
            if kind == "customer"
            else f"月份“{value}”未说明年份，请选择或填写四位年份。"
        )
        self.value = value
        self.codes = codes
        self.kind = kind


def request_periods(value):
    """Read evidenced ERP month forms, including a shared year in a month list."""
    result = []
    for match in re.finditer(
        r"(20\d{2})[./年](\d{1,2})(?:月)?((?:[、,，及和 ]+\d{1,2}月)*)", value
    ):
        year, month, tail = match.groups()
        for number in [int(month), *map(int, re.findall(r"(\d{1,2})月", tail))]:
            if not 1 <= number <= 12:
                raise ValueError("月份不在1至12之间。")
            period = f"{year}.{number:02d}"
            if period not in result:
                result.append(period)
    return result


async def resolve_analysis_scope(
    connector, requirements, user_input, database, previous=None
):
    """Resolve only evidenced customer/month values against the real ERP schema."""
    import copy

    engine = getattr(connector, "_engine", None)
    if engine is None or engine.dialect.name != "sqlite":
        return
    tables = await asyncio.to_thread(inspect(engine).get_table_names)
    if "ERP销售" not in tables:
        return
    user_input = re.sub(r"^\[Database:[^\]]+\]\s*", "", user_input)
    fields = {
        c["name"]
        for c in await asyncio.to_thread(inspect(engine).get_columns, "ERP销售")
    }
    candidate = requirements.get("analysis_scope", {})
    scope = copy.deepcopy(previous or {})
    if scope and scope.get("database") != database:
        raise ValueError("数据源改变后需重新明确客户和月份范围，不能沿用旧数据源条件。")
    scope["database"] = database
    grouping = re.search(r"按月|逐月|每月|分月|各月", user_input)
    if grouping:
        if "期间" not in fields:
            raise ValueError("当前销售表没有期间字段，不能证明按月范围。")
        scope.update(group_by=["期间"], grouping_evidence=grouping.group())
    elif re.search(r"不分月|只看总计|合并为总计", user_input):
        scope.update(group_by=[], grouping_evidence=user_input)
    periods = request_periods(user_input)
    short_months = list(
        dict.fromkeys(int(n) for n in re.findall(r"(?<!\d)(\d{1,2})月", user_input))
    )
    if not periods and short_months:
        if any(not 1 <= month <= 12 for month in short_months):
            raise ValueError("月份不在1至12之间。")
        years = {p[:4] for p in scope.get("periods", [])}
        if len(years) == 1:
            year = next(iter(years))
            periods = [f"{year}.{month:02d}" for month in short_months]
        else:
            if "期间" not in fields:
                raise ValueError("当前销售表没有期间字段，不能证明指定月份范围。")
            _, actual_periods = await query_database(
                connector, "SELECT DISTINCT 期间 FROM ERP销售"
            )
            years = sorted(
                {
                    str(row[0])[:4]
                    for row in actual_periods
                    if re.fullmatch(r"20\d{2}\.\d{2}", str(row[0]))
                }
            )
            raise AnalysisScopeChoice(
                "、".join(f"{month}月" for month in short_months), years, kind="year"
            )
    # The executable month set is proved by parsing the human phrase itself.
    # Rejected model candidate updates cannot remove or expand that month set.
    if periods:
        if "期间" not in fields:
            raise ValueError("当前销售表没有期间字段，不能证明指定月份范围。")
        scope.update(periods=periods, period_evidence=user_input)
    elif re.search(r"所有月份|全部月份|不限月份", user_input):
        scope.update(periods=[], period_evidence=user_input)
    customers = candidate.get("customer_candidates", [])
    if not customers:
        names = []
        if "客户名称" in fields:
            _, actual_names = await query_database(
                connector, "SELECT DISTINCT 客户名称 FROM ERP销售"
            )
            actual_names = [
                r[0] for r in actual_names if isinstance(r[0], str) and r[0]
            ]
            names = [name for name in actual_names if name in user_input]
            names = [
                name
                for name in names
                if not any(name != other and name in other for other in names)
            ]
            if not names:
                object_match = re.search(
                    r"(?:分析|查询|统计)([\u4e00-\u9fffA-Za-z]+?)(?=20\d{2}|[，,。；;\s]|$)",
                    user_input,
                )
                short = (
                    object_match.group(1).split("的")[0].removeprefix("客户")
                    if object_match
                    else ""
                )
                if len(short) >= 2 and any(short in name for name in actual_names):
                    names = [short]
        if not names:
            names = re.findall(
                r"[\u4e00-\u9fffA-Za-z0-9]+(?:有限公司|公司)", user_input
            )
            names = [
                re.sub(
                    r"^(?:请|按月|逐月|每月|各月|分月|分析|查询|统计|客户|对)+", "", n
                )
                for n in names
            ]
        codes = re.findall(
            r"(?<![A-Za-z0-9])[A-Z]{2,}\d{3,}(?![A-Za-z0-9])", user_input
        )
        customers = [{"value": n, "evidence": n} for n in [*names, *codes]]
    if re.search(r"所有客户|全部客户|全客户", user_input):
        scope.update(customer_codes=[], customer_evidence=user_input)
    elif customers:
        if "客户编码" not in fields:
            raise ValueError("当前销售表没有客户编码字段，不能证明客户范围。")
        codes = []
        for item in customers:
            value, evidence = item.get("value", ""), item.get("evidence", "")
            if (
                not value
                or not evidence
                or evidence not in user_input
                or value not in evidence
            ):
                raise ValueError("客户候选必须逐项匹配当前人类原文。")
            _, rows = await query_database(
                connector,
                "SELECT DISTINCT 客户编码 FROM ERP销售 WHERE 客户编码=:value",
                {"value": value},
            )
            if not rows and "客户名称" in fields:
                _, rows = await query_database(
                    connector,
                    "SELECT DISTINCT 客户编码 FROM ERP销售 WHERE 客户名称=:value",
                    {"value": value},
                )
                if not rows:
                    escaped = (
                        value.replace("\\", "\\\\")
                        .replace("%", "\\%")
                        .replace("_", "\\_")
                    )
                    _, rows = await query_database(
                        connector,
                        "SELECT DISTINCT 客户编码 FROM ERP销售 "
                        "WHERE 客户名称 LIKE :value ESCAPE '\\'",
                        {"value": "%" + escaped + "%"},
                    )
            if not rows:
                raise ValueError(
                    f"客户“{value}”没有匹配，不能取消客户条件进行全库分析。"
                )
            if len(rows) != 1:
                raise AnalysisScopeChoice(value, [r[0] for r in rows])
            codes.append(rows[0][0])
        scope.update(
            customer_codes=list(dict.fromkeys(codes)),
            customer_evidence="；".join(c["evidence"] for c in customers),
        )
    elif not scope.get("customer_evidence") and "客户" in user_input:
        raise ValueError("未能证明目标客户，请明确客户名称或编码；不能默认为全客户。")
    scope.setdefault("customer_codes", [])
    scope.setdefault("periods", [])
    requirements["analysis_scope"] = scope


async def scoped_sales_rows(state, connector):
    scope = state.get("task_requirements", {}).get("analysis_scope")
    if not scope:
        return None
    if scope.get("database") != state.get("resource_context", {}).get(
        "database", scope.get("database")
    ):
        raise ValueError("当前数据源与分析范围不一致。")
    conditions, parameters = [], {}
    for key, column in (("customer_codes", "客户编码"), ("periods", "期间")):
        values = scope.get(key, [])
        if values:
            placeholders = []
            for index, value in enumerate(values):
                name = f"{key}_{index}"
                placeholders.append(":" + name)
                parameters[name] = value
            conditions.append(column + " IN (" + ",".join(placeholders) + ")")
    _, rows = await query_database(
        connector,
        "SELECT rowid AS source_sales_row_id,* FROM ERP销售"
        + (" WHERE " + " AND ".join(conditions) if conditions else ""),
        parameters,
    )
    return {row[0] for row in rows}


async def check_analysis_scope(state, connector, statement, columns, rows):
    target = await scoped_sales_rows(state, connector)
    sources, aggregate = select_sources(statement)
    if target is None or "erp销售" not in sources:
        return None
    if "source_sales_row_id" in columns:
        if not sales_identity_column(statement):
            return "范围检查需要来自ERP销售真实rowid的source_sales_row_id。"
        identities = [r[columns.index("source_sales_row_id")] for r in rows]
        duplicates = len(identities) - len(set(identities))
        if duplicates:
            return (
                f"真实销售范围{len(target)}行，关联结果{len(identities)}行，其中重复销售身份{duplicates}行；"
                f"范围外身份{len(set(identities) - target)}行，"
                f"遗漏{len(target - set(identities))}行。"
                "不能把重复关联行当成真实销售。先按实际产品/期间键和价格去除完全重复成本映射；"
                "不能删除真实销售行或填补NULL。原合法源结果仍保留。"
            )
        complete_sets = [
            {
                r["source_sales_row_id"]
                for r in state.get("cost_input_evidence", {}).get("records", [])
                if all(
                    r.get(c) is not None for c in contract.get("required_inputs", [])
                )
            }
            for contract in state.get("task_requirements", {}).get("ratios", [])
            if contract.get("population") == "complete_inputs"
        ]
        if (
            sources.intersection({"材料单价", "工费单价"})
            and len(set(identities)) == len(identities)
            and any(set(identities) == complete for complete in complete_sets)
        ):
            return None
        if len(set(identities)) != len(identities) or set(identities) != target:
            return (
                f"销售范围不一致：目标{len(target)}行，实际{len(identities)}行；"
                f"多出{len(set(identities) - target)}行，"
                f"遗漏{len(target - set(identities))}行；不能登记交付来源。"
            )
        return None
    if not aggregate:
        # Exploration without identities remains usable but cannot certify delivery.
        return None
    located = original_count_scope(statement)
    if located is None:
        return "销售聚合需要同范围未关联销售明细及source_sales_row_count来源证明。"
    scope, ctes = located
    _, _, relations = _select_bindings(scope, ctes)
    if len(relations) != 1:
        return "销售聚合原范围未能证明，请先查询同范围销售身份明细。"
    alias, relation = next(iter(relations.items()))
    identity = visible_source_column(relation, "ERP销售", "rowid", ctes)
    if identity is None:
        return "销售聚合原来源需暴露真实rowid；请先查询同范围销售身份明细。"
    tokens = scope.tokens
    start = next(i for i, t in enumerate(tokens) if t.normalized == "FROM")
    end = next(
        (
            i
            for i, t in enumerate(tokens)
            if i > start and t.normalized in {"GROUP BY", "ORDER BY", "LIMIT"}
        ),
        len(tokens),
    )
    q = connector._engine.dialect.identifier_preparer.quote
    prefix = (
        "WITH " + ",".join(q(n) + " AS (" + str(v) + ")" for n, v in ctes.items()) + " "
        if ctes
        else ""
    )
    _, actual = await query_database(
        connector,
        prefix
        + "SELECT "
        + q(alias)
        + "."
        + q(identity)
        + " "
        + "".join(str(t) for t in tokens[start:end]).rstrip("; "),
    )
    identities = [r[0] for r in actual]
    if len(set(identities)) != len(identities) or set(identities) != target:
        return "销售聚合的实际来源与目标客户/月度范围不一致。"
    main_ctes, _, main_relations = _select_bindings(statement)
    candidates = [
        (alias, visible_source_column(relation, "ERP销售", "rowid", main_ctes))
        for alias, relation in main_relations.items()
    ]
    candidates = [(alias, field) for alias, field in candidates if field is not None]
    if len(candidates) != 1:
        return "销售聚合需可追溯的同范围明细来源，无法证明实际汇总销售身份。"
    alias, identity = candidates[0]
    tokens = statement.tokens
    start = next(i for i, t in enumerate(tokens) if t.normalized == "FROM")
    end = next(
        (
            i
            for i, t in enumerate(tokens)
            if i > start and t.normalized in {"GROUP BY", "ORDER BY", "LIMIT"}
        ),
        len(tokens),
    )
    prefix = (
        "WITH "
        + ",".join(q(n) + " AS (" + str(v) + ")" for n, v in main_ctes.items())
        + " "
        if main_ctes
        else ""
    )
    _, actual = await query_database(
        connector,
        prefix
        + "SELECT "
        + q(alias)
        + "."
        + q(identity)
        + " "
        + "".join(str(t) for t in tokens[start:end]).rstrip("; "),
    )
    identities = [r[0] for r in actual]
    if len(set(identities)) != len(identities) or set(identities) != target:
        return "销售聚合实际汇总范围与目标范围不一致，原销售计数不能代替汇总来源证明。"
    return None


async def establish_cost_inputs(state, connector):
    """Read the adopted ERP inputs; never infer missing labor as zero."""
    contracts = [
        r
        for r in state.get("task_requirements", {}).get("ratios", [])
        if r.get("cost_basis")
    ]
    if not contracts or not state.get("task_requirements", {}).get("analysis_scope"):
        return
    identities = await scoped_sales_rows(state, connector)
    parameters = {f"id{i}": value for i, value in enumerate(sorted(identities))}
    placeholders = ",".join(":" + name for name in parameters) or "NULL"
    columns, sales = await query_database(
        connector,
        "SELECT rowid AS source_sales_row_id,* FROM ERP销售 "
        f"WHERE rowid IN ({placeholders})",
        parameters,
    )
    records = [dict(zip(columns, row)) for row in sales]
    required_inputs = {
        field for r in contracts for field in r.get("required_inputs", [])
    }
    for table, price, keys in (
        ("材料单价", "材料成本单价", ["期间", "产品编码"]),
        ("工费单价", "工费", ["产品编码"]),
    ):
        if price not in required_inputs:
            continue
        fields = {
            c["name"]
            for c in await asyncio.to_thread(
                inspect(connector._engine).get_columns, table
            )
        }
        keys = [k for k in keys if k in fields]
        if "产品编码" not in keys or price not in fields:
            raise ValueError(f"{table}缺少实际成本键或价格字段。")
        q = connector._engine.dialect.identifier_preparer.quote
        _, prices = await query_database(
            connector,
            "SELECT " + ",".join(q(n) for n in [*keys, price]) + " FROM " + q(table),
        )
        mapping = {}
        for row in prices:
            mapping.setdefault(tuple(row[:-1]), set()).add(row[-1])
        for record in records:
            values = mapping.get(tuple(record[k] for k in keys), {None})
            if len(values) > 1:
                rule = confirmed_price_rule(state, table, price)
                available = [v for v in values if v is not None]
                if not rule or None in values:
                    raise ValueError(f"{table}存在实际适用价格冲突，需明确口径。")
                record[price] = (
                    max(available)
                    if rule == "max"
                    else min(available)
                    if rule == "min"
                    else sum(available) / len(available)
                )
            else:
                record[price] = next(iter(values))
    import copy

    state["cost_input_evidence"] = {
        "analysis_scope": copy.deepcopy(state["task_requirements"]["analysis_scope"]),
        "records": records,
    }


def _error(message: str) -> str:
    return json.dumps(
        {
            "success": False,
            "error": message,
            "chunks": [{"output_type": "text", "content": message}],
        },
        ensure_ascii=False,
    )


def _save_result(
    state: Dict[str, Any], data: Any, prefix: str, suffix: str = ".json"
) -> Path:
    from dbgpt.configs.model_config import PILOT_PATH

    base = Path(PILOT_PATH, "tmp").resolve()
    directory = (base / (state.get("conv_id") or "default")).resolve()
    directory.relative_to(base)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{prefix}_{uuid.uuid4().hex[:12]}{suffix}"
    content = (
        data
        if isinstance(data, str)
        else json.dumps(data, ensure_ascii=False, default=str)
    )
    path.write_text(content, encoding="utf-8")
    return path


async def query_database(connector: Any, sql: str, parameters=None):
    """Run off the event loop; interrupt SQLite on timeout or cancellation."""
    cancelled = threading.Event()
    deadline = time.monotonic() + QUERY_TIMEOUT_SECONDS

    def execute():
        engine = getattr(connector, "_engine", None)
        if engine is not None and engine.dialect.name == "sqlite":
            with engine.connect() as connection:
                raw = connection.connection.driver_connection
                raw.set_progress_handler(
                    lambda: int(cancelled.is_set() or time.monotonic() > deadline),
                    1000,
                )
                try:
                    cursor = connection.execute(text(sql), parameters or {})
                    return list(cursor.keys()), list(cursor.fetchall())
                finally:
                    raw.set_progress_handler(None, 0)
        if callable(getattr(connector, "query_ex", None)):
            return connector.query_ex(sql, timeout=QUERY_TIMEOUT_SECONDS)
        result = connector.run(sql)
        if not result:
            return [], []
        names = [str(c[0]) if isinstance(c, tuple) else str(c) for c in result[0]]
        return names, result[1:]

    try:
        return await asyncio.wait_for(
            asyncio.to_thread(execute), timeout=QUERY_TIMEOUT_SECONDS
        )
    finally:
        cancelled.set()


async def check_cost_prices(state, connector, statement, columns, rows):
    sources, _ = select_sources(statement)
    cost_tables = sources.intersection({"材料单价", "工费单价"})
    if not rows or "erp销售" not in sources or not cost_tables:
        return None
    engine = getattr(connector, "_engine", None)
    if engine is None or engine.dialect.name != "sqlite":
        return "该结果未完成成本价格检查：当前销售行身份检查仅支持本项目SQLite来源。"
    quote = engine.dialect.identifier_preparer.quote

    def schema(table):
        return {c["name"] for c in inspect(engine).get_columns(table)}

    sales_columns = await asyncio.to_thread(schema, "ERP销售")
    product = next(
        (n for n in ("产品编码", "product_code", "product") if n in sales_columns), None
    )
    period = next((n for n in ("期间", "period", "month") if n in sales_columns), None)
    if product is None:
        return "该结果未完成成本价格检查：无法确定真实销售产品键。"
    keys = [product, *([period] if period else [])]
    if "source_sales_row_id" in columns:
        identities = list(
            dict.fromkeys(row[columns.index("source_sales_row_id")] for row in rows)
        )
        placeholders = ",".join(f":id{i}" for i in range(len(identities)))
        sql = (
            "SELECT DISTINCT "
            + ",".join(quote(k) for k in keys)
            + f" FROM {quote('ERP销售')} WHERE rowid IN ({placeholders})"
        )
        _, sales_keys = await query_database(
            connector, sql, {f"id{i}": value for i, value in enumerate(identities)}
        )
    else:
        located = original_count_scope(statement)
        if located is None:
            return "该结果未完成成本价格检查：未能定位原销售范围。"
        scope, ctes = located
        _, _, relations = _select_bindings(scope, ctes)
        if len(relations) != 1:
            return "该结果未完成成本价格检查：原销售范围需要明确的未关联来源。"
        alias, relation = next(iter(relations.items()))
        visible = [visible_source_column(relation, "ERP销售", k, ctes) for k in keys]
        if any(k is None for k in visible):
            return (
                "该结果未完成成本价格检查：原销售范围需暴露产品/期间键，"
                "请先返回源销售明细。"
            )
        tokens = scope.tokens
        start = next(i for i, t in enumerate(tokens) if t.normalized == "FROM")
        if any(t.normalized == "HAVING" for t in tokens[start:]):
            return "该结果未完成成本价格检查：HAVING范围需先返回真实销售明细。"
        end = next(
            (
                i
                for i, t in enumerate(tokens)
                if i > start and t.normalized in {"GROUP BY", "ORDER BY", "LIMIT"}
            ),
            len(tokens),
        )
        prefix = (
            "WITH "
            + ",".join(
                quote(name) + " AS (" + str(query) + ")" for name, query in ctes.items()
            )
            + " "
            if ctes
            else ""
        )
        sql = (
            prefix
            + "SELECT DISTINCT "
            + ",".join(quote(alias) + "." + quote(k) for k in visible)
            + " "
            + "".join(str(t) for t in tokens[start:end]).rstrip("; ")
        )
        _, sales_keys = await query_database(connector, sql)
    for table in sorted(cost_tables):
        fields = await asyncio.to_thread(schema, table)
        cost_product = next(
            (n for n in ("产品编码", "product_code", "product") if n in fields), None
        )
        cost_period = next(
            (n for n in ("期间", "period", "month") if n in fields), None
        )
        prices = [
            n
            for n in (
                ("材料成本单价", "price", "unit_price")
                if table == "材料单价"
                else ("工费", "price", "unit_price")
            )
            if n in fields
        ]
        if cost_product is None or len(prices) != 1 or cost_period and period is None:
            return f"该结果未完成成本价格检查：{table}的实际关联键或价格字段不明确。"
        price = prices[0]
        selected_columns = [
            cost_product,
            *([cost_period] if cost_period else []),
            price,
        ]
        _, costs = await query_database(
            connector,
            "SELECT "
            + ",".join(quote(c) for c in selected_columns)
            + " FROM "
            + quote(table),
        )
        relevant = {(r[0], *([r[1]] if cost_period else [])) for r in sales_keys}
        mappings = {}
        for row in costs:
            key = tuple(row[:-1])
            if key in relevant and row[-1] is not None:
                mappings.setdefault(key, set()).add(row[-1])
        conflicts = [
            (key, sorted(values)) for key, values in mappings.items() if len(values) > 1
        ]
        if conflicts:
            query_text = str(statement)
            operator = confirmed_price_rule(state, table, price)
            permitted = operator and re.search(
                rf"\b{operator}\s*\(\s*(?:[\w\"`]+\.)?[\"`]?{re.escape(price)}[\"`]?\s*\)",
                query_text,
                re.I,
            )
            if permitted:
                continue
            return (
                f"{table}价格冲突："
                + json.dumps(conflicts[:10], ensure_ascii=False)
                + "。这些实际适用价格需要明确口径，不能随意取首条、最高价或均值。"
            )
    return None


def make_sql_query(react_state: Dict[str, Any], database_connector: Optional[Any]):
    @tool(
        description=(
            "对所选数据库执行只读 SELECT 查询。返回前 50 行预览及完整 JSON 结果文件。"
            "Python 用 QUERY_RESULT_PATH 读取最近结果："
            "r=json.load(open(QUERY_RESULT_PATH, encoding='utf-8')); "
            "df=pd.DataFrame(r['rows'], columns=r['columns'])。"
            "数据库画像优先使用 database_profile，无需导出全部明细。"
            '参数: {"sql": "SELECT 语句"}'
        )
    )
    async def sql_query(sql: str) -> str:
        if database_connector is None:
            return _error("未选择数据库，请先选择一个数据源。")
        statements = sqlparse.parse(sql.strip())
        if len(statements) != 1 or statements[0].get_type() != "SELECT":
            return _error("安全限制：仅允许单条只读 SELECT 查询。")
        try:
            names, rows = await query_database(database_connector, sql.strip())
            rows = [list(row) for row in (rows or [])]
            sources, aggregate = select_sources(statements[0])
            if "erp销售" in sources:
                await establish_cost_inputs(react_state, database_connector)
            scope_error = await check_analysis_scope(
                react_state, database_connector, statements[0], names, rows
            )
            if scope_error:
                return _error(scope_error)
            if (
                sources == {"erp销售"}
                and "source_sales_row_id" in names
                and not sales_identity_column(statements[0])
            ):
                return _error(
                    "原销售明细的source_sales_row_id必须来自ERP销售的真实rowid。"
                )
            quality_error = validate_cost_join(react_state, sql, names, rows)
            if not quality_error and _ratio_definitions(react_state):
                quality_error = await check_cost_prices(
                    react_state, database_connector, statements[0], names, rows
                )
                if quality_error:
                    react_state.setdefault("query_data_gaps", {})["sales_cost_join"] = (
                        quality_error
                    )
            if not quality_error:
                if (
                    "erp销售" not in sources
                    and react_state.get("task_requirements", {}).get("analysis_scope")
                    and any(
                        metric in names
                        for metric, _, _ in _ratio_definitions(react_state)
                    )
                ):
                    return _error(
                        "财务SQL指标必须来自当前范围的真实销售来源，常数或独立成本表不能作为销售汇总证据。"
                    )
                quality_error = validate_explicit_ratios(react_state, names, rows)
            if quality_error:
                return _error(quality_error)
            path = _save_result(react_state, {"columns": names, "rows": rows}, "query")
            result_id = record_validated_result(
                react_state, names, rows, source="sql", result_id=path.stem
            )
            snapshot = react_state["validated_results"][result_id]
            if snapshot["columns"] != names:
                names, rows = snapshot["columns"], snapshot["rows"]
                path.write_text(
                    json.dumps(
                        {"columns": names, "rows": rows},
                        ensure_ascii=False,
                        default=str,
                    ),
                    encoding="utf-8",
                )
            if sources == {"erp销售"} and not aggregate:
                react_state["sales_source_read"] = True
                react_state["sales_cost_join_verified"] = False
            elif sources and sources <= {"材料单价", "工费单价"}:
                react_state["cost_source_read"] = True
                react_state["sales_cost_join_verified"] = False
            react_state["validated_results"][result_id]["sales_source"] = (
                sources == {"erp销售"}
                and not aggregate
                and "source_sales_row_id" in names
            )
            react_state["validated_results"][result_id]["sql_sources"] = sorted(sources)
            react_state["validated_results"][result_id]["result_file"] = str(path)
            if sources and sources <= {"材料单价", "工费单价"}:
                price_conflicts = []
                price_mapping_checked = True
                for cost_table in sources:
                    from collections import defaultdict

                    engine = getattr(database_connector, "_engine", None)
                    if engine is None:
                        price_mapping_checked = False
                        continue
                    cost_fields = {
                        c["name"]
                        for c in await asyncio.to_thread(
                            inspect(engine).get_columns, cost_table
                        )
                    }
                    products = [
                        n
                        for n in ("产品编码", "product_code", "product")
                        if n in cost_fields
                    ]
                    periods = [
                        n for n in ("期间", "period", "month") if n in cost_fields
                    ]
                    price_names = (
                        ("材料成本单价", "price", "unit_price")
                        if cost_table == "材料单价"
                        else ("工费", "price", "unit_price")
                    )
                    price_names = [n for n in price_names if n in cost_fields]
                    if len(products) != 1 or len(periods) > 1 or len(price_names) != 1:
                        price_mapping_checked = False
                        continue
                    key_fields, price_field = [products[0], *periods], price_names[0]
                    q = database_connector._engine.dialect.identifier_preparer.quote
                    _, original_prices = await query_database(
                        database_connector,
                        "SELECT "
                        + ",".join(q(n) for n in [*key_fields, price_field])
                        + " FROM "
                        + q(cost_table),
                    )
                    groups = defaultdict(set)
                    for row in original_prices:
                        if row[-1] is not None:
                            groups[tuple(row[:-1])].add(row[-1])
                    price_conflicts.extend(
                        {
                            "table": cost_table,
                            "price_field": price_field,
                            "keys": dict(zip(key_fields, key)),
                            "prices": sorted(values),
                        }
                        for key, values in groups.items()
                        if len(values) > 1
                    )
                react_state["validated_results"][result_id]["price_conflicts"] = (
                    price_conflicts
                )
                react_state["validated_results"][result_id]["price_mapping_checked"] = (
                    price_mapping_checked
                )
            react_state["query_result_path"] = str(path)
            header = "| " + " | ".join(names) + " |"
            separator = "| " + " | ".join(["---"] * len(names)) + " |"
            preview_rows = [
                "| " + " | ".join(str(v) for v in row) + " |" for row in rows[:50]
            ]
            preview = "\n".join([header, separator] + preview_rows)[:20_000]
            preview += f"\n\n预览最多 50 行；查询实际返回 {len(rows)} 行。"
            return json.dumps(
                {
                    "success": True,
                    "row_count": len(rows),
                    "result_file": str(path),
                    "result_id": result_id,
                    "preview_truncated": len(rows) > 50,
                    "preview": {"columns": names, "rows": rows[:50]},
                    "chunks": [
                        {"output_type": "markdown", "content": preview},
                        {
                            "output_type": "text",
                            "content": (
                                f"完整结果文件：{path}。仅预览被截断，完整文件未截断。"
                                " Python 可直接读取 QUERY_RESULT_PATH。"
                                " 按columns映射取值："
                                "records=[dict(zip(r['columns'],row)) "
                                "for row in r['rows']]；"
                                "不要对SELECT *假定固定列下标，"
                                "source_sales_row_id也会占一列。"
                                f' 财务报告引用result_ids=["{result_id}"]，'
                                "不要重抄数值。"
                                "\nPython可通过QUERY_RESULTS[result_id]['path']"
                                "读取实际已验证来源；当前可用来源："
                                + json.dumps(
                                    query_result_catalog(react_state),
                                    ensure_ascii=False,
                                )
                            ),
                        },
                    ],
                },
                ensure_ascii=False,
                default=str,
            )
        except Exception as error:
            return _error(f"SQL 执行失败：{error}")

    return sql_query


def make_database_profile(state: Dict[str, Any], connector: Optional[Any]):
    @tool(
        description=(
            "获取所选关系数据库的完整画像：各表名称、字段类型、可空性及实际行数。"
            "生成数据库画像报告时先调用本工具，再用 sql_query 补充必要的聚合统计。"
            "完整结果保存为 JSON 文件，Python 通过 DATABASE_PROFILE_PATH 读取。无参数。"
        )
    )
    async def database_profile() -> str:
        if connector is None:
            return _error("未选择数据库。")
        try:
            engine = getattr(connector, "_engine", None)
            if engine is None:
                return _error("当前数据源不支持关系数据库画像。")
            tables = []
            for name in sorted(await asyncio.to_thread(connector.get_table_names)):
                columns = await asyncio.to_thread(connector.get_columns, name)
                quoted = engine.dialect.identifier_preparer.quote(name)
                _, counts = await query_database(
                    connector, f"SELECT COUNT(*) AS row_count FROM {quoted}"
                )
                tables.append(
                    {
                        "name": name,
                        "row_count": int(counts[0][0]),
                        "columns": [
                            {
                                "name": c["name"],
                                "type": str(c["type"]),
                                "nullable": c.get("nullable"),
                            }
                            for c in columns
                        ],
                    }
                )
            profile = {"tables": tables}
            path = _save_result(state, profile, "database_profile")
            state["database_profile"] = profile
            state["database_profile_path"] = str(path)
            return json.dumps(
                {
                    "success": True,
                    "preview": profile,
                    "chunks": [
                        {
                            "output_type": "text",
                            "content": (
                                json.dumps(profile, ensure_ascii=False)[:20_000]
                                + f"\n完整画像文件：{path}；"
                                "Python 使用 DATABASE_PROFILE_PATH。"
                            ),
                        }
                    ],
                },
                ensure_ascii=False,
            )
        except Exception as error:
            return _error(f"数据库画像获取失败：{error}")

    return database_profile
