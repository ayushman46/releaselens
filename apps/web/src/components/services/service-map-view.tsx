"use client";

import "@xyflow/react/dist/style.css";

import { Background, Controls, Handle, MarkerType, Position, ReactFlow, type Edge, type Node, type NodeProps } from "@xyflow/react";
import { X } from "lucide-react";
import Link from "next/link";
import { useMemo, useState } from "react";

import { RADIUS_ROLE_ICON, RadiusLegend, radiusNodeClasses } from "@/components/graph/radius-legend";
import { AnalysisStatusPill } from "@/components/rl/analysis-status";
import { ApiErrorState } from "@/components/rl/api-error-state";
import { ServiceMapSetup, ServicesSetup } from "@/components/rl/setup-empty-states";
import { approxP95, p95Bucket } from "@/components/services/services-view";
import { useSession } from "@/components/session/session-provider";
import { Button } from "@/components/ui/button";
import { Select } from "@/components/ui/field";
import { KeyValueList } from "@/components/ui/key-value";
import { LoadingState } from "@/components/ui/skeleton";
import { KIND_LABEL, RADIUS_ROLE_META, STATUS_META, type RadiusRole } from "@/lib/analysis";
import { useServiceMap, useServices } from "@/lib/api/queries";
import type { ServiceItem, ServiceMap, ServiceMapNode } from "@/lib/api/types";
import { DASH, formatCount, formatDateTime, formatRate, formatRelativeTime } from "@/lib/format";
import { layeredLayout } from "@/lib/graph-layout";
import { TONE_CLASSES } from "@/lib/tones";
import { useTheme } from "@/lib/use-theme";
import { cn } from "@/lib/utils";

const MAP_LABELS: Partial<Record<RadiusRole, string>> = { outside: "Not in the latest analysis" };

/** How a map node is drawn: from the environment's latest analysis, when it covered the service. */
export function mapRole(node: ServiceMapNode): RadiusRole {
  if (node.probable_origin) return "origin";
  if (node.impact?.startsWith("AFFECTED") || node.status === "REGRESSED") return "affected";
  if (node.status === "INSUFFICIENT_DATA" || node.impact?.startsWith("UNKNOWN")) return "unknown";
  if (node.status === "HEALTHY" || node.status === "WARNING" || node.impact?.startsWith("HEALTHY")) return "healthy";
  return "outside";
}

type MapNodeData = {
  service: string;
  role: RadiusRole;
  status: ServiceMapNode["status"];
  catalog: ServiceItem | undefined;
};

function MapNodeView({ data, selected }: NodeProps<Node<MapNodeData>>) {
  const Icon = RADIUS_ROLE_ICON[data.role];
  const meta = RADIUS_ROLE_META[data.role];
  const health = data.catalog?.health;
  return (
    <div className={cn("w-48 rounded-lg px-2.5 py-2 text-left shadow-panel", radiusNodeClasses(data.role), selected && "ring-2 ring-ring ring-offset-1")}>
      <Handle type="target" position={Position.Left} className="!size-1.5 !border-0 !bg-border-strong" />
      <div className="flex items-center gap-1.5">
        <Icon aria-hidden="true" className={cn("size-3.5 shrink-0", data.role === "outside" ? "text-muted-foreground" : TONE_CLASSES[meta.tone].text)} />
        <span className="truncate text-[13px] font-semibold text-foreground">{data.service}</span>
        {data.catalog?.latest_version_seen ? (
          <span className="ml-auto shrink-0 font-mono text-[10px] text-muted-foreground">{data.catalog.latest_version_seen}</span>
        ) : null}
      </div>
      <div className="mt-0.5 text-[11px] text-foreground-secondary">
        {MAP_LABELS[data.role] ?? meta.label}
        {data.status && data.role !== "origin" ? ` · ${STATUS_META[data.status].label}` : ""}
      </div>
      <div className="mt-1 flex gap-3 text-[11px] text-muted-foreground tabular-nums">
        <span>p95 {health ? approxP95(health) : DASH}</span>
        <span>err {health ? formatRate(health.error_rate, 1) : DASH}</span>
      </div>
      <Handle type="source" position={Position.Right} className="!size-1.5 !border-0 !bg-border-strong" />
    </div>
  );
}

const NODE_TYPES = { service: MapNodeView };

function ServicePanel({
  node,
  catalog,
  map,
  onClose,
}: {
  node: ServiceMapNode;
  catalog: ServiceItem | undefined;
  map: ServiceMap;
  onClose: () => void;
}) {
  const role = mapRole(node);
  const calls = map.edges.filter((e) => e.source === node.service);
  const callers = map.edges.filter((e) => e.target === node.service);
  const health = catalog?.health;
  return (
    <aside aria-labelledby="panel-title" className="rounded-xl border bg-panel shadow-panel">
      <div className="flex items-center gap-2 border-b px-4 py-3">
        <h2 id="panel-title" className="truncate text-sm font-semibold">
          {node.service}
        </h2>
        <Button variant="ghost" size="icon-sm" className="ml-auto" onClick={onClose} aria-label={`Close details for ${node.service}`}>
          <X aria-hidden="true" />
        </Button>
      </div>
      <div className="px-4">
        <KeyValueList
          labelWidth="sm:grid-cols-[minmax(0,7rem)_minmax(0,1fr)]"
          items={[
            { label: "Latest analysis", value: node.status ? <AnalysisStatusPill status={node.status} size="sm" /> : MAP_LABELS[role] ?? DASH },
            ...(node.probable_origin ? [{ label: "Role", value: "Probable origin of the latest regression" }] : []),
            ...(node.impact ? [{ label: "Blast radius", value: node.impact.replace(/_/g, " ").toLowerCase() }] : []),
            { label: "Version", value: catalog?.latest_version_seen ?? DASH },
            {
              label: "Traffic",
              value: health ? `${health.request_rate_per_min.toFixed(1)} req/min · ${formatCount(health.requests)} requests (15 min)` : DASH,
            },
            { label: "Error rate", value: health ? `${formatRate(health.error_rate)} (${formatCount(health.errors)} errors)` : DASH },
            { label: "p95 latency", value: health ? `${approxP95(health)}${p95Bucket(health) ? ` · bucket ${p95Bucket(health)}` : ""}` : DASH },
            {
              label: "Last deployment",
              value: catalog?.last_deployment ? (
                <Link href={`/app/deployments/${catalog.last_deployment.id}`} className="text-brand-text hover:underline">
                  {catalog.last_deployment.version}, {formatRelativeTime(catalog.last_deployment.deployed_at)}
                </Link>
              ) : (
                "None reported"
              ),
            },
            { label: "Calls", value: calls.length ? calls.map((e) => `${e.target} (${formatCount(e.call_count)})`).join(", ") : "None seen" },
            { label: "Called by", value: callers.length ? callers.map((e) => `${e.source} (${formatCount(e.call_count)})`).join(", ") : "None seen" },
          ]}
        />
      </div>
    </aside>
  );
}

export function MapCanvas({ map, catalog, selected, onSelect }: { map: ServiceMap; catalog: Map<string, ServiceItem>; selected: string | null; onSelect: (s: string | null) => void }) {
  const theme = useTheme();
  const { nodes, edges } = useMemo(() => {
    const positions = layeredLayout(map.nodes.map((n) => n.service), map.edges);
    return {
      nodes: map.nodes.map<Node<MapNodeData>>((n) => ({
        id: n.service,
        type: "service",
        position: positions.get(n.service) ?? { x: 0, y: 0 },
        data: { service: n.service, role: mapRole(n), status: n.status, catalog: catalog.get(n.service) },
        selected: n.service === selected,
        draggable: false,
        connectable: false,
        ariaLabel: `${n.service}: ${MAP_LABELS[mapRole(n)] ?? RADIUS_ROLE_META[mapRole(n)].label}. Press Enter for details.`,
      })),
      edges: map.edges.map<Edge>((e) => ({
        id: `${e.source}->${e.target}`,
        source: e.source,
        target: e.target,
        markerEnd: { type: MarkerType.ArrowClosed, width: 16, height: 16 },
        label: formatCount(e.call_count),
        labelStyle: { fontSize: 10, fill: "var(--text-muted)" },
        labelBgStyle: { fill: "var(--panel)" },
        ariaLabel: `${e.source} calls ${e.target}: ${formatCount(e.call_count)} calls since ${formatDateTime(e.first_seen)}`,
      })),
    };
  }, [map, catalog, selected]);

  return (
    <div className="h-[28rem] w-full rounded-xl border bg-panel shadow-panel">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={NODE_TYPES}
        colorMode={theme}
        fitView
        fitViewOptions={{ padding: 0.15 }}
        nodesDraggable={false}
        nodesConnectable={false}
        zoomOnScroll={false}
        preventScrolling={false}
        onNodeClick={(_, node) => onSelect(node.id)}
        onSelectionChange={({ nodes: picked }) => {
          if (picked[0]) onSelect(picked[0].id);
        }}
        onPaneClick={() => onSelect(null)}
        proOptions={{ hideAttribution: true }}
        minZoom={0.25}
      >
        <Background gap={20} size={1} />
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  );
}

const WINDOWS = [
  { hours: 1, label: "Last hour" },
  { hours: 6, label: "Last 6 hours" },
  { hours: 24, label: "Last 24 hours" },
  { hours: 168, label: "Last 7 days" },
];

export function ServiceMapView() {
  const { orgId } = useSession();
  const services = useServices(orgId);
  const catalogRows = useMemo(() => services.data?.pages.flatMap((p) => p.items) ?? [], [services.data]);
  const environments = useMemo(
    () => [...new Set(catalogRows.map((s) => s.environment).filter((e): e is string => Boolean(e)))].sort(),
    [catalogRows],
  );
  const [envChoice, setEnvChoice] = useState<string | null>(null);
  const environment = envChoice && environments.includes(envChoice) ? envChoice : environments[0];
  const [windowHours, setWindowHours] = useState(24);
  const map = useServiceMap(orgId, environment, windowHours);
  const [selected, setSelected] = useState<string | null>(null);
  const catalog = useMemo(
    () => new Map(catalogRows.filter((s) => s.environment === environment).map((s) => [s.service, s])),
    [catalogRows, environment],
  );

  if (services.isPending) return <LoadingState label="Loading services…" lines={6} />;
  if (services.isError) return <ApiErrorState what="The service catalog" error={services.error} onRetry={() => void services.refetch()} />;
  if (environments.length === 0) {
    return catalogRows.length === 0 ? <ServicesSetup /> : <ServiceMapSetup />;
  }

  const selectedNode = map.data?.nodes.find((n) => n.service === selected);
  return (
    <>
      <div className="mb-4 flex flex-wrap items-end gap-3">
        <div>
          <label htmlFor="map-env" className="mb-1 block text-xs font-medium text-muted-foreground">
            Environment
          </label>
          <Select id="map-env" value={environment} onChange={(e) => setEnvChoice(e.target.value)} className="h-8 w-auto">
            {environments.map((env) => (
              <option key={env} value={env}>
                {env}
              </option>
            ))}
          </Select>
        </div>
        <div>
          <label htmlFor="map-window" className="mb-1 block text-xs font-medium text-muted-foreground">
            Edges seen
          </label>
          <Select id="map-window" value={windowHours} onChange={(e) => setWindowHours(Number(e.target.value))} className="h-8 w-auto">
            {WINDOWS.map((w) => (
              <option key={w.hours} value={w.hours}>
                {w.label}
              </option>
            ))}
          </Select>
        </div>
        {map.data?.analysis ? (
          <p className="text-xs text-muted-foreground">
            Health from the latest analysis:{" "}
            <Link href={`/app/deployments/${map.data.analysis.deployment_id}`} className="text-brand-text hover:underline">
              {map.data.analysis.deployed_service} deploy, {KIND_LABEL[map.data.analysis.kind].toLowerCase()}, {formatRelativeTime(map.data.analysis.analysed_at)}
            </Link>
          </p>
        ) : null}
      </div>

      {map.isPending ? (
        <LoadingState label="Loading the service map…" lines={6} />
      ) : map.isError ? (
        <ApiErrorState what="The service map" error={map.error} onRetry={() => void map.refetch()} />
      ) : map.data.nodes.length === 0 ? (
        <ServiceMapSetup />
      ) : (
        <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_20rem]">
          <div className="min-w-0 space-y-3">
            <MapCanvas map={map.data} catalog={catalog} selected={selected} onSelect={setSelected} />
            <RadiusLegend roles={[...new Set(map.data.nodes.map(mapRole))]} labels={MAP_LABELS} />
          </div>
          <div className="space-y-4">
            {selectedNode ? (
              <ServicePanel node={selectedNode} catalog={catalog.get(selectedNode.service)} map={map.data} onClose={() => setSelected(null)} />
            ) : null}
            <nav aria-label="Services on the map" className="rounded-xl border bg-panel p-3 shadow-panel">
              <h2 className="px-1 pb-2 text-xs font-medium tracking-wide text-muted-foreground uppercase">Services ({map.data.nodes.length})</h2>
              <ul className="space-y-0.5">
                {map.data.nodes
                  .map((n) => n.service)
                  .sort()
                  .map((service) => (
                    <li key={service}>
                      <button
                        type="button"
                        aria-pressed={service === selected}
                        onClick={() => setSelected(service === selected ? null : service)}
                        className="w-full rounded-md px-2 py-1.5 text-left text-sm text-foreground-secondary hover:bg-subtle hover:text-foreground aria-pressed:bg-subtle aria-pressed:text-foreground"
                      >
                        {service}
                      </button>
                    </li>
                  ))}
              </ul>
            </nav>
          </div>
        </div>
      )}
    </>
  );
}
