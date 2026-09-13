"""DeepEye-SQL pipeline orchestrator.

Wires the SDLC-inspired stages together:

1. Semantic value retrieval
2. Robust schema linking (+ relational closure)
3. N-version SQL generation
4. Tool-chain verification + confidence-aware selection
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from .checkers import SQLToolChain
from .generation import NVersionGenerator
from .llm import LLMComplete, to_complete
from .schema_linking import RobustSchemaLinker
from .schema_profile import SchemaProfile
from .schemas import DeepEyeSQLResult
from .selection import ConfidenceAwareSelector
from .value_retrieval import SemanticValueRetriever

logger = logging.getLogger(__name__)


class DeepEyeSQLPipeline:
    """The full DeepEye-SQL Text-to-SQL pipeline."""

    def __init__(
        self,
        connector: Any,
        complete: Any,
        *,
        model_name: Optional[str] = None,
        conv_id: Optional[str] = None,
        temperature: float = 0.7,
        confidence_threshold: float = 0.6,
        sampling_budget: int = 1,
        checker_sampling_budget: int = 1,
        evaluator_sampling_budget: int = 3,
        filter_top_k: int = 2,
        max_rows: int = 100,
        include_value_stats: bool = True,
    ):
        self._connector = connector
        self._complete: LLMComplete = to_complete(
            complete, model_name=model_name, conv_id=conv_id
        )
        self._temperature = temperature
        self._confidence_threshold = confidence_threshold
        self._sampling_budget = sampling_budget
        self._checker_sampling_budget = checker_sampling_budget
        self._evaluator_sampling_budget = evaluator_sampling_budget
        self._filter_top_k = filter_top_k
        self._max_rows = max_rows
        self._include_value_stats = include_value_stats

        self._value_retriever = SemanticValueRetriever(connector, self._complete)
        self._linker = RobustSchemaLinker(
            connector,
            self._complete,
            sampling_budget=sampling_budget,
        )
        self._generator = NVersionGenerator(
            self._complete,
            connector,
            temperature=temperature,
            sampling_budget=sampling_budget,
        )

    @property
    def connector(self) -> Any:
        return self._connector

    async def run(
        self,
        question: str,
        *,
        hint: str = "",
        few_shot_examples: Optional[List[tuple]] = None,
        schema_profile: Optional[SchemaProfile] = None,
    ) -> DeepEyeSQLResult:
        """Run the pipeline end-to-end and return the quality-gated result."""
        trace: Dict[str, Any] = {}

        # Build the schema profile (value stats/examples).
        profile = schema_profile or SchemaProfile.from_connector(
            self._connector,
            include_value_stats=self._include_value_stats,
            include_value_examples=True,
        )

        # Stage 1a: Semantic value retrieval.
        retrieved = await self._value_retriever.retrieve(question, hint)
        profile.apply_value_retrieval(retrieved)
        trace["retrieved_values"] = {
            k: [v.value for v in vals] for k, vals in retrieved.values.items()
        }

        # Stage 1b: Robust schema linking.
        linked = await self._linker.link(
            question, hint, profile, retrieved, few_shot_examples=few_shot_examples
        )
        trace["linked_tables"] = sorted(linked.tables)
        trace["linking_sources"] = {
            k: sorted(v) for k, v in linked.source.items()
        }

        # Stage 2: N-version SQL generation.
        candidates = await self._generator.generate(
            question,
            hint,
            linked.schema_text,
            few_shot_examples=few_shot_examples,
        )
        trace["generated_candidates"] = len(candidates)
        if not candidates:
            return DeepEyeSQLResult(
                question=question,
                success=False,
                error="No SQL candidates were generated.",
                trace=trace,
            )

        # Stage 3: Tool-chain verification + targeted repair.
        tool_chain = SQLToolChain(
            self._connector,
            self._complete,
            question,
            hint,
            linked.schema_text,
            checker_sampling_budget=self._checker_sampling_budget,
            max_rows=self._max_rows,
        )
        candidates = await tool_chain.verify_and_repair(candidates)
        trace["verified_candidates"] = [
            {"generator": c.generator, "sql": c.sql, "passed": c.passed}
            for c in candidates
        ]
        passed = [c for c in candidates if c.passed]
        if not passed:
            return DeepEyeSQLResult(
                question=question,
                success=False,
                error="All SQL candidates failed the verification tool-chain.",
                trace=trace,
            )

        # Stage 4: Confidence-aware selection.
        selector = ConfidenceAwareSelector(
            self._connector,
            self._complete,
            question,
            hint,
            linked.schema_text,
            confidence_threshold=self._confidence_threshold,
            filter_top_k=self._filter_top_k,
            evaluator_sampling_budget=self._evaluator_sampling_budget,
            max_rows=self._max_rows,
        )
        best, confidence, selected_by = await selector.select(passed)
        if best is None:
            return DeepEyeSQLResult(
                question=question,
                success=False,
                error="No SQL candidate could be executed against the database.",
                trace=trace,
            )

        trace["selected_by"] = selected_by
        trace["confidence"] = confidence
        return DeepEyeSQLResult(
            question=question,
            sql=best.sql,
            columns=best.result_columns or [],
            rows=best.result_rows or [],
            confidence=confidence,
            candidate_count=len(passed),
            selected_by=selected_by,
            trace=trace,
            success=True,
        )
