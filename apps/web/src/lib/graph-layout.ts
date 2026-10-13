/**
 * Layered left-to-right layout for a call graph: callers left of callees. Each node's column is
 * its longest path from a root (a node nobody calls); cycles are broken by ignoring back edges
 * found in a DFS. Rows inside a column are alphabetical. Deterministic for the same input.
 */
export function layeredLayout(
  nodes: string[],
  edges: Array<{ source: string; target: string }>,
  { gapX = 240, gapY = 110 }: { gapX?: number; gapY?: number } = {},
): Map<string, { x: number; y: number }> {
  const ids = [...new Set(nodes)].sort();
  const known = new Set(ids);
  const out = new Map<string, string[]>();
  for (const id of ids) out.set(id, []);
  for (const e of edges) {
    if (known.has(e.source) && known.has(e.target) && e.source !== e.target) out.get(e.source)!.push(e.target);
  }
  for (const list of out.values()) list.sort();

  // Drop back edges (DFS colouring) so the graph is a DAG.
  const state = new Map<string, 0 | 1 | 2>();
  const dag = new Map<string, string[]>(ids.map((id) => [id, []]));
  const visit = (id: string) => {
    state.set(id, 1);
    for (const next of out.get(id)!) {
      const s = state.get(next) ?? 0;
      if (s === 1) continue; // back edge
      dag.get(id)!.push(next);
      if (s === 0) visit(next);
    }
    state.set(id, 2);
  };
  const indegree = new Map(ids.map((id) => [id, 0]));
  for (const list of out.values()) for (const t of list) indegree.set(t, (indegree.get(t) ?? 0) + 1);
  for (const id of ids.filter((i) => indegree.get(i) === 0)) if (!state.get(id)) visit(id);
  for (const id of ids) if (!state.get(id)) visit(id);

  // Longest-path depth over the DAG (Kahn order).
  const depth = new Map(ids.map((id) => [id, 0]));
  const inDag = new Map(ids.map((id) => [id, 0]));
  for (const list of dag.values()) for (const t of list) inDag.set(t, inDag.get(t)! + 1);
  const queue = ids.filter((id) => inDag.get(id) === 0);
  while (queue.length) {
    const id = queue.shift()!;
    for (const t of dag.get(id)!) {
      depth.set(t, Math.max(depth.get(t)!, depth.get(id)! + 1));
      inDag.set(t, inDag.get(t)! - 1);
      if (inDag.get(t) === 0) queue.push(t);
    }
  }

  const columns = new Map<number, string[]>();
  for (const id of ids) {
    const d = depth.get(id)!;
    columns.set(d, [...(columns.get(d) ?? []), id]);
  }
  const positions = new Map<string, { x: number; y: number }>();
  for (const [col, list] of columns) {
    const offset = ((list.length - 1) * gapY) / 2;
    list.forEach((id, i) => positions.set(id, { x: col * gapX, y: i * gapY - offset }));
  }
  return positions;
}
