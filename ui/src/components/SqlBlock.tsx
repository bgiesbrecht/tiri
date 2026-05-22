import * as React from "react";
import { Highlight, themes } from "prism-react-renderer";
import { Copy, Check } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

/**
 * Syntax-highlighted SQL block with copy-to-clipboard.
 *
 * Always uses a dark Prism theme regardless of the app theme — SQL is
 * easier to read on dark in both modes, and consistency matters more
 * than blending in. Step pill rendered when the SQL came from one
 * specific step of a multi-step plan.
 *
 * Line numbers appear once the SQL has 5+ lines so short single-step
 * queries don't pick up visual noise.
 */

export interface SqlBlockProps {
  sql: string;
  label?: string;
  step?: { current: number; total: number };
  className?: string;
}

export function SqlBlock({ sql, label, step, className }: SqlBlockProps) {
  const [copied, setCopied] = React.useState(false);
  const trimmed = (sql || "").trim();
  const lineCount = trimmed.split("\n").length;
  const showLineNumbers = lineCount >= 5;

  const handleCopy = React.useCallback(async () => {
    try {
      await navigator.clipboard.writeText(trimmed);
      setCopied(true);
      setTimeout(() => setCopied(false), 1200);
    } catch {
      /* clipboard may be unavailable in non-HTTPS dev — silently ignore */
    }
  }, [trimmed]);

  return (
    <div
      className={cn(
        "group relative overflow-hidden rounded-md border border-zinc-800 bg-zinc-950 text-zinc-100 shadow-sm",
        className,
      )}
    >
      <div className="flex items-center justify-between border-b border-zinc-800 px-3 py-1.5">
        <div className="flex items-center gap-2 text-xs text-zinc-400">
          <span className="font-mono uppercase tracking-wider">
            {label || "sql"}
          </span>
          {step ? (
            <Badge variant="muted" className="bg-zinc-800 text-zinc-300">
              Step {step.current}/{step.total}
            </Badge>
          ) : null}
        </div>
        <Button
          size="icon"
          variant="ghost"
          aria-label="Copy SQL"
          onClick={handleCopy}
          className="h-7 w-7 text-zinc-400 hover:bg-zinc-800 hover:text-zinc-100"
        >
          {copied ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
        </Button>
      </div>
      <Highlight code={trimmed} language="sql" theme={themes.vsDark}>
        {({ className: prismClass, style, tokens, getLineProps, getTokenProps }) => (
          <pre
            className={cn(
              "overflow-x-auto p-3 text-[13px] leading-relaxed",
              prismClass,
            )}
            style={{ ...style, background: "transparent" }}
          >
            {tokens.map((line, i) => {
              const { key: _lineKey, ...lineProps } = getLineProps({ line });
              return (
                <div key={i} {...lineProps} className="table-row">
                  {showLineNumbers && (
                    <span className="table-cell select-none pr-3 text-right text-zinc-600">
                      {i + 1}
                    </span>
                  )}
                  <span className="table-cell">
                    {line.map((token, j) => {
                      const { key: _tokenKey, ...tokenProps } = getTokenProps({ token });
                      return <span key={j} {...tokenProps} />;
                    })}
                  </span>
                </div>
              );
            })}
          </pre>
        )}
      </Highlight>
    </div>
  );
}
