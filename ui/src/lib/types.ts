/**
 * Wire-format types for the Tiri API. Kept narrow — only the fields the
 * UI actually reads. Server types are the source of truth; if a field is
 * missing here add it rather than relying on `as any`.
 */

// ── Room config (subset — full shape is in tiri/data_models.py RoomConfig) ──

export interface RoomConfig {
  room_id: string;
  title: string;
  tables: string[];
  warehouse_id: string;
  text_instruction?: string;
  examples?: Array<{ id: string; question: string; sql: string }>;
  joins?: Array<Record<string, unknown>>;
  sql_filters?: Array<Record<string, unknown>>;
  sql_expressions?: Array<Record<string, unknown>>;
  sql_measures?: Array<Record<string, unknown>>;
  metrics?: Array<Record<string, unknown>>;
  sample_questions?: string[];
  benchmarks?: Benchmark[];
  default_filters?: string[];
  mcp_servers?: string[];
  hypothesis_mode_enabled?: boolean;
  domain_knowledge?: string[];
}

export interface Benchmark {
  id?: string;
  question: string;
  expected_sql: string;
  expected_row_count?: number | null;
  notes?: string;
}

// ── /rooms/{id}/tables ────────────────────────────────────────────────────

export interface MetadataConflict {
  field: string;
  values: Record<string, string>;
  resolved_to: string;
}

export interface TableColumnMeta {
  name: string;
  data_type: string;
  description: string;
  synonyms: string[];
  sample_values: string[];
  value_description: string;
  semantic_type: string;
  currency_code: string;
  date_format: string;
  is_primary_key: boolean;
  is_foreign_key: boolean;
  foreign_key_table: string;
  foreign_key_column: string;
  is_high_cardinality: boolean;
  exclude_from_select_star: boolean;
  metadata_sources: string[];
  conflicts: MetadataConflict[];
}

export interface TableMetaResponse {
  name: string;
  description: string;
  synonyms: string[];
  grain: string;
  domain: string;
  freshness: string;
  default_date_column: string;
  default_filter: string;
  recommended_joins: string[];
  row_count: number | null;
  metadata_sources: string[];
  conflicts: MetadataConflict[];
  columns: TableColumnMeta[];
}

export interface SchemaMetaResponse {
  name: string;
  description: string;
  domain: string;
  freshness: string;
  owner: string;
  synonyms: string[];
  notes: string;
  metadata_sources: string[];
}

export interface RoomTablesResponse {
  room_id: string;
  schemas: SchemaMetaResponse[];
  tables: TableMetaResponse[];
}

// ── /config/routing response ─────────────────────────────────────────────

export interface ConfigRoutingResponse {
  providers: Array<{ name: string; type: ProviderType }>;
  routing: {
    intent: string;
    planning: string;
    sql: string;
    synthesis: string;
    clarify: string;
    viz_summary: string;
    embed: string;
  };
}

export type ProviderType =
  | "databricks"
  | "anthropic"
  | "openai"
  | "ollama"
  | "custom";

// ── SSE events from /conversations/{id}/messages/stream ──────────────────

export type SSEEvent =
  | { type: "status"; text: string }
  | { type: "mcp_context"; entries: string[] }
  | {
      type: "plan";
      steps: Array<{
        step_id: string;
        description: string;
        depends_on: string[];
      }>;
      synthesis_instruction: string;
    }
  | { type: "sql"; sql: string }
  | {
      type: "result";
      columns: string[];
      rows: Array<Record<string, unknown>>;
      truncated: boolean;
    }
  | {
      type: "steps";
      results: Array<{
        step_id: string;
        description: string;
        sql: string;
        columns: string[];
        row_count: number;
      }>;
    }
  | { type: "viz"; spec: Record<string, unknown>; summary: string }
  | {
      type: "synthesis";
      answer: string;
      data_supports: string[];
      data_does_not_support: string[];
      would_need: string[];
      confidence: "high" | "medium" | "low";
      confidence_rationale: string;
    }
  | {
      type: "hypotheses";
      disclaimer: string;
      confidence: "low";
      hypotheses: Array<{
        statement: string;
        supporting_patterns: string[];
        contradicting_patterns: string[];
        testability: string;
        suggested_test: string | null;
        domain_knowledge_used: string[];
      }>;
    }
  | { type: "clarify"; question: string }
  | { type: "error"; message: string }
  | { type: "done"; turn_id: string };

// ── ConversationTurn (subset — full shape is in tiri/data_models.py) ─────

export interface ConversationTurn {
  turn_id: string;
  room_id: string;
  conversation_id: string;
  question: string;
  sql?: string | null;
  query_result?: {
    columns: string[];
    rows: Array<Record<string, unknown>>;
    row_count: number;
    truncated: boolean;
    duration_ms: number;
  } | null;
  viz?: {
    chart_type: string;
    vega_lite_spec: Record<string, unknown>;
    summary: string;
  } | null;
  clarification_question?: string | null;
  error?: string | null;
  duration_ms: number;
  synthesized_answer?: {
    answer: string;
    data_supports: string[];
    data_does_not_support: string[];
    would_need: string[];
    confidence: "high" | "medium" | "low";
    confidence_rationale: string;
  } | null;
  hypothesis_result?: {
    hypotheses: Array<{
      statement: string;
      supporting_patterns: string[];
      contradicting_patterns: string[];
      testability: string;
      suggested_test: string | null;
      domain_knowledge_used: string[];
    }>;
    confidence: "low";
    disclaimer: string;
  } | null;
}

// ── Backend selection ────────────────────────────────────────────────────

export interface Backend {
  /** Provider name from /config/routing, e.g. "databricks", "anthropic". */
  provider: string;
  /** Provider type, used for the badge color. */
  type: ProviderType;
  /** Concrete model identifier, e.g. "databricks-meta-llama-3-3-70b-instruct". */
  model: string;
  /** Full "provider::model" identifier — what gets sent as model_override. */
  id: string;
  /** Human label for the UI. */
  label: string;
  /** Whether this entry was added via "Add custom backend" rather than
   * derived from /config/routing. */
  custom?: boolean;
}
