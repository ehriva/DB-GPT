# DeepEye-SQL for DB-GPT

A faithful reimplementation of the **DeepEye-SQL** pipeline
([arXiv:2510.17586](https://arxiv.org/abs/2510.17586), SIGMOD 2026) as a DB-GPT
agent and AWEL workflow. DeepEye-SQL reframes Text-to-SQL as a verifiable
Software Development Life Cycle (SDLC) workflow rather than free-form
generation.

## Pipeline

| Stage | SDLC phase | Module |
|---|---|---|
| 1a. Semantic value retrieval | Requirements | `value_retrieval.py` |
| 1b. Robust schema linking (+ relational closure) | Requirements | `schema_linking.py`, `schema_profile.py` |
| 2. N-version SQL generation | Implementation | `generation.py` |
| 3. Tool-chain verification & targeted repair | Testing | `checkers.py` |
| 4. Confidence-aware selection | Release / quality gate | `selection.py` |

`pipeline.py` orchestrates the stages; `agent.py` exposes them as a
`ConversableAgent`; `workflow.py` exposes them as an AWEL HTTP DAG.

## Usage

### As a DB-GPT agent

```python
from dbgpt.agent import AgentContext, AgentMemory, LLMConfig, UserProxyAgent
from dbgpt.agent.expand import DeepEyeSQLAgent
from dbgpt.agent.resource import SQLiteDBResource

resource = SQLiteDBResource("db", "/path/to/database.db")
context = AgentContext(conv_id="demo")
memory = AgentMemory()
memory.gpts_memory.init(conv_id="demo")

user = await UserProxyAgent().bind(memory).bind(context).build()
agent = (
    await DeepEyeSQLAgent()
    .bind(context)
    .bind(LLMConfig(llm_client=llm_client))
    .bind(resource)
    .bind(memory)
    .build()
)
await user.initiate_chat(agent, message="How many users are from France?")
```

### As an AWEL HTTP workflow

```python
from dbgpt.agent.expand.deepeye_sql import build_deepeye_sql_dag
from dbgpt.core.awel import setup_dev_environment

dag = build_deepeye_sql_dag(connector_provider, llm_provider,
                            endpoint="/api/v1/deepeye_sql")
setup_dev_environment([dag], port=5556)
```

### Standalone

```python
from dbgpt.agent.expand.deepeye_sql import DeepEyeSQLPipeline

pipeline = DeepEyeSQLPipeline(connector, llm_complete)
result = await pipeline.run("How many users are from France?")
print(result.sql, result.rows, result.confidence)
```

See `examples/agents/deepeye_sql_agent_example.py` and
`examples/agents/deepeye_sql_awel_example.py`.

## Configuration

Key knobs on `DeepEyeSQLPipeline` / `DeepEyeSQLAgent`:

* `temperature` (0.7) — generation temperature.
* `sampling_budget` (1) — LLM samples per generator/linker; the paper's
  published config uses 12.
* `checker_sampling_budget` (1) — execution-repair samples; published config 16.
* `evaluator_sampling_budget` (3) — pairwise adjudication votes per pair;
  the paper uses 3 (majority), the reference code uses 16 (mean).
* `confidence_threshold` (0.6) — shortcut threshold for the quality gate.
* `filter_top_k` (2) — candidates carried into low-confidence adjudication.

## Features

* **Embedding-based value retrieval** — `Qwen3-Embedding` (or any
  sentence-transformers / OpenAI-compatible embedding) with a persistent
  per-database index (`index_dir`); falls back to a deterministic token-overlap
  index when no embedding backend is configured.
* **Dynamic few-shot retrieval** — LLM masking + embedding + top-K cross-domain
  example retrieval feeds the ICL generator and reversed linker
  (`few_shot_examples_path`, `num_examples`).
* **Progressive schema stripping** — `max_schema_tokens` trims value
  statistics/examples, then descriptions, then truncates, to fit the model
  context window.
* **Execution caching + timing refinement** — a per-run LRU execution cache and
  ±3σ timing re-measurement for selection tie-breaks.
* **TOML / env config** — all knobs in :class:`DeepEyeSQLConfig`, loadable from
  a TOML section or `DEEPEYE_SQL_*` env vars.
* **Dialect-aware date functions** — generation prompts adapt `STRFTIME` /
  `YEAR` / `EXTRACT` / `DATEPART` to the connector dialect.
* **Read-only enforcement** — SELECT-only, single-statement execution.
* **Streaming progress + tracing** — per-stage progress callbacks and
  `root_tracer` spans.
* **Cross-conversation caching** — schema profiles and value indices are cached
  process-wide (`clear_caches()`).

## Notes / adaptations

* The vector index uses an embedding model when configured, otherwise a
  deterministic in-memory token-overlap index, so the pipeline runs without
  extra dependencies.
* The ICL generator runs even without a few-shot corpus (with an empty
  example section); configure `few_shot_examples_path` (or pass
  `few_shot_examples=[(question, hint, sql), ...]`) for cross-domain examples.
* Execution is SELECT-only and row-bounded (default 100 rows) for safety.
