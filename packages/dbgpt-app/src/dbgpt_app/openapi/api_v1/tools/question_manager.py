"""QuestionManager — pending question state with asyncio.Event."""

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class QuestionOption:
    label: str
    description: str


@dataclass
class QuestionInfo:
    question: str
    header: str
    options: List[QuestionOption]
    multiple: bool = False
    custom: bool = True


@dataclass
class PendingQuestion:
    request_id: str
    conv_id: str
    questions: List[dict]  # raw dicts from LLM JSON for easy serialization
    event: asyncio.Event = field(default_factory=asyncio.Event)
    answers: Optional[List[List[str]]] = None
    rejected: bool = False
    decisions: Dict[str, Any] = field(default_factory=dict)


class QuestionManager:
    """Global manager for all pending question requests across sessions.

    Each question tool invocation:
    1. Calls ``create()`` → gets a PendingQuestion with a fresh asyncio.Event.
    2. Pushes a ``question.asked`` SSE event to the frontend.
    3. Awaits ``pending.event.wait()`` — blocks the tool coroutine.
    4. When the user replies via HTTP, ``reply()`` sets the event and stores answers.
    5. The tool coroutine wakes up, reads the answers, and returns to the LLM.
    """

    def __init__(self) -> None:
        self._pending: Dict[str, PendingQuestion] = {}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _new_id(self) -> str:
        return f"que_{uuid.uuid4().hex[:12]}"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create(
        self,
        conv_id: str,
        questions: List[dict],
        request_id: Optional[str] = None,
    ) -> PendingQuestion:
        rid = request_id or self._new_id()
        pq = PendingQuestion(request_id=rid, conv_id=conv_id, questions=questions)
        self._pending[rid] = pq
        logger.info("QuestionManager.create: request_id=%s, n=%d", rid, len(questions))
        return pq

    def reply(
        self,
        request_id: str,
        answers: List[List[str]],
        conv_id: Optional[str] = None,
        decisions: Optional[Dict[str, Any]] = None,
    ) -> None:
        pq = self._pending.get(request_id)
        if not pq:
            raise KeyError(f"No pending question: {request_id}")
        self._validate(pq, conv_id)
        if len(answers) != len(pq.questions):
            raise ValueError("回答数量与问题数量不符。")
        for question, answer in zip(pq.questions, answers):
            labels = [o["label"] for o in question.get("options", [])]
            if not answer or (not question.get("multiple") and len(answer) != 1):
                raise ValueError("请为每个问题提交有效回答。")
            if any(
                not isinstance(a, str)
                or not a.strip()
                or (len(a) > 4000 and a not in labels)
                for a in answer
            ):
                raise ValueError("回答不能为空或过长。")
            if question.get("custom") is False and any(a not in labels for a in answer):
                raise ValueError("回答不在可选项中。")
        pq.answers = answers
        pq.decisions = decisions or {}
        pq.event.set()
        logger.info("QuestionManager.reply: request_id=%s answered", request_id)

    def reject(self, request_id: str, conv_id: Optional[str] = None) -> None:
        pq = self._pending.get(request_id)
        if not pq:
            raise KeyError(f"No pending question: {request_id}")
        self._validate(pq, conv_id)
        pq.rejected = True
        pq.event.set()
        logger.info("QuestionManager.reject: request_id=%s rejected", request_id)

    @staticmethod
    def _validate(pq: PendingQuestion, conv_id: Optional[str]) -> None:
        if any(q.get("kind") == "term" for q in pq.questions) and conv_id != pq.conv_id:
            raise ValueError("回答与当前会话不匹配。")
        if pq.event.is_set():
            raise ValueError("此问题已经回答或取消。")

    def remove(self, request_id: str) -> None:
        self._pending.pop(request_id, None)

    def list_pending(self, conv_id: Optional[str] = None) -> List[dict]:
        result = []
        for pq in self._pending.values():
            if conv_id is None or pq.conv_id == conv_id:
                result.append(
                    {
                        "request_id": pq.request_id,
                        "conv_id": pq.conv_id,
                        "questions": pq.questions,
                    }
                )
        return result


# ---------------------------------------------------------------------------
# Module-level singleton — same pattern as _todo_list / REACT_AGENT_MEMORY_CACHE
# ---------------------------------------------------------------------------
question_manager = QuestionManager()
