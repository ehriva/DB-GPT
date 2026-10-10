"""Render financial tables and factual summaries from successful tool results."""

import copy
from collections import Counter
from datetime import datetime
from html import escape
from uuid import uuid4

from .query_quality import _ratio_definitions, ratio_contract


def record_validated_result(
    state, columns, rows, *, source, result_id=None, source_result_ids=None
):
    columns, rows = list(columns), [list(row) for row in rows]
    required_inputs = {
        c
        for r in state.get("task_requirements", {}).get("ratios", [])
        for c in r.get("required_inputs", [])
    } or {"材料成本单价", "工费", "销售数量", "销售金额"}
    if "source_sales_row_id" in columns and required_inputs <= set(columns):
        derived_missing = [
            int(any(row[columns.index(c)] is None for c in required_inputs))
            for row in rows
        ]
        missing_column = _column(columns, ["missing_input_count", "所需输入缺失数"])
        if missing_column is None:
            columns.append("missing_input_count")
            for row, missing in zip(rows, derived_missing):
                row.append(missing)
        elif any(
            row[missing_column] != expected
            for row, expected in zip(rows, derived_missing)
        ):
            raise ValueError("销售成本明细的缺失数与实际源字段NULL不一致。")
    result_id = result_id or f"{source}_{uuid4().hex[:12]}"
    units = {}
    for metric, _, _ in _ratio_definitions(state):
        for column in columns:
            if (
                column == metric
                or column == f"局部{metric}"
                or column == f"{metric}百分比"
            ):
                units[column] = (
                    "percent"
                    if column.endswith("百分比")
                    else ratio_contract(state, metric)["unit"]
                )
    snapshot = {
        "columns": list(columns),
        "rows": [list(row) for row in rows],
        "units": units,
        "availability": list(state.get("data_warnings", {}).values()),
        "source": source,
        "conv_id": state.get("conv_id"),
        "database": state.get("term_datasource", ""),
        "source_result_ids": list(source_result_ids or []),
        "analysis_scope": copy.deepcopy(
            state.get("task_requirements", {}).get("analysis_scope")
        ),
    }
    state.setdefault("validated_results", {})[result_id] = copy.deepcopy(snapshot)
    return result_id


def _column(columns, names):
    return next((columns.index(name) for name in names if name in columns), None)


def render_results(state, result_ids, title):
    if (
        not isinstance(result_ids, list)
        or not result_ids
        or any(not isinstance(r, str) for r in result_ids)
    ):
        raise ValueError("财务报告必须通过result_ids选择当前任务的已验证结果。")
    if len(set(result_ids)) != len(result_ids):
        raise ValueError("报告结果不能重复引用。")
    snapshots = []
    for rid in result_ids:
        snapshot = state.get("validated_results", {}).get(rid)
        if (
            not snapshot
            or snapshot["conv_id"] != state.get("conv_id")
            or snapshot["database"] != state.get("term_datasource", "")
            or snapshot.get("analysis_scope")
            != state.get("task_requirements", {}).get("analysis_scope")
        ):
            raise ValueError("引用的结果不存在、属于旧任务或数据源不匹配。")
        snapshots.append(snapshot)
    for metric, _, _ in _ratio_definitions(state):
        if not any(metric in s["columns"] for s in snapshots):
            raise ValueError(f"报告缺少已验证的{metric}完整结果。")
        if "期间" in state.get("task_requirements", {}).get("analysis_scope", {}).get(
            "group_by", []
        ):
            monthly = False
            for snapshot in snapshots:
                period = _column(
                    snapshot["columns"], ["期间", "月份", "period", "month"]
                )
                if (
                    metric in snapshot["columns"]
                    and period is not None
                    and "source_sales_row_id" not in snapshot["columns"]
                ):
                    months = [row[period] for row in snapshot["rows"]]
                    monthly = monthly or len(set(months)) == len(months)
            if not monthly:
                raise ValueError(
                    f"原需求按月分析，报告必须交付{metric}的逐月汇总；总计或逐行明细不能替代。"
                )
    if state.get("query_data_gaps"):
        # require_value can still deliver the unavailable report, but the task
        # itself must stay incomplete.
        blocking = [
            key
            for key in state["query_data_gaps"]
            if state.get("ratio_validation_status", {}).get(key) != "missing_inputs"
        ]
        if blocking:
            raise ValueError("报告结果校验尚未完成：" + "、".join(blocking))

    fact_snapshots = list(snapshots)
    visited = set(result_ids)
    position = 0
    while position < len(fact_snapshots):
        for source_id in fact_snapshots[position].get("source_result_ids", []):
            if source_id in visited:
                continue
            referenced = state.get("validated_results", {}).get(source_id)
            if (
                not referenced
                or referenced["conv_id"] != state.get("conv_id")
                or referenced["database"] != state.get("term_datasource", "")
                or referenced.get("analysis_scope")
                != state.get("task_requirements", {}).get("analysis_scope")
            ):
                raise ValueError("报告依赖的源结果不存在、属于其他任务或数据源。")
            visited.add(source_id)
            fact_snapshots.append(referenced)
        position += 1

    details = {}
    source_rows = {}
    summaries = []
    for snapshot in fact_snapshots:
        columns = snapshot["columns"]
        identity = _column(columns, ["source_sales_row_id"])
        missing = _column(columns, ["missing_input_count", "所需输入缺失数"])
        period = _column(columns, ["期间", "月份", "month", "period"])
        product = _column(columns, ["产品编码", "产品代码", "料号", "product_code"])
        if missing is None:
            continue
        if identity is not None:
            for row in snapshot["rows"]:
                if period is not None and row[identity] is not None:
                    source_rows[row[identity]] = str(row[period])
                if not row[missing]:
                    continue
                if row[identity] is None or period is None or product is None:
                    raise ValueError("缺失明细必须包含真实销售行身份、月份和产品。")
                missing_fields = tuple(
                    c
                    for c in ("材料成本单价", "工费", "销售数量", "销售金额")
                    if c in columns and row[columns.index(c)] is None
                )
                value = (str(row[period]), str(row[product]), missing_fields)
                if row[identity] in details and details[row[identity]] != value:
                    raise ValueError("同一销售行缺失明细发生冲突。")
                details[row[identity]] = value
        elif any(snapshot is selected for selected in snapshots) and any(
            metric in columns for metric, _, _ in _ratio_definitions(state)
        ):
            summaries.append((snapshot, missing, period))
    months = Counter(value[0] for value in details.values())
    products = Counter(value[1] for value in details.values())
    missing_fields = sorted({field for value in details.values() for field in value[2]})
    for snapshot, missing, period in summaries:
        total = sum(row[missing] for row in snapshot["rows"])
        if total != len(details):
            raise ValueError("摘要与实际销售行缺失明细统计不一致；请选择完整明细结果。")
        if period is not None:
            counts = Counter()
            for row in snapshot["rows"]:
                counts[str(row[period])] += row[missing]
            if any(count != months[month] for month, count in counts.items()):
                raise ValueError("按月缺失行数与实际明细不一致。")
            full_count = _column(snapshot["columns"], ["source_sales_row_count"])
            complete_count = _column(
                snapshot["columns"], ["成本完整行数", "complete_cost_row_count"]
            )
            actual_counts = Counter(source_rows.values())
            if full_count is not None and complete_count is not None:
                for row in snapshot["rows"]:
                    month = str(row[period])
                    if (
                        row[full_count] == actual_counts[month]
                        and row[complete_count] != actual_counts[month] - months[month]
                    ):
                        raise ValueError("成本完整行数与真实源销售行及缺失明细不一致。")

    warnings = {
        w["metric"]: w for snapshot in snapshots for w in snapshot["availability"]
    }
    if details:
        for metric, warning in warnings.items():
            if warning["reason"] == "missing_input":
                warning = {
                    **warning,
                    "affected_rows": len(details),
                    "message": (
                        f"{metric}：缺少{'、'.join(missing_fields) or '所需输入'}，"
                        f"完整比率为NULL，影响{len(details)}行、{len(products)}个产品。"
                    ),
                }
                warnings[metric] = warning
                state.setdefault("data_warnings", {})[metric] = copy.deepcopy(warning)
    facts = ["本报告的表格来自当前任务已验证的工具结果；局部指标仅代表成本完整子集。"]
    facts.append(
        "结果来源："
        + "、".join(result_ids)
        + "；数据源："
        + state.get("term_datasource", "无数据库")
    )
    if details:
        facts.append(
            f"所需输入缺失影响{len(details)}行、{len(products)}个产品；完整指标为NULL。"
        )
        facts.append(
            "缺失统计以source_sales_row_id标识销售行，按返回的期间及产品编码分组。"
        )
        facts.extend(
            f"{month}：缺失{count}行。" for month, count in sorted(months.items())
        )
        facts.extend(
            f"产品{product}：影响{count}行。"
            for product, count in sorted(products.items())
        )
    facts.extend(w["message"] for w in warnings.values())
    incomplete = []
    for metric, numerator, _ in _ratio_definitions(state):
        contract = ratio_contract(state, metric)
        warning = warnings.get(metric, {})
        if contract["unavailable_policy"] == "require_value" and warning.get(
            "reason"
        ) in {"missing_input", "zero_denominator"}:
            incomplete.append((metric, numerator, contract, warning))
    if incomplete:
        facts.append("报告状态：未完成；用户要求完整的指标数值，以下范围无法交付。")
        for metric, numerator, contract, warning in incomplete:
            scope = (
                "成本完整子集"
                if contract.get("population") == "complete_inputs"
                else "全量"
            )
            if warning["reason"] == "missing_input":
                missing = "、".join(missing_fields) or "所需输入"
                facts.append(
                    f"因缺{missing}无法满足所要求的{scope}{numerator}和{metric}完整数值要求。"
                )
            else:
                facts.append(
                    f"所要求的{scope}{metric}完整数值在本范围不适用：分母为0。"
                )
    tables = []
    summary_tables = []
    for snapshot in snapshots:
        columns, rows = snapshot["columns"], snapshot["rows"]

        def format_value(column, value):
            if value is None:
                return "NULL"
            if column in snapshot["units"]:
                fraction = float(value) / (
                    100 if snapshot["units"][column] == "percent" else 1
                )
                return f"{fraction:.2%}"
            if isinstance(value, float):
                return f"{value:,.2f}"
            return str(value)

        formatted = [[format_value(c, v) for c, v in zip(columns, row)] for row in rows]
        table = (
            "<table><thead><tr>"
            + "".join(f"<th>{escape(c)}</th>" for c in columns)
            + "</tr></thead><tbody>"
        )
        table += "".join(
            "<tr>" + "".join(f"<td>{escape(v)}</td>" for v in row) + "</tr>"
            for row in formatted
        )
        tables.append(table + "</tbody></table>")
        if any(metric in columns for metric, _, _ in _ratio_definitions(state)):
            summary_tables.append(
                "| "
                + " | ".join(columns)
                + " |\n| "
                + " | ".join("---" for _ in columns)
                + " |\n"
                + "\n".join("| " + " | ".join(row) + " |" for row in formatted)
            )
    summary = "\n".join(facts) + "\n\n" + "\n\n".join(summary_tables)
    html = (
        '<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8"><title>'
        + escape(title)
        + "</title><style>"
        "body{font-family:sans-serif;margin:32px;color:#172033}"
        "table{border-collapse:collapse;margin:24px 0}"
        "td,th{padding:10px;border:1px solid #cbd5e1}"
        "th{background:#eef2ff}"
        "</style></head><body><h1>"
        + escape(title)
        + "</h1><p>生成时间："
        + datetime.now().astimezone().isoformat(timespec="seconds")
        + "</p>"
    )
    html += (
        "".join(f"<p>{escape(fact)}</p>" for fact in facts)
        + "".join(tables)
        + "</body></html>"
    )
    return html, summary
