"""Blast radius of a probable origin (spec M9).

From the origin, breadth-first search the service graph upstream (callers, transitively) and
downstream (callees, transitively). **Graph reachability alone never makes a service affected**:
a reachable service is AFFECTED only if its *own* telemetry regressed — its status is REGRESSED
under the strict detector, or under the relaxed variant
(:meth:`rl_core.regression.windows.WindowConfig.relaxed`) when one was evaluated. Otherwise it
is HEALTHY. Services that are not reachable from the origin in either direction (e.g. a sibling
callee of a common caller, like ``inventory`` next to ``payment`` under ``orders``) are not in the
radius at all.

Two classes go beyond the spec's four, so that missing data is never presented as health:
``UNKNOWN_UPSTREAM`` / ``UNKNOWN_DOWNSTREAM`` for reachable services whose telemetry could not be
assessed (INSUFFICIENT_DATA under every evaluated variant, or no evidence at all).

A service reachable in both directions (only possible with a cycle through the origin) is
classified by its shorter path; ties go to upstream, where impact on callers is what users feel.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict

from rl_core.graph import ServiceGraph
from rl_core.regression.evidence import ServiceStatus


class Impact(StrEnum):
    AFFECTED_UPSTREAM = "AFFECTED_UPSTREAM"
    AFFECTED_DOWNSTREAM = "AFFECTED_DOWNSTREAM"
    HEALTHY_UPSTREAM = "HEALTHY_UPSTREAM"
    HEALTHY_DOWNSTREAM = "HEALTHY_DOWNSTREAM"
    UNKNOWN_UPSTREAM = "UNKNOWN_UPSTREAM"
    UNKNOWN_DOWNSTREAM = "UNKNOWN_DOWNSTREAM"


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class RadiusNode(_Model):
    service: str
    direction: Literal["upstream", "downstream"]
    depth: int  # shortest hop count from the origin
    impact: Impact
    strict_status: ServiceStatus
    relaxed_status: ServiceStatus | None  # None when the relaxed variant was not evaluated
    affected_via: Literal["strict", "relaxed"] | None


class BlastRadius(_Model):
    origin: str
    nodes: tuple[RadiusNode, ...]  # upstream first, then downstream; by depth, then name
    affected: tuple[str, ...]
    not_in_radius: tuple[str, ...]  # graph services unreachable from the origin


def _classify(
    direction: Literal["upstream", "downstream"],
    strict: ServiceStatus,
    relaxed: ServiceStatus | None,
) -> tuple[Impact, Literal["strict", "relaxed"] | None]:
    up = direction == "upstream"
    if strict is ServiceStatus.REGRESSED:
        return (Impact.AFFECTED_UPSTREAM if up else Impact.AFFECTED_DOWNSTREAM), "strict"
    if relaxed is ServiceStatus.REGRESSED:
        return (Impact.AFFECTED_UPSTREAM if up else Impact.AFFECTED_DOWNSTREAM), "relaxed"
    unknown_strict = strict is ServiceStatus.INSUFFICIENT_DATA
    unknown_relaxed = relaxed is None or relaxed is ServiceStatus.INSUFFICIENT_DATA
    if unknown_strict and unknown_relaxed:
        return (Impact.UNKNOWN_UPSTREAM if up else Impact.UNKNOWN_DOWNSTREAM), None
    return (Impact.HEALTHY_UPSTREAM if up else Impact.HEALTHY_DOWNSTREAM), None


def compute_blast_radius(
    graph: ServiceGraph,
    origin: str,
    strict_status: Mapping[str, ServiceStatus],
    relaxed_status: Mapping[str, ServiceStatus] | None = None,
) -> BlastRadius:
    """Classify every service reachable from ``origin`` (see module docstring).

    ``strict_status`` maps service → status from the strict detector; services missing from it
    are treated as INSUFFICIENT_DATA. ``relaxed_status`` holds relaxed-detector statuses for the
    services where it was evaluated.
    """
    if origin not in graph:
        raise KeyError(f"origin {origin!r} is not in the service graph")
    relaxed = relaxed_status or {}
    up = graph.bfs_upstream(origin)
    down = graph.bfs_downstream(origin)

    placed: dict[str, tuple[Literal["upstream", "downstream"], int]] = {}
    for service, depth in up.items():
        placed[service] = ("upstream", depth)
    for service, depth in down.items():
        if service not in placed or depth < placed[service][1]:
            placed[service] = ("downstream", depth)

    nodes: list[RadiusNode] = []
    for service, (direction, depth) in placed.items():
        strict = strict_status.get(service, ServiceStatus.INSUFFICIENT_DATA)
        rel = relaxed.get(service)
        impact, via = _classify(direction, strict, rel)
        nodes.append(
            RadiusNode(
                service=service,
                direction=direction,
                depth=depth,
                impact=impact,
                strict_status=strict,
                relaxed_status=rel,
                affected_via=via,
            )
        )
    nodes.sort(key=lambda n: (n.direction != "upstream", n.depth, n.service))
    affected = tuple(n.service for n in nodes if n.affected_via is not None)
    outside = tuple(s for s in graph.nodes if s != origin and s not in placed)
    return BlastRadius(origin=origin, nodes=tuple(nodes), affected=affected, not_in_radius=outside)
