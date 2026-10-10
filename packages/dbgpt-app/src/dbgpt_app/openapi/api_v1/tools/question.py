"""question tool — ask user questions and block until answered."""

import asyncio
import json
import logging
import re
from typing import Any, Callable, Dict

from dbgpt.agent.resource.tool.base import tool

from .question_manager import question_manager

logger = logging.getLogger(__name__)

_DESCRIPTION = """\
Use this tool when you need to ask the user questions during \
execution. This allows you to:
1. Gather user preferences or requirements
2. Clarify ambiguous instructions
3. Get decisions on implementation choices as you work
4. Offer choices to the user about what direction to take.

Usage notes:
- IMPORTANT: Always write the question text, header, option \
labels, and descriptions in the SAME language the user is using. \
If the user writes in English, all content must be in English; \
if in Chinese, use Chinese, etc.
- When `custom` is enabled (default), a "Type your own answer" \
option is added automatically; don't include "Other" or \
catch-all options
- Answers are returned as arrays of labels; set `multiple: true` \
to allow selecting more than one
- If you recommend a specific option, make that the first option \
in the list and add "(Recommended)" at the end of the label
- Do not repeat a business-term meaning already confirmed in this conversation.
  Only ask about concrete unresolved conflicts; do not ask for routine formulas.
  Use clarify_term with aspect="formula" and evidence for a new actual conflict.
- Missing input data is not a definition conflict. Under explain policy keep full
  metrics NULL, explain actual missing data and finish the remaining report.
  Never offer filling missing costs with zero or dropping real sales as a fix.
Parameter: {"questions": [{"question": "...", "header": "...", \
"options": [{"label": "...", "description": "..."}], \
"multiple": false}]}
"""


def make_question(react_state: Dict[str, Any], stream_callback: Callable):
    """Return a ``question`` FunctionTool bound to react_state and stream_callback."""

    @tool(description=_DESCRIPTION)
    async def question(questions: str) -> str:
        """Ask the user one or more questions and wait for their answers.

        Args:
            questions: JSON string of a list of question objects. Each question has:
                - question (str): Complete question text
                - header (str): Very short label (max 30 chars)
                - options (list): [{label, description}] available choices
                - multiple (bool, optional): allow multi-select
        """
        conv_id = react_state.get("conv_id", "default")

        # Parse questions JSON
        try:
            parsed_questions = (
                json.loads(questions) if isinstance(questions, str) else questions
            )
            if not isinstance(parsed_questions, list):
                parsed_questions = parsed_questions.get("questions", [])
        except Exception as e:
            return json.dumps(
                {
                    "chunks": [
                        {
                            "output_type": "text",
                            "content": f"Error: invalid questions JSON — {e}",
                        }
                    ]
                },
                ensure_ascii=False,
            )

        from ..terms.service import confirmed_question_terms

        remaining = []
        adopted = []
        data_policy_applied = False
        for item in parsed_questions:
            question_text = item.get("header", "") + " " + item.get("question", "")
            ratios = react_state.get("task_requirements", {}).get("ratios", [])
            if (
                ratios
                and all(r.get("unavailable_policy") == "explain" for r in ratios)
                and re.search(r"缺失|缺少|NULL|missing", question_text, re.I)
                and not re.search(
                    r"冲突|不同价格|多个价格|conflict", question_text, re.I
                )
            ):
                known_missing = False
                for snapshot in react_state.get("validated_results", {}).values():
                    if (
                        snapshot.get("conv_id") != conv_id
                        or snapshot.get("database")
                        != react_state.get("term_datasource", "")
                        or snapshot.get("source") != "sql"
                    ):
                        continue
                    fields = snapshot.get("columns", [])
                    for index, field in enumerate(fields):
                        if field in question_text and any(
                            row[index] is None for row in snapshot.get("rows", [])
                        ):
                            known_missing = True
                if known_missing:
                    data_policy_applied = True
                    continue
            matches, aspect = confirmed_question_terms(react_state, item)
            if matches and aspect == "formula":
                if react_state.get("term_explanation_only") or all(
                    (
                        t.get("clarification_required") is False
                        or t.get("formula_confirmed")
                    )
                    and t.get("datasource", "")
                    == react_state.get("term_datasource", "")
                    for t in matches
                ):
                    adopted.extend(matches)
                    continue
                unresolved = react_state.setdefault("unresolved_terms", [])
                for match in matches:
                    name = match["term"] + "（计算口径）"
                    if name not in unresolved:
                        unresolved.append(name)
                return json.dumps(
                    {
                        "success": False,
                        "error": (
                            "含义已确认。请用clarify_term并设置aspect=formula，"
                            "单独确认和保存计算口径。"
                        ),
                        "chunks": [
                            {
                                "output_type": "text",
                                "content": (
                                    "不要重新选择含义；用clarify_term"
                                    "(term, reason, aspect='formula')澄清口径。"
                                ),
                            }
                        ],
                    },
                    ensure_ascii=False,
                )
            if matches:
                adopted.extend(matches)
            else:
                remaining.append(item)
        if data_policy_applied and not remaining:
            return json.dumps(
                {
                    "success": True,
                    "chunks": [
                        {
                            "output_type": "text",
                            "content": (
                                "已查询确认输入缺失，按当前explain交付政策继续，无需询问是否接受NULL。"
                                "完整指标保持NULL，单列成本完整子集及实际缺失说明。"
                                "保留全部原销售行，不填0，不丢销售行；继续结果校验和要求的报告。"
                                "这是既定政策，不是用户选择了新口径。"
                            ),
                        }
                    ],
                },
                ensure_ascii=False,
            )
        if adopted and not remaining:
            return json.dumps(
                {
                    "success": True,
                    "chunks": [
                        {
                            "output_type": "text",
                            "content": (
                                "以下名词的含义已经确定（用户确认或上下文明确），不要重复询问；"
                                "按这些定义继续当前任务："
                                "若原需求只是说明名词，不需要补问计算口径，不进行实际数据计算。"
                            )
                            + json.dumps(adopted, ensure_ascii=False),
                        }
                    ],
                },
                ensure_ascii=False,
            )
        parsed_questions = remaining

        # 1. Register in QuestionManager → creates asyncio.Event
        pq = question_manager.create(conv_id=conv_id, questions=parsed_questions)

        # 2. Push question.asked SSE event to frontend
        await stream_callback(
            "question.asked",
            {
                "request_id": pq.request_id,
                "conv_id": conv_id,
                "questions": parsed_questions,
            },
        )
        logger.info(
            "question tool: pushed question.asked, request_id=%s", pq.request_id
        )

        # 3. Block until user answers (or timeout)
        try:
            await asyncio.wait_for(pq.event.wait(), timeout=300)
        except asyncio.TimeoutError:
            question_manager.remove(pq.request_id)
            await stream_callback(
                "question.rejected", {"request_id": pq.request_id, "conv_id": conv_id}
            )
            return json.dumps(
                {
                    "chunks": [
                        {
                            "output_type": "text",
                            "content": (
                                "Question timed out after 300 seconds."
                                " Proceeding without user answer."
                            ),
                        }
                    ]
                },
                ensure_ascii=False,
            )
        finally:
            question_manager.remove(pq.request_id)

        # 4. User rejected
        if pq.rejected:
            return json.dumps(
                {
                    "chunks": [
                        {
                            "output_type": "text",
                            "content": (
                                "The user dismissed the question."
                                " Proceeding without answer."
                            ),
                        }
                    ]
                },
                ensure_ascii=False,
            )

        # 5. Format answers for the LLM
        answers = pq.answers or []
        parts = []
        for i, q in enumerate(parsed_questions):
            q_text = q.get("question", "")
            a_text = (
                ", ".join(answers[i])
                if i < len(answers) and answers[i]
                else "Unanswered"
            )
            parts.append(f'"{q_text}"="{a_text}"')
        formatted = ", ".join(parts)
        output = (
            f"User has answered your questions: {formatted}."
            " You can now continue with the user's answers"
            " in mind."
        )
        return json.dumps(
            {"chunks": [{"output_type": "text", "content": output}]},
            ensure_ascii=False,
        )

    return question
