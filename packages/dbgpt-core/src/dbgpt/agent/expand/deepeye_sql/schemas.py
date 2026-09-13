"""Data schemas for the DeepEye-SQL pipeline."""

from __future__ import annotations

import dataclasses
from typing import Any, Dict, List, Optional, Set, Tuple


@dataclasses.dataclass
class RetrievedValue:
    """A retrieved database value with its cosine distance (1 - similarity)."""

    value: str
    distance: float


@dataclasses.dataclass
class RetrievedValues:
    """Semantic value retrieval result.

    ``values`` maps a qualified column key (``"table.column"``) to the list of
    retrieved database values that are semantically relevant to the question.
    """

    values: Dict[str, List[RetrievedValue]] = dataclasses.field(default_factory=dict)

    def to_prompt_block(self, top_k: Optional[int] = None) -> str:
        """Render the retrieved values as a prompt-friendly block."""
        if not self.values:
            return ""
        lines: List[str] = []
        for column, vals in self.values.items():
            shown = vals if top_k is None else vals[:top_k]
            rendered = ", ".join(repr(v.value) for v in shown)
            lines.append(f"{column}: [{rendered}]")
        return "\n".join(lines)


@dataclasses.dataclass
class LinkedSchema:
    """Robust schema-linking result.

    Attributes:
        tables: The set of linked table names.
        columns: Mapping of table name -> selected column names. An empty list
            means "all columns of this table".
        schema_text: Rendered schema of the linked tables after relational
            closure (PK/FK columns force-included).
        fk_edges: Foreign-key edges ``(source_table, target_table)``.
        source: Which linking strategies contributed (for observability).
    """

    tables: Set[str] = dataclasses.field(default_factory=set)
    columns: Dict[str, List[str]] = dataclasses.field(default_factory=dict)
    schema_text: str = ""
    fk_edges: List[Tuple[str, str]] = dataclasses.field(default_factory=list)
    source: Dict[str, Set[str]] = dataclasses.field(default_factory=dict)

    def table_columns(self, table: str) -> List[str]:
        """Return selected columns for a table (empty means all)."""
        return self.columns.get(table, [])


@dataclasses.dataclass
class CandidateSQL:
    """A single SQL candidate produced by one of the N generators."""

    sql: str
    generator: str
    index: int
    passed: bool = True
    confidence: Optional[float] = None
    exec_time: Optional[float] = None
    result_signature: Optional[str] = None
    result_rows: Optional[List[List[Any]]] = None
    result_columns: Optional[List[str]] = None
    execution_error: Optional[str] = None

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return (
            f"CandidateSQL(generator={self.generator!r}, index={self.index}, "
            f"passed={self.passed}, sql={self.sql[:60]!r})"
        )


@dataclasses.dataclass
class CheckerReport:
    """Report of a single deterministic checker in the tool-chain."""

    name: str
    passed: bool
    message: str = ""
    # The checker may rewrite SQL deterministically (no LLM needed).
    rewritten_sql: Optional[str] = None
    details: Dict[str, Any] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class DeepEyeSQLResult:
    """Final result of the DeepEye-SQL pipeline."""

    question: str
    sql: Optional[str] = None
    columns: List[str] = dataclasses.field(default_factory=list)
    rows: List[List[Any]] = dataclasses.field(default_factory=list)
    confidence: float = 0.0
    candidate_count: int = 0
    selected_by: str = "none"
    trace: Dict[str, Any] = dataclasses.field(default_factory=dict)
    success: bool = False
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serializable dictionary representation."""
        return {
            "question": self.question,
            "sql": self.sql,
            "columns": self.columns,
            "rows": [list(r) for r in self.rows],
            "confidence": self.confidence,
            "candidate_count": self.candidate_count,
            "selected_by": self.selected_by,
            "trace": self.trace,
            "success": self.success,
            "error": self.error,
        }
