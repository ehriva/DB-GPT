"""DeepEyeSQLAgent — a DB-GPT ConversableAgent running the DeepEye-SQL pipeline.

Bind a :class:`~dbgpt.agent.resource.database.DBResource` (e.g. an
:class:`~dbgpt.agent.resource.database.RDBMSConnectorResource` or
:class:`~dbgpt.agent.resource.database.SQLiteDBResource`) and an
:class:`~dbgpt.agent.util.llm.llm.LLMConfig`, then use the agent like any other
DB-GPT agent in a multi-agent dialogue.

Unlike a generic Text-to-SQL agent, the reply goes through the full DeepEye-SQL
SDLC pipeline (value retrieval → schema linking → N-version generation →
tool-chain verification → confidence-aware selection).
"""

from __future__ import annotations

import logging
from typing import Any, List, Optional, Tuple

from ..core.agent import Agent, AgentMessage
from ..core.base_agent import ConversableAgent
from ..core.profile import DynConfig, ProfileConfig
from ..resource.database import DBResource
from .config import DeepEyeSQLConfig
from .multischema import wrap_multi_schema
from .pipeline import DeepEyeSQLPipeline
from .schemas import DeepEyeSQLResult

logger = logging.getLogger(__name__)

_RESULT_PREVIEW_ROWS = 20

_STAGE_LABELS = {
    "value_retrieval": "Retrieving relevant database values",
    "few_shot": "Retrieving few-shot examples",
    "schema_linking": "Linking relevant schema",
    "generation": "Generating SQL candidates",
    "verification": "Verifying and repairing SQL",
    "selection": "Selecting the best SQL",
}


class DeepEyeSQLAgent(ConversableAgent):
    """A DeepEye-SQL pipeline agent."""

    profile: ProfileConfig = ProfileConfig(
        name=DynConfig(
            "DeepEye",
            category="agent",
            key="dbgpt_agent_expand_deepeye_sql_agent_profile_name",
        ),
        role=DynConfig(
            "DeepEyeSQLAgent",
            category="agent",
            key="dbgpt_agent_expand_deepeye_sql_agent_profile_role",
        ),
        goal=DynConfig(
            "Translate the user's natural-language question into a correct, "
            "verified SQL query over the bound database by running the "
            "DeepEye-SQL pipeline (semantic value retrieval, robust schema "
            "linking, N-version generation, deterministic tool-chain "
            "verification, and confidence-aware selection).",
            category="agent",
            key="dbgpt_agent_expand_deepeye_sql_agent_profile_goal",
        ),
        constraints=DynConfig(
            [
                "Only emit read-only SELECT queries.",
                "Ground filter values in the actual database values retrieved "
                "during the pipeline; never hallucinate literals.",
                "Return the final SQL and its execution result.",
            ],
            category="agent",
            key="dbgpt_agent_expand_deepeye_sql_agent_profile_constraints",
        ),
        desc=DynConfig(
            "An SDLC-guided Text-to-SQL agent that verifies and quality-gates "
            "SQL before returning it.",
            category="agent",
            key="dbgpt_agent_expand_deepeye_sql_agent_profile_desc",
        ),
    )

    # --- Pipeline configuration ------------------------------------------
    config: Optional[DeepEyeSQLConfig] = None
    temperature: float = 0.7
    confidence_threshold: float = 0.6
    sampling_budget: int = 1
    checker_sampling_budget: int = 1
    evaluator_sampling_budget: int = 3
    filter_top_k: int = 2
    max_rows: int = 100
    include_value_stats: bool = True
    few_shot_examples: Optional[List] = None
    # Multi-schema reflection: when True (or when `schemas` is set), the bound
    # connector is wrapped to reflect tables across all non-system schemas
    # using schema-qualified names.
    multi_schema: bool = False
    schemas: Optional[List[str]] = None

    def __init__(self, **kwargs):
        """Create a DeepEyeSQLAgent."""
        super().__init__(**kwargs)

    # ------------------------------------------------------------------
    # Resource / availability
    # ------------------------------------------------------------------
    @property
    def database(self) -> DBResource:
        """Return the bound database resource."""
        dbs: List[DBResource] = DBResource.from_resource(self.resource)
        if not dbs:
            raise ValueError(
                "DeepEyeSQLAgent requires a database resource "
                "(e.g. RDBMSConnectorResource or SQLiteDBResource)."
            )
        return dbs[0]

    @property
    def connector(self) -> Any:
        """Return the underlying (optionally schema-aware) RDBMS connector."""
        connector = getattr(self.database, "connector", None)
        if connector is None:
            raise ValueError(
                "The bound DBResource does not expose a `.connector`; bind an "
                "RDBMSConnectorResource (or SQLiteDBResource) instead."
            )
        if self.multi_schema or self.schemas:
            cached = getattr(self, "_schema_aware_connector", None)
            if cached is None:
                cached = wrap_multi_schema(connector, schemas=self.schemas)
                object.__setattr__(self, "_schema_aware_connector", cached)
            return cached
        return connector

    def check_available(self) -> None:
        """Validate the agent configuration (no generic Action required)."""
        self.identity_check()
        if self.agent_context is None:
            raise ValueError(
                f"{self.name}[{self.role}] Missing context in which agent is running!"
            )
        # Ensure the DB resource is present.
        self.connector  # raises a descriptive error if missing
        if not self.is_human and (
            self.llm_config is None or self.llm_config.llm_client is None
        ):
            raise ValueError(
                f"{self.name}[{self.role}] Model configuration is missing or "
                "model service is unavailable!"
            )

    # ------------------------------------------------------------------
    # Pipeline execution hooks
    # ------------------------------------------------------------------
    async def thinking(
        self,
        messages: List[AgentMessage],
        sender: Optional[Agent] = None,
        prompt: Optional[str] = None,
        stream_callback=None,
    ) -> Tuple[Optional[str], Optional[str]]:
        """Return the user question directly; the pipeline does its own LLM calls."""
        # Stash the stream callback so `act` can emit pipeline-stage progress.
        object.__setattr__(self, "_llm_stream_callback", stream_callback)
        question = messages[-1].content if messages else ""
        return question, None

    def _make_progress_callback(self):
        cb = getattr(self, "_llm_stream_callback", None)
        if cb is None:
            return None

        async def _progress(stage: str, payload) -> None:
            label = _STAGE_LABELS.get(stage, stage)
            status = (payload or {}).get("status", "")
            text = f"🔍 {label}..." if status == "start" else f"✅ {label}"
            try:
                await cb({"delta_text": text, "delta_thinking": ""})
            except Exception:  # pragma: no cover - progress is best-effort
                logger.debug("progress callback error", exc_info=True)

        return _progress

    async def act(
        self,
        message: AgentMessage,
        sender: Agent,
        reviewer: Optional[Agent] = None,
        is_retry_chat: bool = False,
        last_speaker_name: Optional[str] = None,
        **kwargs,
    ):
        """Run the DeepEye-SQL pipeline and package the result as an action."""
        from ..core.action.base import ActionOutput

        question = message.current_goal or message.content or ""
        model_name = await self._a_select_llm_model()
        pipeline = await self._get_pipeline(model_name)
        result = await pipeline.run(
            question,
            hint="",
            few_shot_examples=self.few_shot_examples,
            progress_callback=self._make_progress_callback(),
        )
        content = _format_result(result)
        return ActionOutput(
            content=content,
            is_exe_success=result.success,
            have_retry=False,
            terminate=True,
            observations=content,
            action="DeepEyeSQLPipeline",
        )

    async def _get_pipeline(self, model_name: Optional[str]) -> DeepEyeSQLPipeline:
        """Return a (cached) pipeline for the given model.

        The pipeline is cached per model so the value index and schema profile
        are built once per conversation rather than on every turn.
        """
        cache = getattr(self, "_deepeye_pipeline_cache", None)
        if cache is None:
            cache = {}
            object.__setattr__(self, "_deepeye_pipeline_cache", cache)
        if model_name not in cache:
            cache[model_name] = DeepEyeSQLPipeline(
                self.connector,
                self.llm_client,
                config=self.config,
                model_name=model_name,
                conv_id=self.not_null_agent_context.conv_id,
                temperature=self.temperature,
                confidence_threshold=self.confidence_threshold,
                sampling_budget=self.sampling_budget,
                checker_sampling_budget=self.checker_sampling_budget,
                evaluator_sampling_budget=self.evaluator_sampling_budget,
                filter_top_k=self.filter_top_k,
                max_rows=self.max_rows,
                include_value_stats=self.include_value_stats,
            )
        return cache[model_name]

    async def adjust_final_message(
        self, is_success: bool, reply_message: AgentMessage
    ):
        """Surface the pipeline output as the final message content."""
        report = reply_message.action_report
        if report is not None and report.content:
            reply_message.content = report.content
        reply_message.success = is_success
        return is_success, reply_message


def _format_result(result: DeepEyeSQLResult) -> str:
    """Render a DeepEyeSQLResult as human-readable markdown."""
    if not result.success:
        return f"**DeepEye-SQL failed:** {result.error or 'unknown error'}"

    lines = ["**SQL:**", "```sql", result.sql or "", "```", ""]
    lines.append(
        f"**Confidence:** {result.confidence:.2f} (selected by "
        f"`{result.selected_by}`; {result.candidate_count} candidates)"
    )
    lines.append(f"**Rows returned:** {len(result.rows)}")

    if result.columns and result.rows:
        rows = result.rows[:_RESULT_PREVIEW_ROWS]
        header = "| " + " | ".join(str(c) for c in result.columns) + " |"
        sep = "|" + "---|" * len(result.columns)
        body = [
            "| " + " | ".join(_cell(v) for v in row) + " |" for row in rows
        ]
        lines.extend(["", header, sep, *body])
        if len(result.rows) > _RESULT_PREVIEW_ROWS:
            lines.append(f"\n... ({len(result.rows) - _RESULT_PREVIEW_ROWS} more rows)")
    return "\n".join(lines)


def _cell(value: Any) -> str:
    if value is None:
        return "NULL"
    s = str(value)
    return s if len(s) <= 80 else s[:77] + "..."
