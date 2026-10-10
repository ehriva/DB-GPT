"""Regressions from the real monthly-profit acceptance failure."""

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from dbgpt_app.openapi.api_v1.react_task import missing_deliverables
from dbgpt_app.openapi.api_v1.tools.query_quality import validate_explicit_ratios
from dbgpt_app.openapi.api_v1.tools.sql_query import make_sql_query


@pytest.mark.parametrize("database", ["", "sales"])
def test_given_average_with_empty_database_scope_needs_no_sql_source(database):
    from dbgpt_app.openapi.api_v1.tools.query_quality import validate_python_ratio_output

    s = {"term_datasource": database, "task_requirements": {
        "analysis_scope": {"database": {"name": database, "type": "sqlite" if database else ""}, "periods": [], "customer_codes": []},
        "ratios": [{"metric": "总体平均销售单价", "numerator": "总销售金额", "denominator": "总销售数量", "unit": "fraction", "unavailable_policy": "explain"}],
    }}
    output = json.dumps({"columns": ["总销售金额", "总销售数量", "总体平均销售单价", "missing_input_count"], "rows": [[1000, 40, 25, 0]]}, ensure_ascii=False)
    error = validate_python_ratio_output(s, output)
    assert bool(error) is bool(database)


@pytest.mark.parametrize("joined_source", [False, True])
@pytest.mark.parametrize("product_column", [False, True])
@pytest.mark.parametrize("change", [None, "wrong_total", "missing_month", "duplicate_total", "wrong_count", "wrong_cost"])
def test_monthly_cost_output_with_total_is_checked_against_sources(change, joined_source, product_column):
    from dbgpt_app.openapi.api_v1.tools.query_quality import validate_python_ratio_output

    scope = {"periods": ["2026.01", "2026.06"], "group_by": ["期间"]}
    records = [
        {"source_sales_row_id": 10, "期间": "2026.01", "产品编码": "A", "销售金额": 100, "销售数量": 10, "材料成本单价": 2, "工费": 1},
        {"source_sales_row_id": 20, "期间": "2026.06", "产品编码": "B", "销售金额": 200, "销售数量": 20, "材料成本单价": 3, "工费": None},
    ]
    s = {
        "task_requirements": {"analysis_scope": scope, "ratios": [{
            "metric": "毛利率", "numerator": "毛利额", "denominator": "销售额",
            "unit": "fraction", "unavailable_policy": "explain", "population": "full",
            "cost_basis": "standard", "required_inputs": ["材料成本单价", "工费", "销售数量", "销售金额"],
        }]},
        "cost_input_evidence": {"analysis_scope": scope, "records": records},
        "sales_source_read": True, "cost_source_read": True,
        "validated_results": {"source": {"analysis_scope": scope, "columns": ["source_sales_row_id", "期间"], "rows": [[10, "2026.01"], [20, "2026.06"]], "sql_sources": ["erp销售"]}},
    }
    s["validated_results"]["source"].update(sales_source=True, conv_id="total", database="sales")
    s.update(conv_id="total", term_datasource="sales")
    s["validated_results"]["cost"] = {"conv_id": "total", "database": "sales", "sql_sources": ["材料单价"], "analysis_scope": scope, "columns": [], "rows": [], "price_mapping_checked": True}
    rows = [["2026.01", 70, 100, .7, 0, 1, 1], ["2026.06", None, 200, None, 1, 1, 1], ["合计", None, 300, None, 1, 2, 2]]
    if change == "wrong_total":
        rows[-1][2] = 301
    elif change == "missing_month":
        rows.pop(1)
    elif change == "duplicate_total":
        rows.append(list(rows[-1]))
    elif change == "wrong_count":
        rows[-1][-2:] = [1, 1]
    payload = {"columns": ["期间", "毛利额", "销售额", "毛利率", "missing_input_count", "source_sales_row_count", "joined_sales_row_count"], "rows": rows, "source_result_ids": ["source", "cost"], "joined_sales_row_ids": [10, 20]}
    if product_column:
        payload["columns"].insert(1, "产品编码")
        products = {"2026.01": "A", "2026.06": "B", "合计": None}
        for row in rows:
            row.insert(1, products[row[0]])
    if joined_source:
        fields = list(records[0])
        s["validated_results"]["join"] = {"conv_id": "total", "database": "sales", "analysis_scope": scope, "columns": fields, "rows": [[r[c] for c in fields] for r in records], "sql_sources": ["erp销售", "材料单价", "工费单价"]}
        payload["source_result_ids"] = ["source"]
        payload["joined_result_id"] = "join"
        if change == "wrong_cost":
            s["validated_results"]["join"]["rows"][0][fields.index("工费")] = 0
    elif change == "wrong_cost":
        rows[0][1] = 80
        rows[0][3] = .8
    error = validate_python_ratio_output(s, json.dumps(payload, ensure_ascii=False))
    if change is None:
        assert error is None
        assert s["data_warnings"]["毛利率"]["affected_rows"] == 1
        assert "sales_cost_join" not in s["query_data_gaps"]
    else:
        assert error


@pytest.mark.parametrize("marker", ["合计", "总计"])
@pytest.mark.parametrize("identity", [None, "A", "合计", 0])
def test_total_row_preserves_non_null_product_and_sales_identity(marker, identity):
    from dbgpt_app.openapi.api_v1.tools.query_quality import _monthly_total_row

    for column in ("产品编码", "产品代码", "料号", "product_code", "source_sales_row_id"):
        assert _monthly_total_row(["期间", column], [marker, identity]) is (
            identity is None
        )
    assert not _monthly_total_row(
        ["期间", "产品编码", "source_sales_row_id"], [marker, None, 10]
    )


@pytest.mark.asyncio
async def test_scoped_requery_rejects_missing_customer_without_overwriting_source(
    tmp_path, monkeypatch
):
    from sqlalchemy import text
    from sqlalchemy.pool import StaticPool
    from dbgpt.configs import model_config

    monkeypatch.setattr(model_config, "PILOT_PATH", str(tmp_path))
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    with engine.begin() as connection:
        connection.execute(
            text("CREATE TABLE ERP销售 (客户编码 TEXT,期间 TEXT,销售金额 REAL)")
        )
        connection.execute(
            text("INSERT INTO ERP销售 VALUES ('A','2026.01',100),('B','2026.01',200)")
        )
    s = {
        "conv_id": "scope",
        "term_datasource": "sales",
        "task_requirements": {
            "analysis_scope": {
                "database": {"name": "sales", "type": "sqlite"},
                "customer_codes": ["A"],
                "periods": ["2026.01"],
                "customer_evidence": "A",
                "period_evidence": "2026.01",
            }
        },
    }
    query = make_sql_query(s, SimpleNamespace(_engine=engine))
    correct = json.loads(
        await query(
            "SELECT rowid AS source_sales_row_id,* FROM ERP销售 WHERE 客户编码='A' AND 期间='2026.01'"
        )
    )
    assert correct["success"]
    previous = s["query_result_path"]
    wrong = json.loads(
        await query(
            "SELECT rowid AS source_sales_row_id,* FROM ERP销售 WHERE 期间='2026.01'"
        )
    )
    assert wrong["success"] is False
    assert s["query_result_path"] == previous
    assert len(s["validated_results"]) == 1
    engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "condition",
    [
        "客户编码='B'",
        "客户编码='A' AND 期间='2026.01'",
        "客户编码='A'",
        "客户编码='A' AND rowid<>1",
    ],
)
async def test_scoped_sales_reject_wrong_customer_months_and_missing_id(
    tmp_path, monkeypatch, condition
):
    from sqlalchemy import text
    from sqlalchemy.pool import StaticPool
    from dbgpt.configs import model_config

    monkeypatch.setattr(model_config, "PILOT_PATH", str(tmp_path))
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    with engine.begin() as connection:
        connection.execute(
            text("CREATE TABLE ERP销售 (客户编码 TEXT,期间 TEXT,销售金额 REAL)")
        )
        connection.execute(
            text(
                "INSERT INTO ERP销售 VALUES ('A','2026.01',100),('A','2026.06',100),('A','2026.07',100),('B','2026.01',100),('B','2026.06',100)"
            )
        )
    s = {
        "conv_id": "scope",
        "term_datasource": "sales",
        "task_requirements": {
            "analysis_scope": {
                "database": {"name": "sales", "type": "sqlite"},
                "customer_codes": ["A"],
                "periods": ["2026.01", "2026.06"],
            }
        },
    }
    query = make_sql_query(s, SimpleNamespace(_engine=engine))
    wrong = json.loads(
        await query(
            "SELECT rowid AS source_sales_row_id,* FROM ERP销售 WHERE " + condition
        )
    )
    assert not wrong["success"]
    assert not s.get("validated_results")
    correct = json.loads(
        await query(
            "WITH s AS (SELECT e.rowid AS source_sales_row_id,e.期间 FROM ERP销售 e WHERE e.客户编码='A' AND e.期间 IN ('2026.06','2026.01')) SELECT * FROM s"
        )
    )
    assert correct["success"]
    duplicate = json.loads(
        await query(
            "SELECT e.rowid AS source_sales_row_id,e.期间 FROM ERP销售 e JOIN ERP销售 x ON x.客户编码=e.客户编码 WHERE e.客户编码='A' AND e.期间 IN ('2026.01','2026.06')"
        )
    )
    assert not duplicate["success"]
    aggregate = json.loads(
        await query(
            "SELECT (SELECT COUNT(*) FROM ERP销售 WHERE 客户编码='B' AND 期间 IN ('2026.01','2026.06')) AS source_sales_row_count, SUM(销售金额) AS 销售额 FROM ERP销售 WHERE 客户编码='B' AND 期间 IN ('2026.01','2026.06')"
        )
    )
    assert not aggregate["success"]
    engine.dispose()


@pytest.mark.asyncio
async def test_cost_subset_uses_actual_null_inputs_zero_labor_and_same_population(
    tmp_path, monkeypatch
):
    from sqlalchemy import text
    from sqlalchemy.pool import StaticPool
    from dbgpt.configs import model_config
    from dbgpt_app.openapi.api_v1.terms.service import apply_cost_contract

    monkeypatch.setattr(model_config, "PILOT_PATH", str(tmp_path))
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    with engine.begin() as connection:
        for sql in [
            "CREATE TABLE ERP销售 (客户编码 TEXT,期间 TEXT,产品编码 TEXT,销售数量 REAL,销售金额 REAL)",
            "CREATE TABLE 材料单价 (期间 TEXT,产品编码 TEXT,材料成本单价 REAL)",
            "CREATE TABLE 工费单价 (产品编码 TEXT,工费 REAL)",
            "INSERT INTO ERP销售 VALUES ('A','2026.01','p1',2,100),('A','2026.01','p2',2,0),('A','2026.01','p3',2,200),('A','2026.01','p4',2,50),('A','2026.01','p5',NULL,10)",
            "INSERT INTO 材料单价 VALUES ('2026.01','p1',10),('2026.01','p2',10),('2026.01','p3',10),('2026.01','p4',NULL),('2026.01','p5',2)",
            "INSERT INTO 工费单价 VALUES ('p1',5),('p2',0),('p3',NULL),('p4',5),('p5',2)",
        ]:
            connection.execute(text(sql))
    s = {
        "conv_id": "cost",
        "term_datasource": "sales",
        "task_requirements": {
            "analysis_scope": {
                "database": {"name": "sales", "type": "sqlite"},
                "customer_codes": ["A"],
                "periods": ["2026.01"],
            },
            "ratios": [
                {
                    "metric": "子集毛利率",
                    "numerator": "子集毛利额",
                    "denominator": "子集销售额",
                    "unit": "fraction",
                    "unavailable_policy": "explain",
                }
            ],
        },
    }
    apply_cost_contract(s["task_requirements"], "成本完整子集")
    s["task_requirements"]["analysis_scope"]["group_by"] = ["期间"]
    query = make_sql_query(s, SimpleNamespace(_engine=engine))
    base = "WITH base AS (SELECT rowid AS source_sales_row_id,* FROM ERP销售 WHERE 客户编码='A' AND 期间='2026.01'), j AS (SELECT e.*,m.材料成本单价,g.工费 FROM base e LEFT JOIN 材料单价 m ON m.期间=e.期间 AND m.产品编码=e.产品编码 LEFT JOIN 工费单价 g ON g.产品编码=e.产品编码) "
    detail = json.loads(await query(base + "SELECT * FROM j"))
    assert detail["success"]
    from pathlib import Path
    stored = json.loads(Path(detail["result_file"]).read_text(encoding="utf-8"))
    assert "missing_input_count" in stored["columns"]
    identity_index = stored["columns"].index("source_sales_row_id")
    missing_index = stored["columns"].index("missing_input_count")
    assert {row[identity_index]: row[missing_index] for row in stored["rows"]} == {1: 0, 2: 0, 3: 1, 4: 1, 5: 1}
    subset = json.loads(
        await query(
            base
            + "SELECT * FROM j WHERE 材料成本单价 IS NOT NULL AND 工费 IS NOT NULL AND 销售数量 IS NOT NULL AND 销售金额 IS NOT NULL"
        )
    )
    assert subset["success"] and subset["row_count"] == 2
    wrong_subset = json.loads(
        await query(
            base
            + "SELECT * FROM j WHERE 材料成本单价 IS NOT NULL AND 销售金额>0 LIMIT 2"
        )
    )
    assert not wrong_subset["success"]
    columns = [
        "期间",
        "子集毛利额",
        "子集销售额",
        "子集毛利率",
        "子集毛利率_missing_input_count",
        "成本完整行数",
    ]
    from dbgpt_app.openapi.api_v1.tools.query_quality import validate_explicit_ratios

    assert (
        validate_explicit_ratios(s, columns, [["2026.01", 50, 100, 0.5, 0, 2]]) is None
    )
    assert validate_explicit_ratios(s, columns[1:], [[50,100,.5,0,2]])
    month_alias = ["月份", *columns[1:]]
    assert validate_explicit_ratios(s, month_alias, [["2026.01",50,100,.5,0,2]]) is None
    assert validate_explicit_ratios(s, month_alias, [["2026.06",50,100,.5,0,2]])
    assert validate_explicit_ratios(s, columns, [["2026.01", 70, 100, 0.7, 0, 2]])
    assert validate_explicit_ratios(s, columns, [["2026.01", 50, 300, 50 / 300, 0, 2]])
    assert validate_explicit_ratios(s, columns, [["2026.01", 50, 100, 0.5, 0, 3]])
    engine.dispose()

def state():
    return {
        "original_user_input": "毛利率=毛利额/销售额，按月汇总",
        "task_requirements": {
            "ratios": [
                {
                    "metric": "毛利率",
                    "numerator": "毛利额",
                    "denominator": "销售额",
                    "unit": "fraction",
                    "unavailable_policy": "require_value",
                }
            ]
        },
    }


def test_followup_retains_requested_ratio_without_repeating_formula():
    from dbgpt_app.openapi.api_v1.tools.query_quality import (
        require_explicit_ratio_results,
    )

    s = {
        "original_user_input": "继续原需求，纠正报告",
        "task_requirements": {
            "ratios": [
                {
                    "metric": "毛利率",
                    "numerator": "毛利额",
                    "denominator": "销售额",
                    "unit": "fraction",
                    "unavailable_policy": "explain",
                }
            ]
        },
    }
    require_explicit_ratio_results(s)
    assert "毛利率" in s.get("query_data_gaps", {})
    assert validate_explicit_ratios(
        s,
        ["毛利额", "销售额", "毛利率", "missing_input_count"],
        [[4, 1000, 0.5, 0]],
    )


@pytest.mark.parametrize(
    "policy,expected", [("explain", "succeeded"), ("require_value", "incomplete")]
)
def test_verified_null_obeys_user_delivery_policy(policy, expected):
    from dbgpt.agent.core.agent import AgentMessage
    from dbgpt.agent.core.action.base import ActionOutput
    from dbgpt_app.openapi.api_v1.react_task import task_result_for_reply
    from dbgpt_app.openapi.api_v1.tools.query_quality import (
        require_explicit_ratio_results,
    )

    s = {
        "task_requirements": {
            "ratios": [
                {
                    "metric": "毛利率",
                    "numerator": "毛利额",
                    "denominator": "销售额",
                    "unit": "fraction",
                    "unavailable_policy": policy,
                }
            ]
        }
    }
    require_explicit_ratio_results(s)
    assert (
        validate_explicit_ratios(
            s,
            ["毛利额", "销售额", "毛利率", "missing_input_count"],
            [[None, 100, None, 1]],
        )
        is None
    )
    reply = AgentMessage(
        success=True,
        action_report=ActionOutput(
            content="完整毛利率NULL，缺1行工费",
            is_exe_success=True,
            terminate=True,
        ),
    )
    result = task_result_for_reply(reply, s)
    assert result.status == expected
    assert result.warnings[0]["affected_rows"] == 1
    s["requires_report"] = True
    assert task_result_for_reply(reply, s).status == "incomplete"


@pytest.mark.parametrize("actual,valid", [(0.004, True), (0.009, False), (0.4, False)])
def test_small_fraction_ratio_has_strict_units_and_precision(actual, valid):
    error = validate_explicit_ratios(
        state(),
        ["毛利额", "销售额", "毛利率", "missing_input_count"],
        [[4, 1000, actual, 0]],
    )
    assert (error is None) is valid


def test_real_inconsistent_sum_population_ratio_is_rejected():
    columns = ["期间", "销售额", "毛利额", "毛利率百分比", "所需输入缺失数"]
    assert validate_explicit_ratios(
        state(), columns, [["2026.01", 68672.56, 19981.979, 81.8, 0]]
    )


@pytest.mark.asyncio
async def test_actual_sql_tool_rejects_inconsistent_metric_and_tracks_delivery_gap():
    engine = create_engine("sqlite://")
    s = state()
    result = json.loads(
        await make_sql_query(s, SimpleNamespace(_engine=engine))(
            "SELECT 68672.56 AS 销售额, 19981.979 AS 毛利额, "
            "81.8 AS 毛利率, 0 AS missing_input_count"
        )
    )
    assert result["success"] is False
    assert missing_deliverables(s)
    assert "query_result_path" not in s
    engine.dispose()


def test_missing_inputs_cannot_look_like_complete_profit():
    s = state()
    columns = ["销售额", "毛利额", "毛利率", "所需输入缺失数"]
    assert validate_explicit_ratios(s, columns, [[100, 20, 0.2, 1]])
    assert missing_deliverables(s)
    assert validate_explicit_ratios(s, columns, [[100, 20, None, 1]]) is None
    assert missing_deliverables(s)
    assert validate_explicit_ratios(s, columns, [[100, 20, 0.2, 0]]) is None
    assert not missing_deliverables(s)


def test_zero_matches_zero_denominator_and_unrelated_query_are_valid():
    assert validate_explicit_ratios(state(), ["期间", "销售额"], []) is None
    assert validate_explicit_ratios(state(), ["销售额"], [[100]]) is None
    assert (
        validate_explicit_ratios(
            state(),
            ["销售额", "毛利额", "毛利率", "missing_input_count"],
            [[0, 0, None, 0]],
        )
        is None
    )


def test_raw_queries_and_partial_ratios_do_not_complete_original_request():
    from dbgpt_app.openapi.api_v1.tools.query_quality import (
        require_explicit_ratio_results,
    )

    s = state()
    require_explicit_ratio_results(s)
    assert missing_deliverables(s)
    assert validate_explicit_ratios(s, ["销售额"], [[100]]) is None
    assert (
        validate_explicit_ratios(
            s,
            ["销售额", "毛利额", "局部毛利率", "missing_input_count"],
            [[100, 20, 0.2, 0]],
        )
        is None
    )
    assert missing_deliverables(s)


def test_python_results_use_same_complete_and_missing_input_contract():
    from dbgpt_app.openapi.api_v1.tools.query_quality import (
        require_explicit_ratio_results,
        validate_python_ratio_output,
    )

    s = state()
    require_explicit_ratio_results(s)
    columns = ["销售额", "毛利额", "毛利率", "missing_input_count"]
    assert validate_python_ratio_output(s, "仅打印局部毛利率20%") is None
    assert missing_deliverables(s)
    assert (
        validate_python_ratio_output(
            s, json.dumps({"columns": columns, "rows": [[100, 20, None, 1]]})
        )
        is None
    )
    assert missing_deliverables(s)
    assert (
        validate_python_ratio_output(
            s, json.dumps({"columns": columns, "rows": [[100, 20, 0.2, 0]]})
        )
        is None
    )
    assert not missing_deliverables(s)


def test_explicit_ratio_zero_matches_can_complete_without_inventing_rows():
    from dbgpt_app.openapi.api_v1.tools.query_quality import (
        require_explicit_ratio_results,
    )

    s = state()
    require_explicit_ratio_results(s)
    assert validate_explicit_ratios(s, ["毛利率"], []) is None
    assert not missing_deliverables(s)


@pytest.mark.asyncio
async def test_real_python_tool_cannot_bypass_ratio_delivery(tmp_path, monkeypatch):
    from dbgpt.configs import model_config
    from dbgpt_app.openapi.api_v1.tools.code_interpreter import make_code_interpreter
    from dbgpt_app.openapi.api_v1.tools.query_quality import (
        require_explicit_ratio_results,
    )

    monkeypatch.setattr(model_config, "PILOT_PATH", str(tmp_path))
    monkeypatch.setattr(model_config, "STATIC_MESSAGE_IMG_PATH", str(tmp_path / "img"))
    s = state()
    require_explicit_ratio_results(s)
    execute = make_code_interpreter(s)
    result = json.loads(await execute("print('局部结果已完成')"))
    assert result["success"]
    assert missing_deliverables(s)
    payload = {
        "columns": ["销售额", "毛利额", "毛利率", "missing_input_count"],
        "rows": [[100, 20, None, 1]],
    }
    script = "import json\nprint(json.dumps(" + repr(payload) + "))"
    assert json.loads(await execute(script))["success"]
    assert missing_deliverables(s)
    payload["rows"] = [[100, 20, 0.2, 0]]
    script = "import json\nprint(json.dumps(" + repr(payload) + "))"
    assert json.loads(await execute(script))["success"]
    assert not missing_deliverables(s)
    known = {"original_user_input": "毛利额20元，销售额100元。毛利率=毛利额/销售额"}
    require_explicit_ratio_results(known)
    known_tool = make_code_interpreter(known)
    assert not json.loads(await known_tool("print('50.00%')"))["success"]
    assert missing_deliverables(known)
    assert json.loads(await known_tool("print('20.00%')"))["success"]
    assert not missing_deliverables(known)


def test_explanation_only_does_not_require_ratio_calculation():
    from dbgpt_app.openapi.api_v1.tools.query_quality import (
        require_explicit_ratio_results,
    )

    s = state()
    s["term_explanation_only"] = True
    require_explicit_ratio_results(s)
    assert not missing_deliverables(s)


@pytest.mark.asyncio
@pytest.mark.parametrize("join_style", ["explicit", "comma"])
async def test_actual_cost_join_fanout_is_rejected_before_results_are_saved(
    tmp_path, monkeypatch, join_style
):
    from sqlalchemy import text
    from sqlalchemy.pool import StaticPool

    from dbgpt.configs import model_config

    monkeypatch.setattr(model_config, "PILOT_PATH", str(tmp_path))
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE ERP销售 (产品编码 TEXT, 销售额 REAL)"))
        conn.execute(text("CREATE TABLE 工费单价 (产品编码 TEXT, 工费 REAL)"))
        conn.execute(text("INSERT INTO ERP销售 VALUES ('a', 100), ('a', 100)"))
        conn.execute(text("INSERT INTO 工费单价 VALUES ('a', 2), ('a', 2)"))
    s = state()
    execute = make_sql_query(s, SimpleNamespace(_engine=engine))
    relation = (
        "ERP销售 e JOIN 工费单价 g ON e.产品编码=g.产品编码"
        if join_style == "explicit"
        else "ERP销售 e, 工费单价 g WHERE e.产品编码=g.产品编码"
    )
    result = json.loads(
        await execute(
            "SELECT e.rowid AS source_sales_row_id, e.销售额, g.工费 FROM " + relation
        )
    )
    assert result["success"] is False
    assert "query_result_path" not in s
    assert missing_deliverables(s)
    corrected = json.loads(
        await execute(
            "SELECT e.rowid AS source_sales_row_id, e.销售额, g.工费 "
            "FROM ERP销售 e JOIN (SELECT DISTINCT 产品编码, 工费 FROM 工费单价) g "
            "ON e.产品编码=g.产品编码 ORDER BY e.rowid"
        )
    )
    assert corrected["success"]
    with open(s["query_result_path"], encoding="utf-8") as saved:
        assert json.load(saved)["rows"] == [[1, 100, 2], [2, 100, 2]]
    assert "sales_cost_join" not in s["query_data_gaps"]
    engine.dispose()


def test_cost_join_validation_preserves_identical_real_sales_and_aggregates():
    from dbgpt_app.openapi.api_v1.tools.query_quality import validate_cost_join

    sql = "SELECT e.rowid AS source_sales_row_id,e.销售额 FROM ERP销售 e JOIN 工费单价 g ON e.产品编码=g.产品编码"
    s = state()
    assert validate_cost_join(s, sql, ["销售额"], [[100], [100]])
    assert (
        validate_cost_join(
            s, sql, ["source_sales_row_id", "销售额"], [[1, 100], [2, 100]]
        )
        is None
    )
    assert not missing_deliverables(s)
    counts = ["source_sales_row_count", "joined_sales_row_count"]
    assert validate_cost_join(s, sql, counts, [[2, 4]])
    assert missing_deliverables(s)
    count_sql = "SELECT (SELECT COUNT(*) FROM ERP销售) AS source_sales_row_count, COUNT(*) AS joined_sales_row_count FROM ERP销售 e JOIN 工费单价 g ON e.产品编码=g.产品编码"
    assert validate_cost_join(s, count_sql, counts, [[2, 2]]) is None
    assert not missing_deliverables(s)
    assert validate_cost_join(s, "SELECT * FROM ERP销售", ["销售额"], [[100]]) is None


@pytest.mark.parametrize("prefix", ["定义", "我定义"])
def test_natural_chinese_formula_introduction_is_not_part_of_metric_name(prefix):
    from dbgpt_app.openapi.api_v1.tools.query_quality import (
        require_explicit_ratio_results,
    )

    s = {"original_user_input": prefix + "毛利率=毛利额/销售额，请计算"}
    require_explicit_ratio_results(s)
    assert set(s["query_data_gaps"]) == {"毛利率"}
    assert (
        validate_explicit_ratios(
            s,
            ["毛利额", "销售额", "毛利率", "missing_input_count"],
            [[20, 100, 0.2, 0]],
        )
        is None
    )
    assert not missing_deliverables(s)


@pytest.mark.parametrize("output", ["20.00%", "0.2"])
def test_known_user_inputs_allow_verified_plain_python_output(output):
    from dbgpt_app.openapi.api_v1.tools.query_quality import (
        require_explicit_ratio_results,
        validate_python_ratio_output,
    )

    s = {"original_user_input": "毛利额20元，销售额100元。定义毛利率=毛利额/销售额"}
    require_explicit_ratio_results(s)
    assert validate_python_ratio_output(s, output) is None
    assert not missing_deliverables(s)


def test_incorrect_plain_output_and_queried_inputs_stay_unverified():
    from dbgpt_app.openapi.api_v1.tools.query_quality import (
        require_explicit_ratio_results,
        validate_python_ratio_output,
    )

    s = {"original_user_input": "毛利额20元，销售额100元。毛利率=毛利额/销售额"}
    require_explicit_ratio_results(s)
    assert validate_python_ratio_output(s, "50.00%")
    assert missing_deliverables(s)
    s = state()
    require_explicit_ratio_results(s)
    assert validate_python_ratio_output(s, "20.00%") is None
    assert missing_deliverables(s)


@pytest.mark.parametrize("missing", [0, 1])
def test_python_replacement_can_clear_join_gap_without_clearing_missing_inputs(missing):
    from dbgpt_app.openapi.api_v1.tools.query_quality import (
        require_explicit_ratio_results,
        validate_python_ratio_output,
    )

    s = state()
    require_explicit_ratio_results(s)
    s["query_data_gaps"]["sales_cost_join"] = "先前关联尚未通过校验"
    s["validated_results"] = {
        "source_sales": {
            "columns": ["source_sales_row_id"],
            "rows": [[1], [2], [3]],
            "sales_source": True,
            "conv_id": None,
            "database": "",
        }
    }
    payload = {
        "columns": [
            "毛利额",
            "销售额",
            "毛利率",
            "missing_input_count",
            "source_sales_row_count",
            "joined_sales_row_count",
        ],
        "rows": [
            [None if missing else 20, 100, None if missing else 0.2, missing, 3, 3]
        ],
        "source_result_ids": ["source_sales"],
        "joined_sales_row_ids": [1, 2, 3],
    }
    assert validate_python_ratio_output(s, json.dumps(payload)) is None
    assert "sales_cost_join" not in s["query_data_gaps"]
    assert bool(missing_deliverables(s)) is bool(missing)


def test_python_replacement_cannot_hide_fanout_or_omit_identity_check():
    from dbgpt_app.openapi.api_v1.tools.query_quality import (
        ratio_delivery_hint,
        require_explicit_ratio_results,
        validate_python_ratio_output,
    )

    s = state()
    require_explicit_ratio_results(s)
    s["query_data_gaps"]["sales_cost_join"] = "先前关联尚未通过校验"
    payload = {
        "columns": ["毛利额", "销售额", "毛利率", "missing_input_count"],
        "rows": [[20, 100, 0.2, 0]],
    }
    assert validate_python_ratio_output(s, json.dumps(payload)) is None
    assert missing_deliverables(s)
    assert "source_sales_row_count" in ratio_delivery_hint(s)
    payload["columns"] += ["source_sales_row_count", "joined_sales_row_count"]
    payload["rows"][0] += [3, 4]
    assert validate_python_ratio_output(s, json.dumps(payload))
    assert missing_deliverables(s)


def test_verified_missing_inputs_do_not_request_repeated_format_validation():
    from dbgpt_app.openapi.api_v1.tools.query_quality import ratio_delivery_hint

    s = state()
    assert (
        validate_explicit_ratios(
            s,
            ["毛利额", "销售额", "毛利率", "missing_input_count"],
            [[None, 100, None, 1]],
        )
        is None
    )
    assert s["ratio_validation_status"]["毛利率"] == "missing_inputs"
    assert "JSON" not in ratio_delivery_hint(s)
    assert missing_deliverables(s)


@pytest.mark.parametrize(
    "projection",
    [
        "period, product_code, unit_price",
        "period, product_code, COUNT(*) AS cnt",
    ],
)
def test_separate_cost_query_with_sales_filter_is_not_a_sales_join(projection):
    from dbgpt_app.openapi.api_v1.tools.query_quality import validate_cost_join

    s = state()
    s["query_data_gaps"] = {"sales_cost_join": "prior join still unverified"}
    sql = (
        f'SELECT {projection} FROM "材料单价" '
        "WHERE product_code IN (SELECT DISTINCT product_code "
        "FROM \"ERP销售\" WHERE customer_name = 'customer')"
    )
    assert validate_cost_join(s, sql, ["product_code"], [["P1"]]) is None
    assert s["query_data_gaps"]["sales_cost_join"] == "prior join still unverified"


@pytest.mark.parametrize(
    "relation",
    [
        "ERP销售 e, 工费单价 g WHERE e.产品编码=g.产品编码",
        "(SELECT rowid, * FROM ERP销售) e JOIN 工费单价 g ON e.产品编码=g.产品编码",
    ],
)
def test_supported_join_spellings_reject_same_sales_fanout(relation):
    from dbgpt_app.openapi.api_v1.tools.query_quality import validate_cost_join

    assert validate_cost_join(
        state(),
        f"SELECT e.rowid AS source_sales_row_id FROM {relation}",
        ["source_sales_row_id"],
        [[1], [1]],
    )


def test_cte_join_is_checked_and_independent_cost_count_does_not_make_outer_aggregate():
    from dbgpt_app.openapi.api_v1.tools.query_quality import validate_cost_join

    assert validate_cost_join(
        state(),
        "WITH sales AS (SELECT rowid, * FROM ERP销售), costs AS (SELECT * FROM 工费单价) "
        "SELECT s.rowid AS source_sales_row_id FROM sales s, costs c WHERE s.产品编码=c.产品编码",
        ["source_sales_row_id"],
        [[1], [1]],
    )
    assert (
        validate_cost_join(
            state(),
            "SELECT e.rowid AS source_sales_row_id FROM ERP销售 e "
            "JOIN (SELECT 产品编码, COUNT(*) AS cnt FROM 工费单价 GROUP BY 产品编码) g "
            "ON e.产品编码=g.产品编码",
            ["source_sales_row_id"],
            [[1], [2]],
        )
        is None
    )


def test_joined_counts_cannot_both_claim_to_be_original_count():
    from dbgpt_app.openapi.api_v1.tools.query_quality import validate_cost_join

    sql = "SELECT COUNT(*) AS source_sales_row_count, COUNT(*) AS joined_sales_row_count FROM ERP销售 e JOIN 工费单价 g ON e.产品编码=g.产品编码"
    assert validate_cost_join(
        state(), sql, ["source_sales_row_count", "joined_sales_row_count"], [[2, 2]]
    )


def test_python_printed_equal_constants_are_not_join_provenance():
    from dbgpt_app.openapi.api_v1.tools.query_quality import (
        validate_python_ratio_output,
    )

    s = state()
    s["query_data_gaps"] = {"sales_cost_join": "unverified"}
    assert validate_python_ratio_output(
        s,
        json.dumps(
            {
                "columns": ["source_sales_row_count", "joined_sales_row_count"],
                "rows": [[3, 3]],
            }
        ),
    )


@pytest.mark.parametrize(
    "num,den,actual,unit,valid",
    [
        (20, 100, 0.2, "fraction", True),
        (20, 100, 20, "percent", True),
        (20, 100, 20, "fraction", False),
        (-4, 1000, -0.004, "fraction", True),
        (1e-9, 1000, 1e-12, "fraction", True),
        (0.1 + 0.2, 3, 0.1, "fraction", True),
        (20, 100, float("nan"), "fraction", False),
    ],
)
def test_ratio_units_handle_real_numeric_edges(num, den, actual, unit, valid):
    s = state()
    s["task_requirements"]["ratios"][0]["unit"] = unit
    error = validate_explicit_ratios(
        s,
        ["毛利额", "销售额", "毛利率", "missing_input_count"],
        [[num, den, actual, 0]],
    )
    assert (error is None) is valid


def test_display_rounding_uses_actual_decimal_places_only_for_percent_display():
    from dbgpt_app.openapi.api_v1.tools.query_quality import (
        validate_python_ratio_output,
    )

    s = {"original_user_input": "毛利额1元，销售额3元。毛利率=毛利额/销售额"}
    assert validate_python_ratio_output(s, "33.33%") is None
    assert validate_python_ratio_output(s, "33.39%")
    assert validate_python_ratio_output(s, "0.3333")


def test_null_with_zero_missing_count_cannot_pass_input_validation():
    assert validate_explicit_ratios(
        state(),
        ["毛利额", "销售额", "毛利率", "missing_input_count"],
        [[None, 100, None, 0]],
    )


def test_python_cost_computation_requires_join_evidence_without_prior_failed_join():
    from dbgpt_app.openapi.api_v1.tools.query_quality import (
        validate_python_ratio_output,
    )

    s = state()
    s["sales_source_read"] = True
    s["cost_source_read"] = True
    assert validate_python_ratio_output(
        s,
        json.dumps(
            {
                "columns": ["毛利额", "销售额", "毛利率", "missing_input_count"],
                "rows": [[20, 100, 0.2, 0]],
            }
        ),
    )


def test_cte_grouped_original_and_joined_counts_have_distinct_real_sources():
    from dbgpt_app.openapi.api_v1.tools.query_quality import validate_cost_join

    sql = """WITH base AS (SELECT rowid,* FROM ERP销售),
    costs AS (SELECT DISTINCT 产品编码,工费 FROM 工费单价),
    joined AS (SELECT e.rowid,e.产品编码 FROM base e JOIN costs g ON e.产品编码=g.产品编码),
    originals AS (SELECT 产品编码,COUNT(*) AS cnt FROM base GROUP BY 产品编码)
    SELECT j.产品编码,o.cnt AS source_sales_row_count,COUNT(*) AS joined_sales_row_count
    FROM joined j JOIN originals o ON j.产品编码=o.产品编码 GROUP BY j.产品编码"""
    assert (
        validate_cost_join(
            state(),
            sql,
            ["产品编码", "source_sales_row_count", "joined_sales_row_count"],
            [["A", 2, 2]],
        )
        is None
    )


def test_full_null_and_complete_subset_ratio_have_separate_input_scopes():
    s = state()
    s["task_requirements"]["ratios"][0]["unavailable_policy"] = "explain"
    s["task_requirements"]["ratios"].append(
        {
            "metric": "局部毛利率",
            "numerator": "局部毛利额",
            "denominator": "局部销售额",
            "unit": "fraction",
            "unavailable_policy": "explain",
        }
    )
    assert (
        validate_explicit_ratios(
            s,
            [
                "毛利额",
                "销售额",
                "毛利率",
                "局部毛利额",
                "局部销售额",
                "局部毛利率",
                "missing_input_count",
                "局部毛利率_missing_input_count",
            ],
            [[None, 100, None, 20, 50, 0.4, 1, 0]],
        )
        is None
    )
    assert not missing_deliverables(s)
    assert set(s["data_warnings"]) == {"毛利率"}


def test_cost_row_identity_cannot_be_presented_as_sales_identity():
    from dbgpt_app.openapi.api_v1.tools.query_quality import validate_cost_join

    sql = "SELECT g.rowid AS source_sales_row_id FROM ERP销售 e JOIN 工费单价 g ON e.产品编码=g.产品编码"
    assert validate_cost_join(state(), sql, ["source_sales_row_id"], [[1], [2]])


def test_distinct_products_do_not_prove_sales_row_counts():
    from dbgpt_app.openapi.api_v1.tools.query_quality import validate_cost_join

    sql = "SELECT (SELECT COUNT(DISTINCT 产品编码) FROM ERP销售) AS source_sales_row_count, COUNT(DISTINCT e.产品编码) AS joined_sales_row_count FROM ERP销售 e JOIN 工费单价 g ON e.产品编码=g.产品编码"
    assert validate_cost_join(
        state(), sql, ["source_sales_row_count", "joined_sales_row_count"], [[1, 1]]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("confirmed", [False, True])
async def test_averaging_conflicting_cost_prices_does_not_hide_conflict(
    tmp_path, monkeypatch, confirmed
):
    from sqlalchemy import text
    from sqlalchemy.pool import StaticPool
    from dbgpt.configs import model_config

    monkeypatch.setattr(model_config, "PILOT_PATH", str(tmp_path))
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE ERP销售 (产品编码 TEXT, 销售额 REAL)"))
        connection.execute(text("CREATE TABLE 工费单价 (产品编码 TEXT, 工费 REAL)"))
        connection.execute(text("INSERT INTO ERP销售 VALUES ('A',100)"))
        connection.execute(text("INSERT INTO 工费单价 VALUES ('A',2),('A',4)"))
    s = state()
    if confirmed:
        s["confirmed_terms"] = [
            {
                "term": "工费",
                "datasource": "",
                "formula": "同产品工费取算术平均",
                "formula_confirmed": True,
            }
        ]
    result = json.loads(
        await make_sql_query(s, SimpleNamespace(_engine=engine))(
            "SELECT e.rowid AS source_sales_row_id,e.销售额,g.工费 FROM ERP销售 e "
            "JOIN (SELECT 产品编码,AVG(工费) AS 工费 FROM 工费单价 GROUP BY 产品编码) g "
            "ON e.产品编码=g.产品编码"
        )
    )
    assert result["success"] is confirmed
    if not confirmed:
        assert "价格冲突" in result["error"]
        assert "2" in result["error"] and "4" in result["error"]
    engine.dispose()


@pytest.mark.asyncio
async def test_aggregate_price_check_uses_original_sales_scope_only(
    tmp_path, monkeypatch
):
    from sqlalchemy import text
    from sqlalchemy.pool import StaticPool
    from dbgpt.configs import model_config

    monkeypatch.setattr(model_config, "PILOT_PATH", str(tmp_path))
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE ERP销售 (产品编码 TEXT, 销售额 REAL)"))
        connection.execute(text("CREATE TABLE 工费单价 (产品编码 TEXT, 工费 REAL)"))
        connection.execute(text("INSERT INTO ERP销售 VALUES ('A',100),('B',200)"))
        connection.execute(text("INSERT INTO 工费单价 VALUES ('A',2),('B',4),('B',6)"))
    tool = make_sql_query(state(), SimpleNamespace(_engine=engine))

    def query(product):
        return (
            "WITH base AS (SELECT * FROM ERP销售 WHERE 产品编码='" + product + "'), "
            "g AS (SELECT 产品编码,AVG(工费) AS 工费 FROM 工费单价 GROUP BY 产品编码) "
            "SELECT (SELECT COUNT(*) FROM base) AS source_sales_row_count,COUNT(*) AS joined_sales_row_count "
            "FROM base e JOIN g ON e.产品编码=g.产品编码"
        )

    assert json.loads(await tool(query("A")))["success"]
    conflicting = json.loads(await tool(query("B")))
    assert not conflicting["success"] and "价格冲突" in conflicting["error"]
    engine.dispose()


@pytest.mark.asyncio
async def test_python_join_rejects_conflict_hidden_in_cost_summary(
    tmp_path, monkeypatch
):
    from sqlalchemy import text
    from sqlalchemy.pool import StaticPool
    from dbgpt.configs import model_config
    from dbgpt_app.openapi.api_v1.tools.query_quality import (
        validate_python_ratio_output,
    )

    monkeypatch.setattr(model_config, "PILOT_PATH", str(tmp_path))
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE ERP销售 (产品编码 TEXT, 销售额 REAL)"))
        connection.execute(text("CREATE TABLE 工费单价 (产品编码 TEXT, 工费 REAL)"))
        connection.execute(text("INSERT INTO ERP销售 VALUES ('A',100)"))
        connection.execute(text("INSERT INTO 工费单价 VALUES ('A',2),('A',4)"))
    s = state()
    query = make_sql_query(s, SimpleNamespace(_engine=engine))
    sales = json.loads(
        await query("SELECT rowid AS source_sales_row_id,产品编码,销售额 FROM ERP销售")
    )
    costs = json.loads(
        await query("SELECT 产品编码,AVG(工费) AS 工费 FROM 工费单价 GROUP BY 产品编码")
    )
    assert sales["success"] and costs["success"]
    error = validate_python_ratio_output(
        s,
        json.dumps(
            {
                "columns": [
                    "毛利额",
                    "销售额",
                    "毛利率",
                    "missing_input_count",
                    "source_sales_row_count",
                    "joined_sales_row_count",
                ],
                "rows": [[20, 100, 0.2, 0, 1, 1]],
                "source_result_ids": [sales["result_id"], costs["result_id"]],
                "joined_sales_row_ids": [1],
            }
        ),
    )
    assert error and "价格冲突" in error
    engine.dispose()
