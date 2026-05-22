import * as React from "react";
import { AlertTriangle, ChevronDown, Database, FileJson, Upload } from "lucide-react";
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { ScrollArea } from "@/components/ui/scroll-area";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Skeleton } from "@/components/ui/skeleton";
import { useToast } from "@/hooks/use-toast";
import { TableMetaInspector } from "@/components/TableMetaInspector";
import {
  createRoom,
  getRoom,
  getRoomTables,
  listRoomKeys,
} from "@/lib/api";
import { cn } from "@/lib/utils";
import type {
  RoomConfig,
  RoomTablesResponse,
  SchemaMetaResponse,
  TableMetaResponse,
} from "@/lib/types";

/**
 * RoomsView — grid of room cards with Inspect / Run benchmarks / Ask
 * actions. The "Load room JSON" button opens a file picker and POSTs the
 * resulting RoomConfig to /rooms. Empty state guides operators toward
 * either the file picker here or the CLI's `load-room` command.
 */

export function RoomsView() {
  const [roomIds, setRoomIds] = React.useState<string[] | null>(null);
  const [rooms, setRooms] = React.useState<Record<string, RoomConfig>>({});
  const [loadingError, setLoadingError] = React.useState<string | null>(null);
  const fileInputRef = React.useRef<HTMLInputElement>(null);
  const { toast } = useToast();

  const load = React.useCallback(async () => {
    try {
      const ids = await listRoomKeys();
      setRoomIds(ids);
      setLoadingError(null);
      const fetched = await Promise.all(
        ids.map(async (id) => {
          try {
            return [id, await getRoom(id)] as const;
          } catch {
            return [id, null] as const;
          }
        }),
      );
      setRooms(
        Object.fromEntries(fetched.filter(([, v]) => v) as Array<[string, RoomConfig]>),
      );
    } catch (err) {
      setLoadingError((err as Error)?.message ?? "Failed to list rooms");
      setRoomIds([]);
    }
  }, []);

  React.useEffect(() => {
    void load();
  }, [load]);

  const handleFile = async (file: File) => {
    try {
      const text = await file.text();
      const config = JSON.parse(text) as RoomConfig;
      await createRoom(config);
      toast({
        title: "Room loaded",
        description: `${config.title} (${config.room_id}) added to the catalogue.`,
        variant: "success",
      });
      await load();
    } catch (err) {
      toast({
        title: "Couldn't load room JSON",
        description: (err as Error)?.message ?? "Invalid file",
        variant: "destructive",
      });
    } finally {
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  };

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-xl font-semibold">Rooms</h2>
          <p className="text-sm text-muted-foreground">
            Configured Tiri rooms. Use Ask or Benchmarks to query a room.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <input
            ref={fileInputRef}
            type="file"
            accept=".json,application/json"
            className="hidden"
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) void handleFile(f);
            }}
          />
          <Button
            variant="outline"
            size="sm"
            onClick={() => fileInputRef.current?.click()}
          >
            <Upload className="h-4 w-4" />
            Load room JSON
          </Button>
        </div>
      </div>

      {loadingError && (
        <Card className="border-destructive/40 bg-destructive/5">
          <CardContent className="py-4 text-sm text-destructive">
            {loadingError}
          </CardContent>
        </Card>
      )}

      {roomIds === null ? (
        <RoomGridSkeleton />
      ) : roomIds.length === 0 ? (
        <EmptyState />
      ) : (
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
          {roomIds.map((id) => {
            const config = rooms[id];
            if (!config) {
              return (
                <Card key={id} className="opacity-50">
                  <CardHeader>
                    <CardTitle className="font-mono text-sm">{id}</CardTitle>
                    <CardDescription>(couldn't load config)</CardDescription>
                  </CardHeader>
                </Card>
              );
            }
            return <RoomCard key={id} config={config} />;
          })}
        </div>
      )}
    </div>
  );
}

function RoomCard({ config }: { config: RoomConfig }) {
  const [inspectOpen, setInspectOpen] = React.useState(false);
  const tables = config.tables ?? [];
  const examples = config.examples ?? [];
  const benchmarks = config.benchmarks ?? [];

  const visibleTables = tables.slice(0, 5);
  const overflowCount = tables.length - visibleTables.length;

  return (
    <Card className="flex flex-col">
      <CardHeader className="space-y-1">
        <div className="flex items-baseline justify-between gap-2">
          <CardTitle className="leading-tight">{config.title}</CardTitle>
          <span className="font-mono text-xs text-muted-foreground">
            {config.room_id}
          </span>
        </div>
        {config.text_instruction && (
          <CardDescription className="line-clamp-2">
            {config.text_instruction}
          </CardDescription>
        )}
      </CardHeader>
      <CardContent className="flex-1 space-y-3">
        <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
          <span>
            <strong className="text-foreground">{tables.length}</strong> tables
          </span>
          <span aria-hidden="true">·</span>
          <span>
            <strong className="text-foreground">{examples.length}</strong> examples
          </span>
          <span aria-hidden="true">·</span>
          <span>
            <strong className="text-foreground">{benchmarks.length}</strong> benchmarks
          </span>
        </div>
        <div className="flex flex-wrap gap-1">
          {visibleTables.map((t) => (
            <Badge key={t} variant="muted" className="font-mono text-[10px]">
              {t}
            </Badge>
          ))}
          {overflowCount > 0 && (
            <Badge variant="muted" className="text-[10px]">
              +{overflowCount} more
            </Badge>
          )}
        </div>
        <Collapsible open={inspectOpen} onOpenChange={setInspectOpen}>
          <CollapsibleTrigger asChild>
            <Button variant="ghost" size="sm" className="h-7 -ml-2 px-2 text-xs">
              <FileJson className="h-3.5 w-3.5" />
              Inspect
              <ChevronDown
                className={cn(
                  "h-3.5 w-3.5 transition-transform",
                  inspectOpen && "rotate-180",
                )}
              />
            </Button>
          </CollapsibleTrigger>
          <CollapsibleContent className="data-[state=open]:animate-fade-in">
            <RoomInspectorTabs config={config} active={inspectOpen} />
          </CollapsibleContent>
        </Collapsible>
      </CardContent>
      <CardFooter className="flex justify-end gap-2 pt-0 text-xs">
        <CardActionTip />
      </CardFooter>
    </Card>
  );
}

function CardActionTip() {
  return (
    <span className="text-muted-foreground italic">
      Use the Ask / Benchmarks tabs to query this room
    </span>
  );
}

function RoomGridSkeleton() {
  return (
    <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
      {[0, 1, 2, 3].map((i) => (
        <Card key={i}>
          <CardHeader>
            <Skeleton className="h-5 w-2/3" />
            <Skeleton className="h-4 w-1/2" />
          </CardHeader>
          <CardContent className="space-y-3">
            <Skeleton className="h-4 w-3/4" />
            <div className="flex gap-1">
              <Skeleton className="h-5 w-16" />
              <Skeleton className="h-5 w-20" />
              <Skeleton className="h-5 w-14" />
            </div>
          </CardContent>
        </Card>
      ))}
    </div>
  );
}

function RoomInspectorTabs({
  config,
  active,
}: {
  config: RoomConfig;
  active: boolean;
}) {
  // Lazy-load the tables payload only when the Tables tab is selected.
  // We track which tab the user landed on so the GET /tables call doesn't
  // fire when they only ever look at the JSON.
  const [tab, setTab] = React.useState<"json" | "tables">("json");
  const [tablesData, setTablesData] = React.useState<RoomTablesResponse | null>(
    null,
  );
  const [tablesLoading, setTablesLoading] = React.useState(false);
  const [tablesError, setTablesError] = React.useState<string | null>(null);

  React.useEffect(() => {
    if (!active) return;
    if (tab !== "tables") return;
    if (tablesData || tablesLoading) return;
    setTablesLoading(true);
    setTablesError(null);
    getRoomTables(config.room_id)
      .then((res) => setTablesData(res))
      .catch((err) =>
        setTablesError((err as Error)?.message ?? "Failed to load tables"),
      )
      .finally(() => setTablesLoading(false));
  }, [active, tab, config.room_id, tablesData, tablesLoading]);

  return (
    <Tabs
      value={tab}
      onValueChange={(v) => setTab(v as "json" | "tables")}
      className="mt-2"
    >
      <TabsList className="h-8">
        <TabsTrigger value="json" className="text-xs">
          Config JSON
        </TabsTrigger>
        <TabsTrigger value="tables" className="text-xs">
          Tables
        </TabsTrigger>
      </TabsList>
      <TabsContent value="json" className="mt-2">
        <ScrollArea className="h-[300px] rounded-md border bg-muted/30">
          <pre className="p-3 text-xs leading-relaxed font-mono">
            {JSON.stringify(config, null, 2)}
          </pre>
        </ScrollArea>
      </TabsContent>
      <TabsContent value="tables" className="mt-2">
        <TablesPanel
          data={tablesData}
          loading={tablesLoading}
          error={tablesError}
        />
      </TabsContent>
    </Tabs>
  );
}

function TablesPanel({
  data,
  loading,
  error,
}: {
  data: RoomTablesResponse | null;
  loading: boolean;
  error: string | null;
}) {
  const [selectedName, setSelectedName] = React.useState<string | null>(null);
  const [schemaFilter, setSchemaFilter] = React.useState<string | null>(null);

  // When data first arrives, default-select the first table.
  React.useEffect(() => {
    if (data && data.tables.length > 0 && !selectedName) {
      setSelectedName(data.tables[0].name);
    }
  }, [data, selectedName]);

  if (error) {
    return (
      <Card className="border-destructive/40 bg-destructive/5">
        <CardContent className="py-4 text-sm text-destructive">
          {error}
        </CardContent>
      </Card>
    );
  }

  if (loading && !data) {
    return (
      <div className="grid grid-cols-[1fr_2fr] gap-3">
        <div className="space-y-1">
          {[0, 1, 2, 3, 4].map((i) => (
            <Skeleton key={i} className="h-12 w-full" />
          ))}
        </div>
        <TableMetaInspector table={null} loading />
      </div>
    );
  }

  if (!data || data.tables.length === 0) {
    return (
      <Card className="border-dashed">
        <CardContent className="space-y-2 py-6 text-center text-sm">
          <p className="text-muted-foreground">
            This room has no resolvable tables — or no metadata providers
            beyond the catalog physical schema are configured.
          </p>
          <p className="text-xs text-muted-foreground">
            Add a YAML or dbt metadata provider in <code className="font-mono">tiri.toml</code>{" "}
            to enrich tables with descriptions, synonyms, and join hints.
          </p>
        </CardContent>
      </Card>
    );
  }

  const filteredTables = schemaFilter
    ? data.tables.filter((t) => schemaPrefix(t.name) === schemaFilter)
    : data.tables;
  const selected =
    filteredTables.find((t) => t.name === selectedName) ??
    filteredTables[0] ??
    data.tables[0];

  return (
    <div className="space-y-3">
      {data.schemas.length > 0 && (
        <SchemasStrip
          schemas={data.schemas}
          activeSchema={schemaFilter}
          onToggle={(name) => {
            setSchemaFilter((cur) => (cur === name ? null : name));
            setSelectedName(null);
          }}
        />
      )}
      <div className="grid h-[480px] grid-cols-[1fr_2fr] gap-3">
        <Card className="overflow-hidden">
          <ScrollArea className="h-full">
            <div className="space-y-0.5 p-1.5">
              {filteredTables.length === 0 ? (
                <div className="px-2 py-4 text-center text-xs text-muted-foreground">
                  No tables in {schemaFilter}.
                </div>
              ) : (
                filteredTables.map((t) => (
                  <TableListItem
                    key={t.name}
                    table={t}
                    selected={t.name === selected?.name}
                    onSelect={() => setSelectedName(t.name)}
                  />
                ))
              )}
            </div>
          </ScrollArea>
        </Card>
        <Card className="overflow-hidden p-3">
          <TableMetaInspector table={selected} />
        </Card>
      </div>
    </div>
  );
}

function schemaPrefix(fqn: string): string {
  const parts = fqn.split(".");
  return parts.length >= 2 ? `${parts[0]}.${parts[1]}` : fqn;
}

function SchemasStrip({
  schemas,
  activeSchema,
  onToggle,
}: {
  schemas: SchemaMetaResponse[];
  activeSchema: string | null;
  onToggle: (name: string) => void;
}) {
  return (
    <Card>
      <div className="space-y-2 p-3">
        <div className="text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
          Schemas in this room
          {activeSchema && (
            <span className="ml-2 normal-case text-muted-foreground/80">
              · filtering to <code className="font-mono">{activeSchema}</code> ·
              <button
                type="button"
                className="ml-1 underline hover:no-underline"
                onClick={() => onToggle(activeSchema)}
              >
                clear
              </button>
            </span>
          )}
        </div>
        <div className="flex flex-wrap gap-2">
          {schemas.map((s) => (
            <SchemaChip
              key={s.name}
              schema={s}
              active={s.name === activeSchema}
              onClick={() => onToggle(s.name)}
            />
          ))}
        </div>
      </div>
    </Card>
  );
}

function SchemaChip({
  schema,
  active,
  onClick,
}: {
  schema: SchemaMetaResponse;
  active: boolean;
  onClick: () => void;
}) {
  const tooltip = [
    schema.description,
    schema.notes ? `Notes: ${schema.notes}` : null,
    schema.owner ? `Owner: ${schema.owner}` : null,
  ]
    .filter(Boolean)
    .join("\n\n");
  return (
    <button
      type="button"
      onClick={onClick}
      title={tooltip || schema.name}
      className={cn(
        "group flex flex-col items-start gap-1 rounded-md border px-3 py-1.5 text-left transition-colors",
        active
          ? "border-primary/40 bg-primary/10 ring-1 ring-primary/30"
          : "border-border bg-card hover:bg-muted/40",
      )}
    >
      <div className="flex items-center gap-1.5">
        <code className="font-mono text-xs">{schema.name}</code>
        {schema.freshness && (
          <Badge variant="muted" className="text-[9px]">
            {schema.freshness}
          </Badge>
        )}
      </div>
      {(schema.domain || schema.owner) && (
        <div className="flex items-center gap-1.5 text-[10px] text-muted-foreground">
          {schema.domain && <span>{schema.domain}</span>}
          {schema.domain && schema.owner && <span aria-hidden="true">·</span>}
          {schema.owner && <span>{schema.owner}</span>}
        </div>
      )}
    </button>
  );
}

function TableListItem({
  table,
  selected,
  onSelect,
}: {
  table: TableMetaResponse;
  selected: boolean;
  onSelect: () => void;
}) {
  const conflictCount =
    table.conflicts.length +
    table.columns.reduce((n, c) => n + c.conflicts.length, 0);
  const shortName = (() => {
    const parts = table.name.split(".");
    return parts.length >= 2 ? parts.slice(-2).join(".") : table.name;
  })();
  return (
    <button
      type="button"
      onClick={onSelect}
      className={cn(
        "block w-full rounded px-2 py-1.5 text-left text-xs transition-colors",
        selected ? "bg-primary/10 text-foreground" : "hover:bg-muted/50",
      )}
    >
      <div className="flex items-center gap-1.5">
        <code className="font-mono text-xs">{shortName}</code>
        {conflictCount > 0 && (
          <AlertTriangle className="h-2.5 w-2.5 text-warning" />
        )}
      </div>
      <div className="mt-0.5 text-[10px] text-muted-foreground">
        {table.metadata_sources.length === 0
          ? "catalog only"
          : `${table.metadata_sources.length} source${
              table.metadata_sources.length === 1 ? "" : "s"
            }`}
        {" · "}
        {table.columns.length} cols
      </div>
    </button>
  );
}

function EmptyState() {
  return (
    <Card className="border-dashed">
      <CardContent className="flex flex-col items-center justify-center gap-3 py-12 text-center">
        <Database className="h-8 w-8 text-muted-foreground" />
        <div>
          <p className="text-sm font-medium">No rooms yet</p>
          <p className="mt-1 text-xs text-muted-foreground">
            Load a room JSON above, or use the CLI:
          </p>
        </div>
        <code className="rounded bg-muted px-3 py-1.5 text-xs font-mono">
          python -m tiri.cli load-room demo/tpch_sales_config.json
        </code>
      </CardContent>
    </Card>
  );
}
