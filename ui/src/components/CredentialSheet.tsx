import * as React from "react";
import { KeyRound, ShieldCheck } from "lucide-react";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetFooter,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
} from "@/components/ui/sheet";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Separator } from "@/components/ui/separator";
import { ScrollArea } from "@/components/ui/scroll-area";
import { useToast } from "@/hooks/use-toast";
import { clearCredentials, postCredentials } from "@/lib/api";
import { cn } from "@/lib/utils";
import type { ConfigRoutingResponse, ProviderType } from "@/lib/types";

/**
 * CredentialSheet — slide-in panel for applying session credential
 * overrides to the configured providers.
 *
 * Behavior:
 *  - Each known provider has a row; the value column shows "[from config]"
 *    in muted text until the user types a new value.
 *  - On Apply, POST /config/credentials with all non-empty entries.
 *  - On Clear session overrides, DELETE /config/credentials.
 *  - Inputs are type="password" by default so values don't show in plain
 *    text. The eye toggle reveals when the user wants to verify a paste.
 *  - Single-provider deployments collapse to a single labeled Input + Apply
 *    button instead of the full table.
 */

const PROVIDER_KEY_NAME: Record<ProviderType, string> = {
  databricks: "DATABRICKS_TOKEN",
  anthropic: "ANTHROPIC_API_KEY",
  openai: "OPENAI_API_KEY",
  ollama: "OLLAMA_BASE_URL",
  custom: "API_KEY",
};

const PROVIDER_VARIANT: Record<ProviderType, "databricks" | "anthropic" | "openai" | "ollama" | "custom"> = {
  databricks: "databricks",
  anthropic: "anthropic",
  openai: "openai",
  ollama: "ollama",
  custom: "custom",
};

type Status = "config" | "session" | "error";

export interface CredentialSheetProps {
  routing: ConfigRoutingResponse | null;
  onChange?: () => void;
  /** Track which providers have session overrides — caller manages this. */
  sessionProviders: Set<string>;
  onSessionProvidersChange: (next: Set<string>) => void;
}

export function CredentialSheet({
  routing,
  onChange,
  sessionProviders,
  onSessionProvidersChange,
}: CredentialSheetProps) {
  const [open, setOpen] = React.useState(false);
  const [drafts, setDrafts] = React.useState<Record<string, string>>({});
  const [submitting, setSubmitting] = React.useState(false);
  const { toast } = useToast();

  const providers = routing?.providers ?? [];
  const single = providers.length === 1;

  const triggerColor =
    sessionProviders.size > 0 ? "bg-warning" : providers.length > 0 ? "bg-success" : "bg-muted-foreground";

  const handleApply = async () => {
    const credentials = Object.entries(drafts)
      .filter(([_, v]) => v.trim().length > 0)
      .map(([provider, value]) => ({
        provider,
        key:
          PROVIDER_KEY_NAME[
            providers.find((p) => p.name === provider)?.type ?? "custom"
          ],
        value: value.trim(),
      }));
    if (credentials.length === 0) {
      toast({
        title: "No credentials to apply",
        description: "Enter at least one value before applying.",
        variant: "default",
      });
      return;
    }
    setSubmitting(true);
    try {
      const result = await postCredentials(credentials);
      const newSession = new Set(sessionProviders);
      credentials.forEach((c) => {
        if (
          result.accepted.includes(`${c.provider}::${c.key}`) ||
          result.warnings.some((w) => w.startsWith(`${c.provider}::`))
        ) {
          newSession.add(c.provider);
        }
      });
      onSessionProvidersChange(newSession);
      if (result.rejected.length > 0) {
        toast({
          title: "Some credentials rejected",
          description: result.rejected.join("; "),
          variant: "destructive",
        });
      } else if (result.warnings.length > 0) {
        toast({
          title: "Credentials applied with warnings",
          description: result.warnings.join("; "),
          variant: "default",
        });
      } else {
        toast({
          title: "Credentials applied",
          description: `${result.accepted.length} updated for this session.`,
          variant: "success",
        });
      }
      setDrafts({});
      onChange?.();
    } catch (err) {
      toast({
        title: "Failed to apply credentials",
        description: (err as Error)?.message ?? "Network error",
        variant: "destructive",
      });
    } finally {
      setSubmitting(false);
    }
  };

  const handleClear = async () => {
    setSubmitting(true);
    try {
      await clearCredentials();
      onSessionProvidersChange(new Set());
      setDrafts({});
      toast({
        title: "Session overrides cleared",
        description: "Restart the server to fully restore tiri.toml values.",
        variant: "default",
      });
      onChange?.();
    } catch (err) {
      toast({
        title: "Failed to clear",
        description: (err as Error)?.message ?? "Network error",
        variant: "destructive",
      });
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Sheet open={open} onOpenChange={setOpen}>
      <SheetTrigger asChild>
        <Button
          variant="ghost"
          size="sm"
          className="h-9 gap-2 px-2"
          aria-label="Provider credentials"
        >
          <span
            className={cn(
              "inline-block h-2 w-2 rounded-full",
              triggerColor,
            )}
          />
          <KeyRound className="h-4 w-4" />
          <span className="hidden text-xs sm:inline-block">
            {sessionProviders.size > 0 ? "Session overrides" : "Credentials"}
          </span>
        </Button>
      </SheetTrigger>
      <SheetContent side="right" className="w-full sm:max-w-md flex flex-col">
        <SheetHeader>
          <SheetTitle className="flex items-center gap-2">
            <ShieldCheck className="h-4 w-4 text-primary" />
            Provider credentials
          </SheetTitle>
          <SheetDescription>
            Session overrides take priority over <code className="rounded bg-muted px-1 py-0.5 text-[11px]">tiri.toml</code> values.
            Overrides clear when the server restarts.
          </SheetDescription>
        </SheetHeader>

        <Separator className="my-3" />

        <ScrollArea className="flex-1 -mx-6 px-6">
          {single && providers[0] ? (
            <SingleProviderInput
              provider={providers[0].name}
              providerType={providers[0].type}
              keyName={PROVIDER_KEY_NAME[providers[0].type]}
              value={drafts[providers[0].name] ?? ""}
              status={
                sessionProviders.has(providers[0].name) ? "session" : "config"
              }
              onChange={(v) =>
                setDrafts((prev) => ({ ...prev, [providers[0].name]: v }))
              }
            />
          ) : (
            <div className="space-y-3">
              {providers.map((p) => (
                <ProviderRow
                  key={p.name}
                  provider={p.name}
                  providerType={p.type}
                  keyName={PROVIDER_KEY_NAME[p.type]}
                  value={drafts[p.name] ?? ""}
                  status={
                    sessionProviders.has(p.name) ? "session" : "config"
                  }
                  onChange={(v) =>
                    setDrafts((prev) => ({ ...prev, [p.name]: v }))
                  }
                />
              ))}
              {providers.length === 0 && (
                <p className="text-sm text-muted-foreground">
                  No providers are configured. Start the server with at least one
                  <code className="rounded bg-muted px-1 py-0.5 text-[11px]">[llm.providers.NAME]</code>
                  block in <code className="rounded bg-muted px-1 py-0.5 text-[11px]">tiri.toml</code>.
                </p>
              )}
            </div>
          )}
        </ScrollArea>

        <SheetFooter className="mt-3 flex-row justify-between gap-2 sm:justify-between">
          <Button
            variant="ghost"
            size="sm"
            onClick={handleClear}
            disabled={submitting || sessionProviders.size === 0}
          >
            Clear session overrides
          </Button>
          <Button onClick={handleApply} disabled={submitting} size="sm">
            Apply
          </Button>
        </SheetFooter>
      </SheetContent>
    </Sheet>
  );
}

function SingleProviderInput({
  provider,
  providerType,
  keyName,
  value,
  status,
  onChange,
}: {
  provider: string;
  providerType: ProviderType;
  keyName: string;
  value: string;
  status: Status;
  onChange: (v: string) => void;
}) {
  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <Label className="flex items-center gap-2 text-sm">
          <Badge variant={PROVIDER_VARIANT[providerType]}>{provider}</Badge>
          <span className="font-mono text-xs">{keyName}</span>
        </Label>
        <StatusBadge status={status} />
      </div>
      <Input
        type="password"
        autoComplete="off"
        placeholder={status === "config" ? "[from config — paste to override]" : "[session override active]"}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="font-mono text-xs"
      />
    </div>
  );
}

function ProviderRow({
  provider,
  providerType,
  keyName,
  value,
  status,
  onChange,
}: {
  provider: string;
  providerType: ProviderType;
  keyName: string;
  value: string;
  status: Status;
  onChange: (v: string) => void;
}) {
  return (
    <div className="space-y-1.5 rounded-md border p-3">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Badge variant={PROVIDER_VARIANT[providerType]}>{provider}</Badge>
          <span className="font-mono text-[11px] text-muted-foreground">
            {keyName}
          </span>
        </div>
        <StatusBadge status={status} />
      </div>
      <Input
        type="password"
        autoComplete="off"
        placeholder={status === "config" ? "[from config — paste to override]" : "[session override active]"}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="font-mono text-xs"
      />
    </div>
  );
}

function StatusBadge({ status }: { status: Status }) {
  if (status === "session") {
    return <Badge variant="warning">session</Badge>;
  }
  if (status === "error") {
    return <Badge variant="destructive">error</Badge>;
  }
  return <Badge variant="muted">config</Badge>;
}
