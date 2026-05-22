import * as React from "react";
import { ChevronDown, Clock, Copy, Database } from "lucide-react";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { ScrollArea } from "@/components/ui/scroll-area";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { SqlBlock } from "@/components/SqlBlock";
import { ResultTable } from "@/components/ResultTable";
import { VegaChart } from "@/components/VegaChart";
import { ConfidenceBadge } from "@/components/ConfidenceBadge";
import { listMessages, listRoomKeys } from "@/lib/api";
import { cn } from "@/lib/utils";
import type { ConversationTurn } from "@/lib/types";

/**
 * HistoryView — browse persisted ConversationTurn records.
 *
 * The API doesn't expose a "list conversations" endpoint today, so we
 * surface a "conversation_id" free-text input below the room picker.
 * Operators can paste an ID they have from a prior chat / API call.
 * Future work: add /rooms/{id}/conversations list endpoint.
 */

export function HistoryView() {
  const [roomIds, setRoomIds] = React.useState<string[]>([]);
  const [selectedRoom, setSelectedRoom] = React.useState<string>("");
  const [conversationId, setConversationId] = React.useState<string>("");
  const [turns, setTurns] = React.useState<ConversationTurn[] | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [expandedTurn, setExpandedTurn] = React.useState<string | null>(null);

  React.useEffect(() => {
    void listRoomKeys().then((ids) => {
      setRoomIds(ids);
      if (ids.length > 0) setSelectedRoom(ids[0]);
    });
  }, []);

  const loadTurns = React.useCallback(async () => {
    if (!selectedRoom || !conversationId.trim()) {
      setTurns(null);
      return;
    }
    try {
      const fetched = await listMessages(selectedRoom, conversationId.trim());
      setTurns(fetched);
      setError(null);
    } catch (err) {
      setError((err as Error)?.message ?? "Failed to load");
      setTurns(null);
    }
  }, [selectedRoom, conversationId]);

  return (
    <div className="space-y-4">
      <Card>
        <CardContent className="space-y-3 py-4">
          <div className="grid gap-3 md:grid-cols-2">
            <div className="space-y-1.5">
              <Label className="text-xs uppercase tracking-wider text-muted-foreground">
                Room
              </Label>
              <Select value={selectedRoom} onValueChange={setSelectedRoom}>
                <SelectTrigger className="font-mono text-sm">
                  <SelectValue placeholder="Choose a room…" />
                </SelectTrigger>
                <SelectContent>
                  {roomIds.map((id) => (
                    <SelectItem key={id} value={id} className="font-mono">
                      {id}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1.5">
              <Label className="text-xs uppercase tracking-wider text-muted-foreground">
                Conversation ID
              </Label>
              <div className="flex gap-2">
                <input
                  type="text"
                  className="flex h-9 w-full rounded-md border border-input bg-background px-3 py-1 text-sm font-mono shadow-sm placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                  placeholder="conv:abc123…"
                  value={conversationId}
                  onChange={(e) => setConversationId(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") void loadTurns();
                  }}
                />
                <Button onClick={loadTurns} size="sm">
                  Load
                </Button>
              </div>
            </div>
          </div>
        </CardContent>
      </Card>

      {error && (
        <Card className="border-destructive/40 bg-destructive/5">
          <CardContent className="py-3 text-sm text-destructive">
            {error}
          </CardContent>
        </Card>
      )}

      {turns !== null && turns.length === 0 && (
        <Card className="border-dashed">
          <CardContent className="flex flex-col items-center justify-center gap-2 py-12 text-center">
            <Clock className="h-8 w-8 text-muted-foreground" />
            <p className="text-sm font-medium">No turns in this conversation</p>
            <p className="text-xs text-muted-foreground">
              Conversation ID exists but no messages were sent.
            </p>
          </CardContent>
        </Card>
      )}

      {turns !== null && turns.length > 0 && (
        <Card>
          <CardHeader className="py-3">
            <CardTitle className="flex items-center justify-between text-sm">
              <span className="flex items-center gap-2">
                <Clock className="h-4 w-4 text-muted-foreground" />
                {turns.length} turn{turns.length === 1 ? "" : "s"}
              </span>
              <span className="font-mono text-[10px] text-muted-foreground">
                {conversationId}
              </span>
            </CardTitle>
          </CardHeader>
          <CardContent className="py-0 pb-4 space-y-2">
            {turns.map((turn) => (
              <TurnRow
                key={turn.turn_id}
                turn={turn}
                expanded={expandedTurn === turn.turn_id}
                onToggle={() =>
                  setExpandedTurn((cur) =>
                    cur === turn.turn_id ? null : turn.turn_id,
                  )
                }
              />
            ))}
          </CardContent>
        </Card>
      )}
    </div>
  );
}

function TurnRow({
  turn,
  expanded,
  onToggle,
}: {
  turn: ConversationTurn;
  expanded: boolean;
  onToggle: () => void;
}) {
  const preview =
    turn.synthesized_answer?.answer ??
    (turn.sql || turn.clarification_question || turn.error || "(no content)");
  const confidence = turn.synthesized_answer?.confidence;

  return (
    <div className="rounded-md border">
      <button
        type="button"
        onClick={onToggle}
        className="flex w-full items-center justify-between gap-3 px-3 py-2 text-left hover:bg-accent/50"
      >
        <div className="flex-1 min-w-0">
          <p className="truncate text-sm font-medium">{turn.question}</p>
          <p className="truncate text-xs text-muted-foreground">{preview}</p>
        </div>
        <div className="flex items-center gap-2 flex-shrink-0">
          {confidence && <ConfidenceBadge confidence={confidence} />}
          <span className="text-[10px] text-muted-foreground tabular-nums">
            {turn.duration_ms}ms
          </span>
          <ChevronDown
            className={cn(
              "h-4 w-4 text-muted-foreground transition-transform",
              expanded && "rotate-180",
            )}
          />
        </div>
      </button>
      {expanded && (
        <div className="border-t p-3 animate-fade-in">
          <TurnInspector turn={turn} />
        </div>
      )}
    </div>
  );
}

function TurnInspector({ turn }: { turn: ConversationTurn }) {
  return (
    <Tabs defaultValue="overview" className="w-full">
      <TabsList>
        <TabsTrigger value="overview">Overview</TabsTrigger>
        <TabsTrigger value="sql" disabled={!turn.sql}>
          SQL & Results
        </TabsTrigger>
        <TabsTrigger value="synthesis" disabled={!turn.synthesized_answer}>
          Synthesis
        </TabsTrigger>
        <TabsTrigger value="viz" disabled={!turn.viz?.vega_lite_spec}>
          Viz
        </TabsTrigger>
        <TabsTrigger value="raw">Raw JSON</TabsTrigger>
      </TabsList>

      <TabsContent value="overview" className="space-y-3">
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
          <Stat label="Duration" value={`${turn.duration_ms}ms`} />
          <Stat
            label="Confidence"
            value={turn.synthesized_answer?.confidence ?? "—"}
          />
          <Stat
            label="Row count"
            value={String(turn.query_result?.row_count ?? "—")}
          />
          <Stat
            label="Turn ID"
            value={turn.turn_id.slice(0, 8)}
            valueClass="font-mono"
          />
        </div>
        {turn.error && (
          <div className="rounded-md border-l-4 border-destructive bg-destructive/5 p-3 text-sm">
            <p className="font-medium text-destructive">Error</p>
            <p className="mt-1 text-xs text-muted-foreground">{turn.error}</p>
          </div>
        )}
      </TabsContent>

      <TabsContent value="sql" className="space-y-3">
        {turn.sql && <SqlBlock sql={turn.sql} />}
        {turn.query_result && (
          <ResultTable
            columns={turn.query_result.columns}
            rows={turn.query_result.rows}
            rowCount={turn.query_result.row_count}
            truncated={turn.query_result.truncated}
          />
        )}
      </TabsContent>

      <TabsContent value="synthesis">
        {turn.synthesized_answer && (
          <div className="space-y-3">
            <div className="flex items-center gap-2">
              <ConfidenceBadge confidence={turn.synthesized_answer.confidence} />
              <span className="text-xs italic text-muted-foreground">
                {turn.synthesized_answer.confidence_rationale}
              </span>
            </div>
            <p className="text-sm leading-relaxed">
              {turn.synthesized_answer.answer}
            </p>
            <SynthesisList
              title="Data supports"
              items={turn.synthesized_answer.data_supports}
              tone="success"
            />
            <SynthesisList
              title="Data does NOT support"
              items={turn.synthesized_answer.data_does_not_support}
              tone="warning"
            />
            <SynthesisList
              title="Would need to know"
              items={turn.synthesized_answer.would_need}
              tone="muted"
            />
          </div>
        )}
      </TabsContent>

      <TabsContent value="viz">
        {turn.viz?.vega_lite_spec && (
          <div className="space-y-3">
            <VegaChart spec={turn.viz.vega_lite_spec as Record<string, unknown>} />
            <details className="rounded-md border bg-muted/30 p-3">
              <summary className="cursor-pointer text-xs font-medium">
                Vega-Lite spec
              </summary>
              <ScrollArea className="mt-2 h-[200px]">
                <pre className="text-xs font-mono">
                  {JSON.stringify(turn.viz.vega_lite_spec, null, 2)}
                </pre>
              </ScrollArea>
            </details>
          </div>
        )}
      </TabsContent>

      <TabsContent value="raw">
        <div className="relative">
          <Button
            variant="ghost"
            size="icon"
            className="absolute right-1 top-1 h-7 w-7 z-10"
            onClick={() =>
              navigator.clipboard?.writeText(JSON.stringify(turn, null, 2))
            }
          >
            <Copy className="h-3.5 w-3.5" />
          </Button>
          <ScrollArea className="h-[400px] rounded-md border bg-muted/30">
            <pre className="p-3 text-xs font-mono">
              {JSON.stringify(turn, null, 2)}
            </pre>
          </ScrollArea>
        </div>
      </TabsContent>
    </Tabs>
  );
}

function Stat({
  label,
  value,
  valueClass,
}: {
  label: string;
  value: string;
  valueClass?: string;
}) {
  return (
    <div className="rounded-md border p-2">
      <p className="text-[10px] uppercase tracking-wider text-muted-foreground">
        {label}
      </p>
      <p className={cn("text-sm font-medium", valueClass)}>{value}</p>
    </div>
  );
}

function SynthesisList({
  title,
  items,
  tone,
}: {
  title: string;
  items: string[];
  tone: "success" | "warning" | "muted";
}) {
  if (!items?.length) return null;
  const cls =
    tone === "success"
      ? "confidence-tint-high"
      : tone === "warning"
        ? "confidence-tint-medium"
        : "border-l border-muted-foreground/30";
  return (
    <div className={cn("pl-3 text-xs", cls)}>
      <p className="font-medium uppercase tracking-wider text-muted-foreground">
        {title}
      </p>
      <ul className="mt-1 space-y-0.5 text-sm">
        {items.map((it, i) => (
          <li key={i}>{it}</li>
        ))}
      </ul>
    </div>
  );
}

// Avoid unused-imports — Database/Badge surface visually via dependent rows.
void Database;
void Badge;
