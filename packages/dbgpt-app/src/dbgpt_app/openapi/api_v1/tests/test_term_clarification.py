"""Verify current-model suggestions, blocking and shared persistence."""

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, text

from dbgpt.agent.resource.tool.base import tool
from dbgpt.agent.resource.tool.pack import ToolPack
from dbgpt_app.openapi.api_v1.terms.service import (
    MODEL_CHOICE,
    TermClarifier,
    contains_term,
    explanation_only_request,
    install_execution_guards,
    record_term_check_failure,
    term_scope_description,
)
from dbgpt_app.openapi.api_v1.tools.question import make_question
from dbgpt_app.openapi.api_v1.tools.question_manager import (
    QuestionManager,
    question_manager,
)
from dbgpt_app.openapi.api_v1.tools.sql_query import make_sql_query
from dbgpt_app.openapi.api_v1.react_final import TaskResult
from dbgpt_app.openapi.api_v1.react_task import incomplete_summary


def test_complete_cost_subset_cannot_be_redefined_as_material_only():
    from dbgpt_app.openapi.api_v1.terms.service import validate_request_scope

    request = "分析成本完整子集毛利率"
    response = {
        "terms": [],
        "request_scope": {
            "action": "new",
            "scope_evidence": request,
            "requirements": {
                "ratios": [
                    {
                        "metric": "子集毛利率（仅材料成本）",
                        "numerator": "子集毛利额",
                        "denominator": "子集销售额",
                        "unit": "fraction",
                        "unavailable_policy": "explain",
                    }
                ]
            },
        },
    }
    _, _, requirements = validate_request_scope(response, request, {})
    ratio = requirements["ratios"][0]
    assert "仅材料" not in ratio["metric"]
    assert ratio["population"] == "complete_inputs"
    assert ratio["required_inputs"] == ["材料成本单价", "工费", "销售数量", "销售金额"]


@pytest.mark.asyncio
async def test_analysis_scope_resolves_evidenced_name_months_and_preserves_other_conditions():
    from sqlalchemy.pool import StaticPool
    from dbgpt_app.openapi.api_v1.tools.sql_query import (
        resolve_analysis_scope,
        AnalysisScopeChoice,
    )

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    with engine.begin() as connection:
        connection.execute(
            text("CREATE TABLE ERP销售 (客户编码 TEXT,客户名称 TEXT,期间 TEXT)")
        )
        connection.execute(
            text(
                "INSERT INTO ERP销售 VALUES ('A','武汉钧恒科技有限公司','2026.01'),('B','武汉钧恒分公司','2026.06'),('C','含%_字符公司','2026.07')"
            )
        )
    connector = SimpleNamespace(_engine=engine)
    database = {"name": "sales", "type": "sqlite"}
    requirements = {}
    requirements["analysis_scope"] = {"period_candidates": [
        {"value": "2026.01", "evidence": "2026年1月"},
        {"value": "2026.06", "evidence": "2026年6月"},
        {"value": "2026.07", "evidence": "7月"},
    ]}
    await resolve_analysis_scope(
        connector, requirements, "分析武汉钧恒科技有限公司2026年1月、6月、7月", database
    )
    original = requirements["analysis_scope"]
    assert original["customer_codes"] == ["A"]
    assert original["periods"] == ["2026.01", "2026.06", "2026.07"]
    invented = {"analysis_scope": {"period_candidates": [{"value": "2025.12", "evidence": "整段需求"}]}}
    await resolve_analysis_scope(connector, invented, "分析武汉钧恒科技有限公司2026年1月、6月、7月", database)
    assert invented["analysis_scope"]["periods"] == ["2026.01", "2026.06", "2026.07"]
    assert "2025.12" not in invented["analysis_scope"]["periods"]
    for request in ("继续原需求", "只改报告排版"):
        current = {}
        await resolve_analysis_scope(connector, current, request, database, original)
        assert current["analysis_scope"] == original
    current = {}
    await resolve_analysis_scope(connector, current, "改为2026.06", database, original)
    assert current["analysis_scope"]["customer_codes"] == ["A"]
    assert current["analysis_scope"]["periods"] == ["2026.06"]
    await resolve_analysis_scope(
        connector, current, "改为所有客户", database, current["analysis_scope"]
    )
    assert current["analysis_scope"]["customer_codes"] == []
    assert current["analysis_scope"]["periods"] == ["2026.06"]
    candidate = {
        "analysis_scope": {
            "customer_candidates": [{"value": "武汉钧恒", "evidence": "武汉钧恒"}]
        }
    }
    with pytest.raises(AnalysisScopeChoice):
        await resolve_analysis_scope(connector, candidate, "分析武汉钧恒", database)
    questions = []

    async def select_customer(kind, payload):
        if kind == "question.asked":
            questions.append(payload)
            question_manager.reply(payload["request_id"], [["B"]], "scope-choice")

    clarifier = TermClarifier(
        {"conv_id": "scope-choice"},
        select_customer,
        empty_llm,
        store=MemoryStore(),
        database_connector=connector,
    )
    await clarifier.resolve_scope(candidate, "分析武汉钧恒2026.06", database)
    assert len(questions) == 1
    assert candidate["analysis_scope"]["customer_codes"] == ["B"]
    assert candidate["analysis_scope"]["periods"] == ["2026.06"]
    candidate = {
        "analysis_scope": {"customer_candidates": [{"value": "%_", "evidence": "%_"}]}
    }
    await resolve_analysis_scope(connector, candidate, "分析%_", database)
    assert candidate["analysis_scope"]["customer_codes"] == ["C"]
    with pytest.raises(ValueError, match="没有匹配"):
        await resolve_analysis_scope(connector, {}, "分析无匹配公司", database)
    no_months = {}
    await resolve_analysis_scope(connector, no_months, "[Database: sqlite_2026.01] 分析所有客户的销售", database)
    assert no_months["analysis_scope"]["periods"] == []
    monthly = {}
    await resolve_analysis_scope(connector, monthly, "按月分析武汉钧恒科技有限公司2026年1月、6月、7月", database)
    assert monthly["analysis_scope"]["group_by"] == ["期间"]
    continued = {}
    await resolve_analysis_scope(connector, continued, "继续原需求，只改排版", database, monthly["analysis_scope"])
    assert continued["analysis_scope"]["group_by"] == ["期间"]
    relative = {}
    await resolve_analysis_scope(connector, relative, "改为6月、7月", database, monthly["analysis_scope"])
    assert relative["analysis_scope"]["periods"] == ["2026.06", "2026.07"]
    with pytest.raises(AnalysisScopeChoice):
        await resolve_analysis_scope(connector, {}, "按月分析武汉钧恒科技有限公司6月、7月", database)
    engine.dispose()


def test_layout_continuation_cannot_be_reset_by_model_new_action():
    from dbgpt_app.openapi.api_v1.terms.service import validate_request_scope
    previous = {"requires_report": True, "ratios": [{"metric": "毛利率", "numerator": "毛利额", "denominator": "销售额", "unit": "fraction", "unavailable_policy": "explain"}],
                "analysis_scope": {"database": {"name": "sales", "type": "sqlite"}, "customer_codes": ["A"], "periods": ["2026.01"]}}
    request = "继续原需求，只改报告排版"
    raw = {"terms": [], "request_scope": {"action": "new", "scope_evidence": request,
           "requirements": {"requires_report": False, "ratios": []}}}
    _, action, requirements = validate_request_scope(raw, request, previous)
    assert action == "continue"
    assert requirements["requires_report"] is True
    assert requirements["ratios"][0]["metric"] == "毛利率"
    assert requirements["analysis_scope"] == previous["analysis_scope"]


@pytest.mark.asyncio
async def test_adopted_human_formula_is_not_overwritten_by_model_requirements():
    adopted = {"term": "ROI", "definition": "投资回报率", "formula": "净收益/成本", "formula_confirmed": True,
               "datasource": "sales", "adopted_by": "user"}
    s = {"conv_id": "manual-contract", "term_datasource": "sales", "confirmed_terms": [adopted]}
    async def callback(*args):
        pass
    async def llm(prompt):
        return {"terms": [{"term": "ROI", "decision": "resolved", "definition": "投资回报率", "formula": "收入/销售额"}],
                "request_scope": {"action": "new", "scope_evidence": "计算ROI", "requirements": {"ratios": [
                    {"metric": "ROI", "numerator": "收入", "denominator": "销售额", "unit": "fraction", "unavailable_policy": "explain"}]}}}
    assert await TermClarifier(s, callback, llm, store=MemoryStore()).preflight("计算ROI", "")
    assert s["task_requirements"]["ratios"][0]["numerator"] == "净收益"
    assert s["task_requirements"]["ratios"][0]["denominator"] == "成本"
    assert adopted["formula"] == "净收益/成本"


@pytest.mark.asyncio
async def test_cost_subset_term_and_requirements_share_the_same_adopted_meaning():
    s = {"conv_id": "subset-meaning"}
    async def callback(*args):
        pass
    async def llm(prompt):
        return {"terms": [{"term": "成本完整子集", "decision": "resolved", "definition": "仅材料成本齐全的销售行", "formula": "仅材料成本"}],
                "request_scope": {"action": "new", "scope_evidence": "成本完整子集", "requirements": {"ratios": [
                    {"metric": "子集毛利率", "numerator": "子集毛利额", "denominator": "子集销售额", "unit": "fraction", "unavailable_policy": "explain"}]}}}
    assert await TermClarifier(s, callback, llm, store=MemoryStore()).preflight("分析成本完整子集", "")
    assert "仅材料" not in s["confirmed_terms"][0]["definition"]
    assert "工费" in s["task_requirements"]["ratios"][0]["required_inputs"]


def test_material_only_contract_is_explicit_and_continuation_preserves_it():
    from dbgpt_app.openapi.api_v1.terms.service import apply_cost_contract

    requirements = {
        "ratios": [
            {
                "metric": "子集毛利率",
                "numerator": "子集毛利额",
                "denominator": "子集销售额",
            }
        ]
    }
    apply_cost_contract(requirements, "只考虑材料成本，分析成本完整子集")
    assert "工费" not in requirements["ratios"][0]["required_inputs"]
    apply_cost_contract(requirements, "继续原需求")
    assert requirements["ratios"][0]["cost_basis"] == "material_only"


@pytest.mark.parametrize("metrics", [[], ["毛利率"], ["子集毛利率"], ["毛利率", "子集毛利率"]])
def test_explicit_full_and_complete_subset_requirements_cannot_be_omitted(metrics):
    from dbgpt_app.openapi.api_v1.terms.service import apply_cost_contract

    requirements = {"ratios": [{"metric": metric, "numerator": "毛利额", "denominator": "销售额", "unit": "fraction", "unavailable_policy": "explain"} for metric in metrics]}
    apply_cost_contract(requirements, "请按月分析销售额和毛利率，分别展示全量指标与成本完整子集指标")
    ratios = requirements["ratios"]
    assert len(ratios) == 2
    assert {r["population"] for r in ratios} == {"full", "complete_inputs"}
    assert all(r["required_inputs"] == ["材料成本单价", "工费", "销售数量", "销售金额"] for r in ratios)
    apply_cost_contract(requirements, "继续原需求，只改报告排版")
    assert len(requirements["ratios"]) == 2


def test_full_value_request_excluding_subset_does_not_add_subset_requirement():
    from dbgpt_app.openapi.api_v1.terms.service import apply_cost_contract

    requirements = {"ratios": [{"metric": "毛利率", "numerator": "毛利额", "denominator": "销售额", "unit": "fraction", "unavailable_policy": "require_value"}]}
    apply_cost_contract(requirements, "全量毛利率必须给出完整数值，不能用成本完整子集的数值替代")
    assert len(requirements["ratios"]) == 1
    assert requirements["ratios"][0]["population"] == "full"
    assert requirements["ratios"][0]["unavailable_policy"] == "require_value"


@pytest.mark.asyncio
async def test_preflight_persists_both_populations_when_model_only_returns_full():
    request = "分别展示全量指标与成本完整子集指标的毛利率，并生成中文HTML报告"
    s = {"conv_id": "dual-preflight"}

    async def callback(*args):
        raise AssertionError("This explicit request needs no clarification")

    async def llm(prompt):
        return {"terms": [], "request_scope": {"action": "new", "scope_evidence": request, "requirements": {"requires_report": True, "ratios": [{"metric": "毛利率", "numerator": "毛利额", "denominator": "销售额", "unit": "fraction", "unavailable_policy": "explain"}]}}}

    assert await TermClarifier(s, callback, llm, store=MemoryStore()).preflight(request, "")
    assert s["requires_report"]
    assert {r["population"] for r in s["task_requirements"]["ratios"]} == {"full", "complete_inputs"}


@pytest.mark.asyncio
async def test_auto_ratio_formula_display_groups_a_subtraction_numerator():
    s = {"conv_id": "formula-display"}
    request = "解释ROI计算口径"

    async def callback(*args):
        raise AssertionError("No clarification needed")

    async def llm(prompt):
        return {"terms": [{"term": "ROI", "decision": "resolved", "definition": "投资收益比例", "formula": "(收入-成本)/成本"}], "request_scope": {"action": "new", "scope_evidence": request, "requirements": {"ratios": [{"metric": "ROI", "numerator": "收入-成本", "denominator": "成本", "unit": "fraction", "unavailable_policy": "explain"}]}}}

    assert await TermClarifier(s, callback, llm, store=MemoryStore()).preflight(request, "")
    assert s["confirmed_terms"][0]["formula"] == "(收入-成本)/成本"
    assert s["task_requirements"]["ratios"][0]["numerator"] == "收入-成本"


def test_explicit_custom_cost_formula_is_not_replaced_by_standard_costing():
    from dbgpt_app.openapi.api_v1.terms.service import apply_cost_contract
    requirements = {"ratios": [{"metric": "子集毛利率", "numerator": "子集毛利额", "denominator": "子集销售额"}]}
    request = "分析成本完整子集。毛利额=销售金额-销售数量*材料成本单价*1.1"
    apply_cost_contract(requirements, request)
    ratio = requirements["ratios"][0]
    assert ratio["cost_basis"] == "custom"
    assert ratio["required_inputs"] == ["材料成本单价", "销售数量", "销售金额"]
    assert ratio["cost_evidence"] == request


def test_erp_ratio_contract_does_not_use_model_sql_as_column_labels():
    from dbgpt_app.openapi.api_v1.terms.service import validate_request_scope
    request = "分析成本完整子集毛利率"
    response = {"terms": [], "request_scope": {"action": "new", "scope_evidence": request,
        "requirements": {"ratios": [{"metric": "成本完整子集毛利率",
        "numerator": "SUM(销售金额)-SUM(销售数量*(材料成本单价+工费))",
        "denominator": "SUM(销售金额) WHERE 工费 IS NOT NULL", "unit": "fraction", "unavailable_policy": "explain"}]}}}
    _, _, requirements = validate_request_scope(response, request, {})
    ratio = requirements["ratios"][0]
    assert ratio["numerator"] == "成本完整子集毛利额"
    assert ratio["denominator"] == "成本完整子集销售额"


@pytest.mark.asyncio
async def test_given_gross_ratio_without_database_does_not_require_erp_cost_inputs():
    from dbgpt_app.openapi.api_v1.tools.query_quality import validate_explicit_ratios
    s = {"conv_id": "given-gross"}
    request = "毛利额20元，销售额100元。毛利率=毛利额/销售额"
    async def callback(*args):
        pass
    async def llm(prompt):
        return {"terms": [{"term": "毛利率", "decision": "resolved", "definition": "毛利额除以销售额", "formula": "毛利额/销售额"}],
                "request_scope": {"action": "new", "scope_evidence": request, "requirements": {"ratios": [
                    {"metric": "毛利率", "numerator": "毛利额", "denominator": "销售额", "unit": "fraction", "unavailable_policy": "explain"}]}}}
    assert await TermClarifier(s, callback, llm, store=MemoryStore()).preflight(request, "")
    assert validate_explicit_ratios(s, ["毛利额", "销售额", "毛利率", "missing_input_count"], [[20,100,.2,0]]) is None


def preflight_fixture_response(prompt, terms):
    """Old terminology fixtures now also supply the explicit new scope contract."""
    if '"previous_task_requirements"' not in prompt or not isinstance(terms, list):
        return terms
    from dbgpt_app.openapi.api_v1.terms.service import requirements_from_input

    context = json.loads(prompt.split("\n", 1)[1])
    return {"terms": terms, "request_scope": {
        "action": "new", "scope_evidence": context["request"],
        "requirements": requirements_from_input(context["request"], [], ""),
    }}


@pytest.mark.asyncio
@pytest.mark.parametrize("aspect", ["meaning", "formula"])
async def test_manual_confirmation_is_reused_without_new_conflict(aspect):
    adopted = {
        "term": "ROI",
        "aliases": ["投资回报率"],
        "definition": "投资回报率",
        "formula": "净收益 / 成本",
        "formula_confirmed": True,
        "datasource": "sales",
    }
    state = {
        "conv_id": "reuse",
        "term_datasource": "sales",
        "confirmed_terms": [adopted],
    }

    async def callback(kind, payload):
        assert kind != "question.asked"

    clarifier = TermClarifier(state, callback, empty_llm, store=MemoryStore())
    assert (await clarifier.clarify("投资回报率", aspect=aspect))["formula_confirmed"]
    assert not state.get("unresolved_terms")


@pytest.mark.asyncio
async def test_confirmed_formula_is_not_blindly_reused_for_new_datasource():
    adopted = {
        "term": "ROI",
        "definition": "投资回报率",
        "formula": "收益/成本",
        "formula_confirmed": True,
        "datasource": "old",
    }
    state = {
        "conv_id": "switch",
        "term_datasource": "new",
        "confirmed_terms": [adopted],
    }

    async def llm(prompt):
        return preflight_fixture_response(prompt, [{"term": "ROI", "definition": "投资回报率", "decision": "resolved"}])

    async def callback(*args):
        pass

    assert await TermClarifier(state, callback, llm, store=MemoryStore()).preflight(
        "解释ROI", ""
    )
    assert state["confirmed_terms"][0]["formula_confirmed"] is False
    assert adopted["formula_confirmed"] is True


@pytest.mark.asyncio
async def test_generic_question_reuses_manually_confirmed_formula():
    state = {
        "conv_id": "reuse",
        "confirmed_terms": [
            {"term": "ROI", "definition": "投资回报率", "formula_confirmed": True}
        ],
    }

    async def callback(kind, payload):
        assert kind != "question.asked"

    result = json.loads(
        await make_question(state, callback)(
            json.dumps(
                [{"header": "计算口径", "question": "ROI公式需要确认吗？"}],
                ensure_ascii=False,
            )
        )
    )
    assert result["success"] and not state.get("unresolved_terms")


@pytest.mark.asyncio
@pytest.mark.parametrize("formula", [None, "", "金额/数量"])
async def test_model_optional_formula_is_normalized(formula):
    async def llm(prompt):
        return preflight_fixture_response(prompt, [
            {
                "term": "平均销售单价",
                "decision": "resolved",
                "definition": "单位销售金额",
                "formula": formula,
                "record_id": None,
                "candidates": None,
            }
        ])

    state = {"conv_id": "null-fields"}

    async def callback(kind, payload):
        assert kind != "question.asked"

    assert await TermClarifier(state, callback, llm, store=MemoryStore()).preflight(
        "平均销售单价", ""
    )
    assert state["confirmed_terms"][0]["formula"] == (formula or "")


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [123, {}, ["wrong"], {"decision": "invalid"}])
async def test_bad_model_batch_does_not_overwrite_confirmed_snapshot(bad):
    original = {"term": "旧指标", "definition": "已确认定义"}
    state = {"conv_id": "bad-batch", "confirmed_terms": [original.copy()]}

    async def llm(prompt):
        second = {
            "term": "数量",
            "definition": "数量",
            "decision": "resolved",
            "formula": bad,
        }
        return preflight_fixture_response(prompt, [
            {"term": "销售额", "definition": "收入", "decision": "resolved"},
            second,
        ])

    async def callback(*args):
        pass

    with pytest.raises(ValueError):
        await TermClarifier(state, callback, llm, store=MemoryStore()).preflight(
            "销售额和数量", ""
        )
    assert state["confirmed_terms"] == [original]


def test_safe_term_failure_categories_do_not_echo_raw_error():
    for stage, error, expected in [
        ("model", TimeoutError("PRIVATE_BODY"), "model_timeout"),
        ("model_format", ValueError("PRIVATE_BODY"), "model_format"),
        ("shared_store", RuntimeError("PRIVATE_BODY"), "term_store"),
    ]:
        state = {"conv_id": "safe", "term_check_stage": stage}
        record_term_check_failure(state, error)
        assert state["term_check_error_code"] == expected
        message = incomplete_summary(
            TaskResult("incomplete", "term_check_failed"), state
        )
        assert "PRIVATE_BODY" not in message
        assert "相关分析未执行" in message


def test_formula_status_does_not_mark_basic_fields_pending():
    assert "待确认" not in term_scope_description({"formula": ""})
    assert "自动采用" in term_scope_description(
        {"formula": "金额/数量", "adopted_by": "context"}
    )
    assert "已确认" in term_scope_description(
        {"formula": "金额/数量", "formula_confirmed": True}
    )
    assert "待确认" in term_scope_description({"formula": "金额/数量"})
    assert "`" in term_scope_description(
        {"formula": "价格*数量", "adopted_by": "context"}, markdown=True
    )


class MemoryStore:
    def __init__(self, records=(), fail=False):
        self.records = list(records)
        self.fail = fail

    def find(self, term="", datasource=""):
        return [
            dict(r)
            for r in self.records
            if not term or term == r["term"] or term in r.get("aliases", [])
        ]

    def save(self, value):
        if self.fail:
            raise RuntimeError("storage unavailable")
        self.records.append({**value, "id": "saved"})
        return self.records[-1]


@pytest.mark.asyncio
async def test_shared_manual_reuse_preserves_record_id_without_resaving():
    record = {"id": "shared-roi", "term": "ROI", "definition": "投资回报率", "datasource": "", "source_type": "user", "formula": ""}
    store = MemoryStore([record])
    s = {"conv_id": "shared-no-write"}
    async def callback(kind, payload):
        if kind == "question.asked":
            label = payload["questions"][0]["options"][0]["label"]
            question_manager.reply(payload["request_id"], [[label]], s["conv_id"], {"save_term": False})
    result = await TermClarifier(s, callback, empty_llm, store=store).clarify("ROI")
    assert result["id"] == "shared-roi"
    assert result["adopted_by"] == "user"
    assert store.records == [record]


@pytest.mark.asyncio
async def test_preflight_scope_preserves_requirements_and_rejects_history_evidence():
    previous = {"requires_report": True, "ratios": [{
        "metric": "毛利率", "numerator": "毛利额", "denominator": "销售额",
        "unit": "fraction", "unavailable_policy": "explain",
    }]}
    state = {"conv_id": "scope", "task_requirements": previous}

    async def callback(kind, payload):
        assert kind != "question.asked"

    async def llm(prompt):
        assert '"previous_task_requirements"' in prompt
        return {"terms": [], "request_scope": {
            "action": "continue", "scope_evidence": "", "requirements": {},
        }}

    clarifier = TermClarifier(state, callback, llm, store=MemoryStore())
    assert await clarifier.preflight("继续原需求，改报告排版", "")
    assert state["task_requirements"]["ratios"] == previous["ratios"]
    assert state["requires_report"] is True

    async def bad_llm(prompt):
        return {"terms": [], "request_scope": {
            "action": "new", "scope_evidence": "模型已取消计划",
            "requirements": {"ratios": []},
        }}

    clarifier.llm = bad_llm
    with pytest.raises(ValueError):
        await clarifier.preflight("继续原需求", "")
    assert state["task_requirements"]["ratios"] == previous["ratios"]


@pytest.mark.parametrize("action,text,updates,report,ratios", [
    ("continue", "改为六月客户甲", {}, True, 1),
    ("continue", "仅改排版", {}, True, 1),
    ("change", "不要毛利率", {"ratios": []}, True, 0),
    ("new", "解释销售额是什么", {"ratios": [], "requires_report": False}, False, 0),
    ("new", "你好", {"ratios": [], "requires_report": False}, False, 0),
])
def test_scope_actions_only_replace_explicit_requirements(action, text, updates, report, ratios):
    from dbgpt_app.openapi.api_v1.terms.service import validate_request_scope

    previous = {"requires_report": True, "ratios": [{
        "metric": "毛利率", "numerator": "毛利额", "denominator": "销售额",
        "unit": "fraction", "unavailable_policy": "explain",
    }]}
    _, _, requirements = validate_request_scope({
        "terms": [], "request_scope": {"action": action, "scope_evidence": text,
                                          "requirements": updates},
    }, text, previous)
    assert requirements["requires_report"] is report
    assert len(requirements["ratios"]) == ratios
    assert len(previous["ratios"]) == 1


def test_legacy_request_reconstruction_uses_applicable_confirmed_formula():
    from dbgpt_app.openapi.api_v1.terms.service import requirements_from_input

    confirmed = [{"term": "毛利率", "formula": "毛利额/销售额",
                  "formula_confirmed": True, "datasource": "monthly"}]
    requirements = requirements_from_input("输出毛利率报告", confirmed, "monthly")
    assert requirements["ratios"][0]["metric"] == "毛利率"
    assert not requirements_from_input("输出毛利率", confirmed, "other")["ratios"]


@pytest.mark.asyncio
async def test_current_explicit_formula_updates_task_snapshot_without_changing_shared_record():
    old = {"term": "毛利率", "definition": "毛利率", "formula": "毛利额/销售额",
           "formula_confirmed": True, "datasource": "monthly", "adopted_by": "user"}
    state = {"conv_id": "new-formula", "term_datasource": "monthly", "confirmed_terms": [old]}

    async def callback(*args):
        pass

    async def llm(prompt):
        return {"terms": [{"term": "毛利率", "decision": "resolved", "definition": "毛利率", "formula": "毛利额/成本"}],
                "request_scope": {"action": "new", "scope_evidence": "毛利率=毛利额/成本",
                                  "requirements": {"ratios": [{"metric": "毛利率", "numerator": "毛利额", "denominator": "成本", "unit": "fraction", "unavailable_policy": "explain"}]}}}

    assert await TermClarifier(state, callback, llm, store=MemoryStore()).preflight("毛利率=毛利额/成本，请计算", "")
    assert state["confirmed_terms"][0]["formula"] == "毛利额/成本"
    assert old["formula"] == "毛利额/销售额"


def test_financial_analysis_does_not_invent_a_database_profile_deliverable():
    from dbgpt_app.openapi.api_v1.terms.service import validate_request_scope

    _, _, requirements = validate_request_scope({"terms": [], "request_scope": {
        "action": "new", "scope_evidence": "分析销售并生成报告",
        "requirements": {"requires_report": True, "requires_database_profile": True, "ratios": []},
    }}, "分析销售并生成报告", None)
    assert requirements["requires_database_profile"] is False


def test_plain_ratio_column_defaults_to_fraction_despite_model_display_preference():
    from dbgpt_app.openapi.api_v1.terms.service import validate_request_scope

    _, _, requirements = validate_request_scope({"terms": [], "request_scope": {
        "action": "new", "scope_evidence": "输出毛利率",
        "requirements": {"ratios": [{"metric": "毛利率", "numerator": "毛利额", "denominator": "销售额", "unit": "percent", "unavailable_policy": "explain"}]},
    }}, "输出毛利率", None)
    assert requirements["ratios"][0]["unit"] == "fraction"


@pytest.mark.asyncio
@pytest.mark.parametrize("provide_requirements", [True, False])
async def test_legacy_followup_reconstructs_ratios_from_saved_human_scope_in_same_preflight(provide_requirements):
    state = {"conv_id": "legacy-scope", "legacy_requirements_unresolved": True,
             "previous_user_input": "分析毛利率并生成报告", "task_requirements": {"requires_report": True, "ratios": []}}
    calls = []

    async def callback(kind, payload):
        assert kind != "question.asked"

    async def llm(prompt):
        calls.append(prompt)
        return {"terms": [{"term": "毛利率", "decision": "resolved", "definition": "毛利率", "formula": "毛利额/销售额"}],
                "request_scope": {"action": "continue", "scope_evidence": "继续原需求", "requirements": {
                    "ratios": [{"metric": "毛利率", "numerator": "毛利额", "denominator": "销售额", "unit": "fraction", "unavailable_policy": "explain"}]} if provide_requirements else {}}}

    assert await TermClarifier(state, callback, llm, store=MemoryStore()).preflight("继续原需求，改排版", "")
    assert len(calls) == 1
    assert state["requires_report"] is True
    assert state["task_requirements"]["ratios"][0]["metric"] == "毛利率"
    assert state["confirmed_terms"][0]["formula"] == "毛利额/销售额"


@pytest.mark.asyncio
async def test_unknown_legacy_formula_is_clarified_instead_of_dropped(monkeypatch):
    from dbgpt_app.openapi.api_v1.terms.service import legacy_ratio_names

    assert legacy_ratio_names("请分析ROI及毛利率") == ["ROI", "毛利率"]
    state = {"conv_id": "legacy-unknown", "legacy_requirements_unresolved": True,
             "previous_user_input": "分析ROI并生成报告", "task_requirements": {"requires_report": True, "ratios": []}}
    questions = []

    async def callback(kind, payload):
        if kind == "question.asked":
            questions.append(payload)
            question_manager.reply(payload["request_id"], [["投资回报率"]], state["conv_id"], decisions={
                "formula": "收益/成本", "formula_confirmed": True,
            })

    async def candidates(self, term, reason):
        return []

    monkeypatch.setattr(TermClarifier, "model_candidates", candidates)

    async def llm(prompt):
        return {"terms": [], "request_scope": {"action": "continue", "scope_evidence": "", "requirements": {}}}

    assert await TermClarifier(state, callback, llm, store=MemoryStore()).preflight("继续原需求", "")
    assert len(questions) == 1
    assert state["task_requirements"]["ratios"][0]["metric"] == "ROI"


@pytest.mark.asyncio
async def test_followup_assesses_required_metric_when_datasource_changes():
    state = {"conv_id": "source-scope", "term_datasource": "new",
             "previous_resource_context": {"database": {"name": "old", "type": "sqlite"}},
             "task_requirements": {"ratios": [{"metric": "毛利率", "numerator": "毛利额", "denominator": "销售额", "unit": "fraction", "unavailable_policy": "explain"}]}}
    questions = []

    async def callback(kind, payload):
        if kind == "question.asked":
            questions.append(payload)
            question_manager.reply(payload["request_id"], [["采用当前库口径"]], state["conv_id"], decisions={
                "formula": "毛利额/销售额", "formula_confirmed": True,
            })

    async def llm(prompt):
        assert '"previous_database"' in prompt
        return {"terms": [{"term": "毛利率", "decision": "conflict", "reason": "当前库有两个实际成本口径", "candidates": [{"definition": "含全部成本"}]}],
                "request_scope": {"action": "continue", "scope_evidence": "", "requirements": {}}}

    assert await TermClarifier(state, callback, llm, store=MemoryStore()).preflight("继续原需求", "当前库存在两个适用成本字段")
    assert len(questions) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", ["继续原分析", "开始新任务", None])
async def test_scope_clarification_preserves_or_clears_only_user_chosen_scope(answer):
    previous = {"requires_report": True, "ratios": [{"metric": "毛利率", "numerator": "毛利额", "denominator": "销售额", "unit": "fraction", "unavailable_policy": "explain"}]}
    state = {"conv_id": "clarify-scope", "task_requirements": previous}
    events = []

    async def callback(kind, payload):
        events.append(kind)
        if kind == "question.asked":
            if answer is None:
                question_manager.reject(payload["request_id"], state["conv_id"])
            else:
                question_manager.reply(payload["request_id"], [[answer]], state["conv_id"])

    async def llm(prompt):
        return {"terms": [], "request_scope": {"action": "clarify", "scope_evidence": "", "requirements": {}}}

    ready = await TermClarifier(state, callback, llm, store=MemoryStore()).preflight("这一项呢", "")
    if answer is None:
        assert not ready
        assert state["task_requirements"] == previous
        assert "question.rejected" in events
    else:
        assert ready
        assert state["requires_report"] is (answer == "继续原分析")
        assert len(state["task_requirements"]["ratios"]) == (1 if answer == "继续原分析" else 0)


@pytest.mark.asyncio
async def test_scope_timeout_rejects_pending_question_without_erasing_previous_scope():
    previous = {"requires_report": True, "ratios": []}
    state = {"conv_id": "scope-timeout", "task_requirements": previous}
    events = []

    async def callback(kind, payload):
        events.append(kind)

    async def llm(prompt):
        return {"terms": [], "request_scope": {"action": "clarify", "scope_evidence": "", "requirements": {}}}

    assert not await TermClarifier(state, callback, llm, store=MemoryStore(), timeout=0.01).preflight("这一项呢", "")
    assert state["term_stop_reason"] == "scope_timeout"
    assert state["task_requirements"] == previous
    assert "question.rejected" in events
    assert question_manager.list_pending(state["conv_id"]) == []


def test_explicit_complete_numeric_values_cannot_be_relaxed_by_model_scope():
    from dbgpt_app.openapi.api_v1.terms.service import validate_request_scope

    text = "必须给出完整数值"
    _, _, requirements = validate_request_scope({"terms": [], "request_scope": {
        "action": "new", "scope_evidence": text, "requirements": {"ratios": [{
            "metric": "毛利率", "numerator": "毛利额", "denominator": "销售额", "unit": "fraction", "unavailable_policy": "explain",
        }]}}}, text, None)
    assert requirements["ratios"][0]["unavailable_policy"] == "require_value"


def test_required_complete_report_does_not_imply_required_complete_numbers():
    from dbgpt_app.openapi.api_v1.terms.service import requirements_from_input

    requirements = requirements_from_input("必须生成完整报告，毛利率=毛利额/销售额，缺工费可输出NULL并说明", [])
    assert requirements["requires_report"]
    assert requirements["ratios"][0]["unavailable_policy"] == "explain"


@pytest.mark.parametrize("user_text,policy", [
    ("请补齐工费数据再计算毛利率", "require_value"),
    ("不用补齐工费，毛利率NULL并说明", "explain"),
    ("补充工费缺失说明", "explain"),
])
def test_backfill_policy_distinguishes_requested_data_and_limitation_notes(user_text, policy):
    from dbgpt_app.openapi.api_v1.terms.service import requirements_from_input

    assert requirements_from_input(user_text + "。毛利率=毛利额/销售额", [])["ratios"][0]["unavailable_policy"] == policy


@pytest.mark.asyncio
async def test_verified_missing_cost_does_not_ask_to_fill_zero_or_drop_sales():
    state = {"conv_id": "missing-cost-policy", "term_datasource": "monthly",
             "task_requirements": {"ratios": [{"metric": "毛利率", "unavailable_policy": "explain"}]},
             "validated_results": {"cost": {"source": "sql", "conv_id": "missing-cost-policy", "database": "monthly",
                                                "columns": ["产品编码", "工费"], "rows": [["A",None]]}}}

    async def callback(kind, payload):
        assert kind != "question.asked", "明确缺数据应按既定NULL政策继续，不向用户提出错误选择"

    result = json.loads(await make_question(state, callback)(json.dumps([{
        "header": "工费单价缺失处理", "question": "产品A工费单价为NULL，请问如何处理？",
        "options": [{"label": "视为0处理"}, {"label": "跳过该产品销售行"}],
    }], ensure_ascii=False)))
    assert result["success"]
    assert "NULL" in result["chunks"][0]["content"]
    assert question_manager.list_pending(state["conv_id"]) == []


@pytest.mark.asyncio
async def test_price_conflict_outside_current_sales_scope_does_not_interrupt_task():
    state = {"conv_id": "outside-price", "term_datasource": "monthly", "original_user_input": "分析客户甲的销售和毛利率",
             "validated_results": {
                 "sales": {"conv_id": "outside-price", "database": "monthly", "sales_source": True,
                           "columns": ["source_sales_row_id", "产品编码"], "rows": [[1,"A"]]},
                 "cost": {"conv_id": "outside-price", "database": "monthly", "price_conflicts": [
                     {"table": "工费单价", "keys": {"产品编码": "110010100003"}, "prices": [3.6,10.8]}]}}}

    async def callback(kind, payload):
        assert kind != "question.asked"

    clarifier = TermClarifier(state, callback, empty_llm, store=MemoryStore())
    result = await clarifier.clarify("产品110010100003工费单价", reason="两个不同价格", evidence="3.6和10.8")
    assert "范围" in result["scope_note"]
    assert not state.get("unresolved_terms")


async def empty_llm(prompt):
    return preflight_fixture_response(prompt, [])


@pytest.mark.asyncio
async def test_saved_candidates_require_selection_and_keep_snapshot():
    store = MemoryStore(
        [
            {
                "id": "old",
                "term": "ARR",
                "definition": "年度经常性收入",
                "datasource": "sales",
                "formula": "月收入*12",
                "formula_confirmed": True,
            }
        ]
    )
    events = []

    async def callback(kind, payload):
        events.append((kind, payload))
        if kind == "question.asked":
            label = payload["questions"][0]["options"][0]["label"]
            question_manager.reply(
                payload["request_id"], [[label]], "conv", {"save_term": False}
            )

    state = {"conv_id": "conv", "term_datasource": "sales"}
    service = TermClarifier(state, callback, empty_llm, store=store)
    assert await service.preflight("计算 ARR 前先让我选择解释", "")
    assert any(k == "question.asked" for k, _ in events)
    snapshot = state["confirmed_terms"][0]
    assert snapshot["definition"] == "年度经常性收入"
    assert snapshot["adopted_by"] == "user"
    assert snapshot["formula_confirmed"]
    store.records[0]["definition"] = "changed"
    assert snapshot["definition"] == "年度经常性收入"
    assert not state["unresolved_terms"]


@pytest.mark.asyncio
async def test_current_model_reinterprets_saved_term_without_fabricated_sources():
    calls = []

    async def llm(prompt):
        calls.append(prompt)
        return preflight_fixture_response(prompt, [
            {
                "definition": "年度经常性收入（Annual Recurring Revenue）",
                "sources": [{"url": "https://invented.example/"}],
            }
        ])

    asks = []

    async def callback(kind, payload):
        if kind != "question.asked":
            return
        asks.append(payload)
        label = (
            MODEL_CHOICE
            if len(asks) == 1
            else payload["questions"][0]["options"][0]["label"]
        )
        question_manager.reply(
            payload["request_id"], [[label]], "conv", {"save_term": True}
        )

    store = MemoryStore([{"id": "old", "term": "ARR", "definition": "会计收益率"}])
    state = {"conv_id": "conv"}
    service = TermClarifier(state, callback, llm, store=store)
    result = await service.clarify("ARR")
    assert len(calls) == 1
    assert len(asks) == 2
    assert result["sources"] == []
    assert result["source_type"] == "model"
    assert "未经资料核验" in asks[1]["questions"][0]["options"][0]["description"]
    assert result["saved"]


@pytest.mark.asyncio
async def test_custom_save_failure_is_visible():
    async def callback(kind, payload):
        if kind == "question.asked":
            question_manager.reply(
                payload["request_id"],
                [["公司自定义解释"]],
                "conv",
                {
                    "save_term": True,
                    "formula": "收入减材料成本",
                    "formula_confirmed": True,
                },
            )

    state = {"conv_id": "conv"}
    result = await TermClarifier(
        state, callback, empty_llm, store=MemoryStore(fail=True)
    ).clarify("内部简称")
    assert result["definition"] == "公司自定义解释"
    assert result["formula_confirmed"]
    assert not result["saved"] and result["save_error"]


@pytest.mark.asyncio
async def test_unknown_term_model_suggestions_wait_for_confirmation_before_saving():
    store = MemoryStore()

    async def llm(prompt):
        return preflight_fixture_response(prompt, [{"definition": "息税折旧摊销前利润（EBITDA）", "formula": "待确认"}])

    async def callback(kind, payload):
        if kind == "question.asked":
            assert store.records == []
            option = payload["questions"][0]["options"][0]
            assert option["source_type"] == "model"
            question_manager.reply(payload["request_id"], [[option["label"]]], "conv")

    result = await TermClarifier(
        {"conv_id": "conv"}, callback, llm, store=store
    ).clarify("EBITDA")
    assert result["saved"] and result["formula_confirmed"] is False
    assert len(store.records) == 1


@pytest.mark.asyncio
async def test_model_failure_falls_back_to_custom_explanation():
    async def llm(prompt):
        raise ValueError("model unavailable")

    async def callback(kind, payload):
        if kind == "question.asked":
            assert "模型理解失败" in payload["questions"][0]["question"]
            assert payload["questions"][0]["options"] == []
            question_manager.reply(payload["request_id"], [["用户自己的解释"]], "conv")

    result = await TermClarifier(
        {"conv_id": "conv"}, callback, llm, store=MemoryStore()
    ).clarify("内部简称")
    assert result["source_type"] == "user"


@pytest.mark.asyncio
@pytest.mark.parametrize("reject", [True, False])
async def test_cancel_timeout_keep_execution_blocked(reject):
    async def callback(kind, payload):
        if reject and kind == "question.asked":
            question_manager.reject(payload["request_id"], "conv")

    state = {"conv_id": "conv"}
    assert (
        await TermClarifier(
            state, callback, empty_llm, store=MemoryStore(), timeout=0.01
        ).clarify("毛利")
        is None
    )
    assert state["unresolved_terms"] == ["毛利"]
    assert state["term_stop_reason"] == ("term_cancelled" if reject else "term_timeout")
    assert not question_manager.list_pending("conv")
    calls = []

    @tool(description="test analysis")
    async def sql_query(sql: str):
        calls.append(sql)
        return "executed"

    pack = ToolPack([sql_query])
    install_execution_guards(pack, state)
    result = await pack.async_execute(resource_name="sql_query", sql="SELECT 1")
    assert json.loads(result)["success"] is False
    assert not calls


def test_wrong_conversation_duplicate_and_invalid_selection():
    manager = QuestionManager()
    pq = manager.create(
        "conv", [{"kind": "term", "custom": False, "options": [{"label": "A"}]}]
    )
    with pytest.raises(ValueError):
        manager.reply(pq.request_id, [["A"]], "wrong")
    with pytest.raises(ValueError):
        manager.reply(pq.request_id, [["B"]], "conv")
    manager.reply(pq.request_id, [["A"]], "conv")
    with pytest.raises(ValueError):
        manager.reply(pq.request_id, [["A"]], "conv")
    assert contains_term("分析 ARR", "ARR")
    assert not contains_term("barrier", "ARR")


@pytest.mark.asyncio
async def test_shared_alias_with_multiple_meanings_is_one_question():
    store = MemoryStore(
        [
            {
                "id": "a",
                "term": "贡献毛益",
                "definition": "收入减变动成本",
                "aliases": ["CM"],
            },
            {
                "id": "b",
                "term": "配置管理",
                "definition": "管理系统配置",
                "aliases": ["CM"],
            },
        ]
    )
    questions = []

    async def callback(kind, payload):
        if kind == "question.asked":
            questions.append(payload)
            options = payload["questions"][0]["options"]
            question_manager.reply(
                payload["request_id"], [[options[1]["label"]]], "conv"
            )

    state = {"conv_id": "conv"}
    assert await TermClarifier(state, callback, empty_llm, store=store).preflight(
        "解释 CM", ""
    )
    assert len(questions) == 1
    assert state["confirmed_terms"][0]["term"] == "配置管理"


@pytest.mark.asyncio
async def test_real_sql_is_blocked_until_confirmed_then_returns_actual_rows(
    tmp_path, monkeypatch
):
    from dbgpt.configs import model_config

    monkeypatch.setattr(model_config, "PILOT_PATH", str(tmp_path))
    engine = create_engine(f"sqlite:///{tmp_path / 'sample.db'}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE sample(amount INTEGER)"))
        connection.execute(text("INSERT INTO sample VALUES (10), (20)"))
    executed = []
    event.listen(
        engine, "before_cursor_execute", lambda *args: executed.append(args[2])
    )
    state = {"conv_id": "term-sql-qa"}
    query = make_sql_query(state, SimpleNamespace(_engine=engine))
    pack = ToolPack([query])
    install_execution_guards(pack, state)

    async def callback(kind, payload):
        if kind == "question.asked":
            blocked = json.loads(
                await pack.async_execute(
                    resource_name="sql_query", sql="SELECT amount FROM sample"
                )
            )
            assert not blocked["success"]
            assert executed == []
            label = payload["questions"][0]["options"][0]["label"]
            question_manager.reply(
                payload["request_id"], [[label]], "term-sql-qa", {"save_term": False}
            )

    store = MemoryStore([{"id": "old", "term": "销售额", "definition": "订单金额"}])
    assert await TermClarifier(state, callback, empty_llm, store=store).preflight(
        "分析销售额前先确认含义", ""
    )
    result = json.loads(
        await pack.async_execute(
            resource_name="sql_query", sql="SELECT amount FROM sample"
        )
    )
    assert result["success"] and result["row_count"] == 2
    assert result["preview"]["rows"] == [[10], [20]]
    assert len(executed) == 1
    engine.dispose()


@pytest.mark.asyncio
async def test_full_saved_definition_is_visible_and_selectable_at_api_limit():
    definition = "完整解释" * 1000

    async def callback(kind, payload):
        if kind == "question.asked":
            label = payload["questions"][0]["options"][0]["label"]
            assert definition in label
            question_manager.reply(
                payload["request_id"], [[label]], "conv", {"save_term": False}
            )

    store = MemoryStore([{"id": "long", "term": "内部指标", "definition": definition}])
    result = await TermClarifier(
        {"conv_id": "conv"}, callback, empty_llm, store=store
    ).clarify("内部指标")
    assert result["definition"] == definition


@pytest.mark.asyncio
async def test_generic_question_cannot_reask_confirmed_meaning_but_keeps_preferences():
    state = {
        "conv_id": "conv",
        "confirmed_terms": [
            {
                "term": "年度经常性收入",
                "aliases": ["ARR"],
                "definition": "订阅业务年化收入",
            }
        ],
    }
    asked = []

    async def callback(kind, payload):
        if kind == "question.asked":
            asked.append(payload)
            question_manager.reply(payload["request_id"], [["HTML"]])

    question = make_question(state, callback)
    repeated = {
        "header": "ARR 含义确认",
        "question": "请选择ARR的具体含义",
        "options": [{"label": "ARPU"}],
    }
    result = json.loads(await question(json.dumps([repeated], ensure_ascii=False)))
    assert result["success"] and "订阅业务年化收入" in result["chunks"][0]["content"]
    assert asked == []
    preferences = {
        "header": "报告格式",
        "question": "ARR报告输出HTML还是PDF？",
        "options": [{"label": "HTML"}, {"label": "PDF"}],
    }
    result = await question(json.dumps([repeated, preferences], ensure_ascii=False))
    assert len(asked) == 1 and len(asked[0]["questions"]) == 1
    assert "HTML" in result


@pytest.mark.asyncio
@pytest.mark.parametrize("confirmed", [True, False])
async def test_formula_confirmation_preserves_meaning_and_blocks_unconfirmed_math(
    confirmed,
):
    adopted = {
        "term": "毛利",
        "definition": "收入减销售成本",
        "source_type": "user",
        "formula_confirmed": False,
    }
    state = {"conv_id": "conv", "confirmed_terms": [adopted]}

    async def callback(kind, payload):
        if kind == "question.asked":
            q = payload["questions"][0]
            assert q["header"] == "计算口径" and q["custom"] is False
            assert len(q["options"]) == 1
            question_manager.reply(
                payload["request_id"],
                [[q["options"][0]["label"]]],
                "conv",
                {
                    "formula": "销售收入 - 销售成本",
                    "formula_confirmed": confirmed,
                    "save_term": False,
                },
            )

    result = await TermClarifier(
        state, callback, empty_llm, store=MemoryStore()
    ).clarify("毛利", aspect="formula")
    if confirmed:
        assert result["definition"] == adopted["definition"]
        assert result["formula_confirmed"] and state["unresolved_terms"] == []
        assert len(state["confirmed_terms"]) == 1
    else:
        assert result is None
        assert state["term_stop_reason"] == "term_formula_unconfirmed"
        assert state["unresolved_terms"] == ["毛利（计算口径）"]


@pytest.mark.asyncio
async def test_explanation_does_not_require_formula_but_analysis_cannot_fake_success():
    assert explanation_only_request("请解释ARR，不查询数据库")
    assert not explanation_only_request("解释ARR并计算本月收入")
    assert not explanation_only_request("create a meaningful feature")
    state = {
        "conv_id": "conv",
        "confirmed_terms": [
            {"term": "ARR", "definition": "订阅收入", "formula_confirmed": False}
        ],
        "term_explanation_only": True,
    }

    async def no_question(kind, payload):
        raise AssertionError("纯解释不应追加计算口径问题")

    question = {
        "header": "ARR计算口径",
        "question": "请确认ARR的计算口径",
        "options": [],
    }
    result = json.loads(
        await make_question(state, no_question)(
            json.dumps([question], ensure_ascii=False)
        )
    )
    assert result["success"] and not state.get("unresolved_terms")
    state["term_explanation_only"] = False
    result = json.loads(
        await make_question(state, no_question)(
            json.dumps([question], ensure_ascii=False)
        )
    )
    assert not result["success"]
    assert state["unresolved_terms"] == ["ARR（计算口径）"]


@pytest.mark.parametrize(
    "user_text", ["ROI是什么", "什么是ROI？", "ROI是啥", "贡献毛益是什么？"]
)
def test_definition_questions_do_not_require_numeric_formula(user_text):
    assert explanation_only_request(user_text)


@pytest.mark.parametrize(
    "user_text",
    ["本月ROI是什么", "本月ROI是多少", "ROI是什么并帮我算一下", "解释ROI并计算本月ROI"],
)
def test_definition_words_do_not_bypass_actual_calculation(user_text):
    assert not explanation_only_request(user_text)


@pytest.mark.asyncio
async def test_roi_definition_cannot_be_blocked_by_unnecessary_formula_tool():
    state = {
        "conv_id": "roi-definition",
        "term_explanation_only": explanation_only_request("ROI是什么"),
        "confirmed_terms": [
            {"term": "ROI", "definition": "投资回报率", "formula_confirmed": False}
        ],
    }

    async def unexpected_question(kind, payload):
        raise AssertionError("纯定义任务不应请求计算口径")

    result = await TermClarifier(
        state, unexpected_question, empty_llm, store=MemoryStore()
    ).clarify("ROI", aspect="formula")
    assert result["definition"] == "投资回报率"
    assert result["formula_confirmed"] is False
    assert not state.get("unresolved_terms") and not state.get("term_stop_reason")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "user_text,kind,formula",
    [
        ("计算平均销售单价", "resolved", "销售金额 / 销售数量"),
        ("平均销售单价按未税金额除以数量计算", "resolved", "未税金额 / 数量"),
        ("计算平均销售单价", "missing_data", ""),
    ],
)
async def test_clear_metric_and_missing_data_do_not_ask(user_text, kind, formula):
    async def llm(prompt):
        return preflight_fixture_response(prompt, [
            {
                "term": "平均销售单价",
                "decision": kind,
                "definition": "每单位商品的平均销售金额",
                "formula": formula,
                "reason": "已明确，或应说明缺数据",
            }
        ])

    async def callback(kind, payload):
        assert kind != "question.asked"

    state = {"conv_id": "screening"}
    clarifier = TermClarifier(state, callback, llm, store=MemoryStore())
    assert await clarifier.preflight(user_text, "销售金额, 销售数量")
    result = await clarifier.clarify(
        "平均销售单价", "公式尚未人工确认", aspect="formula"
    )
    assert result is not None
    assert not result["formula_confirmed"]
    assert not state["unresolved_terms"]


@pytest.mark.asyncio
async def test_shared_match_alone_does_not_require_selection():
    async def callback(kind, payload):
        assert kind != "question.asked"

    state = {"conv_id": "screening"}
    store = MemoryStore(
        [
            {
                "id": "price",
                "term": "平均销售单价",
                "definition": "每单位平均销售金额",
                "formula": "销售金额 / 销售数量",
            }
        ]
    )
    assert await TermClarifier(state, callback, empty_llm, store=store).preflight(
        "计算平均销售单价", "销售金额,销售数量"
    )
    assert state["confirmed_terms"][0]["adopted_by"] == "context"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "decision,term,reason",
    [
        ("unknown", "星桥率", "内部指标没有定义"),
        ("conflict", "平均销售单价", "数据同时提供含税和未税金额，未明确采用哪个"),
    ],
)
async def test_actual_ambiguity_still_requires_selection(decision, term, reason):
    async def llm(prompt):
        return preflight_fixture_response(prompt, [
            {
                "term": term,
                "decision": decision,
                "reason": reason,
                "candidates": [{"definition": "用户选择的业务定义"}],
            }
        ])

    asked = []

    async def callback(kind, payload):
        if kind == "question.asked":
            asked.append(payload)
            label = payload["questions"][0]["options"][0]["label"]
            question_manager.reply(
                payload["request_id"], [[label]], "screening", {"save_term": False}
            )

    state = {"conv_id": "screening"}
    assert await TermClarifier(state, callback, llm, store=MemoryStore()).preflight(
        "分析" + term, "含税金额,未税金额,数量"
    )
    assert len(asked) == 1


@pytest.mark.asyncio
async def test_generic_question_cannot_reask_context_resolved_formula():
    state = {
        "conv_id": "screening",
        "confirmed_terms": [
            {
                "term": "平均销售单价",
                "definition": "平均销售金额",
                "formula": "金额 / 数量",
                "formula_confirmed": False,
                "adopted_by": "context",
                "clarification_required": False,
            }
        ],
    }

    async def callback(kind, payload):
        assert kind != "question.asked"

    result = json.loads(
        await make_question(state, callback)(
            json.dumps(
                [
                    {
                        "header": "计算口径",
                        "question": "平均销售单价的公式是什么？",
                        "options": [{"label": "金额 / 数量"}],
                    }
                ],
                ensure_ascii=False,
            )
        )
    )
    assert result["success"]
    assert not state.get("unresolved_terms")


@pytest.mark.asyncio
async def test_context_can_choose_shared_meaning_without_changing_library():
    records = [
        {"id": "revenue", "term": "ARR", "definition": "年度经常性收入"},
        {"id": "accounting", "term": "ARR", "definition": "会计收益率"},
    ]
    store = MemoryStore(records)

    async def llm(prompt):
        return preflight_fixture_response(prompt, [
            {
                "term": "ARR",
                "decision": "resolved",
                "record_id": "revenue",
                "reason": "需求已指定年度经常性收入",
            }
        ])

    async def callback(kind, payload):
        assert kind != "question.asked"

    state = {"conv_id": "screening"}
    assert await TermClarifier(state, callback, llm, store=store).preflight(
        "解释SaaS年度经常性收入ARR", ""
    )
    assert state["confirmed_terms"][0]["definition"] == "年度经常性收入"
    assert store.records == records
    store.records[0]["definition"] = "修改后的解释"
    assert state["confirmed_terms"][0]["definition"] == "年度经常性收入"


@pytest.mark.asyncio
async def test_model_resolved_item_does_not_replace_user_confirmed_meaning():
    original = {"term": "ARR", "definition": "会计收益率", "saved": False}
    state = {"conv_id": "screening", "confirmed_terms": [original.copy()]}

    async def llm(prompt):
        return preflight_fixture_response(prompt, [{"term": "ARR", "decision": "resolved", "definition": "年度经常性收入"}])

    async def callback(kind, payload):
        assert kind != "question.asked"

    assert await TermClarifier(state, callback, llm, store=MemoryStore()).preflight(
        "继续解释ARR", ""
    )
    assert state["confirmed_terms"] == [{**original, "adopted_by": "user"}]
    assert "adopted_by" not in original


@pytest.mark.asyncio
async def test_new_actual_conflict_can_reopen_automatically_adopted_formula():
    state = {
        "conv_id": "screening",
        "confirmed_terms": [
            {
                "term": "平均销售单价",
                "definition": "单位销售金额",
                "formula": "金额 / 数量",
                "formula_confirmed": False,
                "adopted_by": "context",
                "clarification_required": False,
            }
        ],
    }
    asked = []

    async def callback(kind, payload):
        if kind == "question.asked":
            asked.append(payload)
            label = payload["questions"][0]["options"][0]["label"]
            question_manager.reply(
                payload["request_id"],
                [[label]],
                "screening",
                {
                    "save_term": False,
                    "formula": "未税金额 / 数量",
                    "formula_confirmed": True,
                },
            )

    result = await TermClarifier(
        state, callback, empty_llm, store=MemoryStore()
    ).clarify(
        "平均销售单价",
        "需要选择实际金额口径",
        aspect="formula",
        evidence="新读入表同时有含税金额和未税金额，业务资料没有指定",
    )
    assert len(asked) == 1
    assert result["formula_confirmed"]
    assert result["formula"] == "未税金额 / 数量"
    assert not state["unresolved_terms"]


@pytest.mark.asyncio
async def test_context_override_is_not_claimed_as_saved_or_user_confirmed():
    record = {
        "id": "price",
        "term": "平均销售单价",
        "definition": "单位销售金额",
        "formula": "含税金额 / 数量",
        "formula_confirmed": True,
        "source_type": "user",
    }
    store = MemoryStore([record.copy()])

    async def llm(prompt):
        return preflight_fixture_response(prompt, [
            {
                "term": "平均销售单价",
                "decision": "resolved",
                "record_id": "price",
                "definition": "单位销售金额",
                "formula": "未税金额 / 数量",
            }
        ])

    async def callback(kind, payload):
        assert kind != "question.asked"

    state = {"conv_id": "screening"}
    assert await TermClarifier(state, callback, llm, store=store).preflight(
        "平均销售单价按未税金额除以数量计算", "含税金额,未税金额,数量"
    )
    adopted = state["confirmed_terms"][0]
    assert not adopted["saved"] and not adopted["formula_confirmed"]
    assert adopted["source_type"] == "model"
    assert store.records == [record]


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["resolved", "missing_data"])
async def test_missing_definition_on_clear_quantity_does_not_stop_preflight(decision):
    names = ["销售额", "毛利率", "毛利额", "材料成本单价", "工费单价"]
    raw = [
        {"term": name, "decision": "resolved", "definition": name + "的明确含义"}
        for name in names
    ]
    raw.append(
        {
            "term": "数量",
            "decision": decision,
            "reason": "常见业务指标，含义明确，无需解释",
            "formula": "",
            "record_id": "",
            "candidates": [],
        }
    )

    async def llm(prompt):
        return preflight_fixture_response(prompt, raw)

    async def callback(kind, payload):
        assert kind != "question.asked"

    state = {"conv_id": "missing-definition-regression"}
    store = MemoryStore()
    assert await TermClarifier(state, callback, llm, store=store).preflight(
        "我定义毛利额=销售额-材料成本单价*数量-工费单价*数量，"
        "毛利率=毛利额/销售额。请输出客户每月的销售额和毛利率。",
        "",
    )
    assert [t["term"] for t in state["confirmed_terms"]] == names
    assert state["term_check_pending"] is False
    assert not state["unresolved_terms"]
    assert not state.get("term_stop_reason")
    assert store.records == []


@pytest.mark.asyncio
@pytest.mark.parametrize("automatic", [False, True])
async def test_legacy_snapshot_adoption_source_is_explicit_without_history_rewrite(automatic):
    snapshot = {
        "term": "ARR", "definition": "会计收益率", "datasource": "",
        "formula_confirmed": False, "saved": False,
    }
    if automatic:
        snapshot["clarification_required"] = False
    state = {"conv_id": "legacy-source", "confirmed_terms": [snapshot]}

    async def callback(kind, payload):
        assert kind != "question.asked"

    assert await TermClarifier(
        state, callback, empty_llm, store=MemoryStore()
    ).preflight("只解释ARR，不计算", "")
    assert state["confirmed_terms"][0]["adopted_by"] == (
        "context" if automatic else "user"
    )
    assert "adopted_by" not in snapshot


def test_user_confirmed_formula_keeps_confirmed_status_with_automatic_meaning():
    description = term_scope_description({
        "formula": "净收益/成本", "formula_confirmed": True,
        "adopted_by": "context",
    })
    assert "已确认" in description
    assert "自动采用" not in description


@pytest.mark.asyncio
async def test_json_boundary_diagnostic_does_not_log_model_body(caplog):
    from dbgpt.core import ModelOutput
    from dbgpt_app.openapi.api_v1.terms.service import make_json_llm

    marker = "private_format_diagnostic_marker"

    class Client:
        async def generate(self, request):
            return ModelOutput(text='[{"definition":"' + marker, error_code=0,
                               finish_reason="length")

    with pytest.raises(json.JSONDecodeError):
        await make_json_llm(Client(), "model")("non-sensitive test prompt")
    assert "finish=length" in caplog.text
    assert "position=" in caplog.text
    assert marker not in caplog.text


@pytest.mark.asyncio
async def test_thinking_only_preflight_is_format_failure_not_request_failure(caplog):
    from dbgpt.core import ModelOutput
    from dbgpt_app.openapi.api_v1.terms.service import make_json_llm
    class Client:
        async def generate(self, request):
            return ModelOutput.build(text=None, thinking="PRIVATE_THINKING_ONLY", finish_reason="length")
    with pytest.raises(json.JSONDecodeError):
        await make_json_llm(Client(), "model")("synthetic")
    assert "PRIVATE_THINKING_ONLY" not in caplog.text
    assert "finish=length" in caplog.text
