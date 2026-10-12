"""Service dependency graph derived from traces, with cycle-safe traversals.

**Edges.** ``A → B`` ("A calls B") exists when a span of service ``A`` is the parent of a span of
service ``B`` with ``A != B``. ``call_count`` is the number of such parent/child span pairs. In
OTel-instrumented HTTP this is normally ``A``'s CLIENT span parenting ``B``'s SERVER span, so the
count equals the number of observed calls. Same-service parent/child pairs are never edges
(self-loops are ignored everywhere, including :meth:`ServiceGraph.from_edges`).

**Nodes.** Every service that appears in the input, including services with no edges at all.

**Traversals.** ``bfs_*`` return ``{service: depth}`` in breadth-first order, where depth is the
shortest number of hops from the start. ``dfs_*`` return pre-order lists. All traversals keep a
visited set, so cycles, diamonds (two paths to one node) and multiple roots terminate and report
each node once. **The start node is never part of the result**, even when a cycle leads back to
it: callers ask "who else is reachable", and the start node is already the subject.
Neighbours are visited in sorted order, so results are deterministic.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict

from rl_core.telemetry.index import TraceIndex
from rl_core.telemetry.model import Span


class EdgeSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source: str
    target: str
    call_count: int


class GraphSnapshot(BaseModel):
    """JSON-serialisable graph, as stored with a regression analysis."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    nodes: tuple[str, ...]
    edges: tuple[EdgeSnapshot, ...]


@dataclass(frozen=True, slots=True)
class Edge:
    source: str
    target: str
    call_count: int


class ServiceGraph:
    """Directed multigraph collapsed to weighted edges ``(source, target) → call_count``."""

    __slots__ = ("_edges", "_in", "_nodes", "_out")

    def __init__(
        self, nodes: Iterable[str] = (), edges: Mapping[tuple[str, str], int] | None = None
    ):
        self._nodes: set[str] = set(nodes)
        self._edges: dict[tuple[str, str], int] = {}
        self._out: dict[str, set[str]] = {}
        self._in: dict[str, set[str]] = {}
        for (source, target), count in (edges or {}).items():
            self._add_edge(source, target, count)
        for node in self._nodes:
            self._out.setdefault(node, set())
            self._in.setdefault(node, set())

    def _add_edge(self, source: str, target: str, count: int) -> None:
        self._nodes.add(source)
        self._nodes.add(target)
        self._out.setdefault(source, set())
        self._in.setdefault(source, set())
        self._out.setdefault(target, set())
        self._in.setdefault(target, set())
        # Validate before the self-loop shortcut: a negative count is corrupt input either way.
        if count < 0:
            raise ValueError(f"call_count must be non-negative, got {count} for {source}->{target}")
        if source == target:
            return  # self-loops carry no dependency information between services
        self._edges[(source, target)] = self._edges.get((source, target), 0) + count
        self._out[source].add(target)
        self._in[target].add(source)

    # ------------------------------------------------------------------ constructors
    @classmethod
    def from_edges(
        cls,
        edges: Iterable[tuple[str, str] | tuple[str, str, int]],
        nodes: Iterable[str] = (),
    ) -> ServiceGraph:
        """Build from ``(source, target)`` or ``(source, target, call_count)`` rows.

        Repeated rows add up; a missing count means 1. This is also how ``service_edges`` rows are
        loaded.
        """
        graph = cls(nodes)
        for row in edges:
            if len(row) == 3:
                source, target, count = row
            elif len(row) == 2:
                source, target = row[0], row[1]
                count = 1
            else:
                raise ValueError(f"edge rows are (source, target[, call_count]), got {row!r}")
            graph._add_edge(source, target, int(count))
        return graph

    @classmethod
    def from_spans(
        cls,
        spans: Iterable[Span] | TraceIndex,
        *,
        start_ns: int | None = None,
        end_ns: int | None = None,
    ) -> ServiceGraph:
        """Build from spans. With a window, only child spans starting in ``[start_ns, end_ns]``
        create edges and nodes; parents are looked up in the whole input, so a call whose parent
        started just before the window still counts.
        """
        index = spans if isinstance(spans, TraceIndex) else TraceIndex.build(spans)
        graph = cls()
        counts: dict[tuple[str, str], int] = {}
        for span in index:
            if start_ns is not None and span.start_ns < start_ns:
                continue
            if end_ns is not None and span.start_ns > end_ns:
                continue
            graph._nodes.add(span.service)
            parent = index.parent_of(span)
            if parent is None:
                continue
            graph._nodes.add(parent.service)
            if parent.service != span.service:
                pair = (parent.service, span.service)
                counts[pair] = counts.get(pair, 0) + 1
        for (source, target), count in counts.items():
            graph._add_edge(source, target, count)
        for node in graph._nodes:
            graph._out.setdefault(node, set())
            graph._in.setdefault(node, set())
        return graph

    # ------------------------------------------------------------------ accessors
    @property
    def nodes(self) -> tuple[str, ...]:
        return tuple(sorted(self._nodes))

    @property
    def edges(self) -> tuple[Edge, ...]:
        return tuple(Edge(s, t, c) for (s, t), c in sorted(self._edges.items()))

    def __contains__(self, node: object) -> bool:
        return node in self._nodes

    def _require(self, node: str) -> None:
        if node not in self._nodes:
            raise KeyError(f"unknown service {node!r}")

    def downstream(self, node: str) -> tuple[str, ...]:
        """Direct callees of ``node``."""
        self._require(node)
        return tuple(sorted(self._out[node]))

    def upstream(self, node: str) -> tuple[str, ...]:
        """Direct callers of ``node``."""
        self._require(node)
        return tuple(sorted(self._in[node]))

    def call_count(self, source: str, target: str) -> int:
        return self._edges.get((source, target), 0)

    # ------------------------------------------------------------------ traversals
    def _bfs(self, start: str, adjacency: Mapping[str, set[str]]) -> dict[str, int]:
        self._require(start)
        depth: dict[str, int] = {start: 0}
        queue: deque[str] = deque([start])
        while queue:
            node = queue.popleft()
            for nxt in sorted(adjacency[node]):
                if nxt not in depth:
                    depth[nxt] = depth[node] + 1
                    queue.append(nxt)
        del depth[start]
        return depth

    def _dfs(self, start: str, adjacency: Mapping[str, set[str]]) -> list[str]:
        self._require(start)
        visited: set[str] = {start}
        order: list[str] = []
        # Iterative pre-order DFS; neighbours pushed in reverse so the smallest is visited first.
        stack: list[str] = sorted(adjacency[start], reverse=True)
        while stack:
            node = stack.pop()
            if node in visited:
                continue
            visited.add(node)
            order.append(node)
            stack.extend(n for n in sorted(adjacency[node], reverse=True) if n not in visited)
        return order

    def bfs_downstream(self, node: str) -> dict[str, int]:
        """Everything ``node`` (transitively) calls → shortest hop count."""
        return self._bfs(node, self._out)

    def bfs_upstream(self, node: str) -> dict[str, int]:
        """Everything that (transitively) calls ``node`` → shortest hop count."""
        return self._bfs(node, self._in)

    def dfs_downstream(self, node: str) -> list[str]:
        return self._dfs(node, self._out)

    def dfs_upstream(self, node: str) -> list[str]:
        return self._dfs(node, self._in)

    def snapshot(self) -> GraphSnapshot:
        return GraphSnapshot(
            nodes=self.nodes,
            edges=tuple(
                EdgeSnapshot(source=e.source, target=e.target, call_count=e.call_count)
                for e in self.edges
            ),
        )
