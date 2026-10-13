"use client";

import "@xyflow/react/dist/style.css";

import { Background, Handle, MarkerType, Position, ReactFlow, type Edge, type Node, type NodeProps } from "@xyflow/react";
import { Workflow } from "lucide-react";
import { useMemo } from "react";

import { RADIUS_ROLE_ICON, RadiusLegend, radiusNodeClasses } from "@/components/graph/radius-legend";
import { layoutRadius, RADIUS_ROLE_META, STATUS_META, type RadiusGraph, type RadiusGraphNode } from "@/lib/analysis";
import { TONE_CLASSES } from "@/lib/tones";
import { useTheme } from "@/lib/use-theme";
import { cn } from "@/lib/utils";

// A mapped type (not the interface) so it satisfies React Flow's Record<string, unknown> constraint.
type RadiusNodeData = { [K in keyof RadiusGraphNode]: RadiusGraphNode[K] } & { label: string };

function RadiusNodeView({ data }: NodeProps<Node<RadiusNodeData>>) {
  const meta = RADIUS_ROLE_META[data.role];
  const Icon = RADIUS_ROLE_ICON[data.role];
  return (
    <div
      className={cn("w-44 rounded-lg px-2.5 py-2 text-left shadow-panel", radiusNodeClasses(data.role))}
      data-role={data.role}
    >
      <Handle type="target" position={Position.Left} className="!size-1.5 !border-0 !bg-border-strong" />
      <div className="flex items-center gap-1.5">
        <Icon aria-hidden="true" className={cn("size-3.5 shrink-0", data.role === "outside" ? "text-muted-foreground" : TONE_CLASSES[meta.tone].text)} />
        <span className="truncate text-[13px] font-semibold text-foreground">{data.id}</span>
      </div>
      <div className="mt-0.5 text-[11px] text-foreground-secondary">
        {meta.label}
        {data.direction === "upstream" || data.direction === "downstream" ? ` · ${data.direction} ${data.depth}` : ""}
        {data.via === "relaxed" ? " · relaxed gates" : ""}
      </div>
      {data.status ? <div className="text-[11px] text-muted-foreground">{STATUS_META[data.status].label}</div> : null}
      <Handle type="source" position={Position.Right} className="!size-1.5 !border-0 !bg-border-strong" />
    </div>
  );
}

const NODE_TYPES = { radius: RadiusNodeView };

/** Text equivalent of the graph for screen readers and copy/paste. */
export function RadiusList({ graph }: { graph: RadiusGraph }) {
  const groups = (["origin", "affected", "healthy", "unknown", "outside"] as const)
    .map((role) => ({ role, nodes: graph.nodes.filter((n) => n.role === role) }))
    .filter((g) => g.nodes.length > 0);
  return (
    <dl className="grid gap-x-6 gap-y-2 text-xs sm:grid-cols-2">
      {groups.map((g) => (
        <div key={g.role}>
          <dt className="text-muted-foreground">{RADIUS_ROLE_META[g.role].label}</dt>
          <dd className="text-foreground">
            {g.nodes
              .map((n) => (n.direction === "upstream" || n.direction === "downstream" ? `${n.id} (${n.direction}, ${n.depth} hop${n.depth === 1 ? "" : "s"})` : n.id))
              .join(", ")}
          </dd>
        </div>
      ))}
    </dl>
  );
}

export function BlastRadius({ graph }: { graph: RadiusGraph | null }) {
  const theme = useTheme();
  const { nodes, edges } = useMemo(() => {
    if (!graph) return { nodes: [] as Node<RadiusNodeData>[], edges: [] as Edge[] };
    const positions = layoutRadius(graph);
    return {
      nodes: graph.nodes.map<Node<RadiusNodeData>>((n) => ({
        id: n.id,
        type: "radius",
        position: positions.get(n.id) ?? { x: 0, y: 0 },
        data: { ...n, label: n.id },
        draggable: false,
        connectable: false,
        ariaLabel: `${n.id}: ${RADIUS_ROLE_META[n.role].label}`,
      })),
      edges: graph.edges.map<Edge>((e) => ({
        id: e.id,
        source: e.source,
        target: e.target,
        markerEnd: { type: MarkerType.ArrowClosed, width: 16, height: 16 },
        style: { strokeWidth: 1.25 },
        ariaLabel: `${e.source} calls ${e.target}, ${e.callCount.toLocaleString("en-US")} calls`,
      })),
    };
  }, [graph]);

  return (
    <section aria-labelledby="radius-title" className="rounded-xl border bg-panel shadow-panel">
      <div className="flex flex-wrap items-center gap-2 border-b px-5 py-3.5">
        <Workflow aria-hidden="true" className="size-4 text-muted-foreground" />
        <h2 id="radius-title" className="text-sm font-semibold">
          Blast radius
        </h2>
        {graph?.originIsCandidate ? (
          <span className="text-xs text-muted-foreground">centred on the top candidate (no probable origin was named)</span>
        ) : null}
      </div>
      {!graph ? (
        <p className="px-5 py-4 text-sm text-muted-foreground">
          No blast radius: it is computed from the probable origin, and nothing regressed in this analysis.
        </p>
      ) : (
        <>
          <div className="h-80 w-full" role="img" aria-label="Blast radius graph. The same information is listed below it.">
            <ReactFlow
              nodes={nodes}
              edges={edges}
              nodeTypes={NODE_TYPES}
              colorMode={theme}
              fitView
              fitViewOptions={{ padding: 0.2 }}
              nodesDraggable={false}
              nodesConnectable={false}
              elementsSelectable={false}
              zoomOnScroll={false}
              preventScrolling={false}
              proOptions={{ hideAttribution: true }}
              minZoom={0.3}
            >
              <Background gap={20} size={1} />
            </ReactFlow>
          </div>
          <div className="space-y-3 border-t px-5 py-3.5">
            <RadiusLegend roles={[...new Set(graph.nodes.map((n) => n.role))]} />
            <RadiusList graph={graph} />
          </div>
        </>
      )}
    </section>
  );
}
