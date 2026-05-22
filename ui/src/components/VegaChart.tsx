import * as React from "react";
import vegaEmbed, { type Result as EmbedResult } from "vega-embed";
import { Download } from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

/**
 * VegaChart — renders a Vega-Lite spec with theme-aware colors.
 *
 * Why we pass our own theme config: the spec coming from Tiri's VizAgent
 * uses standard Vega-Lite defaults. We want it to inherit the app's warm
 * palette (primary as the main color, muted gridlines, foreground text)
 * so the chart looks like part of the app rather than a transplant.
 *
 * Error boundary: if vega-embed throws (malformed spec, missing renderer,
 * etc.) we surface the message in place rather than letting the whole
 * AskView column crash.
 */

export interface VegaChartProps {
  spec: Record<string, unknown>;
  className?: string;
  /** Optional filename prefix for the SVG download (without extension). */
  filename?: string;
}

interface ChartState {
  error: string | null;
  view: EmbedResult["view"] | null;
}

export function VegaChart({ spec, className, filename = "chart" }: VegaChartProps) {
  const ref = React.useRef<HTMLDivElement>(null);
  const [state, setState] = React.useState<ChartState>({ error: null, view: null });

  React.useEffect(() => {
    if (!ref.current) return;
    let cancelled = false;
    let active: EmbedResult["view"] | null = null;

    // Inject color config that maps our HSL CSS variables into vega's
    // color scheme. We resolve the variable values at render time so
    // light/dark mode is picked up automatically.
    const resolveHsl = (varName: string): string => {
      const root = document.documentElement;
      const value = getComputedStyle(root).getPropertyValue(varName).trim();
      return value ? `hsl(${value})` : "";
    };

    const themedSpec = {
      ...spec,
      config: {
        ...(spec as { config?: object }).config,
        background: "transparent",
        view: { stroke: "transparent" },
        axis: {
          domainColor: resolveHsl("--border"),
          gridColor: resolveHsl("--border"),
          tickColor: resolveHsl("--border"),
          labelColor: resolveHsl("--muted-foreground"),
          titleColor: resolveHsl("--foreground"),
          labelFontSize: 11,
          titleFontSize: 12,
          labelFont: "Inter, system-ui, sans-serif",
          titleFont: "Inter, system-ui, sans-serif",
        },
        legend: {
          labelColor: resolveHsl("--muted-foreground"),
          titleColor: resolveHsl("--foreground"),
          labelFont: "Inter, system-ui, sans-serif",
          titleFont: "Inter, system-ui, sans-serif",
        },
        mark: { color: resolveHsl("--primary") },
        bar: { color: resolveHsl("--primary") },
        line: { color: resolveHsl("--primary"), strokeWidth: 2 },
        point: { color: resolveHsl("--primary"), size: 60 },
        text: { color: resolveHsl("--foreground") },
      },
    };

    vegaEmbed(ref.current, themedSpec as unknown as Parameters<typeof vegaEmbed>[1], {
      actions: false,
      renderer: "svg",
      ast: false,
      // Width is set by the parent column; tell vega to fill it.
      width: "container" as unknown as undefined,
    })
      .then((result) => {
        if (cancelled) {
          result.view?.finalize();
          return;
        }
        active = result.view;
        setState({ error: null, view: result.view });
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        const message = err instanceof Error ? err.message : String(err);
        setState({ error: message, view: null });
      });

    return () => {
      cancelled = true;
      active?.finalize();
    };
  }, [spec]);

  const handleDownload = React.useCallback(async () => {
    if (!state.view) return;
    const url = await state.view.toImageURL("svg", 2);
    const link = document.createElement("a");
    link.href = url;
    link.download = `${filename}.svg`;
    link.click();
  }, [state.view, filename]);

  if (state.error) {
    return (
      <div
        className={cn(
          "rounded-md border border-dashed border-warning/40 bg-warning/5 p-4 text-sm text-muted-foreground",
          className,
        )}
      >
        <p className="font-medium text-warning">Chart unavailable</p>
        <p className="mt-1 text-xs">{state.error}</p>
      </div>
    );
  }

  return (
    <div className={cn("relative w-full", className)}>
      <div ref={ref} className="vega-chart w-full" />
      {state.view && (
        <Button
          variant="ghost"
          size="icon"
          aria-label="Download SVG"
          className="absolute right-1 top-1 h-7 w-7 opacity-70 hover:opacity-100"
          onClick={handleDownload}
        >
          <Download className="h-3.5 w-3.5" />
        </Button>
      )}
    </div>
  );
}
