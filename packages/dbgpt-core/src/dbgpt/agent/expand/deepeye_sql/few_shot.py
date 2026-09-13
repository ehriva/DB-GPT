"""Dynamic few-shot retrieval for the ICL generator and reversed linker.

The reference implementation masks ``(question, evidence, SQL)`` triples with
an LLM, embeds them, and retrieves the top-K cross-domain examples by a
weighted question/SQL similarity score. This module reproduces that behaviour
with graceful fallbacks: deterministic regex masking when no LLM is provided,
and token-overlap scoring when no embedder is provided.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, List, Optional, Sequence, Tuple

from .embeddings import Embedder, cosine_similarity
from .llm import LLMComplete
from .util import extract_json

logger = logging.getLogger(__name__)

MASK_SYSTEM_PROMPT = (
    "You produce abstract retrieval keys for text-to-SQL few-shot selection. "
    "Return only a compact JSON object and no explanation."
)

MASK_USER_PROMPT_TEMPLATE = """\
Mask database-specific names and literal values while preserving query intent.

Rules:
- Treat the Question and Evidence as one retrieval query.
- In the question, replace schema names, entity names, literal values, numbers, dates, and domain-specific nouns with generic placeholders such as <entity>, <value>, <number>, <date>, or <concept>.
- If Evidence is provided and useful, fold a masked version of it into masked_question as a short "Hint: ..." line. Do not return a separate evidence field.
- In the SQL, replace every table name, column name, alias, string literal, numeric literal, and date/time literal with placeholders.
- Use SQL placeholders such as <table>, <column>, <alias>, <value>, <number>, and <date>.
- The masked_sql MUST NOT contain original table names, original column names, aliases, string literals, numeric literals, or date/time literals from the input SQL.
- Preserve SQL operators, aggregation functions, GROUP BY / HAVING / ORDER BY / LIMIT, joins, subqueries, set operators, and comparison logic.
- Keep the masked SQL syntactically recognizable enough to compare query skeletons.
- Return exactly this JSON schema: {{"masked_question": "...", "masked_sql": "..."}}

Examples:
Input:
Question: Who is the director of the movie Sex, Drink and Bloodshed?
Evidence: None
SQL: SELECT director_name FROM movies WHERE movie_title = 'Sex, Drink and Bloodshed'
Output:
{{"masked_question": "Who is the <concept> of the movie <value>?", "masked_sql": "SELECT <column> FROM <table> WHERE <column> = <value>"}}

Input:
Question: Which department has the most heads older than 56?
Evidence: head means department head.
SQL: SELECT department_id FROM head WHERE age > 56 GROUP BY department_id ORDER BY COUNT(*) DESC LIMIT 1
Output:
{{"masked_question": "Which <entity> has the most <entity> older than <number>?\\nHint: <entity> means <entity>.", "masked_sql": "SELECT <column> FROM <table> WHERE <column> > <number> GROUP BY <column> ORDER BY COUNT(*) DESC LIMIT <number>"}}

Question:
{question}

Evidence:
{evidence}

SQL:
{sql}
"""


@dataclass
class FewShotExample:
    question: str
    evidence: str = ""
    sql: str = ""
    masked_question: str = ""
    masked_sql: str = ""
    question_embedding: List[float] = field(default_factory=list)
    sql_embedding: List[float] = field(default_factory=list)


def deterministic_mask(
    question: str, evidence: str = "", sql: Optional[str] = None
) -> Tuple[str, str]:
    """Regex-based masking fallback (no LLM)."""
    def _mask_text(text: str, value_tok: str, number_tok: str, date_tok: str) -> str:
        text = re.sub(r"'\d{4}-\d{2}-\d{2}[^']*'", date_tok, text)
        text = re.sub(r"'[^']*'", value_tok, text)
        text = re.sub(r"\b\d+(\.\d+)?\b", number_tok, text)
        return text

    mq = _mask_text(question, "<value>", "<number>", "<date>")
    if evidence:
        mq += f"\nHint: {_mask_text(evidence, '<value>', '<number>', '<date>')}"
    ms = _mask_text(sql or "", "<value>", "<number>", "<date>") if sql else ""
    return mq, ms


async def mask_with_llm(
    complete: LLMComplete,
    question: str,
    evidence: str = "",
    sql: Optional[str] = None,
) -> Tuple[str, str]:
    """Mask a (question, evidence, sql) triple with the LLM."""
    prompt = MASK_USER_PROMPT_TEMPLATE.format(
        question=question, evidence=evidence or "None", sql=sql or ""
    )
    try:
        raw = await complete.complete(
            [
                {"role": "system", "content": MASK_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            temperature=0.0,
            max_new_tokens=1024,
        )
        data = extract_json(raw)
        return str(data.get("masked_question", question)), str(
            data.get("masked_sql", sql or "")
        )
    except Exception as e:  # pragma: no cover - depends on LLM
        logger.warning("LLM masking failed (%s); using deterministic mask", e)
        return deterministic_mask(question, evidence, sql)


def load_few_shot_examples(path: str) -> List[Tuple[str, str, str]]:
    """Load few-shot examples from a JSONL or JSON file.

    Each record is ``{"question": str, "evidence": str, "sql": str}`` (``evidence``
    optional). Returns ``(question, evidence, sql)`` tuples.
    """
    examples: List[Tuple[str, str, str]] = []
    with open(path, encoding="utf-8") as f:
        if path.endswith(".jsonl"):
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                examples.append(
                    (rec["question"], rec.get("evidence", ""), rec["sql"])
                )
        else:
            data = json.load(f)
            for rec in data:
                examples.append(
                    (rec["question"], rec.get("evidence", ""), rec["sql"])
                )
    return examples


class FewShotRetriever:
    """Mask, embed and retrieve cross-domain few-shot examples."""

    def __init__(
        self,
        examples: Sequence[Tuple[str, str, str]],
        complete: Optional[LLMComplete] = None,
        embedder: Optional[Embedder] = None,
        *,
        num_examples: int = 5,
        question_weight: float = 0.6,
        sql_weight: float = 0.4,
    ):
        self._complete = complete
        self._embedder = embedder
        self._num_examples = num_examples
        self._question_weight = question_weight
        self._sql_weight = sql_weight
        self._examples: List[FewShotExample] = [
            FewShotExample(question=q, evidence=e, sql=s) for q, e, s in examples
        ]
        self._built = False

    async def build(self) -> None:
        """Mask and embed all examples (idempotent)."""
        if self._built:
            return
        for ex in self._examples:
            if self._complete is not None:
                ex.masked_question, ex.masked_sql = await mask_with_llm(
                    self._complete, ex.question, ex.evidence, ex.sql
                )
            else:
                ex.masked_question, ex.masked_sql = deterministic_mask(
                    ex.question, ex.evidence, ex.sql
                )
        if self._embedder is not None:
            for ex in self._examples:
                ex.question_embedding = self._embedder.embed_query(ex.masked_question)
                if ex.masked_sql:
                    ex.sql_embedding = self._embedder.embed_query(ex.masked_sql)
        self._built = True

    async def retrieve(
        self,
        question: str,
        evidence: str = "",
        preliminary_sql: Optional[str] = None,
    ) -> List[Tuple[str, str, str]]:
        """Retrieve the top-K examples for a query."""
        await self.build()
        if self._complete is not None:
            mq, ms = await mask_with_llm(self._complete, question, evidence, preliminary_sql)
        else:
            mq, ms = deterministic_mask(question, evidence, preliminary_sql)

        scored: List[Tuple[float, int]] = []
        if self._embedder is not None:
            qv = self._embedder.embed_query(mq)
            sv = self._embedder.embed_query(ms) if ms else []
            for i, ex in enumerate(self._examples):
                q_sim = cosine_similarity(qv, ex.question_embedding) if ex.question_embedding else 0.0
                s_sim = (
                    cosine_similarity(sv, ex.sql_embedding)
                    if sv and ex.sql_embedding
                    else 0.0
                )
                score = self._question_weight * q_sim + self._sql_weight * s_sim
                scored.append((score, i))
        else:
            q_tokens = _tokens(mq + " " + ms)
            for i, ex in enumerate(self._examples):
                ex_tokens = _tokens(ex.masked_question + " " + ex.masked_sql)
                overlap = len(q_tokens & ex_tokens)
                union = max(1, len(q_tokens | ex_tokens))
                scored.append((overlap / union, i))

        scored.sort(key=lambda x: (-x[0], x[1]))
        return [
            (self._examples[i].question, self._examples[i].evidence, self._examples[i].sql)
            for _, i in scored[: self._num_examples]
        ]


def _tokens(text: str) -> set:
    return set(re.findall(r"[a-z0-9]+", text.lower()))
