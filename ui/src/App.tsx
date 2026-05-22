import * as React from "react";
import { Sun, Moon, Eye } from "lucide-react";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Button } from "@/components/ui/button";
import { CredentialSheet } from "@/components/CredentialSheet";
import { RoomsView } from "@/views/RoomsView";
import { AskView } from "@/views/AskView";
import { BenchmarksView } from "@/views/BenchmarksView";
import { HistoryView } from "@/views/HistoryView";
import { getRouting } from "@/lib/api";
import type { ConfigRoutingResponse } from "@/lib/types";

/**
 * Tiri QA / demo UI. Four-tab application with a persistent header.
 *
 * Header contains:
 *   - Wordmark (left)
 *   - Theme toggle + credential indicator (right). The credential
 *     indicator dot is green when all configured providers have
 *     credentials from tiri.toml, amber when at least one session
 *     override is active, grey when no providers are configured.
 *
 * Each tab is a separate view module; they share routing config
 * loaded once at mount time via `getRouting()`.
 */

type Theme = "light" | "dark";

function readInitialTheme(): Theme {
  if (typeof window === "undefined") return "light";
  const stored = window.localStorage.getItem("tiri-theme");
  if (stored === "light" || stored === "dark") return stored;
  return window.matchMedia("(prefers-color-scheme: dark)").matches
    ? "dark"
    : "light";
}

export function App() {
  const [routing, setRouting] = React.useState<ConfigRoutingResponse | null>(null);
  const [routingError, setRoutingError] = React.useState<string | null>(null);
  const [sessionProviders, setSessionProviders] = React.useState<Set<string>>(
    () => new Set(),
  );
  const [theme, setTheme] = React.useState<Theme>(readInitialTheme);
  const [activeTab, setActiveTab] = React.useState<string>("rooms");

  React.useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
    window.localStorage.setItem("tiri-theme", theme);
  }, [theme]);

  const loadRouting = React.useCallback(async () => {
    try {
      const data = await getRouting();
      setRouting(data);
      setRoutingError(null);
    } catch (err) {
      setRoutingError((err as Error)?.message ?? "Failed to load routing");
    }
  }, []);

  React.useEffect(() => {
    void loadRouting();
  }, [loadRouting]);

  return (
    <div className="min-h-screen bg-background text-foreground">
      <header className="sticky top-0 z-30 border-b bg-background/95 backdrop-blur supports-[backdrop-filter]:bg-background/75">
        <div className="mx-auto flex h-14 items-center justify-between px-6">
          <div className="flex items-center gap-3">
            <Eye className="h-5 w-5 text-primary" />
            <span className="tiri-wordmark text-lg">Tiri</span>
            <span className="hidden text-xs text-muted-foreground sm:inline-block">
              A witness for your data
            </span>
          </div>
          <div className="flex items-center gap-1">
            <CredentialSheet
              routing={routing}
              sessionProviders={sessionProviders}
              onSessionProvidersChange={setSessionProviders}
              onChange={loadRouting}
            />
            <Button
              variant="ghost"
              size="icon"
              aria-label="Toggle theme"
              onClick={() => setTheme((t) => (t === "dark" ? "light" : "dark"))}
            >
              {theme === "dark" ? (
                <Sun className="h-4 w-4" />
              ) : (
                <Moon className="h-4 w-4" />
              )}
            </Button>
          </div>
        </div>
      </header>

      {routingError && (
        <div className="border-b bg-destructive/10 px-6 py-2 text-sm text-destructive">
          Couldn't load routing config: {routingError}. The UI is still usable
          for inspecting rooms, but Ask + Benchmarks need this to work.
        </div>
      )}

      <main className="mx-auto px-6 py-6 max-w-[1600px]">
        <Tabs value={activeTab} onValueChange={setActiveTab} className="w-full">
          <TabsList className="mb-4">
            <TabsTrigger value="rooms">Rooms</TabsTrigger>
            <TabsTrigger value="ask">Ask</TabsTrigger>
            <TabsTrigger value="benchmarks">Benchmarks</TabsTrigger>
            <TabsTrigger value="history">History</TabsTrigger>
          </TabsList>
          <TabsContent value="rooms" className="m-0">
            <RoomsView />
          </TabsContent>
          <TabsContent value="ask" className="m-0">
            <AskView routing={routing} />
          </TabsContent>
          <TabsContent value="benchmarks" className="m-0">
            <BenchmarksView routing={routing} />
          </TabsContent>
          <TabsContent value="history" className="m-0">
            <HistoryView />
          </TabsContent>
        </Tabs>
      </main>
    </div>
  );
}
