"""DeepEye-SQL pipeline orchestrator.

Wires the SDLC-inspired stages together:

1. Semantic value retrieval
2. Robust schema linking (+ relational closure)
3. N-version SQL generation
4. Tool-chain verification + confidence-aware selection

Supports TOML/env configuration, optional embedding-based retrieval, dynamic
few-shot retrieval, streaming stage progress, per-stage tracing, execution
caching, and cross-conversation schema/value-index caching.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Awaitable, Callable, Dict, List, Optional, Union

from .caching import get_or_build_schema_profile, get_or_build_value_index
from .checkers import SQLToolChain
from .config import DeepEyeSQLConfig
from .embeddings import resolve_embedder
from .execution import ExecutionCache
from .few_shot import FewShotRetriever, load_few_shot_examples
from .generation import NVersionGenerator
from .llm import LLMComplete, to_complete
from .schema_linking import RobustSchemaLinker
from .schema_profile import SchemaProfile
from .schemas import DeepEyeSQLResult
from .selection import ConfidenceAwareSelector
from .tracing import start_span
from .value_retrieval import SemanticValueRetriever

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[str, Dict[str, Any]], Union[None, Awaitable[None]]]

_CONFIG_FIELDS = (
    "temperature",
    "top_k",
    "max_value_length",
    "max_indexed_values",
    "embedding_model",
    "embedding_api_base",
    "embedding_api_key",
    "index_dir",
    "use_local_index",
    "value_distance_threshold",
    "direct_linking_sampling_budget",
    "reversed_linking_sampling_budget",
    "generation_sampling_budget",
    "few_shot_examples_path",
    "num_examples",
    "question_weight",
    "sql_weight",
    "checker_sampling_budget",
    "confidence_threshold",
    "filter_top_k",
    "evaluator_sampling_budget",
    "max_rows",
    "execution_timeout",
    "read_only",
    "include_value_stats",
    "max_schema_tokens",
    "enable_tracing",
)


def _resolve_config(
    config: Optional[DeepEyeSQLConfig], kwargs: Dict[str, Any]
) -> DeepEyeSQLConfig:
    """Merge a config object with explicit keyword overrides."""
    cfg = config or DeepEyeSQLConfig()
    # Backward-compatible single budget applied to linking + generation.
    if "sampling_budget" in kwargs:
        value = kwargs.pop("sampling_budget")
        cfg.generation_sampling_budget = value
        cfg.direct_linking_sampling_budget = value
        cfg.reversed_linking_sampling_budget = value
    for name in _CONFIG_FIELDS:
        if name in kwargs:
            setattr(cfg, name, kwargs.pop(name))
    return cfg


class DeepEyeSQLPipeline:
    """The full DeepEye-SQL Text-to-SQL pipeline."""

    def __init__(
        self,
        connector: Any,
        complete: Any,
        *,
        config: Optional[DeepEyeSQLConfig] = None,
        model_name: Optional[str] = None,
        conv_id: Optional[str] = None,
        **kwargs,
    ):
        cfg = _resolve_config(config, kwargs)
        self._cfg = cfg
        self._connector = connector
        self._complete: LLMComplete = to_complete(
            complete, model_name=model_name, conv_id=conv_id
        )
        self._embedder = resolve_embedder(cfg)
        self._execution_cache = ExecutionCache()

        self._value_retriever = SemanticValueRetriever(
            connector,
            self._complete,
            top_k=cfg.top_k,
            max_value_length=cfg.max_value_length,
            max_indexed_values=cfg.max_indexed_values,
            embedder=self._embedder,
        )
        self._linker = RobustSchemaLinker(
            connector,
            self._complete,
            value_distance_threshold=cfg.value_distance_threshold,
            direct_sampling_budget=cfg.direct_linking_sampling_budget,
            reversed_sampling_budget=cfg.reversed_linking_sampling_budget,
        )
        self._generator = NVersionGenerator(
            self._complete,
            connector,
            temperature=cfg.temperature,
            sampling_budget=cfg.generation_sampling_budget,
        )

        self._few_shot_retriever: Optional[FewShotRetriever] = None
        if cfg.few_shot_examples_path and os.path.exists(cfg.few_shot_examples_path):
            examples = load_few_shot_examples(cfg.few_shot_examples_path)
            if examples:
                self._few_shot_retriever = FewShotRetriever(
                    examples,
                    self._complete,
                    self._embedder,
                    num_examples=cfg.num_examples,
                    question_weight=cfg.question_weight,
                    sql_weight=cfg.sql_weight,
                )

    @property
    def connector(self) -> Any:
        return self._connector

    @property
    def config(self) -> DeepEyeSQLConfig:
        return self._cfg

    async def _emit(
        self,
        callback: Optional[ProgressCallback],
        stage: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        if callback is None:
            return
        data = {"stage": stage, **(payload or {})}
        try:
            result = callback(stage, data)
            if asyncio.iscoroutine(result):
                await result
        except Exception:  # pragma: no cover - progress is best-effort
            logger.debug("progress callback error", exc_info=True)

    def _ensure_index(self) -> None:
        if self._value_retriever._index is None:
            db_id = None
            try:
                db_id = self._connector.get_current_db_name()
            except Exception:
                pass
            self._value_retriever._index = get_or_build_value_index(
                self._connector,
                self._embedder,
                max_value_length=self._cfg.max_value_length,
                max_indexed_values=self._cfg.max_indexed_values,
                index_dir=self._cfg.index_dir,
                db_id=db_id,
            )

    async def run(
        self,
        question: str,
        *,
        hint: str = "",
        few_shot_examples: Optional[List[tuple]] = None,
        schema_profile: Optional[SchemaProfile] = None,
        progress_callback: Optional[ProgressCallback] = None,
    ) -> DeepEyeSQLResult:
        """Run the pipeline end-to-end and return the quality-gated result."""
        cfg = self._cfg
        trace: Dict[str, Any] = {}
        tracer_enabled = cfg.enable_tracing

        # Stage 0: schema profile (cross-conversation cached).
        profile = schema_profile or get_or_build_schema_profile(
            self._connector, include_value_stats=cfg.include_value_stats
        )

        # Stage 1a: Semantic value retrieval.
        await self._emit(progress_callback, "value_retrieval", {"status": "start"})
        with start_span("deepeye.value_retrieval", enabled=tracer_enabled):
            self._ensure_index()
            retrieved = await self._value_retriever.retrieve(question, hint)
            profile.apply_value_retrieval(retrieved)
        trace["retrieved_values"] = {
            k: [v.value for v in vals] for k, vals in retrieved.values.items()
        }
        await self._emit(
            progress_callback,
            "value_retrieval",
            {"status": "done", "columns": list(trace["retrieved_values"])},
        )

        # Resolve few-shot examples (dynamic retrieval when configured).
        few_shot = few_shot_examples
        if few_shot is None and self._few_shot_retriever is not None:
            await self._emit(progress_callback, "few_shot", {"status": "start"})
            with start_span("deepeye.few_shot", enabled=tracer_enabled):
                few_shot = await self._few_shot_retriever.retrieve(question, hint)
            await self._emit(
                progress_callback, "few_shot", {"status": "done", "count": len(few_shot)}
            )

        # Stage 1b: Robust schema linking.
        await self._emit(progress_callback, "schema_linking", {"status": "start"})
        with start_span("deepeye.schema_linking", enabled=tracer_enabled):
            linked = await self._linker.link(
                question,
                hint,
                profile,
                retrieved,
                few_shot_examples=few_shot,
                max_schema_tokens=cfg.max_schema_tokens,
            )
        trace["linked_tables"] = sorted(linked.tables)
        trace["linking_sources"] = {k: sorted(v) for k, v in linked.source.items()}
        await self._emit(
            progress_callback,
            "schema_linking",
            {"status": "done", "tables": trace["linked_tables"]},
        )

        # Stage 2: N-version SQL generation.
        await self._emit(progress_callback, "generation", {"status": "start"})
        with start_span("deepeye.generation", enabled=tracer_enabled):
            candidates = await self._generator.generate(
                question, hint, linked.schema_text, few_shot_examples=few_shot
            )
        trace["generated_candidates"] = len(candidates)
        await self._emit(
            progress_callback,
            "generation",
            {"status": "done", "candidates": len(candidates)},
        )
        if not candidates:
            return DeepEyeSQLResult(
                question=question,
                success=False,
                error="No SQL candidates were generated.",
                trace=trace,
            )

        # Stage 3: Tool-chain verification + targeted repair.
        await self._emit(progress_callback, "verification", {"status": "start"})
        tool_chain = SQLToolChain(
            self._connector,
            self._complete,
            question,
            hint,
            linked.schema_text,
            checker_sampling_budget=cfg.checker_sampling_budget,
            max_rows=cfg.max_rows,
            cache=self._execution_cache,
            timeout=cfg.execution_timeout,
        )
        with start_span("deepeye.verification", enabled=tracer_enabled):
            candidates = await tool_chain.verify_and_repair(candidates)
        trace["verified_candidates"] = [
            {"generator": c.generator, "sql": c.sql, "passed": c.passed}
            for c in candidates
        ]
        await self._emit(
            progress_callback,
            "verification",
            {"status": "done", "passed": sum(1 for c in candidates if c.passed)},
        )
        passed = [c for c in candidates if c.passed]
        if not passed:
            return DeepEyeSQLResult(
                question=question,
                success=False,
                error="All SQL candidates failed the verification tool-chain.",
                trace=trace,
            )

        # Stage 4: Confidence-aware selection.
        await self._emit(progress_callback, "selection", {"status": "start"})
        selector = ConfidenceAwareSelector(
            self._connector,
            self._complete,
            question,
            hint,
            linked.schema_text,
            confidence_threshold=cfg.confidence_threshold,
            filter_top_k=cfg.filter_top_k,
            evaluator_sampling_budget=cfg.evaluator_sampling_budget,
            max_rows=cfg.max_rows,
            cache=self._execution_cache,
            timeout=cfg.execution_timeout,
        )
        with start_span("deepeye.selection", enabled=tracer_enabled):
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
        await self._emit(
            progress_callback,
            "selection",
            {"status": "done", "selected_by": selected_by, "confidence": confidence},
        )
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
