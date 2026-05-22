"""VizAgent — rule-based chart selection + Vega-Lite spec construction.

Key rule (docs/README.md): no LLM call for the spec. Chart type is picked by
rules over column types; the spec is built in Python. Exactly one LLM call,
for the one-sentence summary.

`run()` takes `context: ContextPackage` to consult `ColumnMeta.semantic_type`
as the primary type signal; falls back to value inspection for derived
columns whose alias doesn't appear in any table schema.
"""

from __future__ import annotations

import logging
import re

_log = logging.getLogger("tiri.engine.agents.viz")

from tiri.data_models import (
    ContextPackage,
    LLMMessage,
    QueryResult,
    VizResult,
)
from tiri.providers.base import LLMProvider


_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}")
_BAR_ROW_THRESHOLD = 20

_DateType = "date"
_NumericType = "numeric"
_StringType = "string"
_UnknownType = "unknown"


class VizAgent:
    """Rule-based chart selection. The injected LLM is used only for summary."""

    def __init__(self, llm: LLMProvider) -> None:
        self._llm = llm

    async def run(
        self,
        question: str,
        query_result: QueryResult,
        context: ContextPackage,
    ) -> VizResult:
        types = [
            classify_column(c, query_result, context)
            for c in query_result.columns
        ]
        chart_type = select_chart_type(types, len(query_result.rows))
        spec = build_spec(chart_type, query_result, types)
        summary = await self._summarize(question, query_result)
        return VizResult(
            chart_type=chart_type, vega_lite_spec=spec, summary=summary
        )

    async def _summarize(
        self, question: str, result: QueryResult
    ) -> str:
        top_rows = result.rows[:3]
        prompt = (
            "In one sentence, summarize this query result for a business user.\n"
            f"Question: {question}\n"
            f"Columns: {', '.join(result.columns)}\n"
            f"Row count: {result.row_count}\n"
            f"Top rows: {top_rows}\n"
            "Respond with the sentence only. No preamble. Avoid causal claims "
            "(\"because\", \"due to\"); state what the data shows."
        )
        try:
            response = await self._llm.complete(
                [LLMMessage(role="system", content=prompt)],
                task="viz_summary",
            )
            return response.content.strip()
        except Exception as exc:  # noqa: BLE001
            # The viz_summary is decorative — the SQL + result already convey
            # the answer. A model-side failure here (refusal, guardrail false
            # positive, quota, transient timeout) MUST NOT collapse the whole
            # turn. Degrade to an empty summary; the turn still ships with
            # sql/result/viz_spec intact.
            _log.warning(
                "VizAgent summary generation failed; degrading to empty "
                "summary: %s",
                exc,
            )
            return ""


# ────────────────────────────────────────────────────────────────────────────
# Column type classification
# ────────────────────────────────────────────────────────────────────────────


def classify_column(
    col_name: str,
    query_result: QueryResult,
    context: ContextPackage,
) -> str:
    """Returns "date" | "numeric" | "string" | "unknown".

    Primary signal: `ColumnMeta.semantic_type` from `context.table_schemas`.
    Fallback: inspect the first non-null value in `query_result`.
    """
    # 1. Try the semantic type from any table whose column name matches.
    for table_meta in context.table_schemas.values():
        for col_meta in table_meta.columns:
            if col_meta.name == col_name:
                if col_meta.semantic_type == "date":
                    return _DateType
                if col_meta.semantic_type in ("currency", "measure"):
                    return _NumericType
                if col_meta.semantic_type in ("category", "identifier"):
                    return _StringType
                # semantic_type is "" or another value — fall through to data inspection.
                break

    # 2. Fallback: inspect actual rows.
    for row in query_result.rows:
        val = row.get(col_name)
        if val is None:
            continue
        if isinstance(val, bool):
            return _StringType  # render booleans as nominal categories
        if isinstance(val, (int, float)):
            return _NumericType
        if isinstance(val, str):
            if _DATE_PATTERN.match(val):
                return _DateType
            return _StringType
        # Anything else (datetime, Decimal, etc.): treat by repr length.
        text = str(val)
        if _DATE_PATTERN.match(text):
            return _DateType
        return _StringType
    return _UnknownType


# ────────────────────────────────────────────────────────────────────────────
# Chart-type selection
# ────────────────────────────────────────────────────────────────────────────


def select_chart_type(types: list[str], row_count: int) -> str:
    """Apply the rules from docs/agents.md in priority order."""
    n_numeric = sum(1 for t in types if t == _NumericType)
    n_date = sum(1 for t in types if t == _DateType)
    n_string = sum(1 for t in types if t == _StringType)
    total = len(types)

    if total == 1 and n_numeric == 1 and row_count == 1:
        return "counter"
    if n_date == 1 and n_numeric >= 1 and (n_string + n_date) == 1:
        return "line"
    if n_string == 1 and n_numeric == 1 and (n_string + n_date) == 1:
        return "bar" if row_count <= _BAR_ROW_THRESHOLD else "table"
    if n_numeric == 2 and total == 2:
        return "scatter"
    return "table"


# ────────────────────────────────────────────────────────────────────────────
# Vega-Lite spec construction
# ────────────────────────────────────────────────────────────────────────────


_SCHEMA_URL = "https://vega.github.io/schema/vega-lite/v5.json"


def build_spec(
    chart_type: str, result: QueryResult, types: list[str]
) -> dict:
    """Dispatch to per-chart-type builder. Always returns a valid v5 spec."""
    type_by_col = dict(zip(result.columns, types))
    if chart_type == "counter":
        return _build_counter(result)
    if chart_type == "line":
        return _build_line(result, type_by_col)
    if chart_type == "bar":
        return _build_bar(result, type_by_col)
    if chart_type == "scatter":
        return _build_scatter(result, type_by_col)
    return _build_table(result)


def _build_counter(result: QueryResult) -> dict:
    return {
        "$schema": _SCHEMA_URL,
        "data": {"values": result.rows},
        "mark": {"type": "text", "fontSize": 36},
        "encoding": {
            "text": {"field": result.columns[0], "type": "quantitative"},
        },
    }


def _build_line(result: QueryResult, type_by_col: dict[str, str]) -> dict:
    date_col = next(c for c, t in type_by_col.items() if t == _DateType)
    numeric_cols = [c for c, t in type_by_col.items() if t == _NumericType]
    if len(numeric_cols) == 1:
        return {
            "$schema": _SCHEMA_URL,
            "data": {"values": result.rows},
            "mark": "line",
            "encoding": {
                "x": {"field": date_col, "type": "temporal"},
                "y": {"field": numeric_cols[0], "type": "quantitative"},
            },
        }
    # Multi-series: fold the numeric columns.
    return {
        "$schema": _SCHEMA_URL,
        "data": {"values": result.rows},
        "transform": [{"fold": numeric_cols, "as": ["series", "value"]}],
        "mark": "line",
        "encoding": {
            "x": {"field": date_col, "type": "temporal"},
            "y": {"field": "value", "type": "quantitative"},
            "color": {"field": "series", "type": "nominal"},
        },
    }


def _build_bar(result: QueryResult, type_by_col: dict[str, str]) -> dict:
    str_col = next(c for c, t in type_by_col.items() if t == _StringType)
    num_col = next(c for c, t in type_by_col.items() if t == _NumericType)
    return {
        "$schema": _SCHEMA_URL,
        "data": {"values": result.rows},
        "mark": "bar",
        "encoding": {
            "x": {"field": str_col, "type": "nominal", "sort": "-y"},
            "y": {"field": num_col, "type": "quantitative"},
        },
    }


def _build_scatter(result: QueryResult, type_by_col: dict[str, str]) -> dict:
    numeric_cols = [c for c, t in type_by_col.items() if t == _NumericType]
    return {
        "$schema": _SCHEMA_URL,
        "data": {"values": result.rows},
        "mark": "point",
        "encoding": {
            "x": {"field": numeric_cols[0], "type": "quantitative"},
            "y": {"field": numeric_cols[1], "type": "quantitative"},
        },
    }


def _build_table(result: QueryResult) -> dict:
    return {
        "$schema": _SCHEMA_URL,
        "data": {"values": result.rows},
        "mark": "text",
        "encoding": {
            c: {"field": c, "type": "nominal"} for c in result.columns
        },
    }
