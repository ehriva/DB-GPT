"""Expand your agents."""

from .tool_calling_agent import ToolCallingReActAgent  # noqa: F401

try:
    from .deepeye_sql import DeepEyeSQLAgent  # noqa: F401
except Exception:  # pragma: no cover - keep the expand package importable
    DeepEyeSQLAgent = None  # type: ignore

# Modify the default Excel file directory here
excel_path = "../test_files"
