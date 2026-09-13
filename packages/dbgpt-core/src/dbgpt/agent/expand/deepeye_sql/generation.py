"""N-version SQL generation (Stage 2 of DeepEye-SQL).

Three independent generators — skeleton, in-context-learning (ICL) and
divide-and-conquer — run in parallel, mirroring N-version programming rather
than stochastic self-consistency sampling. Candidate order is
``dc + skeleton + icl`` to match the reference implementation.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, List, Optional, Tuple

from .llm import LLMComplete
from .prompts import (
    render_few_shot_examples,
    render_generation_prompt,
)
from .schemas import CandidateSQL
from .util import extract_llm_sql

logger = logging.getLogger(__name__)

GENERATOR_NAMES = ("skeleton", "icl", "divide_conquer")
# Output order in the reference implementation.
_OUTPUT_ORDER = ("divide_conquer", "skeleton", "icl")


class NVersionGenerator:
    """Generate N candidate SQL queries from diverse reasoning paradigms."""

    def __init__(
        self,
        complete: LLMComplete,
        connector: Any,
        *,
        temperature: float = 0.7,
        sampling_budget: int = 1,
    ):
        self._complete = complete
        self._connector = connector
        self._temperature = temperature
        self._sampling_budget = sampling_budget

    @property
    def dialect(self) -> str:
        return (getattr(self._connector, "dialect", "") or "").lower()

    async def _run_generator(
        self,
        name: str,
        question: str,
        hint: str,
        database_schema: str,
        few_shot_examples: Optional[List[tuple]],
    ) -> List[CandidateSQL]:
        few_shot_block = (
            render_few_shot_examples(few_shot_examples) if few_shot_examples else ""
        )
        prompt = render_generation_prompt(
            name,
            question,
            hint,
            database_schema,
            self.dialect,
            few_shot_examples=few_shot_block,
        )
        candidates: List[CandidateSQL] = []
        for _ in range(self._sampling_budget):
            try:
                raw = await self._complete.complete(
                    [{"role": "user", "content": prompt}],
                    temperature=self._temperature,
                    max_new_tokens=2048,
                )
                sql = extract_llm_sql(raw)
                if sql:
                    candidates.append(CandidateSQL(sql=sql, generator=name, index=0))
            except Exception as e:  # pragma: no cover - depends on LLM
                logger.warning("%s generator failed: %s", name, e)
        return candidates

    async def generate(
        self,
        question: str,
        hint: str,
        database_schema: str,
        *,
        few_shot_examples: Optional[List[tuple]] = None,
    ) -> List[CandidateSQL]:
        """Generate SQL candidates in parallel from the three generators."""
        results = await asyncio.gather(
            self._run_generator("skeleton", question, hint, database_schema, few_shot_examples),
            self._run_generator("icl", question, hint, database_schema, few_shot_examples),
            self._run_generator(
                "divide_conquer", question, hint, database_schema, few_shot_examples
            ),
        )
        by_name = {
            "skeleton": results[0],
            "icl": results[1],
            "divide_conquer": results[2],
        }
        candidates: List[CandidateSQL] = []
        for name in _OUTPUT_ORDER:
            candidates.extend(by_name.get(name, []))
        for i, cand in enumerate(candidates):
            cand.index = i
        return candidates
