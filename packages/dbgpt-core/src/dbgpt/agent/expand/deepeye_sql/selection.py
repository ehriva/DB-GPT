"""Confidence-aware SQL selection (Stage 4 of DeepEye-SQL).

Candidates are clustered by execution-result hash to estimate a consistency
score. If the top candidate's consistency meets the threshold, the
high-confidence shortcut is taken; otherwise an *unbalanced* pairwise
adjudication (LLM compares candidate pairs, biased toward the more-confident
candidate) resolves the ambiguity.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

from .execution import measure_execution_time, safe_execute
from .llm import LLMComplete
from .prompts import render_pair_selection
from .schemas import CandidateSQL
from .util import extract_xml_result, hash_result, result_table_str

logger = logging.getLogger(__name__)

_DEFAULT_CONFIDENCE_THRESHOLD = 0.6
_DEFAULT_FILTER_TOP_K = 2
_DEFAULT_EVALUATOR_BUDGET = 3
_DEFAULT_MAX_ROWS = 100


def _parse_vote(text: str) -> Optional[str]:
    vote = extract_xml_result(text).strip().upper()
    if vote in ("A", "B", "TIE"):
        return vote
    # Be lenient: find the first A/B/TIE token.
    for token in ("A", "B", "TIE"):
        if token in vote:
            return token
    return None


class ConfidenceAwareSelector:
    """Select the final SQL via execution clustering + pairwise adjudication."""

    def __init__(
        self,
        connector: Any,
        complete: LLMComplete,
        question: str,
        hint: str,
        database_schema: str,
        *,
        confidence_threshold: float = _DEFAULT_CONFIDENCE_THRESHOLD,
        filter_top_k: int = _DEFAULT_FILTER_TOP_K,
        evaluator_sampling_budget: int = _DEFAULT_EVALUATOR_BUDGET,
        max_rows: int = _DEFAULT_MAX_ROWS,
        cache: Any = None,
        timeout: Optional[float] = None,
    ):
        self._connector = connector
        self._complete = complete
        self._question = question
        self._hint = hint
        self._database_schema = database_schema
        self._confidence_threshold = confidence_threshold
        self._filter_top_k = filter_top_k
        self._evaluator_budget = evaluator_sampling_budget
        self._max_rows = max_rows
        self._cache = cache
        self._timeout = timeout

    async def select(
        self, candidates: List[CandidateSQL]
    ) -> Tuple[Optional[CandidateSQL], float, str]:
        """Select the best candidate and return ``(candidate, confidence, mode)``."""
        executed = await self._execute_all(candidates)
        ranked = self._rank(executed)
        if not ranked:
            return None, 0.0, "none"
        if len(ranked) == 1 or ranked[0].confidence >= self._confidence_threshold:
            top = ranked[0]
            return top, top.confidence or 0.0, "high_confidence"

        top_k = ranked[: self._filter_top_k]
        winner, confidence = await self._pairwise_adjudication(top_k)
        if winner is None:
            return top_k[0], top_k[0].confidence or 0.0, "fallback"
        return winner, confidence, "adjudication"

    async def _execute_all(
        self, candidates: List[CandidateSQL]
    ) -> List[CandidateSQL]:
        executed: List[CandidateSQL] = []
        for cand in candidates:
            try:
                start = time.perf_counter()
                columns, rows = safe_execute(
                    self._connector,
                    cand.sql,
                    max_rows=self._max_rows,
                    cache=self._cache,
                    timeout=self._timeout,
                )
                cand.result_columns = columns
                cand.result_rows = rows
                cand.exec_time = time.perf_counter() - start
                cand.result_signature = hash_result(rows)
                executed.append(cand)
            except Exception as e:  # pragma: no cover - depends on DB
                cand.execution_error = str(e)
                cand.result_rows = None
                logger.warning("selection execution failed: %s", e)
        return executed

    def _rank(self, executed: List[CandidateSQL]) -> List[CandidateSQL]:
        # valid = non-empty results; fallback = any non-None results.
        valid = [c for c in executed if c.result_rows]
        pool = valid or [c for c in executed if c.result_rows is not None]
        if not pool:
            return []
        sig_counts = Counter(c.result_signature for c in pool)
        total = len(pool)
        for cand in pool:
            cand.confidence = sig_counts[cand.result_signature or ""] / total
        # Dedup by result hash, keep one representative per cluster.
        reps: Dict[str, CandidateSQL] = {}
        for cand in pool:
            reps.setdefault(cand.result_signature or "", cand)
        ranked = sorted(
            reps.values(),
            key=lambda c: (
                -(c.confidence or 0.0),
                getattr(c, "exec_time", float("inf")),
            ),
        )

        # Re-measure execution time for tied consistency scores that appear in
        # the top-K (reference: `_refine_relevant_tied_candidate_timings`).
        top_scores = [c.confidence for c in ranked[: self._filter_top_k]]
        for cand in ranked:
            if (
                cand.confidence in top_scores
                and sum(1 for c in ranked if c.confidence == cand.confidence) > 1
            ):
                cand.exec_time = measure_execution_time(
                    self._connector, cand.sql, max_rows=self._max_rows
                )
        return sorted(
            ranked,
            key=lambda c: (
                -(c.confidence or 0.0),
                getattr(c, "exec_time", float("inf")),
            ),
        )

    async def _pairwise_adjudication(
        self, top_k: List[CandidateSQL]
    ) -> Tuple[Optional[CandidateSQL], float]:
        n = len(top_k)
        if n == 1:
            return top_k[0], top_k[0].confidence or 0.0

        # win_matrix[i][j] = P(candidate i beats candidate j) (mean over voters).
        win = [[0.0] * n for _ in range(n)]

        async def _compare(i: int, j: int) -> Tuple[int, int, float]:
            # A = higher-ranked (i), B = lower-ranked (j).
            a, b = top_k[i], top_k[j]
            prompt = render_pair_selection(
                self._question,
                self._hint,
                self._database_schema,
                a.sql,
                result_table_str(a.result_columns, a.result_rows),
                b.sql,
                result_table_str(b.result_columns, b.result_rows),
            )
            votes = []
            for _ in range(self._evaluator_budget):
                try:
                    raw = await self._complete.complete(
                        [{"role": "user", "content": prompt}],
                        temperature=0.0,
                        max_new_tokens=256,
                    )
                    vote = _parse_vote(raw)
                    if vote is not None:
                        votes.append(vote)
                except Exception as e:  # pragma: no cover
                    logger.warning("adjudication vote failed: %s", e)
            if not votes:
                return i, j, 0.5  # default to A (higher confidence)
            # A -> 1, B -> 0, TIE -> 0.5
            score_a = sum(
                1.0 if v == "A" else (0.5 if v == "TIE" else 0.0) for v in votes
            ) / len(votes)
            return i, j, score_a

        tasks = [asyncio.create_task(_compare(i, j)) for i in range(n) for j in range(i + 1, n)]
        results = await asyncio.gather(*tasks)
        for i, j, score_a in results:
            win[i][j] = score_a
            win[j][i] = 1.0 - score_a

        # ranking_scores[i] = mean(win_row_i) * (conf_i / sum(conf)).
        confs = [c.confidence or 0.0 for c in top_k]
        conf_sum = sum(confs) or 1.0
        scores = [
            (sum(win[i]) / n) * (confs[i] / conf_sum) for i in range(n)
        ]
        best = max(range(n), key=lambda i: scores[i])
        return top_k[best], confs[best]
