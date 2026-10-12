"""Service graph construction and cycle-safe traversals (exact + Hypothesis vs. closure)."""

from __future__ import annotations

import json

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from tracegen import control, generate, make_span

from rl_core.graph import GraphSnapshot, ServiceGraph
from rl_core.telemetry.model import SpanKind

C, S, IN = SpanKind.CLIENT, SpanKind.SERVER, SpanKind.INTERNAL


# ----------------------------------------------------------------------------- construction


def test_from_spans_builds_demo_topology_with_call_counts() -> None:
    spans = generate(control(rate_per_minute=10), seed=1)
    graph = ServiceGraph.from_spans(spans)
    assert graph.nodes == ("fraud", "gateway", "inventory", "orders", "payment")
    edges = {(e.source, e.target): e.call_count for e in graph.edges}
    assert set(edges) == {
        ("gateway", "orders"),
        ("orders", "inventory"),
        ("orders", "payment"),
        ("payment", "fraud"),
    }
    gateway_requests = sum(1 for s in spans if s.service == "gateway" and s.kind is S)
    # Every request reaches orders unless gateway's own work failed first.
    assert 0 < edges[("gateway", "orders")] <= gateway_requests
    # Each edge is one CLIENT → SERVER pair: the callee's SERVER-span count.
    fraud_requests = sum(1 for s in spans if s.service == "fraud" and s.kind is S)
    assert edges[("payment", "fraud")] == fraud_requests


def test_from_spans_ignores_same_service_links_and_keeps_isolated_nodes() -> None:
    spans = [
        make_span("a1", None, "a", S, 0, 10),
        make_span("a2", "a1", "a", IN, 1, 2),  # same service: no edge
        make_span("ac", "a1", "a", C, 3, 9),
        make_span("b1", "ac", "b", S, 4, 8),
        make_span("lonely", None, "batch", S, 0, 5),  # no edges at all
        make_span("orphan", "missing-parent", "c", S, 0, 5),  # parent never received
    ]
    graph = ServiceGraph.from_spans(spans)
    assert graph.nodes == ("a", "b", "batch", "c")
    assert [(e.source, e.target, e.call_count) for e in graph.edges] == [("a", "b", 1)]
    assert graph.downstream("batch") == ()
    assert graph.upstream("c") == ()


def test_from_spans_window_counts_children_whose_parent_is_outside() -> None:
    spans = [
        make_span("a1", None, "a", S, 0, 100),
        make_span("ac", "a1", "a", C, 50, 90),
        make_span("b1", "ac", "b", S, 51, 89),
    ]
    graph = ServiceGraph.from_spans(spans, start_ns=40_000_000, end_ns=60_000_000)
    assert graph.call_count("a", "b") == 1
    assert "a" in graph
    empty = ServiceGraph.from_spans(spans, start_ns=95_000_000)
    assert empty.nodes == ()


def test_from_edges_sums_rows_and_ignores_self_loops() -> None:
    graph = ServiceGraph.from_edges(
        [("a", "b", 3), ("a", "b", 2), ("b", "b", 9), ("b", "c")], nodes=["z"]
    )
    assert graph.call_count("a", "b") == 5
    assert graph.call_count("b", "b") == 0
    assert graph.call_count("b", "c") == 1
    assert graph.nodes == ("a", "b", "c", "z")
    assert graph.bfs_downstream("b") == {"c": 1}
    with pytest.raises(ValueError, match="non-negative"):
        ServiceGraph.from_edges([("a", "b", -1)])


def test_snapshot_is_json_serialisable() -> None:
    graph = ServiceGraph.from_edges([("a", "b", 2)], nodes=["c"])
    snap = graph.snapshot()
    data = json.loads(snap.model_dump_json())
    assert data == {
        "nodes": ["a", "b", "c"],
        "edges": [{"source": "a", "target": "b", "call_count": 2}],
    }
    assert GraphSnapshot.model_validate_json(snap.model_dump_json()) == snap


# ----------------------------------------------------------------------------- traversals


def test_diamond_reports_each_node_once_with_shortest_depth() -> None:
    #   a → b → d,  a → c → d → e,  a → e (shortcut)
    g = ServiceGraph.from_edges(
        [("a", "b"), ("a", "c"), ("b", "d"), ("c", "d"), ("d", "e"), ("a", "e")]
    )
    assert g.bfs_downstream("a") == {"b": 1, "c": 1, "e": 1, "d": 2}
    assert list(g.bfs_downstream("a")) == ["b", "c", "e", "d"]  # breadth-first order
    assert g.dfs_downstream("a") == ["b", "d", "e", "c"]  # pre-order, smallest first
    assert g.bfs_upstream("e") == {"a": 1, "d": 1, "b": 2, "c": 2}


def test_cycle_terminates_and_excludes_start() -> None:
    g = ServiceGraph.from_edges([("a", "b"), ("b", "c"), ("c", "a"), ("c", "d")])
    assert g.bfs_downstream("a") == {"b": 1, "c": 2, "d": 3}
    assert "a" not in g.dfs_downstream("a")
    assert g.bfs_upstream("a") == {"c": 1, "b": 2}


def test_multiple_roots_and_unknown_node() -> None:
    g = ServiceGraph.from_edges([("r1", "x"), ("r2", "x"), ("x", "y")])
    assert g.bfs_upstream("y") == {"x": 1, "r1": 2, "r2": 2}
    assert g.upstream("x") == ("r1", "r2")
    with pytest.raises(KeyError):
        g.bfs_downstream("nope")


# ----------------------------------------------------------------------------- properties

_names = st.sampled_from([f"s{i}" for i in range(7)])


def _closure(nodes: set[str], edges: set[tuple[str, str]]) -> dict[str, dict[str, int]]:
    """Oracle: all-pairs shortest hop counts by Floyd-Warshall."""
    inf = 10**9
    dist = {a: {b: (0 if a == b else inf) for b in nodes} for a in nodes}
    for a, b in edges:
        if a != b:
            dist[a][b] = 1
    for k in nodes:
        for i in nodes:
            for j in nodes:
                if dist[i][k] + dist[k][j] < dist[i][j]:
                    dist[i][j] = dist[i][k] + dist[k][j]
    return {a: {b: d for b, d in row.items() if b != a and d < inf} for a, row in dist.items()}


@settings(max_examples=300, deadline=None)
@given(
    edges=st.lists(st.tuples(_names, _names), max_size=25),
    extra=st.lists(_names, max_size=3),
)
def test_traversals_equal_naive_reachability(
    edges: list[tuple[str, str]], extra: list[str]
) -> None:
    graph = ServiceGraph.from_edges(edges, nodes=extra)
    nodes = set(graph.nodes)
    edge_set = set(edges)
    down = _closure(nodes, edge_set)
    up = _closure(nodes, {(b, a) for a, b in edge_set})
    for node in nodes:
        bfs_down = graph.bfs_downstream(node)
        bfs_up = graph.bfs_upstream(node)
        # same reachable set and shortest-path depth as the oracle; start never included
        assert bfs_down == down[node]
        assert bfs_up == up[node]
        assert node not in bfs_down
        assert node not in bfs_up
        # DFS visits the same set exactly once
        dfs_down = graph.dfs_downstream(node)
        assert sorted(dfs_down) == sorted(bfs_down)
        assert len(dfs_down) == len(set(dfs_down))
        assert sorted(graph.dfs_upstream(node)) == sorted(bfs_up)
