"""Completion checks and progress handling for the interactive main agent."""

import asyncio
import json
import re
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from dbgpt._private.pydantic import PrivateAttr
from dbgpt.agent.core.agent import AgentMessage
from dbgpt.agent.expand.tool_calling_agent import ToolCallingReActAgent

from .react_final import TaskResult


def report_requirements(user_input: str) -> Tuple[bool, bool]:
    """Identify the explicitly supported report and database-profile requests."""
    text = user_input.casefold()
    profile_topic = any(
        word in text for word in ("数据库画像", "database profile")
    ) or (
        any(word in text for word in ("表结构", "table structure", "schema"))
        and any(word in text for word in ("数据量", "row count", "字段", "column"))
    )
    generation = re.search(
        r"生成|制作|创建|输出|渲染|做一份|做一个|给我一份|\b(generate|create|build|render|produce|make)\b",
        text,
    )
    if generation and re.search(
        r"(不要|无需|不用|不必|不需要|do not|don't|no need to)\s*$",
        text[: generation.start()],
    ):
        generation = None
    profile = profile_topic and (
        generation is not None
        or any(word in text for word in ("分析", "获取", "inspect", "analyze"))
    )
    report = generation is not None and any(
        word in text
        for word in ("报告", "report", "网页", "webpage", "web page", "html")
    )
    return report, profile


def data_backfill_requested(text):
    for match in re.finditer(
        r"(?:补齐|补录|补全|补充|获取缺失)[^，,。；;\n]{0,24}?(?:工费|成本|数据|输入)",
        text,
    ):
        if re.search(
            r"(?:不要|无需|不用|不必|不需要|取消)(?:再)?\s*$", text[: match.start()]
        ):
            continue
        if re.match(
            r"(?:数据)?(?:缺失)?(?:的)?(?:说明|原因|提示)", text[match.end() :]
        ):
            continue
        return True
    return False


def explained_backfill_step(state, content):
    ratios = state.get("task_requirements", {}).get("ratios", [])
    return (
        not data_backfill_requested(state.get("original_user_input", ""))
        and bool(ratios)
        and all(r.get("unavailable_policy") == "explain" for r in ratios)
        and any(
            warning.get("reason") == "missing_input"
            and state.get("ratio_validation_status", {}).get(metric) == "missing_inputs"
            for metric, warning in state.get("data_warnings", {}).items()
        )
        and bool(
            re.search(r"(?:补齐|补录|补充|获取缺失).*(?:数据|工费|成本|输入)", content)
        )
    )


def missing_deliverables(state: Dict[str, Any]) -> List[str]:
    missing: List[str] = []
    if state.get("term_check_pending"):
        missing.append("完成必要名词检查")
    missing.extend("确认名词：" + term for term in state.get("unresolved_terms", []))
    missing.extend(state.get("query_data_gaps", {}).values())
    for task in state.get("task_plan", []):
        if task.get("status") != "completed" and explained_backfill_step(
            state, task.get("content", "")
        ):
            task["status"] = "completed"
            task["content"] = "已交付数据限制说明（" + task["content"] + "）"
        if task.get("status") in {"pending", "in_progress"}:
            missing.append("完成任务计划：" + task.get("content", "未完成步骤"))
        elif task.get("status") == "cancelled":
            missing.append("未交付已取消步骤：" + task.get("content", "未完成步骤"))
    for name in state.get("cancelled_required_steps", []):
        if explained_backfill_step(state, name):
            continue
        item = "未交付已取消步骤：" + name
        if item not in missing:
            missing.append(item)
    if state.get("requires_database_profile") and not state.get("database_profile"):
        missing.append("通过 database_profile 获取各表结构、字段和数据量统计")
    if state.get("requires_report"):
        report = state.get("rendered_report")
        if not isinstance(report, dict) or not Path(report.get("path", "")).is_file():
            missing.append("通过 html_interpreter 生成并交付完整 HTML 报告")
        elif report.get("missing"):
            missing.extend(report["missing"])
    return missing


def task_result_for_reply(reply: AgentMessage, state: Dict[str, Any]) -> TaskResult:
    missing = tuple(missing_deliverables(state))
    warnings = tuple(state.get("data_warnings", {}).values())
    if state.get("term_stop_reason"):
        return TaskResult("incomplete", state["term_stop_reason"], missing)
    reason = reply.get_dict_context().get("termination_reason", "interrupted")
    if reason == "user_cancelled":
        return TaskResult("cancelled", reason, missing)
    if reply.success and reply.action_report and reply.action_report.terminate:
        if not missing:
            return TaskResult("succeeded", "completed", warnings=warnings)
        return TaskResult("incomplete", "missing_deliverables", missing, warnings)
    status = "failed" if reason in {"runtime_error", "tool_failure"} else "incomplete"
    return TaskResult(status, reason, missing)


def incomplete_summary(
    result: TaskResult, state: Optional[Dict[str, Any]] = None
) -> str:
    descriptions = {
        "term_cancelled": "你取消了必要名词确认，相关分析未执行。",
        "term_timeout": "必要名词确认超时，相关分析未执行。",
        "scope_cancelled": "你取消了必要任务范围确认，原要求已保留，相关分析未执行。",
        "scope_timeout": "必要任务范围确认超时，原要求已保留，相关分析未执行。",
        "term_check_failed": "名词检查失败，请重试；相关分析未执行。",
        "term_unresolved": "必要名词尚未确认，相关分析未执行。",
        "term_formula_unconfirmed": "必要计算口径尚未确认，相关分析未执行。",
        "no_progress": "连续执行没有取得新结果，已停止。",
        "tool_failure": "工具执行失败，无法继续。",
        "runtime_error": "执行发生错误，未能交付结果。",
        "missing_deliverables": "必要步骤尚未交付，保留当前进度。",
        "timeout": "执行超时，尚未交付结果。",
        "step_limit": "执行达到步数限制，尚未交付结果。",
        "report_validation_failed": "报告交付检查多次失败，尚未交付合格报告。",
    }
    if result.reason == "term_check_failed" and state:
        descriptions["term_check_failed"] = {
            "model_timeout": "名词检查模型响应超时，可重新发起；相关分析未执行。",
            "model_format": "模型返回的名词格式不符合要求，可重试；相关分析未执行。",
            "term_store": "共享术语库读取失败，请检查存储；相关分析未执行。",
            "model_request": "名词检查模型调用失败，可重试；相关分析未执行。",
        }.get(state.get("term_check_error_code"), descriptions["term_check_failed"])
    text = "任务未完成：" + descriptions.get(result.reason, "执行已中断。")
    if result.missing:
        text += "\n\n尚需完成：\n" + "\n".join(f"- {item}" for item in result.missing)
    return text + "\n\n已完成的步骤和文件保留在本次任务中。"


class TaskReActAgent(ToolCallingReActAgent):
    """Main-task loop with delivery validation and finite no-progress recovery."""

    _task_state: Dict[str, Any] = PrivateAttr(default_factory=dict)
    _recent_results: Any = PrivateAttr(default_factory=lambda: deque(maxlen=6))
    _consecutive_failures: int = PrivateAttr(default=0)
    _report_failures: int = PrivateAttr(default=0)

    def __init__(self, *, task_state: Dict[str, Any], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._task_state = task_state

    async def thinking(self, *args: Any, **kwargs: Any):
        # A single hung provider request must not keep the task alive forever.
        for attempt in range(3):
            try:
                return await asyncio.wait_for(
                    super().thinking(*args, **kwargs), timeout=180
                )
            except asyncio.TimeoutError:
                if attempt == 2:
                    raise

    async def verify(
        self, message: AgentMessage, sender: Any, reviewer: Any = None, **kwargs: Any
    ) -> Tuple[bool, Optional[str]]:
        passed, reason = await super().verify(message, sender, reviewer, **kwargs)
        output = message.action_report
        if output is None:
            return passed, reason
        if self._task_state.get("term_stop_reason"):
            output.have_retry = False
            output.is_exe_success = False
            message.context = {
                **message.get_dict_context(),
                "termination_reason": self._task_state["term_stop_reason"],
            }
            return False, "必要名词未确认，停止相关任务。"

        observation = output.observations or output.content
        feedback_added = False
        try:
            payload = json.loads(observation)
        except (TypeError, json.JSONDecodeError):
            payload = None
        payloads = [payload]
        if isinstance(payload, list):
            payloads = []
            for item in payload:
                if not isinstance(item, dict):
                    continue
                try:
                    payloads.append(json.loads(item.get("observation") or "null"))
                except (TypeError, json.JSONDecodeError):
                    continue
        for result in payloads:
            if isinstance(result, dict) and result.get("success") is False:
                passed = False
                details = "\n".join(
                    str(chunk.get("content", ""))
                    for chunk in result.get("chunks", [])
                    if isinstance(chunk, dict) and chunk.get("output_type") == "text"
                )
                reason = result.get("error") or details[:4000] or "工具执行失败。"
                break
        if output.action == "html_interpreter":
            self._report_failures = 0 if passed else self._report_failures + 1
            if self._report_failures >= 5:
                output.have_retry = False
                message.context = {
                    **message.get_dict_context(),
                    "termination_reason": "report_validation_failed",
                }

        if passed and output.terminate:
            missing = missing_deliverables(self._task_state)
            if missing:
                if (
                    any(
                        self._task_state.get("ratio_validation_status", {}).get(label)
                        == "missing_inputs"
                        for label in self._task_state.get("query_data_gaps", {})
                    )
                    or self._task_state.get("cancelled_required_steps")
                    or any(
                        t.get("status") == "cancelled"
                        for t in self._task_state.get("task_plan", [])
                    )
                ):
                    output.have_retry = False
                    output.is_exe_success = False
                    message.context = {
                        **message.get_dict_context(),
                        "termination_reason": "missing_deliverables",
                    }
                    return False, "必要输入缺失或步骤已取消，不能宣称交付完成。"
                passed = False
                reason = "交付检查未通过，请继续完成：" + "；".join(missing)
                output.terminate = False
                feedback_added = True
            else:
                return True, None

        # Result filenames change per call; compare the actual data preview.
        comparable = [
            result.get("preview", result.get("chunks"))
            if isinstance(result, dict)
            else result
            for result in payloads
        ]
        if payload is None:
            comparable = observation
        if output.action == "html_interpreter" and isinstance(payload, dict):
            comparable = [
                chunk.get("content")
                for chunk in payload.get("chunks", [])
                if isinstance(chunk, dict) and chunk.get("output_type") == "html"
            ]
        signature = (
            output.action,
            output.action_input,
            json.dumps(comparable, ensure_ascii=False, sort_keys=True),
        )
        self._recent_results.append(signature)
        recent = list(self._recent_results)
        repeated = recent.count(signature) >= 3
        self._consecutive_failures = 0 if passed else self._consecutive_failures + 1
        stalled = len(recent) >= 5 and all(item == signature for item in recent[-5:])
        if len(recent) == 6 and recent[:2] == recent[2:4] == recent[4:]:
            stalled = True
        if stalled or self._consecutive_failures >= 5:
            feedback_added = True
            passed = False
            reason = "连续执行没有取得新结果，请保留当前进度并说明缺失项。"
            output.have_retry = False
            message.context = {
                **message.get_dict_context(),
                "termination_reason": "no_progress",
            }
        elif repeated or self._consecutive_failures == 3:
            feedback_added = True
            passed = False
            reason = (reason or "重复执行得到相同结果。") + (
                " 请改变方法；SQL 完整结果可通过 QUERY_RESULT_PATH 读取，"
                "数据库画像使用 database_profile，报告使用 html_interpreter。"
            )

        if not passed:
            output.is_exe_success = False
            if feedback_added:
                output.observations = str(reason)
        return passed, reason
