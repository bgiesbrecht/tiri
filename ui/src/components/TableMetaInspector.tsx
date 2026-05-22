import * as React from "react";
import {
  AlertTriangle,
  ChevronDown,
  Key,
  Link2,
  Search,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { Input } from "@/components/ui/input";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";
import type {
  MetadataConflict,
  TableColumnMeta,
  TableMetaResponse,
} from "@/lib/types";

/**
 * TableMetaInspector — right-panel widget. Renders the merged metadata
 * for one table: header with source badges + conflict count, a Fields
 * card (only populated fields), a Columns card (searchable + expandable
 * rows), and a Conflicts collapsible at the bottom if any disagreements
 * exist. Source badges are color-coded across known providers; unknown
 * providers fall through to the muted style.
 */
export function TableMetaInspector({
  table,
  loading,
}: {
  table: TableMetaResponse | null;
  loading?: boolean;
}) {
  if (loading) {
    return <InspectorSkeleton />;
  }
  if (!table) {
    return (
      <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
        Select a table on the left to inspect its metadata.
      </div>
    );
  }
  return (
    <div className="flex h-full flex-col gap-3">
      <InspectorHeader table={table} />
      <ScrollArea className="flex-1 pr-3">
        <div className="space-y-4 pb-2">
          <FieldsCard table={table} />
          <ColumnsCard columns={table.columns} />
          {table.conflicts.length > 0 && (
            <ConflictsCard
              conflicts={table.conflicts.map((c) => ({
                ...c,
                scope: "table" as const,
              }))}
            />
          )}
        </div>
      </ScrollArea>
    </div>
  );
}

function InspectorHeader({ table }: { table: TableMetaResponse }) {
  const allColumnConflicts = table.columns.flatMap((c) => c.conflicts);
  const conflictCount = table.conflicts.length + allColumnConflicts.length;
  return (
    <div className="space-y-2 border-b pb-3">
      <div className="flex items-center gap-2">
        <code className="font-mono text-sm">{table.name}</code>
      </div>
      <div className="flex flex-wrap items-center gap-1.5">
        {table.metadata_sources.map((source) => (
          <SourceBadge key={source} source={source} />
        ))}
        {conflictCount > 0 && (
          <Badge variant="warning" className="gap-1">
            <AlertTriangle className="h-3 w-3" />
            {conflictCount} conflict{conflictCount === 1 ? "" : "s"}
          </Badge>
        )}
      </div>
    </div>
  );
}

function FieldsCard({ table }: { table: TableMetaResponse }) {
  // Pair each row with the conflict for that field (if one exists at the
  // table level) so we can show the warning icon + expandable values.
  const conflictByField: Record<string, MetadataConflict> = {};
  for (const c of table.conflicts) {
    conflictByField[c.field] = c;
  }
  const rows: Array<{ label: string; field: string; value: React.ReactNode }> =
    [];
  if (table.description) {
    rows.push({
      label: "Description",
      field: "description",
      value: table.description,
    });
  }
  if (table.grain) {
    rows.push({ label: "Grain", field: "grain", value: table.grain });
  }
  if (table.domain) {
    rows.push({ label: "Domain", field: "domain", value: table.domain });
  }
  if (table.freshness) {
    rows.push({
      label: "Freshness",
      field: "freshness",
      value: table.freshness,
    });
  }
  if (table.default_date_column) {
    rows.push({
      label: "Default date column",
      field: "default_date_column",
      value: <code className="font-mono text-xs">{table.default_date_column}</code>,
    });
  }
  if (table.default_filter) {
    rows.push({
      label: "Default filter",
      field: "default_filter",
      value: <code className="font-mono text-xs">{table.default_filter}</code>,
    });
  }
  if (table.synonyms.length > 0) {
    rows.push({
      label: "Synonyms",
      field: "synonyms",
      value: (
        <div className="flex flex-wrap gap-1">
          {table.synonyms.map((s) => (
            <Badge key={s} variant="muted" className="text-[10px]">
              {s}
            </Badge>
          ))}
        </div>
      ),
    });
  }
  if (table.recommended_joins.length > 0) {
    rows.push({
      label: "Recommended joins",
      field: "recommended_joins",
      value: (
        <div className="flex flex-wrap gap-1">
          {table.recommended_joins.map((j) => (
            <Badge key={j} variant="muted" className="gap-1 font-mono text-[10px]">
              <Link2 className="h-2.5 w-2.5" />
              {j}
            </Badge>
          ))}
        </div>
      ),
    });
  }

  if (rows.length === 0) {
    return (
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm">Fields</CardTitle>
        </CardHeader>
        <CardContent className="text-xs text-muted-foreground">
          No semantic fields populated. Only the physical schema is available —
          add a metadata provider to enrich descriptions, grain, etc.
        </CardContent>
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm">Fields</CardTitle>
      </CardHeader>
      <CardContent className="space-y-2 text-sm">
        {rows.map(({ label, field, value }) => (
          <FieldRow
            key={field}
            label={label}
            value={value}
            conflict={conflictByField[field]}
          />
        ))}
      </CardContent>
    </Card>
  );
}

function FieldRow({
  label,
  value,
  conflict,
}: {
  label: string;
  value: React.ReactNode;
  conflict?: MetadataConflict;
}) {
  const [expanded, setExpanded] = React.useState(false);
  return (
    <div className="grid grid-cols-[140px_1fr_auto] items-start gap-2 border-b pb-2 last:border-b-0 last:pb-0">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className="text-sm leading-relaxed">
        {value}
        {conflict && expanded && (
          <ConflictDetail conflict={conflict} className="mt-2" />
        )}
      </div>
      {conflict ? (
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          className={cn(
            "inline-flex items-center gap-1 text-[10px]",
            "rounded border border-warning/40 bg-warning/10 px-1.5 py-0.5",
            "text-warning hover:bg-warning/20",
          )}
        >
          <AlertTriangle className="h-2.5 w-2.5" />
          {conflict.resolved_to}
        </button>
      ) : null}
    </div>
  );
}

function ColumnsCard({ columns }: { columns: TableColumnMeta[] }) {
  const [query, setQuery] = React.useState("");
  const filtered = React.useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return columns;
    return columns.filter((c) =>
      c.name.toLowerCase().includes(q) ||
      c.description.toLowerCase().includes(q) ||
      c.synonyms.some((s) => s.toLowerCase().includes(q)),
    );
  }, [columns, query]);

  return (
    <Card>
      <CardHeader className="pb-2">
        <div className="flex items-center justify-between gap-2">
          <CardTitle className="text-sm">
            Columns <span className="text-muted-foreground">({columns.length})</span>
          </CardTitle>
          <div className="relative w-48">
            <Search className="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
            <Input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Filter columns..."
              className="h-7 pl-7 text-xs"
            />
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-1 p-2 pt-1">
        {filtered.length === 0 ? (
          <div className="px-2 py-4 text-center text-xs text-muted-foreground">
            No columns match "{query}".
          </div>
        ) : (
          filtered.map((col) => <ColumnRow key={col.name} column={col} />)
        )}
      </CardContent>
    </Card>
  );
}

function ColumnRow({ column }: { column: TableColumnMeta }) {
  const [open, setOpen] = React.useState(false);
  const hasConflict = column.conflicts.length > 0;
  return (
    <Collapsible open={open} onOpenChange={setOpen}>
      <CollapsibleTrigger asChild>
        <button
          type="button"
          className={cn(
            "flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-xs",
            "hover:bg-muted/50",
            open && "bg-muted/30",
          )}
        >
          <ChevronDown
            className={cn(
              "h-3 w-3 text-muted-foreground transition-transform",
              open && "rotate-180",
            )}
          />
          <code className="font-mono text-xs">{column.name}</code>
          <span className="text-[10px] text-muted-foreground">{column.data_type}</span>
          {column.semantic_type && (
            <Badge variant="muted" className="text-[9px]">
              {column.semantic_type}
            </Badge>
          )}
          {column.is_primary_key && <Badge variant="outline" className="gap-1 text-[9px]"><Key className="h-2 w-2" />PK</Badge>}
          {column.is_foreign_key && <Badge variant="outline" className="gap-1 text-[9px]"><Link2 className="h-2 w-2" />FK</Badge>}
          {hasConflict && (
            <AlertTriangle className="h-3 w-3 text-warning" />
          )}
          <span className="ml-auto truncate text-muted-foreground">
            {column.description}
          </span>
        </button>
      </CollapsibleTrigger>
      <CollapsibleContent className="px-2 pb-2 pt-1">
        <div className="ml-5 space-y-2 border-l pl-3 text-xs">
          {column.description && (
            <div className="text-sm leading-relaxed">{column.description}</div>
          )}
          {column.value_description && (
            <div className="text-muted-foreground">
              <strong className="text-foreground">Values:</strong>{" "}
              {column.value_description}
            </div>
          )}
          {column.sample_values.length > 0 && (
            <div className="flex flex-wrap items-center gap-1">
              <strong className="text-foreground">Samples:</strong>
              {column.sample_values.map((s) => (
                <Badge key={s} variant="muted" className="text-[10px]">
                  {s}
                </Badge>
              ))}
            </div>
          )}
          {column.synonyms.length > 0 && (
            <div className="flex flex-wrap items-center gap-1">
              <strong className="text-foreground">Synonyms:</strong>
              {column.synonyms.map((s) => (
                <Badge key={s} variant="muted" className="text-[10px]">
                  {s}
                </Badge>
              ))}
            </div>
          )}
          {column.is_foreign_key && column.foreign_key_table && (
            <div className="text-muted-foreground">
              <strong className="text-foreground">Foreign key:</strong>{" "}
              <code className="font-mono text-[11px]">
                {column.foreign_key_table}
                {column.foreign_key_column && `.${column.foreign_key_column}`}
              </code>
            </div>
          )}
          {column.is_high_cardinality && (
            <div className="text-muted-foreground">
              <strong className="text-foreground">High cardinality</strong> —
              avoid SELECT DISTINCT or GROUP BY on this column.
            </div>
          )}
          <div className="flex flex-wrap items-center gap-1 pt-1">
            <span className="text-muted-foreground">Sources:</span>
            {column.metadata_sources.map((s) => (
              <SourceBadge key={s} source={s} />
            ))}
          </div>
          {column.conflicts.map((c, i) => (
            <ConflictDetail key={i} conflict={c} className="mt-2" />
          ))}
        </div>
      </CollapsibleContent>
    </Collapsible>
  );
}

function ConflictsCard({
  conflicts,
}: {
  conflicts: Array<MetadataConflict & { scope: "table" }>;
}) {
  const [open, setOpen] = React.useState(false);
  return (
    <Card>
      <Collapsible open={open} onOpenChange={setOpen}>
        <CollapsibleTrigger asChild>
          <CardHeader className="cursor-pointer pb-2 hover:bg-muted/30">
            <div className="flex items-center gap-2">
              <ChevronDown
                className={cn(
                  "h-3.5 w-3.5 transition-transform",
                  open && "rotate-180",
                )}
              />
              <CardTitle className="text-sm">
                {conflicts.length} table-level metadata{" "}
                {conflicts.length === 1 ? "conflict" : "conflicts"}
              </CardTitle>
              <AlertTriangle className="h-3.5 w-3.5 text-warning" />
            </div>
          </CardHeader>
        </CollapsibleTrigger>
        <CollapsibleContent>
          <CardContent className="space-y-3">
            {conflicts.map((c, i) => (
              <ConflictDetail key={i} conflict={c} />
            ))}
            <div className="border-t pt-2 text-xs italic text-muted-foreground">
              Conflicts are not errors — they are recorded when two providers
              disagree on a field. The later provider in the stack wins by
              design. Use this view to verify the right source is overriding.
            </div>
          </CardContent>
        </CollapsibleContent>
      </Collapsible>
    </Card>
  );
}

function ConflictDetail({
  conflict,
  className,
}: {
  conflict: MetadataConflict;
  className?: string;
}) {
  return (
    <div className={cn("rounded border border-warning/30 bg-warning/5 p-2", className)}>
      <div className="mb-1 flex items-center gap-1.5">
        <AlertTriangle className="h-3 w-3 text-warning" />
        <span className="text-xs font-medium">
          {conflict.field}
        </span>
        <span className="text-[10px] text-muted-foreground">
          {Object.keys(conflict.values).length} sources disagree
        </span>
      </div>
      <table className="w-full text-xs">
        <tbody>
          {Object.entries(conflict.values).map(([provider, value]) => {
            const isWinner = provider === conflict.resolved_to;
            return (
              <tr key={provider}>
                <td className="py-0.5 pr-2 align-top">
                  <SourceBadge source={provider} />
                </td>
                <td
                  className={cn(
                    "py-0.5 align-top",
                    isWinner ? "text-foreground" : "text-muted-foreground line-through",
                  )}
                >
                  {value}
                </td>
                <td className="py-0.5 pl-2 align-top text-[10px]">
                  {isWinner && <span className="text-success">won</span>}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

// ── Source-badge color mapping ─────────────────────────────────────────────
// Match colors across CLI/UI per the design notes in the build spec.
// uc_annotations → blue, domain_yaml → green, dbt_manifest → orange,
// delta_table → teal, room_config → purple, static → gray. Unknown
// providers fall through to muted.

const SOURCE_STYLES: Record<string, string> = {
  uc_annotations:
    "border-provider-databricks/40 bg-provider-databricks/10 text-provider-databricks",
  domain_yaml: "border-success/40 bg-success/10 text-success",
  tpch_domain: "border-success/40 bg-success/10 text-success",
  dbt_manifest: "border-provider-anthropic/40 bg-provider-anthropic/10 text-provider-anthropic",
  delta_table: "border-provider-custom/40 bg-provider-custom/10 text-provider-custom",
  room_config: "border-primary/40 bg-primary/10 text-primary",
  static: "border-muted-foreground/30 bg-muted text-muted-foreground",
  catalog: "border-muted-foreground/30 bg-muted text-muted-foreground",
};

export function SourceBadge({ source }: { source: string }) {
  const cls = SOURCE_STYLES[source];
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full border px-2 py-0.5 text-[10px] font-medium",
        cls ?? "border-muted-foreground/30 bg-muted text-muted-foreground",
      )}
      title={`Metadata source: ${source}`}
    >
      {source}
    </span>
  );
}

function InspectorSkeleton() {
  return (
    <div className="space-y-4">
      <div className="space-y-2 border-b pb-3">
        <Skeleton className="h-4 w-1/2" />
        <div className="flex gap-1">
          <Skeleton className="h-4 w-20" />
          <Skeleton className="h-4 w-24" />
        </div>
      </div>
      <Card>
        <CardHeader className="pb-2"><Skeleton className="h-4 w-16" /></CardHeader>
        <CardContent className="space-y-2">
          <Skeleton className="h-3 w-full" />
          <Skeleton className="h-3 w-5/6" />
          <Skeleton className="h-3 w-2/3" />
        </CardContent>
      </Card>
      <Card>
        <CardHeader className="pb-2"><Skeleton className="h-4 w-20" /></CardHeader>
        <CardContent className="space-y-2">
          {[0, 1, 2, 3, 4].map((i) => (
            <Skeleton key={i} className="h-6 w-full" />
          ))}
        </CardContent>
      </Card>
    </div>
  );
}
