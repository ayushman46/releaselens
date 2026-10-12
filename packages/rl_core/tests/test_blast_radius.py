import pytest

from rl_core.blast_radius import Impact, compute_blast_radius
from rl_core.graph import ServiceGraph
from rl_core.regression.evidence import ServiceStatus

R, H, W, X = (
    ServiceStatus.REGRESSED,
    ServiceStatus.HEALTHY,
    ServiceStatus.WARNING,
    ServiceStatus.INSUFFICIENT_DATA,
)
DEMO = ServiceGraph.from_edges(
    [("gateway", "orders"), ("orders", "inventory"), ("orders", "payment"), ("payment", "fraud")]
)


def _impacts(radius: object) -> dict[str, Impact]:
    return {n.service: n.impact for n in radius.nodes}  # type: ignore[attr-defined]


def test_payment_origin_radius() -> None:
    radius = compute_blast_radius(
        DEMO,
        "payment",
        {"payment": R, "orders": R, "gateway": R, "fraud": H, "inventory": H},
    )
    assert _impacts(radius) == {
        "orders": Impact.AFFECTED_UPSTREAM,
        "gateway": Impact.AFFECTED_UPSTREAM,
        "fraud": Impact.HEALTHY_DOWNSTREAM,
    }
    assert radius.not_in_radius == ("inventory",)  # sibling callee: no directed path either way
    assert radius.affected == ("orders", "gateway")
    assert [(n.service, n.depth) for n in radius.nodes] == [
        ("orders", 1),
        ("gateway", 2),
        ("fraud", 1),
    ]


def test_graph_reachable_is_not_affected() -> None:
    # reports calls payment too, but its own telemetry did not regress (e.g. rare calls).
    g = ServiceGraph.from_edges(
        [("gateway", "orders"), ("orders", "payment"), ("reports", "payment"), ("payment", "fraud")]
    )
    radius = compute_blast_radius(
        g, "payment", {"payment": R, "orders": R, "gateway": R, "reports": H, "fraud": H}
    )
    impacts = _impacts(radius)
    assert impacts["reports"] is Impact.HEALTHY_UPSTREAM
    assert impacts["fraud"] is Impact.HEALTHY_DOWNSTREAM
    assert "reports" not in radius.affected
    assert set(radius.affected) == {"orders", "gateway"}


def test_relaxed_detector_can_mark_affected_and_is_recorded() -> None:
    radius = compute_blast_radius(
        DEMO,
        "payment",
        {"payment": R, "orders": R, "gateway": H, "fraud": W},
        {"gateway": R, "fraud": H},
    )
    nodes = {n.service: n for n in radius.nodes}
    assert nodes["gateway"].impact is Impact.AFFECTED_UPSTREAM
    assert nodes["gateway"].affected_via == "relaxed"
    assert nodes["orders"].affected_via == "strict"
    assert nodes["fraud"].impact is Impact.HEALTHY_DOWNSTREAM  # WARNING is not a regression
    assert nodes["fraud"].strict_status is W


def test_missing_or_insufficient_data_is_unknown_not_healthy() -> None:
    radius = compute_blast_radius(DEMO, "payment", {"payment": R, "orders": X}, {"orders": X})
    impacts = _impacts(radius)
    assert impacts["orders"] is Impact.UNKNOWN_UPSTREAM
    assert impacts["gateway"] is Impact.UNKNOWN_UPSTREAM  # no evidence at all
    assert impacts["fraud"] is Impact.UNKNOWN_DOWNSTREAM


def test_cycle_through_origin_uses_shorter_direction() -> None:
    # a → o → b → a: a is upstream at 1 hop and downstream at 2; b the other way round.
    g = ServiceGraph.from_edges([("a", "o"), ("o", "b"), ("b", "a"), ("o", "c")])
    radius = compute_blast_radius(g, "o", {"a": R, "b": R, "c": H})
    nodes = {n.service: n for n in radius.nodes}
    assert (nodes["a"].direction, nodes["a"].depth) == ("upstream", 1)
    assert (nodes["b"].direction, nodes["b"].depth) == ("downstream", 1)
    assert nodes["a"].impact is Impact.AFFECTED_UPSTREAM
    assert nodes["b"].impact is Impact.AFFECTED_DOWNSTREAM
    assert nodes["c"].impact is Impact.HEALTHY_DOWNSTREAM
    # equal distance both ways → upstream
    tie = ServiceGraph.from_edges([("x", "o"), ("o", "x")])
    assert compute_blast_radius(tie, "o", {"x": H}).nodes[0].direction == "upstream"


def test_unknown_origin_rejected() -> None:
    with pytest.raises(KeyError):
        compute_blast_radius(DEMO, "nope", {})
