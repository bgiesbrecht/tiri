import * as React from "react";
import { Plus } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { cn } from "@/lib/utils";
import type { Backend, ProviderType } from "@/lib/types";

/**
 * BackendSelector — checkbox list of available backends with a custom-
 * backend escape hatch.
 *
 * Backends come from /config/routing (one per [llm.providers.NAME] block)
 * cross-referenced with the routing table so the UI can show which models
 * each backend has actually been routed to. The caller may also add
 * `provider::model` entries via "Add custom backend" — useful for testing
 * a model not currently in the routing table.
 */

const PROVIDER_VARIANTS: Record<ProviderType, "databricks" | "anthropic" | "openai" | "ollama" | "custom"> = {
  databricks: "databricks",
  anthropic: "anthropic",
  openai: "openai",
  ollama: "ollama",
  custom: "custom",
};

export interface BackendSelectorProps {
  backends: Backend[];
  selected: string[];
  onChange: (selected: string[]) => void;
  onAddCustom?: (backend: Backend) => void;
  className?: string;
}

export function BackendSelector({
  backends,
  selected,
  onChange,
  onAddCustom,
  className,
}: BackendSelectorProps) {
  const [showCustomInput, setShowCustomInput] = React.useState(false);
  const [customValue, setCustomValue] = React.useState("");
  const [customError, setCustomError] = React.useState<string | null>(null);

  const toggle = (id: string) => {
    const next = selected.includes(id)
      ? selected.filter((s) => s !== id)
      : [...selected, id];
    onChange(next);
  };

  const handleAddCustom = () => {
    const trimmed = customValue.trim();
    if (!trimmed) {
      setCustomError("Enter a `provider::model` identifier");
      return;
    }
    if (!trimmed.includes("::")) {
      setCustomError("Format must be provider::model");
      return;
    }
    const [provider, ...rest] = trimmed.split("::");
    const model = rest.join("::");
    if (!provider || !model) {
      setCustomError("Both provider and model are required");
      return;
    }
    const id = `${provider}::${model}`;
    const knownProvider = backends.find((b) => b.provider === provider);
    const backend: Backend = {
      provider,
      model,
      id,
      label: id,
      type: (knownProvider?.type ?? "custom") as ProviderType,
      custom: true,
    };
    onAddCustom?.(backend);
    onChange([...selected, id]);
    setCustomValue("");
    setShowCustomInput(false);
    setCustomError(null);
  };

  return (
    <div className={cn("space-y-2", className)}>
      <div className="flex flex-wrap gap-2">
        {backends.map((b) => {
          const isSelected = selected.includes(b.id);
          return (
            <button
              key={b.id}
              type="button"
              onClick={() => toggle(b.id)}
              className={cn(
                "group flex items-center gap-2 rounded-md border px-3 py-1.5 text-xs transition-colors",
                isSelected
                  ? "border-primary bg-primary/10 text-foreground"
                  : "border-border bg-card text-muted-foreground hover:bg-accent hover:text-accent-foreground",
              )}
            >
              <span
                className={cn(
                  "inline-block h-2 w-2 rounded-full border",
                  isSelected
                    ? "border-primary bg-primary"
                    : "border-muted-foreground/40",
                )}
              />
              <Badge variant={PROVIDER_VARIANTS[b.type]} className="text-[10px]">
                {b.provider}
              </Badge>
              <span className="font-mono text-xs">{b.model}</span>
            </button>
          );
        })}
        {!showCustomInput && (
          <Button
            variant="ghost"
            size="sm"
            onClick={() => setShowCustomInput(true)}
            className="h-7 px-2 text-xs"
          >
            <Plus className="h-3.5 w-3.5" />
            Add custom backend
          </Button>
        )}
      </div>
      {showCustomInput && (
        <div className="flex items-end gap-2">
          <div className="flex-1 space-y-1">
            <Label htmlFor="custom-backend" className="text-xs">
              Custom backend (provider::model)
            </Label>
            <Input
              id="custom-backend"
              placeholder="e.g. databricks::databricks-claude-opus-4-7"
              value={customValue}
              onChange={(e) => {
                setCustomValue(e.target.value);
                setCustomError(null);
              }}
              onKeyDown={(e) => {
                if (e.key === "Enter") handleAddCustom();
                if (e.key === "Escape") {
                  setShowCustomInput(false);
                  setCustomValue("");
                  setCustomError(null);
                }
              }}
              className="h-8 text-sm font-mono"
              autoFocus
            />
            {customError && (
              <p className="text-xs text-destructive">{customError}</p>
            )}
          </div>
          <Button size="sm" onClick={handleAddCustom}>Add</Button>
          <Button
            size="sm"
            variant="ghost"
            onClick={() => {
              setShowCustomInput(false);
              setCustomValue("");
              setCustomError(null);
            }}
          >
            Cancel
          </Button>
        </div>
      )}
    </div>
  );
}
