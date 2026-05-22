import * as React from "react";
import { Calendar, DollarSign, Hash, Type } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { cn } from "@/lib/utils";

const DATE_PATTERN = /^\d{4}-\d{2}-\d{2}/;
const CURRENCY_HINTS = /(revenue|price|cost|amount|balance|sales|spend)/i;
const NUMERIC_HINTS = /(count|qty|quantity|total|sum|avg|rate|ratio|num)/i;

type ColumnType = "date" | "currency" | "number" | "string";

function detectColumnType(
  column: string,
  rows: Array<Record<string, unknown>>,
): ColumnType {
  if (CURRENCY_HINTS.test(column)) return "currency";
  // Inspect first non-null cell for value-based detection.
  for (const row of rows) {
    const v = row?.[column];
    if (v === null || v === undefined) continue;
    if (typeof v === "number") {
      return NUMERIC_HINTS.test(column) ? "number" : "number";
    }
    if (typeof v === "string") {
      if (DATE_PATTERN.test(v)) return "date";
      const asNum = Number(v);
      if (!Number.isNaN(asNum) && v.trim() !== "") {
        return NUMERIC_HINTS.test(column) ? "number" : "number";
      }
      return "string";
    }
    return "string";
  }
  return "string";
}

function ColumnIcon({ type }: { type: ColumnType }) {
  if (type === "date") return <Calendar className="h-3 w-3 text-muted-foreground" />;
  if (type === "currency") return <DollarSign className="h-3 w-3 text-muted-foreground" />;
  if (type === "number") return <Hash className="h-3 w-3 text-muted-foreground" />;
  return <Type className="h-3 w-3 text-muted-foreground" />;
}

function formatCell(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

export interface ResultTableProps {
  columns: string[];
  rows: Array<Record<string, unknown>>;
  rowCount?: number;
  expectedRowCount?: number | null;
  truncated?: boolean;
  className?: string;
}

const DEFAULT_VISIBLE = 10;

export function ResultTable({
  columns,
  rows,
  rowCount,
  expectedRowCount,
  truncated,
  className,
}: ResultTableProps) {
  const [expanded, setExpanded] = React.useState(false);
  const visible = expanded ? rows : rows.slice(0, DEFAULT_VISIBLE);
  const types = React.useMemo(
    () => columns.map((c) => detectColumnType(c, rows)),
    [columns, rows],
  );
  const total = rowCount ?? rows.length;

  // Row-count badge variant
  let rowBadgeVariant: "muted" | "success" | "destructive" | "warning" = "muted";
  if (expectedRowCount != null) {
    rowBadgeVariant = total === expectedRowCount ? "success" : "destructive";
  }
  const rowBadgeLabel =
    expectedRowCount != null
      ? `${total} row${total === 1 ? "" : "s"} · expected ${expectedRowCount}`
      : `${total} row${total === 1 ? "" : "s"}${truncated ? " (truncated)" : ""}`;

  return (
    <div className={cn("space-y-2", className)}>
      <div className="flex items-center justify-between text-xs text-muted-foreground">
        <Badge variant={rowBadgeVariant}>{rowBadgeLabel}</Badge>
        {rows.length > DEFAULT_VISIBLE && (
          <Button
            variant="ghost"
            size="sm"
            className="h-6 px-2 text-xs"
            onClick={() => setExpanded((v) => !v)}
          >
            {expanded ? "Show first 10" : `Show all ${rows.length} rows`}
          </Button>
        )}
      </div>
      <div className="rounded-md border overflow-auto">
        <Table>
          <TableHeader>
            <TableRow>
              {columns.map((col, i) => (
                <TableHead key={col} className="whitespace-nowrap">
                  <span className="inline-flex items-center gap-1.5">
                    <ColumnIcon type={types[i]} />
                    <span className="font-mono text-[11px] uppercase tracking-wider">
                      {col}
                    </span>
                  </span>
                </TableHead>
              ))}
            </TableRow>
          </TableHeader>
          <TableBody>
            {visible.map((row, i) => (
              <TableRow key={i}>
                {columns.map((col) => (
                  <TableCell
                    key={col}
                    className={cn(
                      "whitespace-nowrap text-sm font-mono tabular-nums",
                      typeof row?.[col] === "number" && "text-right",
                    )}
                  >
                    {formatCell(row?.[col])}
                  </TableCell>
                ))}
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>
    </div>
  );
}
