import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

const TITLES: Record<string, string> = {
  high: "High",
  medium: "Medium",
  low: "Low",
};

const STYLES: Record<string, string> = {
  high: "bg-confidence-high/15 text-confidence-high border-confidence-high/30",
  medium:
    "bg-confidence-medium/15 text-confidence-medium border-confidence-medium/30",
  low: "bg-confidence-low/15 text-confidence-low border-confidence-low/30",
};

/**
 * ConfidenceBadge — pill rendering a confidence level with the semantic
 * confidence color. The semantics are:
 *   high   → success green (clean SQL, unambiguous question)
 *   medium → warning amber (joins / business-term assumptions)
 *   low    → destructive red (causal phrasing / inference / ambiguous)
 */

export function ConfidenceBadge({
  confidence,
  className,
}: {
  confidence: "high" | "medium" | "low";
  className?: string;
}) {
  return (
    <Badge
      variant="outline"
      className={cn("uppercase tracking-wider", STYLES[confidence], className)}
    >
      {TITLES[confidence]} confidence
    </Badge>
  );
}
