"""Check explicitly requested ratios against their returned numerator/denominator."""

import json
import math
import re
from collections import Counter
from decimal import Decimal
from pathlib import Path

import sqlparse
from sqlparse.sql import Function, Identifier, IdentifierList, Parenthesis
from sqlparse.tokens import DML, Keyword


def _ratio_definitions(state):
    if "task_requirements" in state:
        return [
            (item["metric"], item["numerator"], item["denominator"])
            for item in state["task_requirements"].get("ratios", [])
        ]
    definitions = re.findall(
        r"([\w]+)\s*[=＝]\s*([\w]+)\s*/\s*([\w]+)",
        state.get("original_user_input", ""),
    )
    return [
        (re.sub(r"^(?:我定义|定义)(?=\w+)", "", label), numerator, denominator)
        for label, numerator, denominator in definitions
    ]


def require_explicit_ratio_results(state):
    if state.get("term_explanation_only"):
        return
    for label, _, _ in _ratio_definitions(state):
        state.setdefault("query_data_gaps", {}).setdefault(
            label, f"计算{label}：原需求的完整比率及输入完整性尚未通过校验。"
        )


def ratio_contract(state, metric):
    return next(
        (
            r
            for r in state.get("task_requirements", {}).get("ratios", [])
            if r["metric"] == metric
        ),
        {"unit": "fraction", "unavailable_policy": "explain"},
    )


def query_result_catalog(state):
    """Expose actual current files for each SQL source kind to Python recovery."""
    selected = {}
    for rid, snapshot in state.get("validated_results", {}).items():
        path = snapshot.get("result_file")
        if (snapshot.get("source") != "sql" or not path or not Path(path).is_file()
                or snapshot.get("conv_id") != state.get("conv_id")
                or snapshot.get("database") != state.get("term_datasource", "")
                or snapshot.get("analysis_scope") != state.get("task_requirements", {}).get("analysis_scope")):
            continue
        key = (tuple(snapshot.get("sql_sources", [])), "source_sales_row_id" in snapshot["columns"])
        selected[key] = (rid, {"path": path, "columns": snapshot["columns"], "tables": snapshot.get("sql_sources", [])})
    return dict(selected.values())


def ratio_matches(actual, expected, unit="fraction", display=False):
    tolerance = 1e-12
    if display and isinstance(actual, str) and actual.endswith(("%", "％")):
        number = actual[:-1]
        places = len(number.split(".")[1]) if "." in number else 0
        tolerance = 0.5 * 10 ** (-places) / 100
        actual, unit = number, "percent"
    try:
        fraction = float(actual) / (100 if unit == "percent" else 1)
    except (TypeError, ValueError):
        return False
    return (
        math.isfinite(fraction)
        and math.isfinite(expected)
        and math.isclose(
            fraction,
            expected,
            rel_tol=1e-9,
            abs_tol=tolerance,
        )
    )


def validate_python_ratio_output(state, output):
    candidates = [output.strip(), *reversed(output.splitlines())]
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and {"columns", "rows"} <= payload.keys():
            columns, rows = payload["columns"], payload["rows"]
            if state.get("term_datasource") and state.get("task_requirements", {}).get("analysis_scope") and any(metric in columns for metric, _, _ in _ratio_definitions(state)):
                sources = payload.get("source_result_ids", [])
                snapshots = [state.get("validated_results", {}).get(r) for r in sources]
                if not snapshots or any(not s or s.get("analysis_scope") != state["task_requirements"]["analysis_scope"] for s in snapshots):
                    return "Python指标需引用同数据源、同客户/月度范围的source_result_ids。"
                if not any("source_sales_row_id" in s["columns"] and "erp销售" in s.get("sql_sources", []) for s in snapshots):
                    return "Python指标需要引用已核对范围的销售身份明细来源，SELECT *或成本源不能代替。"
            needs_join_check = (
                state.get("sales_source_read")
                and state.get("cost_source_read")
                and not state.get("sales_cost_join_verified")
                and any(metric in columns for metric, _, _ in _ratio_definitions(state))
            )
            if needs_join_check:
                state.setdefault("query_data_gaps", {})["sales_cost_join"] = (
                    "Python销售成本关联尚未核对实际销售行。"
                )
            error = validate_explicit_ratios(state, columns, rows)
            if error:
                return error
            if needs_join_check and not {
                "source_sales_row_count",
                "joined_sales_row_count",
            } <= set(columns):
                return "Python销售成本计算必须返回真实原销售和处理后行数及关联证据；请按提示补充实际校验。"
            if "sales_cost_join" in state.get("query_data_gaps", {}) and {
                "source_sales_row_count",
                "joined_sales_row_count",
            } <= set(columns):
                evidence_error = validate_python_sales_evidence(state, payload)
                if evidence_error:
                    return evidence_error
                return _validate_sales_join_result(state, columns, rows, aggregate=True)
            return None
    return _validate_given_ratio_output(state, output)


def _monthly_total_row(columns, row):
    period = next((c for c in ("期间", "月份", "period", "month") if c in columns), None)
    return (
        period is not None
        and row[columns.index(period)] in {"合计", "总计"}
        and all(
            row[columns.index(c)] is None
            for c in (
                "产品编码", "产品代码", "料号", "product_code", "source_sales_row_id"
            )
            if c in columns
        )
    )


def validate_explicit_ratios(state, columns, rows):
    definitions = _ratio_definitions(state)
    if not definitions:
        return None
    if not isinstance(columns, (list, tuple)) or not all(
        isinstance(name, str) for name in columns
    ):
        return "计算结果的columns必须是列名数组。"
    if not isinstance(rows, (list, tuple)) or any(
        not isinstance(row, (list, tuple)) or len(row) != len(columns) for row in rows
    ):
        return "计算结果的rows必须与columns列数一致。"
    if "期间" in state.get("task_requirements", {}).get("analysis_scope", {}).get("group_by", []) and any(metric in columns for metric, _, _ in definitions) and not any(c in columns for c in ("期间", "月份", "period", "month")):
        return "原需求要求按月汇总，计算结果必须包含期间/月份并逐月返回；总计不能替代月度指标。"
    cost_error = validate_cost_population(state, columns, rows)
    if cost_error:
        return cost_error
    gaps = state.setdefault("query_data_gaps", {})
    for label, numerator, denominator in definitions:
        ratio_indexes = [
            i for i, name in enumerate(columns) if name in {label, f"{label}百分比"}
        ]
        if not ratio_indexes:
            continue
        validation = state.setdefault("ratio_validation_status", {})
        validation[label] = "pending"
        warnings = state.setdefault("data_warnings", {})
        warnings.pop(label, None)
        if not rows:
            validation[label] = "verified"
            gaps.pop(label, None)
            continue
        gaps[label] = f"计算{label}：公式及输入完整性尚未通过校验。"
        num_index = next(
            (i for i, name in enumerate(columns) if name == numerator), None
        )
        den_index = next(
            (i for i, name in enumerate(columns) if name == denominator), None
        )
        quality_index = next(
            (
                columns.index(name)
                for name in (
                    f"{label}_missing_input_count",
                    f"{label}所需输入缺失数",
                    "所需输入缺失数",
                    "missing_input_count",
                )
                if name in columns
            ),
            None,
        )
        if num_index is None or den_index is None or quality_index is None:
            return (
                f"输出{label}时必须同时返回同名分子{numerator}、分母{denominator}和"
                "所需输入缺失数（missing_input_count）；缺失数应由实际源字段的NULL检查计算。"
                "汇总比率必须使用同一范围的汇总分子/汇总分母，不能让SUM分别跳过不同缺失行。"
            )
        missing_rows = 0
        zero_denominators = 0
        for row in rows:
            missing = row[quality_index]
            if (
                isinstance(missing, bool)
                or not isinstance(missing, (int, float))
                or not math.isfinite(missing)
                or missing < 0
                or int(missing) != missing
            ):
                return "所需输入缺失数必须是实际查询得到的非负数量。"
            if not _monthly_total_row(columns, row):
                missing_rows += int(missing)
            for ratio_index in ratio_indexes:
                ratio = row[ratio_index]
                if missing:
                    if ratio is not None:
                        gaps[label] = (
                            f"计算{label}：所需输入存在缺失，不能交付完整比率。"
                        )
                        return (
                            "所需输入存在缺失：完整比率必须返回NULL并列出缺失，"
                            "不能填0或忽略后宣称完整。"
                        )
                    continue
                num, den = row[num_index], row[den_index]
                if num is None or den is None:
                    return "比率的分子/分母缺失，但缺失计数为0，请修正完整性检查。"
                if den == 0:
                    if ratio is not None:
                        return "分母为0时比率应返回NULL，并明确不适用。"
                    if ratio_index == ratio_indexes[0] and not _monthly_total_row(columns, row):
                        zero_denominators += 1
                    continue
                try:
                    expected = float(num) / float(den)
                except (TypeError, ValueError, ZeroDivisionError):
                    return "比率验证需要数值分子、分母和比率，请在展示时再格式化。"
                unit = (
                    "percent"
                    if columns[ratio_index] == f"{label}百分比"
                    else ratio_contract(state, label)["unit"]
                )
                if not ratio_matches(ratio, expected, unit):
                    return (
                        f"{label}不等于返回的{numerator}/{denominator}，"
                        "请改为同一范围的汇总分子除以汇总分母。"
                    )
        if missing_rows or zero_denominators:
            warnings[label] = {
                "metric": label,
                "reason": "missing_input" if missing_rows else "zero_denominator",
                "message": (
                    f"{label}：所需输入缺失，完整比率为NULL，影响{missing_rows}行。"
                    if missing_rows
                    else f"{label}：分母为0，比率为NULL，不适用。"
                ),
                "affected_rows": missing_rows or zero_denominators,
            }
            validation[label] = "missing_inputs"
            if ratio_contract(state, label)["unavailable_policy"] == "require_value":
                gaps[label] = f"计算{label}：用户要求完整数值，但该范围比率不可计算。"
            else:
                gaps.pop(label, None)
        else:
            validation[label] = "verified"
            gaps.pop(label, None)
    return None


def validate_cost_population(state, columns, rows):
    """Check cost totals against real source inputs, before ratio arithmetic."""
    contracts = [
        r
        for r in state.get("task_requirements", {}).get("ratios", [])
        if r.get("cost_basis") in {"standard", "material_only"}
        and r["metric"] in columns
    ]
    evidence = state.get("cost_input_evidence")
    if evidence and "source_sales_row_id" in columns:
        records_by_id = {r["source_sales_row_id"]: r for r in evidence["records"]}
        for values in rows:
            record = records_by_id.get(values[columns.index("source_sales_row_id")])
            if record is None:
                return "销售成本明细包含范围外身份。"
            for field in ("材料成本单价", "工费", "销售数量", "销售金额"):
                if (
                    field in columns
                    and field in record
                    and values[columns.index(field)] != record[field]
                ):
                    return f"{field}与真实源输入不一致，不能填0或更换成本。"
    if not contracts:
        return None
    if not evidence or evidence["analysis_scope"] != state.get(
        "task_requirements", {}
    ).get("analysis_scope"):
        return "成本指标必须有当前范围的真实销售成本输入证据，请先查询销售身份明细。"
    records = evidence["records"]
    group_fields = []
    for source, aliases in (("期间", ("期间", "月份", "period", "month")), ("产品编码", ("产品编码", "产品代码", "料号", "product_code")), ("source_sales_row_id", ("source_sales_row_id",))):
        output = next((c for c in aliases if c in columns), None)
        if output:
            group_fields.append((source, output))
    expected_groups = {tuple(r[source] for source, _ in group_fields) for r in records}
    actual_groups = [tuple(row[columns.index(output)] for _, output in group_fields) for row in rows]
    detail_groups = [key for row, key in zip(rows, actual_groups) if not _monthly_total_row(columns, row)]
    total_count = sum(_monthly_total_row(columns, row) for row in rows)
    if set(detail_groups) != expected_groups or total_count > 1 or len(set(actual_groups)) != len(
        actual_groups
    ):
        return "成本汇总的实际月份/产品集合与目标销售范围不一致。"
    for contract in contracts:
        for row, key in zip(rows, actual_groups):
            all_records = records if _monthly_total_row(columns, row) else [
                r for r in records if tuple(r[source] for source, _ in group_fields) == key
            ]
            complete = [
                r
                for r in all_records
                if all(r[c] is not None for c in contract["required_inputs"])
            ]
            population = (
                complete if contract["population"] == "complete_inputs" else all_records
            )
            missing = len(population) - sum(
                all(r[c] is not None for c in contract["required_inputs"])
                for r in population
            )
            expected_sales = sum(
                (
                    Decimal(str(r["销售金额"]))
                    for r in population
                    if r["销售金额"] is not None
                ),
                Decimal(0),
            )
            profit = (
                None
                if missing
                else sum(
                    (
                        Decimal(str(r["销售金额"]))
                        - Decimal(str(r["销售数量"]))
                        * (
                            Decimal(str(r["材料成本单价"]))
                            + (
                                Decimal(str(r["工费"]))
                                if contract["cost_basis"] == "standard"
                                else 0
                            )
                        )
                        for r in population
                    ),
                    Decimal(0),
                )
            )
            for name, expected in (
                (contract["numerator"], profit),
                (contract["denominator"], expected_sales),
            ):
                if name not in columns:
                    continue
                actual = row[columns.index(name)]
                if expected is None:
                    if actual is not None:
                        return f"{name}所需真实输入缺失，完整指标必须为NULL。"
                elif actual is None or not math.isclose(
                    float(actual), float(expected), rel_tol=1e-9, abs_tol=1e-8
                ):
                    return f"{name}与同范围真实完整行集合及已采用成本口径不一致。"
            missing_name = next(
                (
                    c
                    for c in (
                        contract["metric"] + "_missing_input_count",
                        contract["metric"] + "所需输入缺失数",
                        "missing_input_count",
                        "所需输入缺失数",
                    )
                    if c in columns
                ),
                None,
            )
            if missing_name and row[columns.index(missing_name)] != missing:
                return "成本指标缺失数与真实必需输入集合不一致。"
            if contract["population"] == "complete_inputs":
                for name in ("成本完整行数", "complete_cost_row_count"):
                    if name in columns and row[columns.index(name)] != len(complete):
                        return "成本完整行数与真实必需输入集合不一致。"
    return None


def select_sources(statement, inherited=None):
    """Resolve FROM relations in one SELECT, including supported CTE/subqueries."""
    ctes = dict(inherited or {})
    tokens = [t for t in statement.tokens if not t.is_whitespace]
    select_index = next(
        (
            i
            for i, t in enumerate(tokens)
            if t.ttype is DML and t.normalized == "SELECT"
        ),
        None,
    )
    if select_index is None:
        return set(), False

    def relation(token):
        if isinstance(token, IdentifierList):
            return set().union(*(relation(t) for t in token.get_identifiers()))
        if isinstance(token, Identifier):
            nested = next((t for t in token.tokens if isinstance(t, Parenthesis)), None)
            if nested is not None:
                parsed = sqlparse.parse(nested.value[1:-1])
                return select_sources(parsed[0], ctes)[0] if parsed else set()
            name = (token.get_real_name() or "").casefold()
            return ctes.get(name, {name})
        if isinstance(token, Parenthesis):
            parsed = sqlparse.parse(token.value[1:-1])
            return select_sources(parsed[0], ctes)[0] if parsed else set()
        name = token.value.strip('"`[]').casefold()
        return ctes.get(name, {name})

    for token in tokens[:select_index]:
        entries = (
            token.get_identifiers() if isinstance(token, IdentifierList) else [token]
        )
        for entry in entries:
            if isinstance(entry, Identifier):
                name = (entry.get_real_name() or "").casefold()
                ctes[name] = relation(entry)
    sources = set()
    expecting_relation = False
    for token in tokens[select_index + 1 :]:
        if token.ttype in Keyword and (
            token.normalized == "FROM" or "JOIN" in token.normalized
        ):
            expecting_relation = True
        elif expecting_relation:
            sources.update(relation(token))
            expecting_relation = False

    def aggregate_expression(token):
        if isinstance(token, Parenthesis) and any(t.ttype is DML for t in token.tokens):
            return False
        if isinstance(token, Function) and (token.get_name() or "").casefold() in {
            "sum",
            "avg",
            "count",
            "min",
            "max",
        }:
            return True
        return token.is_group and any(aggregate_expression(t) for t in token.tokens)

    projection = tokens[
        select_index + 1 : next(
            (
                i
                for i, t in enumerate(tokens)
                if i > select_index and t.normalized == "FROM"
            ),
            len(tokens),
        )
    ]
    aggregate = any(t.normalized == "GROUP BY" for t in tokens) or any(
        aggregate_expression(t) for t in projection
    )
    return sources, aggregate


def _select_bindings(statement, inherited=None):
    ctes = dict(inherited or {})
    tokens = [t for t in statement.tokens if not t.is_whitespace]
    start = next(
        (
            i
            for i, t in enumerate(tokens)
            if t.ttype is DML and t.normalized == "SELECT"
        ),
        None,
    )
    if start is None:
        return ctes, [], {}
    for token in tokens[:start]:
        entries = (
            token.get_identifiers() if isinstance(token, IdentifierList) else [token]
        )
        for entry in entries:
            if isinstance(entry, Identifier):
                nested = next(
                    (t for t in entry.tokens if isinstance(t, Parenthesis)), None
                )
                if nested:
                    ctes[(entry.get_real_name() or "").casefold()] = sqlparse.parse(
                        nested.value[1:-1]
                    )[0]
    end = next(
        (i for i, t in enumerate(tokens) if i > start and t.normalized == "FROM"),
        len(tokens),
    )
    projections = []
    for token in tokens[start + 1 : end]:
        projections.extend(
            list(token.get_identifiers())
            if isinstance(token, IdentifierList)
            else [token]
        )
    relations = {}
    expecting = False
    for token in tokens[end:]:
        if token.ttype in Keyword and (
            token.normalized == "FROM" or "JOIN" in token.normalized
        ):
            expecting = True
        elif expecting:
            entries = (
                token.get_identifiers()
                if isinstance(token, IdentifierList)
                else [token]
            )
            for entry in entries:
                if isinstance(entry, Identifier):
                    name = (entry.get_real_name() or "").casefold()
                    alias = (entry.get_alias() or name).casefold()
                    nested = next(
                        (t for t in entry.tokens if isinstance(t, Parenthesis)), None
                    )
                    relations[alias] = (
                        sqlparse.parse(nested.value[1:-1])[0]
                        if nested
                        else ctes.get(name, name)
                    )
                else:
                    name = entry.value.strip('"`[]').casefold()
                    relations[name] = ctes.get(name, name)
            expecting = False
    return ctes, projections, relations


def _sales_identity_reference(statement, reference, inherited=None):
    ctes, _, relations = _select_bindings(statement, inherited)
    parsed = sqlparse.parse("SELECT " + reference)[0]
    _, expressions, _ = _select_bindings(parsed)
    if len(expressions) != 1:
        return False
    expression = expressions[0]
    if isinstance(expression, Identifier):
        if any(isinstance(t, (Function, Parenthesis)) for t in expression.tokens):
            return False
        field = expression.get_real_name()
        parent = expression.get_parent_name()
    else:
        field, parent = expression.value.strip('"`[]'), None
    if parent:
        relation = relations.get(parent.casefold())
    elif len(relations) == 1:
        relation = next(iter(relations.values()))
    else:
        return False
    if isinstance(relation, str):
        return relation == "erp销售" and field.casefold() in {"rowid", "_rowid_", "oid"}
    return relation is not None and sales_identity_column(relation, field, ctes)


def sales_identity_column(statement, column="source_sales_row_id", inherited=None):
    ctes, projections, relations = _select_bindings(statement, inherited)
    expression = next(
        (
            t
            for t in projections
            if isinstance(t, Identifier)
            and (t.get_alias() or t.get_real_name()) == column
        ),
        None,
    )
    if expression is None:
        expression = next(
            (t for t in projections if t.value.strip('"`[]') == column), None
        )
    if expression is None:
        candidates = []
        for alias in relations:
            if any(
                t.value == "*" or t.value.casefold() == alias + ".*"
                for t in projections
            ):
                candidates.append(
                    _sales_identity_reference(statement, alias + "." + column, ctes)
                )
        return sum(candidates) == 1
    if isinstance(expression, Identifier):
        as_index = next(
            (i for i, t in enumerate(expression.tokens) if t.normalized == "AS"), None
        )
        if as_index is not None:
            reference = "".join(str(t) for t in expression.tokens[:as_index]).strip()
        elif expression.get_alias():
            reference = "".join(str(t) for t in expression.tokens[:-1]).strip()
        else:
            reference = str(expression)
    else:
        reference = expression.value
    return _sales_identity_reference(statement, reference, ctes)


def original_count_scope(statement, column="source_sales_row_count", inherited=None):
    """Locate the unjoined COUNT input for a separate read-only source check."""
    ctes, projections, relations = _select_bindings(statement, inherited)
    expression = next(
        (
            t
            for t in projections
            if isinstance(t, Identifier)
            and (t.get_alias() or t.get_real_name()) == column
        ),
        None,
    )
    if expression is None and len(projections) == 1:
        expression = projections[0]
    if expression is None:
        return None

    def locate(token):
        if (
            isinstance(token, Function)
            and (token.get_name() or "").casefold() == "count"
        ):
            return statement, ctes
        if isinstance(token, Parenthesis):
            parsed = sqlparse.parse(token.value[1:-1])
            if parsed and parsed[0].get_type() == "SELECT":
                return original_count_scope(parsed[0], "", ctes)
        if isinstance(token, Identifier) and not any(
            isinstance(t, (Function, Parenthesis)) for t in token.tokens
        ):
            parent, field = token.get_parent_name(), token.get_real_name()
            for alias, relation in relations.items():
                if (parent is None or alias == parent.casefold()) and not isinstance(
                    relation, str
                ):
                    found = original_count_scope(relation, field, ctes)
                    if found:
                        return found
        if token.is_group:
            for child in token.tokens:
                found = locate(child)
                if found:
                    return found
        return None

    return locate(expression)


def visible_source_column(relation, table, column, inherited=None):
    if isinstance(relation, str):
        return column if relation == table.casefold() else None
    ctes, projections, relations = _select_bindings(relation, inherited)
    candidates = set()
    for alias, source in relations.items():
        visible = visible_source_column(source, table, column, ctes)
        if visible is None:
            continue
        for expression in projections:
            if expression.value == "*" or expression.value.casefold() == alias + ".*":
                # SQLite's hidden rowid is not exported by SELECT * into a CTE.
                if visible.casefold() not in {"rowid", "_rowid_", "oid"}:
                    candidates.add(visible)
            elif (
                isinstance(expression, Identifier)
                and (expression.get_real_name() == visible or (
                    visible.casefold() in {"rowid", "_rowid_", "oid"}
                    and sales_identity_column(relation, expression.get_alias() or expression.get_real_name(), ctes)
                ))
            ):
                parent = expression.get_parent_name()
                if parent is None or parent.casefold() == alias:
                    candidates.add(expression.get_alias() or visible)
    return next(iter(candidates)) if len(candidates) == 1 else None


def validate_cost_join(state, sql, columns, rows):
    """Reject fanout in the actual ERP sales-to-cost join used by this report."""
    if state.get("term_explanation_only") or not _ratio_definitions(state):
        return None
    statements = sqlparse.parse(sqlparse.format(sql, strip_comments=True))
    if not statements:
        return "该结果未完成关联检查：无法识别SELECT来源。"
    sources, aggregate = select_sources(statements[0])
    if "erp销售" not in sources or not sources.intersection({"材料单价", "工费单价"}):
        return None
    error = _validate_sales_join_result(state, columns, rows, aggregate=aggregate)
    if error:
        return error
    if not aggregate and not {
        "source_sales_row_count",
        "joined_sales_row_count",
    } <= set(columns):
        if not sales_identity_column(statements[0]):
            state.setdefault("query_data_gaps", {})["sales_cost_join"] = (
                "原销售行标识来源尚未通过校验。"
            )
            return "source_sales_row_id必须来自ERP销售的真实rowid，不能使用成本rowid、金额或新编号。"
    if aggregate or {"source_sales_row_count", "joined_sales_row_count"} <= set(
        columns
    ):
        original = count_sources(statements[0], "source_sales_row_count")
        joined = count_sources(statements[0], "joined_sales_row_count")
        if (
            original != {"erp销售"}
            or "erp销售" not in joined
            or not joined.intersection({"材料单价", "工费单价"})
        ):
            state.setdefault("query_data_gaps", {})["sales_cost_join"] = (
                "原销售和关联后计数来源尚未通过校验。"
            )
            return "该结果未完成关联检查：原行数必须来自未关联销售范围，关联后行数来自处理后的关联范围；不能两者都取关联后的COUNT或相等常数。"
    state["sales_cost_join_verified"] = True
    return None


def count_sources(statement, column, inherited=None, allow_distinct=None):
    """Trace the supported COUNT projections through scalar SELECTs and CTEs."""
    ctes = dict(inherited or {})
    if allow_distinct is None:
        allow_distinct = column == "source_sales_row_count"
    tokens = [t for t in statement.tokens if not t.is_whitespace]
    start = next(
        (
            i
            for i, t in enumerate(tokens)
            if t.ttype is DML and t.normalized == "SELECT"
        ),
        None,
    )
    if start is None:
        return set()
    for token in tokens[:start]:
        entries = (
            token.get_identifiers() if isinstance(token, IdentifierList) else [token]
        )
        for entry in entries:
            if isinstance(entry, Identifier):
                subquery = next(
                    (t for t in entry.tokens if isinstance(t, Parenthesis)), None
                )
                if subquery:
                    ctes[(entry.get_real_name() or "").casefold()] = sqlparse.parse(
                        subquery.value[1:-1]
                    )[0]
    end = next(
        (i for i, t in enumerate(tokens) if i > start and t.normalized == "FROM"),
        len(tokens),
    )
    projections = []
    for token in tokens[start + 1 : end]:
        projections.extend(
            list(token.get_identifiers())
            if isinstance(token, IdentifierList)
            else [token]
        )
    expr = next(
        (
            t
            for t in projections
            if isinstance(t, Identifier)
            and (t.get_alias() or t.get_real_name()) == column
        ),
        None,
    )
    if expr is None:
        # Scalar SELECT COUNT(*) need not name its single projection.
        expr = (
            next((t for t in projections if isinstance(t, Function)), None)
            if len(projections) == 1
            else None
        )
    if expr is None:
        return set()

    def expression_sources(token):
        if isinstance(token, Parenthesis):
            parsed = sqlparse.parse(token.value[1:-1])
            if parsed and parsed[0].get_type() == "SELECT":
                return count_sources(parsed[0], "", ctes, allow_distinct)
        if (
            isinstance(token, Function)
            and (token.get_name() or "").casefold() == "count"
        ):
            arguments = (
                next(t for t in token.tokens if isinstance(t, Parenthesis))
                .value[1:-1]
                .strip()
            )
            distinct = bool(re.match(r"distinct\s", arguments, re.I))
            arguments = re.sub(r"^distinct\s+", "", arguments, flags=re.I)
            if distinct and not allow_distinct:
                return set()
            if arguments != "*" and not _sales_identity_reference(
                statement, arguments, ctes
            ):
                return set()
            inherited_sources = {}
            for name, query in ctes.items():
                inherited_sources[name] = select_sources(query, inherited_sources)[0]
            return select_sources(statement, inherited_sources)[0]
        if isinstance(token, Identifier) and not any(
            isinstance(child, (Function, Parenthesis)) for child in token.tokens
        ):
            field = token.get_real_name()
            parent = token.get_parent_name()
            expecting_relation = False
            for relation_token in tokens[end:]:
                if relation_token.ttype in Keyword and (
                    relation_token.normalized == "FROM"
                    or "JOIN" in relation_token.normalized
                ):
                    expecting_relation = True
                elif expecting_relation:
                    relations = (
                        relation_token.get_identifiers()
                        if isinstance(relation_token, IdentifierList)
                        else [relation_token]
                    )
                    for relation in relations:
                        if isinstance(relation, Identifier):
                            name, alias = relation.get_real_name(), relation.get_alias()
                            if (parent is None or parent in {name, alias}) and (
                                name or ""
                            ).casefold() in ctes:
                                return count_sources(
                                    ctes[name.casefold()], field, ctes, allow_distinct
                                )
                    expecting_relation = False
        if token.is_group:
            sources = set()
            for child in token.tokens:
                sources.update(expression_sources(child))
            return sources
        return set()

    return expression_sources(expr)


def confirmed_price_rule(state, table, price):
    for term in state.get("confirmed_terms", []):
        if (
            term.get("datasource", "") == state.get("term_datasource", "")
            and term.get("formula_confirmed")
            and any(name in term.get("term", "") for name in (table, price))
        ):
            for operator, pattern in (
                ("max", r"最高|最大|\bmax\b"),
                ("min", r"最低|最小|\bmin\b"),
                ("avg", r"平均|均值|\bavg\b"),
            ):
                if re.search(pattern, term.get("formula", ""), re.I):
                    return operator
    return None


def validate_python_sales_evidence(state, payload):
    sources = payload.get("source_result_ids")
    joined_ids = payload.get("joined_sales_row_ids")
    if not isinstance(sources, list) or not sources or not isinstance(joined_ids, list):
        return "Python关联检查必须引用source_result_ids，并返回实际处理后的joined_sales_row_ids；仅打印相等计数不能通过。"
    identities = {}
    source_records = []
    for rid in sources:
        snapshot = (
            state.get("validated_results", {}).get(rid)
            if isinstance(rid, str)
            else None
        )
        if (
            snapshot
            and snapshot.get("sql_sources")
            and set(snapshot["sql_sources"]) <= {"材料单价", "工费单价"}
        ):
            if snapshot["conv_id"] != state.get("conv_id") or snapshot[
                "database"
            ] != state.get("term_datasource", ""):
                return "成本输入属于其他任务或数据源。"
            if snapshot.get("price_mapping_checked") is False:
                return "成本源的价格映射尚未完成检查，不能认证Python关联结果。"
            continue
        if (
            not snapshot
            or not snapshot.get("sales_source")
            or snapshot["conv_id"] != state.get("conv_id")
            or snapshot["database"] != state.get("term_datasource", "")
            or snapshot.get("analysis_scope") != state.get("task_requirements", {}).get("analysis_scope")
        ):
            return "Python关联原行证据必须来自当前任务未关联的ERP销售明细结果。"
        columns = snapshot["columns"]
        index = columns.index("source_sales_row_id")
        period = next(
            (
                columns.index(c)
                for c in ("期间", "月份", "period", "month")
                if c in columns
            ),
            None,
        )
        source_ids = [row[index] for row in snapshot["rows"]]
        if None in source_ids or len(set(source_ids)) != len(source_ids):
            return "原销售输入的行身份重复或缺失，不能去重后当成原销售行数。"
        for row in snapshot["rows"]:
            identities[row[index]] = str(row[period]) if period is not None else ""
            source_records.append(dict(zip(columns, row)))
    cited_costs = [
        state.get("validated_results", {}).get(rid)
        for rid in sources
        if isinstance(rid, str)
    ]
    cited_costs = [
        s
        for s in cited_costs
        if s
        and s.get("sql_sources")
        and set(s["sql_sources"]) <= {"材料单价", "工费单价"}
    ]
    joined_source = state.get("validated_results", {}).get(payload.get("joined_result_id"))
    joined_costs_verified = False
    if payload.get("joined_result_id"):
        if (
            not joined_source
            or joined_source.get("conv_id") != state.get("conv_id")
            or joined_source.get("database") != state.get("term_datasource", "")
            or joined_source.get("analysis_scope") != state.get("task_requirements", {}).get("analysis_scope")
            or "erp销售" not in joined_source.get("sql_sources", [])
            or not {"材料单价", "工费单价"}.intersection(joined_source.get("sql_sources", []))
            or "source_sales_row_id" not in joined_source["columns"]
            or not state.get("cost_input_evidence")
        ):
            return "Python关联结果必须引用当前范围已验证的销售成本明细来源。"
        index = joined_source["columns"].index("source_sales_row_id")
        source_ids = [row[index] for row in joined_source["rows"]]
        if len(source_ids) != len(identities) or set(source_ids) != set(identities):
            return "Python关联来源的实际销售身份与原销售范围不一致。"
        error = validate_cost_population(state, joined_source["columns"], joined_source["rows"])
        if error:
            return error
        joined_costs_verified = True
    if state.get("cost_source_read") and not cited_costs and not joined_costs_verified:
        return "Python关联需在source_result_ids同时引用实际销售及成本源结果，不能省略成本来源。"
    for snapshot in cited_costs:
        if snapshot.get("conv_id") != state.get("conv_id") or snapshot.get(
            "database"
        ) != state.get("term_datasource", ""):
            continue
        for conflict in snapshot.get("price_conflicts", []):
            keys = conflict["keys"]
            if not all(all(k in record for k in keys) for record in source_records):
                return "Python成本价格检查需要源销售明细的真实产品/期间键。"
            if any(
                all(record[k] == value for k, value in keys.items())
                for record in source_records
            ):
                price = conflict.get("price_field") or (
                    "材料成本单价" if conflict["table"] == "材料单价" else "工费"
                )
                rule = confirmed_price_rule(state, conflict["table"], price)
                if rule and price in snapshot["columns"]:
                    expected = (
                        max(conflict["prices"])
                        if rule == "max"
                        else min(conflict["prices"])
                        if rule == "min"
                        else sum(conflict["prices"]) / len(conflict["prices"])
                    )
                    matching = [
                        dict(zip(snapshot["columns"], row)) for row in snapshot["rows"]
                    ]
                    values = [
                        row[price]
                        for row in matching
                        if all(row.get(k) == value for k, value in keys.items())
                    ]
                    if values and all(value == expected for value in values):
                        continue
                return (
                    "Python成本价格冲突："
                    + json.dumps(conflict, ensure_ascii=False)
                    + "；不能自行取首条、最高价或均值。"
                )
    if (
        any(not isinstance(i, (str, int)) for i in joined_ids)
        or len(set(joined_ids)) != len(joined_ids)
        or set(joined_ids) != set(identities)
    ):
        return "Python成本关联放大或丢失了真实销售行。"
    columns, rows = payload["columns"], payload["rows"]
    original, joined = (
        columns.index("source_sales_row_count"),
        columns.index("joined_sales_row_count"),
    )
    detail_rows = [row for row in rows if not _monthly_total_row(columns, row)]
    if sum(row[original] for row in detail_rows) != len(identities) or sum(
        row[joined] for row in detail_rows
    ) != len(joined_ids):
        return "Python计数与实际原销售及处理后销售行不一致。"
    period = next(
        (columns.index(c) for c in ("期间", "月份", "period", "month") if c in columns),
        None,
    )
    if period is not None:
        counts = Counter(identities.values())
        if any(
            row[original] != (len(identities) if _monthly_total_row(columns, row) else counts[str(row[period])])
            or row[joined] != (len(joined_ids) if _monthly_total_row(columns, row) else counts[str(row[period])])
            for row in rows
        ):
            return "Python按月销售计数与实际源明细不一致。"
    return None


def _validate_sales_join_result(state, columns, rows, aggregate=False):
    gaps = state.setdefault("query_data_gaps", {})
    gap = "sales_cost_join"
    if not rows:
        gaps.pop(gap, None)
        return None
    gaps[gap] = "销售与成本关联的行数完整性尚未通过校验。"
    count_columns = {"source_sales_row_count", "joined_sales_row_count"}
    if aggregate or count_columns <= set(columns):
        if not count_columns <= set(columns):
            return (
                "ERP销售成本聚合必须返回source_sales_row_count（原销售行数）和"
                "joined_sales_row_count（关联后行数），核对成本关联未放大销售。"
            )
        original = columns.index("source_sales_row_count")
        joined = columns.index("joined_sales_row_count")
        for row in rows:
            counts = [row[original], row[joined]]
            if any(not isinstance(n, (int, float)) or n < 0 for n in counts):
                return "销售关联行数必须由实际查询计数得到，不能为空。"
            if row[original] != row[joined]:
                return "成本关联放大或丢失了销售行，请先检查成本映射再计算。"
    else:
        if "source_sales_row_id" not in columns:
            return (
                "ERP销售成本明细关联必须返回e.rowid AS source_sales_row_id，"
                "以验证原销售行没有因成本重复记录被放大。"
            )
        index = columns.index("source_sales_row_id")
        identities = [row[index] for row in rows]
        if None in identities or len(set(identities)) != len(identities):
            return (
                "同一原销售行在成本关联结果中重复。请按实际关联键和价格"
                "去除完全重复成本映射；价格冲突需确认，不能删掉真实销售行。"
            )
    gaps.pop(gap, None)
    return None


def _validate_given_ratio_output(state, output):
    if any(
        state.get(key) for key in ("term_datasource", "file_path", "query_result_path")
    ):
        return None
    definitions = _ratio_definitions(state)
    if len(definitions) != 1:
        return None
    label, numerator, denominator = definitions[0]
    request = state.get("original_user_input", "")

    def given_number(name):
        values = re.findall(
            rf"{re.escape(name)}\s*(?:为|是|[:：=])?\s*"
            r"([-+]?\d+(?:\.\d+)?)(?:元)?(?![,\.]\d)(?=[，,;；。\s]|$)",
            request,
        )
        return float(values[0]) if len(values) == 1 else None

    num, den = given_number(numerator), given_number(denominator)
    if num is None or den is None:
        return None
    raw = output.strip()
    if den == 0 and raw.casefold() in {"null", "none"}:
        ratio = None
    else:
        match = re.fullmatch(r"([-+]?\d+(?:\.\d+)?)([%％]?)", raw)
        if not match:
            return None
        if den == 0:
            return "分母为0时比率应为NULL。"
        percent = bool(match[2])
        ratio = float(match[1]) / (100 if percent else 1)
        if not ratio_matches(raw, num / den, display=percent):
            return f"{label}与原文给定的{numerator}/{denominator}不一致。"
        ratio = num / den
        if ratio_contract(state, label)["unit"] == "percent":
            ratio *= 100
    return validate_explicit_ratios(
        state,
        [numerator, denominator, label, "missing_input_count"],
        [[num, den, ratio, 0]],
    )


def ratio_delivery_hint(state):
    gaps = state.get("query_data_gaps", {})
    validation = state.get("ratio_validation_status", {})
    pending = [
        (label, num, den)
        for label, num, den in _ratio_definitions(state)
        if (label in gaps and validation.get(label) != "missing_inputs")
        or (state.get("term_datasource") and state.get("requires_report") and not any(
            label in snapshot["columns"] and snapshot.get("conv_id") == state.get("conv_id")
            and snapshot.get("analysis_scope") == state.get("task_requirements", {}).get("analysis_scope")
            for snapshot in state.get("validated_results", {}).values()))
    ]
    join_pending = "sales_cost_join" in gaps
    if not pending and not join_pending:
        if any(validation.get(label) == "missing_inputs" for label in gaps):
            return (
                "输入缺失已确认，无需重复提交校验。保留完整比率为null，"
                "继续交付可用结果和要求的报告；仅用户要求必须完整数值时任务未完成。"
            )
        return None
    columns = []
    if "期间" in state.get("task_requirements", {}).get("analysis_scope", {}).get("group_by", []):
        columns.append("期间")
    for label, num, den in pending:
        for name in (num, den, label, "missing_input_count"):
            if name not in columns:
                columns.append(name)
        if len(_ratio_definitions(state)) > 1:
            columns.append(f"{label}_missing_input_count")
    if join_pending:
        columns += ["source_sales_row_count", "joined_sales_row_count"]
    return (
        "交付校验尚未完成。若这是最终计算，请最后打印单行JSON，columns使用这些列名："
        + json.dumps(columns, ensure_ascii=False)
        + "，rows为相同列顺序的行数组。比率分子分母必须同范围，缺输入时完整比率为null，"
        "计算值不得提前round；每项计算单位严格使用以下契约（fraction中0.2代表20%）："
        + json.dumps(
            {label: ratio_contract(state, label) for label, _, _ in pending},
            ensure_ascii=False,
        )
        + "。"
        "missing_input_count记录实际缺失数。若有原销售行数和处理后行数，两者必须一致。"
        "完整与子集指标共存时，每个指标用其同名_missing_input_count返回自身范围的实际缺失数；"
        "成本完整子集为0，通用missing_input_count仍记录原销售范围的真实缺失行数。"
        "Python关联必须引用source_result_ids（未关联销售明细result_id），并返回实际joined_sales_row_ids；"
        "joined_sales_row_ids是关联后的全部销售身份，包含缺成本行，不是成本完整子集；"
        "关联前后全量行数相同，完整子集行数另列。实际文件和ID从QUERY_RESULTS读取，不能生成1..N编号代替真实rowid。"
        "系统按源结果核对实际身份和按月计数，不能打印相等常数。"
        "不能用局部比率替代完整比率；此要求由模型完成，无需用户填写。"
    )
