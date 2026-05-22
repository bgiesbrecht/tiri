"""Shared helpers for the agent layer.

Template loading and context-formatting helpers live here so each agent file
stays focused on its prompt/parsing logic.

Engine zero-I/O rule (CLAUDE.md): this module imports only from
`tiri.data_models` and `tiri.providers.base`. No SDK imports, no HTTP, no
filesystem access beyond loading the bundled prompt templates at import time.
Template loading happens once per module load — never per request.
"""

from __future__ import annotations

from pathlib import Path

from tiri.data_models import (
    ConversationTurn,
    ExampleSQL,
    JoinSpec,
    Metric,
    SqlSnippet,
    TableMeta,
)


_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "prompt_templates"


def load_template(name: str) -> str:
    """Load a prompt template by filename. Called once at module-load time
    by each agent module — never inside an async path or per-request."""
    path = _TEMPLATES_DIR / name
    return path.read_text()


def render(template: str, **kwargs: str) -> str:
    """Simple `{key}` substitution.

    DO NOT "fix" this to `str.format()`. The templates contain literal `{`
    and `}` inside JSON example blocks, which would crash `str.format`. The
    `str.replace` approach is also tolerant of the descriptive placeholders
    inside format-hint comments — those are documentation, not substitution
    slots, and stay literal because no caller passes a matching key.
    """
    out = template
    for key, value in kwargs.items():
        out = out.replace("{" + key + "}", value)
    return out


# ────────────────────────────────────────────────────────────────────────────
# Context formatters — produce the human-readable strings injected into prompts
# ────────────────────────────────────────────────────────────────────────────


def format_table_list(tables: dict[str, TableMeta]) -> str:
    """Compact table list for IntentAgent prompts."""
    if not tables:
        return "(none)"
    lines: list[str] = []
    for full_name, tm in tables.items():
        lines.append(f"{full_name} — {tm.description or '(no description)'}")
        if tm.domain:
            lines.append(f"  Domain: {tm.domain}")
        if tm.synonyms:
            lines.append(f"  Synonyms: {', '.join(tm.synonyms)}")
    return "\n".join(lines)


def format_snippet_list(snippets: list[SqlSnippet]) -> str:
    """Compact snippet list for IntentAgent prompts."""
    if not snippets:
        return "(none)"
    lines: list[str] = []
    for s in snippets:
        lines.append(
            f"{s.display_name} ({s.kind}): {s.instruction or '(no instruction)'}"
        )
        if s.synonyms:
            lines.append(f"  Synonyms: {', '.join(s.synonyms)}")
    return "\n".join(lines)


def format_metric_list(metrics: list[Metric]) -> str:
    """Compact metric list for IntentAgent prompts."""
    if not metrics:
        return "(none)"
    lines: list[str] = []
    for m in metrics:
        desc = m.description or "(no description)"
        lines.append(f"{m.display_name} ({m.name}) — {desc}")
        if m.synonyms:
            lines.append(f"  Synonyms: {', '.join(m.synonyms)}")
    return "\n".join(lines)


def format_schemas_for_sql(
    table_full_names: list[str], tables: dict[str, TableMeta]
) -> str:
    """Full schema dump for SQLAgent prompts. Includes grain, default_filter,
    default_date_column, column annotations, sample values, high-cardinality
    warnings — everything the SQL agent needs."""
    if not table_full_names:
        return "(none)"
    lines: list[str] = []
    for full_name in table_full_names:
        tm = tables.get(full_name)
        if tm is None:
            continue
        lines.append(f"{full_name} — {tm.description or '(no description)'}")
        if tm.grain:
            lines.append(f"  Grain: {tm.grain}")
        if tm.default_date_column:
            lines.append(f"  Default date column: {tm.default_date_column}")
        if tm.default_filter:
            lines.append(
                f"  Default filter (apply unless user says otherwise): "
                f"{tm.default_filter}"
            )
        lines.append("  Columns:")
        for c in tm.columns:
            type_tag = f"[{c.semantic_type}]" if c.semantic_type else ""
            head = f"    {c.name}  {c.data_type}  {type_tag}".rstrip()
            if c.description:
                head += f"  — {c.description}"
            lines.append(head)
            if c.synonyms:
                lines.append(f"      Synonyms: {', '.join(c.synonyms)}")
            if c.value_description:
                lines.append(f"      Values: {c.value_description}")
            if c.is_high_cardinality:
                lines.append(
                    "      HIGH CARDINALITY — avoid in GROUP BY without filters"
                )
            elif c.sample_values:
                lines.append(
                    f"      Sample values: {', '.join(c.sample_values)}"
                )
    return "\n".join(lines)


def format_joins(joins: list[JoinSpec]) -> str:
    if not joins:
        return "(none)"
    lines: list[str] = []
    for j in joins:
        lines.append(
            f"{j.left_alias}.{j.left_table} JOIN {j.right_alias}.{j.right_table} "
            f"ON {j.join_on}"
        )
        lines.append(f"  Relationship: {j.relationship_type}")
        if j.instruction:
            lines.append(f"  Note: {j.instruction}")
    return "\n".join(lines)


def format_snippets_for_sql(snippets: list[SqlSnippet]) -> str:
    if not snippets:
        return "(none)"
    lines: list[str] = []
    for s in snippets:
        head = f"{s.display_name} ({s.kind}): {s.sql}"
        if s.instruction:
            head += f"  -- {s.instruction}"
        lines.append(head)
        if s.synonyms:
            lines.append(f"  Synonyms: {', '.join(s.synonyms)}")
    return "\n".join(lines)


def format_metrics_for_sql(metrics: list[Metric]) -> str:
    if not metrics:
        return "(none)"
    lines: list[str] = []
    for m in metrics:
        lines.append(f"{m.display_name} ({m.name})")
        lines.append(f"  Definition: {m.sql}")
        lines.append(f"  Grain: {m.grain}")
        if m.description:
            lines.append(f"  Description: {m.description}")
        if m.synonyms:
            lines.append(f"  Synonyms: {', '.join(m.synonyms)}")
        if m.dimensions:
            lines.append(f"  Valid dimensions: {', '.join(m.dimensions)}")
        if m.filters:
            lines.append(f"  Always-apply filters: {'; '.join(m.filters)}")
        if m.unit:
            lines.append(f"  Unit: {m.unit}")
    return "\n".join(lines)


def format_default_filters(filters: list[str]) -> str:
    return "\n".join(f"- {f}" for f in filters) if filters else "(none)"


def format_mcp_context(entries: list[str]) -> str:
    """EXT-5: format external MCP tool resolutions for inclusion in agent
    prompts. `(none)` when the room has no MCP servers configured, when
    every call failed, or when MCP is wholly disabled for this deployment."""
    if not entries:
        return "(none)"
    return "\n".join(f"- {e}" for e in entries)


def format_examples(examples: list[ExampleSQL]) -> str:
    if not examples:
        return "(none)"
    parts: list[str] = []
    for ex in examples:
        parts.append(f"Q: {ex.question}\nSQL: {ex.sql}")
    return "\n\n".join(parts)


def format_history(history: list[ConversationTurn]) -> str:
    if not history:
        return "(none)"
    parts: list[str] = []
    for turn in history:
        if turn.sql:
            parts.append(f"Q: {turn.question}\nSQL: {turn.sql}")
        elif turn.clarification_question:
            parts.append(
                f"Q: {turn.question}\nClarification: {turn.clarification_question}"
            )
        # Skip error turns — they add noise.
    return "\n\n".join(parts)
