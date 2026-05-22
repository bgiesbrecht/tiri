import * as React from "react";
import { AlertTriangle, ChevronDown, Lightbulb } from "lucide-react";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { SqlBlock } from "@/components/SqlBlock";
import { ResultTable } from "@/components/ResultTable";
import { VegaChart } from "@/components/VegaChart";
import { ConfidenceBadge } from "@/components/ConfidenceBadge";
import { StreamStatus } from "@/components/StreamStatus";
import { useSSEStream } from "@/hooks/use-sse-stream";
import { cn } from "@/lib/utils";
import type { Backend } from "@/lib/types";

/**
 * ResultColumn — one column in the AskView grid for a single backend.
 *
 * Lifecycle:
 *   - Mount with (roomId, backend, question) — auto-creates a conversation
 *     and kicks off an SSE stream. Each backend runs against its own
 *     conversation_id (preserves per-backend history).
 *   - Renders sections progressively as events arrive (plan → sql →
 *     result → viz → synthesis → hypotheses).
 *   - Reports completion via onComplete so AskView can re-enable the
 *     Ask button when ALL columns finish.
 *   - Registers its `cancel` function via registerStop so the parent
 *     Stop button can abort all columns at once.
 */

const PROVIDER_VARIANTS = {
  databricks: "databricks",
  anthropic: "anthropic",
  openai: "openai",
  ollama: "ollama",
  custom: "custom",
} as const;

interface ResultColumnProps {
  roomId: string;
  backend: Backend;
  question: string;
  ensureConversation: (roomId: string, backendId: string) => Promise<string>;
  onComplete: () => void;
  registerStop: (cancel: () => void) => void;
}

export function ResultColumn({
  roomId,
  backend,
  question,
  ensureConversation,
  onComplete,
  registerStop,
}: ResultColumnProps) {
  const sse = useSSEStream();
  const { state, events, stage, startedAt, finishedAt, start, cancel } = sse;
  const startedRef = React.useRef(false);

  React.useEffect(() => {
    if (startedRef.current) return;
    startedRef.current = true;
    void (async () => {
      try {
        const conv = await ensureConversation(roomId, backend.id);
        await start({
          roomId,
          conversationId: conv,
          question,
          modelOverride: backend.id,
        });
      } catch (err) {
        // SSE hook will already have updated its state if start() failed
        // after connection; ensureConversation failures land here.
        console.error("ResultColumn start error", err);
      }
    })();
  }, [start, ensureConversation, roomId, backend.id, question]);

  React.useEffect(() => {
    registerStop(cancel);
  }, [cancel, registerStop]);

  React.useEffect(() => {
    if (state === "done" || state === "error") onComplete();
  }, [state, onComplete]);

  const duration =
    startedAt && finishedAt ? `${finishedAt - startedAt}ms` : null;
  const variant = PROVIDER_VARIANTS[backend.type];

  return (
    <Card
      className={cn(
        "flex flex-col gap-0 overflow-hidden",
        state === "error" && "border-destructive/40",
      )}
    >
      <CardHeader className="space-y-2 py-3">
        <div className="flex items-center justify-between gap-2">
          <CardTitle className="flex flex-wrap items-center gap-2 text-sm">
            <Badge variant={variant}>{backend.provider}</Badge>
            <span className="font-mono text-xs">{backend.model}</span>
          </CardTitle>
          {duration && state === "done" && (
            <span className="text-[11px] text-muted-foreground tabular-nums">
              {duration}
            </span>
          )}
        </div>
        <StreamStatus
          stage={stage}
          state={state}
          error={events.error?.message}
        />
      </CardHeader>

      <Separator />

      <CardContent className="space-y-4 py-4">
        {/* MCP context */}
        {events.mcpContext && events.mcpContext.entries.length > 0 && (
          <SectionShell title="External context (MCP)">
            <ul className="space-y-1 text-xs text-muted-foreground">
              {events.mcpContext.entries.map((entry, i) => (
                <li key={i} className="rounded bg-muted/50 px-2 py-1 font-mono">
                  {entry}
                </li>
              ))}
            </ul>
          </SectionShell>
        )}

        {/* Plan */}
        {events.plan && events.plan.steps.length > 1 && (
          <SectionShell title="Reasoning plan" defaultOpen={state === "running"}>
            <ol className="space-y-2 text-sm">
              {events.plan.steps.map((step, i) => (
                <li key={step.step_id} className="flex gap-2">
                  <span className="font-mono text-xs text-muted-foreground">
                    {i + 1}.
                  </span>
                  <div className="flex-1">
                    <p className="leading-snug">{step.description}</p>
                    {step.depends_on.length > 0 && (
                      <p className="mt-0.5 text-[10px] text-muted-foreground">
                        depends on {step.depends_on.join(", ")}
                      </p>
                    )}
                  </div>
                </li>
              ))}
            </ol>
            {events.plan.synthesis_instruction && (
              <p className="mt-3 text-[11px] italic text-muted-foreground">
                Synthesis: {events.plan.synthesis_instruction}
              </p>
            )}
          </SectionShell>
        )}

        {/* Clarification */}
        {events.clarify && (
          <SectionShell title="Clarification requested">
            <p className="rounded-md border-l-4 border-warning bg-warning/5 p-3 text-sm">
              {events.clarify.question}
            </p>
          </SectionShell>
        )}

        {/* SQL */}
        {events.sql && (
          <SectionShell title="SQL">
            <SqlBlock
              sql={events.sql.sql}
              step={
                events.steps && events.steps.results.length > 1
                  ? { current: 1, total: events.steps.results.length }
                  : undefined
              }
            />
          </SectionShell>
        )}

        {/* Primary result */}
        {events.result && (
          <SectionShell title="Result">
            <ResultTable
              columns={events.result.columns}
              rows={events.result.rows}
              truncated={events.result.truncated}
            />
          </SectionShell>
        )}

        {/* Multi-step summary */}
        {events.steps && events.steps.results.length > 1 && (
          <SectionShell title="Step results">
            <div className="space-y-2 text-xs">
              {events.steps.results.map((s, i) => (
                <div key={s.step_id} className="rounded-md border p-2">
                  <div className="flex items-center justify-between">
                    <span className="font-mono">
                      {i + 1}. {s.step_id}
                    </span>
                    <Badge variant="muted">{s.row_count} rows</Badge>
                  </div>
                  <p className="mt-1 text-muted-foreground">{s.description}</p>
                </div>
              ))}
            </div>
          </SectionShell>
        )}

        {/* Viz */}
        {events.viz && (
          <SectionShell title="Chart">
            <VegaChart
              spec={events.viz.spec}
              filename={`${backend.provider}-${roomId}`}
            />
            {events.viz.summary && (
              <p className="mt-2 text-xs text-muted-foreground italic">
                {events.viz.summary}
              </p>
            )}
          </SectionShell>
        )}

        {/* Synthesis */}
        {events.synthesis && (
          <SectionShell title="Synthesis">
            <div className="space-y-3">
              <ConfidenceBadge confidence={events.synthesis.confidence} />
              <p className="text-sm leading-relaxed">{events.synthesis.answer}</p>
              {events.synthesis.confidence_rationale && (
                <p className="text-xs text-muted-foreground italic">
                  {events.synthesis.confidence_rationale}
                </p>
              )}
              {events.synthesis.data_supports.length > 0 && (
                <BulletList
                  label="Data supports"
                  items={events.synthesis.data_supports}
                  tone="success"
                />
              )}
              {events.synthesis.data_does_not_support.length > 0 && (
                <BulletList
                  label="Data does NOT support"
                  items={events.synthesis.data_does_not_support}
                  tone="warning"
                />
              )}
              {events.synthesis.would_need.length > 0 && (
                <BulletList
                  label="Would need to know"
                  items={events.synthesis.would_need}
                  tone="muted"
                />
              )}
            </div>
          </SectionShell>
        )}

        {/* Hypotheses */}
        {events.hypotheses && (
          <SectionShell title="Hypotheses">
            <div className="space-y-3">
              <div className="rounded-md border-l-4 border-warning bg-warning/5 p-3 text-xs">
                <p className="flex items-center gap-1.5 font-medium">
                  <Lightbulb className="h-3.5 w-3.5" />
                  Provisional — confidence: low
                </p>
                <p className="mt-1 text-muted-foreground">
                  {events.hypotheses.disclaimer}
                </p>
              </div>
              {events.hypotheses.hypotheses.map((h, i) => (
                <div key={i} className="rounded-md border p-3 text-sm space-y-2">
                  <p className="italic">{h.statement}</p>
                  {h.supporting_patterns.length > 0 && (
                    <BulletList
                      label="Supporting"
                      items={h.supporting_patterns}
                      tone="success"
                    />
                  )}
                  {h.contradicting_patterns.length > 0 && (
                    <BulletList
                      label="Contradicting"
                      items={h.contradicting_patterns}
                      tone="warning"
                    />
                  )}
                  <div className="flex items-center justify-between gap-2 pt-1">
                    <Badge variant="muted" className="text-[10px]">
                      {h.testability}
                    </Badge>
                    {h.domain_knowledge_used.length > 0 && (
                      <span className="text-[10px] text-muted-foreground">
                        Used {h.domain_knowledge_used.length} domain axiom
                        {h.domain_knowledge_used.length === 1 ? "" : "s"}
                      </span>
                    )}
                  </div>
                  {h.suggested_test && (
                    <p className="rounded bg-muted px-2 py-1 text-[11px] font-mono">
                      Test: {h.suggested_test}
                    </p>
                  )}
                </div>
              ))}
            </div>
          </SectionShell>
        )}

        {/* Error */}
        {events.error && (
          <div className="rounded-md border-l-4 border-destructive bg-destructive/5 p-3 text-sm">
            <p className="flex items-center gap-1.5 font-medium text-destructive">
              <AlertTriangle className="h-3.5 w-3.5" />
              Error
            </p>
            <p className="mt-1 font-mono text-xs text-muted-foreground break-all">
              {events.error.message}
            </p>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function SectionShell({
  title,
  children,
  defaultOpen = true,
}: {
  title: string;
  children: React.ReactNode;
  defaultOpen?: boolean;
}) {
  const [open, setOpen] = React.useState(defaultOpen);
  return (
    <div className="animate-fade-in">
      <Collapsible open={open} onOpenChange={setOpen}>
        <CollapsibleTrigger asChild>
          <Button
            variant="ghost"
            size="sm"
            className="h-7 -ml-2 px-2 text-xs uppercase tracking-wider text-muted-foreground"
          >
            <ChevronDown
              className={cn(
                "h-3.5 w-3.5 transition-transform",
                !open && "-rotate-90",
              )}
            />
            {title}
          </Button>
        </CollapsibleTrigger>
        <CollapsibleContent className="mt-2 data-[state=open]:animate-fade-in">
          {children}
        </CollapsibleContent>
      </Collapsible>
    </div>
  );
}

function BulletList({
  label,
  items,
  tone,
}: {
  label: string;
  items: string[];
  tone: "success" | "warning" | "muted";
}) {
  const toneClass =
    tone === "success"
      ? "confidence-tint-high"
      : tone === "warning"
        ? "confidence-tint-medium"
        : "border-l border-muted-foreground/30";
  return (
    <div className={cn("pl-3 text-xs", toneClass)}>
      <p className="font-medium uppercase tracking-wider text-muted-foreground">
        {label}
      </p>
      <ul className="mt-1 space-y-0.5">
        {items.map((item, i) => (
          <li key={i} className="text-sm">
            {item}
          </li>
        ))}
      </ul>
    </div>
  );
}
