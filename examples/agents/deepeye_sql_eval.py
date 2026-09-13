"""Execution-accuracy evaluation harness for the DeepEye-SQL pipeline.

Runs the pipeline over a Spider- or BIRD-format dev set and reports execution
accuracy (order-insensitive result-set equivalence), matching the metric used
in the DeepEye-SQL paper.

Usage::

    export SILICONFLOW_API_KEY=sk-xxx
    python examples/agents/deepeye_sql_eval.py \
        --dataset /path/to/spider/dev.json \
        --db-root /path/to/spider/database \
        --model Qwen/Qwen2.5-Coder-32B-Instruct \
        --limit 20

Dataset format (Spider/BIRD): a JSON array of objects with ``db_id``,
``question``, and ``query`` (or ``SQL``), plus optional ``evidence``.
"""

import argparse
import asyncio
import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

from dbgpt.agent.expand.deepeye_sql import DeepEyeSQLConfig, DeepEyeSQLPipeline
from dbgpt.agent.util.llm.llm_client import AIWrapper
from dbgpt_ext.datasource.rdbms.conn_sqlite import SQLiteConnector

from dbgpt.agent.expand.deepeye_sql.execution import safe_execute
from dbgpt.agent.expand.deepeye_sql.util import hash_result


def load_dataset(path: str) -> List[Dict[str, Any]]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, list) else data.get("data", [])


def _db_connector(db_root: str, db_id: str) -> SQLiteConnector:
    # BIRD: database/{db_id}/{db_id}.sqlite ; Spider: database/{db_id}/{db_id}.sqlite
    candidates = [
        os.path.join(db_root, db_id, f"{db_id}.sqlite"),
        os.path.join(db_root, db_id, f"{db_id}.db"),
        os.path.join(db_root, f"{db_id}.sqlite"),
    ]
    for path in candidates:
        if os.path.exists(path):
            return SQLiteConnector.from_file_path(path)
    raise FileNotFoundError(f"No SQLite database found for db_id={db_id!r}")


def build_llm(model_name: str):
    if os.getenv("SILICONFLOW_API_KEY"):
        from dbgpt.model.proxy.llms.siliconflow import SiliconFlowLLMClient

        return AIWrapper(llm_client=SiliconFlowLLMClient(model_alias=model_name)), model_name
    from dbgpt.model.proxy import OpenAILLMClient

    return AIWrapper(llm_client=OpenAILLMClient(model_alias=model_name)), model_name


async def evaluate(
    dataset: List[Dict[str, Any]],
    db_root: str,
    llm_wrapper,
    model_name: str,
    config: DeepEyeSQLConfig,
    limit: Optional[int],
) -> Tuple[int, int, List[Dict[str, Any]]]:
    correct = 0
    total = 0
    report: List[Dict[str, Any]] = []
    pipeline_cache: Dict[str, DeepEyeSQLPipeline] = {}

    for idx, example in enumerate(dataset):
        if limit is not None and idx >= limit:
            break
        db_id = example["db_id"]
        question = example["question"]
        gold_sql = example.get("query") or example.get("SQL")
        evidence = example.get("evidence", "") or ""
        if not gold_sql:
            report.append({"db_id": db_id, "question": question, "error": "no gold SQL"})
            continue

        connector = _db_connector(db_root, db_id)
        if db_id not in pipeline_cache:
            pipeline_cache[db_id] = DeepEyeSQLPipeline(
                connector, llm_wrapper, config=config, model_name=model_name
            )
        pipeline = pipeline_cache[db_id]
        result = await pipeline.run(question, hint=evidence)
        total += 1

        ok = False
        error = result.error
        if result.success:
            try:
                gold_cols, gold_rows = safe_execute(
                    connector, gold_sql, max_rows=config.max_rows
                )
                ok = hash_result(result.rows) == hash_result(gold_rows)
            except Exception as e:  # gold SQL failed to execute
                error = f"gold exec error: {e}"
        if ok:
            correct += 1
        report.append(
            {
                "db_id": db_id,
                "question": question,
                "correct": ok,
                "predicted_sql": result.sql,
                "error": error,
            }
        )
        print(f"[{idx + 1}] {'OK ' if ok else 'ERR'} {db_id}: {question[:70]}")
    return correct, total, report


async def main() -> None:
    parser = argparse.ArgumentParser(description="DeepEye-SQL eval harness")
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--db-root", required=True)
    parser.add_argument("--model", default="Qwen/Qwen2.5-Coder-32B-Instruct")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--config", default=None, help="Path to a TOML config file")
    parser.add_argument("--out", default=None, help="Write per-example report JSON")
    args = parser.parse_args()

    dataset = load_dataset(args.dataset)
    config = (
        DeepEyeSQLConfig.from_toml(args.config) if args.config else DeepEyeSQLConfig()
    )
    llm_wrapper, model_name = build_llm(args.model)

    correct, total, report = await evaluate(
        dataset, args.db_root, llm_wrapper, model_name, config, args.limit
    )
    print(f"\nExecution accuracy: {correct}/{total} = {correct / total:.4f}")
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
