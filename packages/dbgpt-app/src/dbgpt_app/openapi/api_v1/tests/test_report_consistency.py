"""Reports consume verified tool data, never a second copy of model numbers."""

import json

import pytest

from dbgpt_app.openapi.api_v1.tools.html_interpreter import make_html_interpreter


def financial_state():
    return {
        "conv_id": "report-test",
        "term_datasource": "monthly",
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


def test_report_recursively_rejects_same_session_source_from_old_scope():
    from dbgpt_app.openapi.api_v1.tools.report_rendering import (
        record_validated_result,
        render_results,
    )

    s = financial_state()
    old_scope = {
        "database": {"name": "monthly", "type": "sqlite"},
        "customer_codes": ["A"],
        "periods": ["2026.01"],
    }
    s["task_requirements"]["analysis_scope"] = old_scope
    source = record_validated_result(
        s, ["source_sales_row_id", "期间"], [[1, "2026.01"]], source="sql"
    )
    summary = record_validated_result(
        s,
        ["毛利额", "销售额", "毛利率", "missing_input_count"],
        [[2, 10, 0.2, 0]],
        source="python",
        source_result_ids=[source],
    )
    s["task_requirements"]["analysis_scope"] = {**old_scope, "customer_codes": ["B"]}
    with pytest.raises(ValueError):
        render_results(s, [summary], "报告")
    summary = record_validated_result(
        s, ["毛利额", "销售额", "毛利率", "missing_input_count"],
        [[2, 10, 0.2, 0]], source="python", source_result_ids=[source],
    )
    with pytest.raises(ValueError, match="源结果"):
        render_results(s, [summary], "报告")


@pytest.mark.asyncio
async def test_python_receives_actual_files_for_multiple_query_sources(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from sqlalchemy import create_engine, text
    from sqlalchemy.pool import StaticPool
    from dbgpt.configs import model_config
    from dbgpt_app.openapi.api_v1.tools.sql_query import make_sql_query
    from dbgpt_app.openapi.api_v1.tools.code_interpreter import make_code_interpreter
    monkeypatch.setattr(model_config, "PILOT_PATH", str(tmp_path))
    monkeypatch.setattr(model_config, "STATIC_MESSAGE_IMG_PATH", str(tmp_path / "img"))
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE ERP销售 (产品编码 TEXT,销售金额 REAL)"))
        connection.execute(text("CREATE TABLE 材料单价 (产品编码 TEXT,材料成本单价 REAL)"))
        connection.execute(text("INSERT INTO ERP销售 VALUES ('p',100)"))
        connection.execute(text("INSERT INTO 材料单价 VALUES ('p',10)"))
    s = {"conv_id": "multi-files", "term_datasource": "sales"}
    query = make_sql_query(s, SimpleNamespace(_engine=engine))
    sales = json.loads(await query("SELECT rowid AS source_sales_row_id,* FROM ERP销售"))
    cost = json.loads(await query("SELECT * FROM 材料单价"))
    assert sales["success"] and cost["success"]
    code = "sales=json.load(open(QUERY_RESULTS[" + repr(sales["result_id"]) + "][\"path\"],encoding='utf-8'))\n"
    code += "cost=json.load(open(QUERY_RESULTS[" + repr(cost["result_id"]) + "][\"path\"],encoding='utf-8'))\n"
    code += "print(json.dumps({'columns':['check'],'rows':[[sales['rows'][0][2]+cost['rows'][0][1]]]}))"
    result = json.loads(await make_code_interpreter(s)(code))
    assert result["success"]
    assert any(snapshot["rows"] == [[110]] for snapshot in s["validated_results"].values())
    engine.dispose()


@pytest.mark.asyncio
async def test_free_html_cannot_replace_verified_financial_facts(tmp_path):
    result = json.loads(
        await make_html_interpreter(financial_state(), str(tmp_path))(
            html="<html><body>成本完整行数2/15/30，4行2产品，行业正常水平</body></html>"
        )
    )
    assert result["success"] is False
    assert "result_ids" in result["error"]


def test_result_snapshots_render_real_counts_and_reject_unknown_ids():
    from dbgpt_app.openapi.api_v1.tools.report_rendering import (
        record_validated_result,
        render_results,
    )

    s = financial_state()
    rid = record_validated_result(
        s,
        ["期间", "毛利额", "销售额", "毛利率", "成本完整行数", "missing_input_count"],
        [
            ["2026.01", 2, 10, 0.2, 2, 0],
            ["2026.06", 4, 10, 0.4, 16, 0],
            ["2026.07", 3, 10, 0.3, 29, 0],
        ],
        source="sql",
        result_id="query_real",
    )
    html, summary = render_results(s, [rid], "报告")
    assert "<td>16</td>" in html and "<td>29</td>" in html
    assert "20.00%" in html and "行业正常" not in html
    assert summary
    with pytest.raises(ValueError):
        render_results(s, ["other_task_result"], "报告")


def test_missing_statistics_use_sales_identity_and_distinct_products():
    from dbgpt_app.openapi.api_v1.tools.report_rendering import (
        record_validated_result,
        render_results,
    )

    s = financial_state()
    detail = record_validated_result(
        s,
        ["source_sales_row_id", "期间", "产品编码", "missing_input_count"],
        [
            [1, "2026.01", "A", 1],
            [2, "2026.06", "A", 1],
            [3, "2026.06", "B", 1],
            [4, "2026.07", "B", 1],
        ],
        source="sql",
    )
    summary = record_validated_result(
        s,
        ["期间", "毛利额", "销售额", "毛利率", "局部毛利率", "missing_input_count"],
        [
            ["2026.01", None, 100, None, 0.2, 1],
            ["2026.06", None, 200, None, 0.3, 2],
            ["2026.07", None, 300, None, 0.4, 1],
        ],
        source="sql",
    )
    html, text = render_results(s, [detail, summary], "报告")
    assert "4行" in text and "2个产品" in text
    assert "NULL" in html and "20.00%" in html
    s["validated_results"][summary]["rows"][1][-1] = 3
    with pytest.raises(ValueError, match="缺失"):
        render_results(s, [detail, summary], "报告")


def test_raw_verified_sales_cost_rows_supply_missing_details_without_retyping_flags():
    from dbgpt_app.openapi.api_v1.tools.report_rendering import (
        record_validated_result,
        render_results,
    )

    s = financial_state()
    detail = record_validated_result(
        s,
        [
            "source_sales_row_id",
            "期间",
            "产品编码",
            "材料成本单价",
            "工费",
            "销售数量",
            "销售金额",
        ],
        [[1, "2026.01", "A", 3, None, 2, 10]],
        source="sql",
    )
    summary = record_validated_result(
        s,
        ["期间", "毛利额", "销售额", "毛利率", "missing_input_count"],
        [["2026.01", None, 10, None, 1]],
        source="sql",
    )
    html, text = render_results(s, [detail, summary], "报告")
    assert "1行" in text


@pytest.mark.asyncio
async def test_python_database_numbers_without_source_are_not_verified(
    tmp_path, monkeypatch
):
    from dbgpt.configs import model_config
    from dbgpt_app.openapi.api_v1.tools.code_interpreter import make_code_interpreter
    from dbgpt_app.openapi.api_v1.tools.query_quality import (
        require_explicit_ratio_results,
    )

    monkeypatch.setattr(model_config, "PILOT_PATH", str(tmp_path))
    monkeypatch.setattr(
        model_config, "STATIC_MESSAGE_IMG_PATH", str(tmp_path / "images")
    )
    s = financial_state()
    require_explicit_ratio_results(s)
    payload = {
        "columns": ["毛利额", "销售额", "毛利率", "missing_input_count"],
        "rows": [[20, 100, 0.2, 0]],
    }
    result = json.loads(
        await make_code_interpreter(s)(
            "import json\nprint(json.dumps(" + repr(payload) + "))"
        )
    )
    assert not result["success"]
    assert not s.get("validated_results")
    assert s["query_data_gaps"]


def test_report_checks_python_counts_against_linked_source_rows():
    from dbgpt_app.openapi.api_v1.tools.report_rendering import (
        record_validated_result,
        render_results,
    )

    s = financial_state()
    source = record_validated_result(
        s,
        ["source_sales_row_id", "期间", "产品编码", "missing_input_count"],
        [[1, "2026.06", "A", 0], [2, "2026.06", "B", 0], [3, "2026.06", "C", 1]],
        source="sql",
    )
    result = record_validated_result(
        s,
        [
            "期间",
            "毛利额",
            "销售额",
            "毛利率",
            "成本完整行数",
            "missing_input_count",
            "source_sales_row_count",
        ],
        [["2026.06", None, 100, None, 1, 1, 3]],
        source="python",
        source_result_ids=[source],
    )
    with pytest.raises(ValueError, match="完整行数"):
        render_results(s, [result], "报告")


@pytest.mark.asyncio
async def test_python_failure_feedback_exposes_source_column_mapping(tmp_path, monkeypatch):
    from dbgpt.configs import model_config
    from dbgpt_app.openapi.api_v1.tools.code_interpreter import make_code_interpreter

    monkeypatch.setattr(model_config, "PILOT_PATH", str(tmp_path))
    monkeypatch.setattr(model_config, "STATIC_MESSAGE_IMG_PATH", str(tmp_path / "images"))
    s = {"query_result_path": str(tmp_path / "query_input.json"), "validated_results": {
        "query_input": {"columns": ["source_sales_row_id", "客户类型", "销售数量", "销售金额"]}}}
    result = json.loads(await make_code_interpreter(s)("value='外部客户'\nprint(value*2.5)"))
    assert not result["success"]
    feedback = "\n".join(c.get("content", "") for c in result["chunks"] if c["output_type"] == "text")
    assert "dict(zip" in feedback and "销售数量" in feedback


def require_value_state():
    return {
        "conv_id": "require-value-report",
        "term_datasource": "monthly",
        "task_requirements": {
            "ratios": [
                {
                    "metric": "毛利率",
                    "numerator": "毛利额",
                    "denominator": "销售额",
                    "unit": "fraction",
                    "unavailable_policy": "require_value",
                    "population": "full",
                }
            ]
        },
    }


def missing_input_warning(reason, message):
    return {
        "毛利率": {
            "metric": "毛利率",
            "reason": reason,
            "message": message,
            "affected_rows": 1,
        }
    }


def test_require_value_missing_input_report_states_incomplete_full_value_gap():
    from dbgpt_app.openapi.api_v1.tools.report_rendering import (
        record_validated_result,
        render_results,
    )

    s = require_value_state()
    detail = record_validated_result(
        s,
        [
            "source_sales_row_id",
            "期间",
            "产品编码",
            "材料成本单价",
            "工费",
            "销售数量",
            "销售金额",
        ],
        [[1, "2026.01", "A", 3, None, 2, 10]],
        source="sql",
    )
    s["data_warnings"] = missing_input_warning(
        "missing_input", "毛利率：所需输入缺失，完整比率为NULL，影响1行。"
    )
    summary = record_validated_result(
        s,
        ["期间", "毛利额", "销售额", "毛利率", "missing_input_count"],
        [["2026.01", None, 10, None, 1]],
        source="python",
        source_result_ids=[detail],
    )
    html, text = render_results(s, [detail, summary], "报告")
    reason = "因缺工费无法满足所要求的全量毛利额和毛利率完整数值要求"
    assert "未完成" in text and "未完成" in html
    assert reason in text and reason in html


def test_zero_denominator_require_value_report_does_not_claim_missing_inputs():
    from dbgpt_app.openapi.api_v1.tools.query_quality import validate_explicit_ratios
    from dbgpt_app.openapi.api_v1.tools.report_rendering import (
        record_validated_result,
        render_results,
    )

    s = require_value_state()
    columns = ["期间", "毛利额", "销售额", "毛利率", "missing_input_count"]
    rows = [["2026.01", 0, 0, None, 0]]
    assert validate_explicit_ratios(s, columns, rows) is None
    assert s["data_warnings"]["毛利率"]["reason"] == "zero_denominator"
    rid = record_validated_result(s, columns, rows, source="sql")
    html, text = render_results(s, [rid], "报告")
    assert "未完成" in text and "未完成" in html
    reason = "所要求的全量毛利率完整数值在本范围不适用：分母为0。"
    assert reason in text and reason in html
    assert "毛利额和毛利率完整数值在本范围不适用" not in text
    assert "因缺" not in text and "缺工费" not in text


def test_explain_policy_missing_input_report_stays_complete_in_text():
    from dbgpt_app.openapi.api_v1.tools.report_rendering import (
        record_validated_result,
        render_results,
    )

    s = financial_state()
    detail = record_validated_result(
        s,
        [
            "source_sales_row_id",
            "期间",
            "产品编码",
            "材料成本单价",
            "工费",
            "销售数量",
            "销售金额",
        ],
        [[1, "2026.01", "A", 3, None, 2, 10]],
        source="sql",
    )
    s["data_warnings"] = missing_input_warning(
        "missing_input", "毛利率：所需输入缺失，完整比率为NULL，影响1行。"
    )
    summary = record_validated_result(
        s,
        ["期间", "毛利额", "销售额", "毛利率", "missing_input_count"],
        [["2026.01", None, 10, None, 1]],
        source="python",
        source_result_ids=[detail],
    )
    html, text = render_results(s, [detail, summary], "报告")
    assert "缺少工费" in text
    assert "未完成" not in text and "未完成" not in html
    assert "因缺" not in text


def test_computable_require_value_report_is_not_marked_incomplete():
    from dbgpt_app.openapi.api_v1.tools.report_rendering import (
        record_validated_result,
        render_results,
    )

    s = require_value_state()
    rid = record_validated_result(
        s,
        ["毛利额", "销售额", "毛利率", "missing_input_count"],
        [[2, 10, 0.2, 0]],
        source="sql",
    )
    html, text = render_results(s, [rid], "报告")
    assert "20.00%" in html
    assert "未完成" not in text and "未完成" not in html
