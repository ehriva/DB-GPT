"""Optional per-stage tracing for the DeepEye-SQL pipeline.

When running inside DB-GPT, stages are wrapped in ``root_tracer`` spans so they
show up in the observability dashboard. Outside DB-GPT (or if tracing is
disabled), a no-op span is used so the pipeline has no hard dependency.
"""

from __future__ import annotations

from typing import Any, Dict, Optional


class _NullSpan:
    metadata: Dict[str, Any] = {}

    def __enter__(self) -> "_NullSpan":
        return self

    def __exit__(self, *args: Any) -> bool:
        return False

    def end(self, metadata: Optional[Dict[str, Any]] = None) -> None:
        if metadata:
            self.metadata.update(metadata)


def start_span(name: str, metadata: Optional[Dict[str, Any]] = None, enabled: bool = True):
    """Start a (possibly no-op) trace span."""
    if not enabled:
        return _NullSpan()
    try:
        from dbgpt.util.tracer import root_tracer

        return root_tracer.start_span(name, metadata=metadata or {})
    except Exception:
        return _NullSpan()
