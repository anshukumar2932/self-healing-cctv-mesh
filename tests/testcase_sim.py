#!/usr/bin/env python3
"""
P3 (Simulation Engineer) test suite  --  Self-Healing CCTV Mesh Network
Covers: topology.py, nodes.py, links.py, simulation.py

USAGE
    python testcase_sim.py src/                      # folder holding the 4 files
    python testcase_sim.py topology.py nodes.py links.py simulation.py
    python testcase_sim.py src/ -v                   # full tracebacks on errors
    python testcase_sim.py src/ SIM-002 TOP-003      # run selected tests only

Needs: pip install networkx simpy

API CONTRACT THE TESTS EXPECT
  topology.py
    build_topology(nodes, links) -> networkx.Graph
        node attrs = node dict fields (except id); edge attrs = link dict fields
        (except source/destination). Raises ValueError for: self-loop, unknown
        node, or any edge not in the declared 11-edge list.
    set_node_online(G, node_id, online: bool)
    set_link_active(G, a, b, active: bool)
    get_active_graph(G) -> new graph without offline nodes / inactive links
                           (G itself is NOT modified)
  nodes.py
    CameraNode(node_dict)        # raises ValueError on out-of-range fields
        .id  .to_dict()  .set_online(bool)
        .update_state(buffer_available=None, load=None)
            -> {"node_id", "online", "buffer_available", "load"}
  links.py
    Link(link_dict)              # raises ValueError on invalid fields
        .to_dict()  .set_active(bool)
        .update_quality(q) -> {"source", "destination", "quality", "active"}
  simulation.py
    run_simulation(config) -> {"events": [...], "metrics": {...}}
      config: simulation_time, packet_rate, packet_size, seed, nodes, links,
              sources (list, default = all nodes with role "source"),
              max_packets (per source, optional),
              failures [{"failure_type": "relay_node"|"source_camera"|"link",
                         "failed_element": "CCTV3" or "CCTV1-CCTV3",
                         "failure_time": 20.0}],
              router (callable(routing_input)->route dict; default routing.find_route)
      Packets are generated at t = k / packet_rate while t < simulation_time.
      Generation stops at simulation_time; in-flight packets finish.
      Hop delay = link latency (ms) only; bandwidth is not modelled in v1.
      Events (all carry event_type + timestamp in seconds):
        packet_sent      packet_id, source, current_node, next_node
        packet_forwarded packet_id, source, current_node, next_node
        packet_received  packet_id, source, destination
        packet_lost      packet_id, source, current_node, reason
        route_changed    source, old_route (None for first route), new_route
      metrics: packets_sent, packets_delivered, packets_lost, packets_buffered
               (0 if no buffering yet), delivery_rate (0-1), average_hops,
               route_changes (changes AFTER the first route only)

NOTES / DEVIATIONS (P1 to confirm)
  * SIM delivery tests use lossless links (packet_loss 0.0). FIX-BASE has 0.02,
    which would drop packets randomly; random loss is tested in SIM-004/016.
  * The simulator may add event types beyond this list (failure_detected etc.,
    from P4); SIM-013 only checks types it knows are allowed.
  * `packet_forwarded` must be added to the handbook's allowed event types.
"""

import copy
import importlib
import math
import os
import sys
import time
import traceback
from types import SimpleNamespace

NODE_IDS = ["CCTV1", "CCTV2", "CCTV3", "CCTV4", "CCTV5", "CCTV6", "Gateway", "Server"]
EDGES = [
    ("CCTV1", "CCTV2"), ("CCTV1", "CCTV3"), ("CCTV2", "CCTV3"), ("CCTV3", "CCTV4"),
    ("CCTV2", "CCTV5"), ("CCTV3", "CCTV6"), ("CCTV5", "CCTV6"),
    ("CCTV2", "Gateway"), ("CCTV3", "Gateway"), ("CCTV4", "Gateway"),
    ("Gateway", "Server"),
]
ALLOWED_EVENTS = {"packet_sent", "packet_forwarded", "packet_received", "packet_lost",
                  "route_changed", "route_verified", "failure_detected",
                  "recovery_complete", "buffer_drained", "metric_snapshot"}
REQUIRED_KEYS = {
    "packet_sent": {"packet_id", "source", "current_node", "next_node"},
    "packet_forwarded": {"packet_id", "source", "current_node", "next_node"},
    "packet_received": {"packet_id", "source", "destination"},
    "packet_lost": {"packet_id", "source", "current_node", "reason"},
    "route_changed": {"source", "old_route", "new_route"},
}
METRIC_KEYS = {"packets_sent", "packets_delivered", "packets_lost", "delivery_rate",
               "average_hops", "route_changes"}


# --------------------------------------------------------------------------
# Fixtures and helpers
# --------------------------------------------------------------------------
def R(*ids):
    m = {"G": "Gateway", "S": "Server"}
    return [m.get(str(i), "CCTV" + str(i)) for i in ids]


def make_nodes(node=None):
    out = []
    for nid in NODE_IDS:
        n = {"id": nid, "online": True, "battery": 100, "buffer_available": 100,
             "reliability": 0.95, "load": 0.30,
             "role": "source" if nid.startswith("CCTV") else "relay"}
        n.update((node or {}).get(nid, {}))
        out.append(n)
    return out


def make_links(link=None, loss=0.0):
    over = {frozenset(k.split("-")): v for k, v in (link or {}).items()}
    out = []
    for a, b in EDGES:
        l = {"source": a, "destination": b, "quality": 0.90, "bandwidth": 20,
             "latency": 10, "packet_loss": loss, "active": True}
        l.update(over.get(frozenset((a, b)), {}))
        out.append(l)
    return out


def fix_fail_links():
    return {"CCTV1-CCTV3": {"quality": 0.95}, "CCTV3-Gateway": {"quality": 0.95},
            "Gateway-Server": {"quality": 0.95}}


class StubRouter:
    """Stand-in for P2's find_route so P3 is not blocked: fewest hops, then
    highest summed link quality, then alphabetical. Counts its calls."""

    def __init__(self):
        self.calls = 0

    def __call__(self, inp):
        self.calls += 1
        nodes = {n["id"]: n for n in inp["nodes"]}
        res = {"selected_route": None, "score": 0.0, "hops": None,
               "candidate_routes": [], "route_verified": False}
        src, dst = inp["source"], inp["destination"]
        if not nodes[src]["online"]:
            res["reason"] = "source_offline"
            return res
        adj, q = {}, {}
        for l in inp["links"]:
            a, b = l["source"], l["destination"]
            if l["active"] and nodes[a]["online"] and nodes[b]["online"]:
                adj.setdefault(a, []).append(b)
                adj.setdefault(b, []).append(a)
                q[frozenset((a, b))] = l["quality"]
        found = []

        def dfs(cur, path):
            if cur == dst:
                found.append(list(path))
                return
            for nb in adj.get(cur, []):
                if nb not in path:
                    path.append(nb)
                    dfs(nb, path)
                    path.pop()
        dfs(src, [src])
        if not found:
            res["reason"] = "no_route"
            return res
        best = min(found, key=lambda p: (len(p) - 1,
                   -sum(q[frozenset(e)] for e in zip(p, p[1:])), p))
        res["selected_route"] = best
        res["hops"] = len(best) - 1
        return res


def sim_config(time_s=10, rate=10, seed=42, sources=None, max_packets=None,
               node=None, link=None, loss=0.0, failures=None):
    router = StubRouter()
    cfg = {"simulation_time": time_s, "packet_rate": rate, "packet_size": 1024,
           "seed": seed, "nodes": make_nodes(node), "links": make_links(link, loss),
           "failures": failures or [], "router": router}
    if sources is not None:
        cfg["sources"] = sources
    if max_packets is not None:
        cfg["max_packets"] = max_packets
    return cfg, router


def run(m, **kw):
    cfg, router = sim_config(**kw)
    return m.simulation.run_simulation(cfg), router


def evs(out, etype):
    return [e for e in out["events"] if e["event_type"] == etype]


def packet_paths(out):
    """Rebuild each packet's node path from sent/forwarded events."""
    p = {}
    for e in out["events"]:
        if e["event_type"] == "packet_sent":
            p[e["packet_id"]] = [e["current_node"]]
            if e["next_node"] is not None:
                p[e["packet_id"]].append(e["next_node"])
        elif e["event_type"] == "packet_forwarded":
            path = p[e["packet_id"]]
            assert path[-1] == e["current_node"], \
                f"{e['packet_id']}: hop starts at {e['current_node']}, but packet was at {path[-1]}"
            path.append(e["next_node"])
    return p


def approx(actual, expected, label, tol=0.001):
    assert actual is not None, f"{label}: got None, expected {expected}"
    assert abs(actual - expected) <= tol, f"{label}: expected {expected}, got {actual}"


def link_dict(a, b, **kw):
    d = {"source": a, "destination": b, "quality": 0.9, "bandwidth": 20,
         "latency": 10, "packet_loss": 0.0, "active": True}
    d.update(kw)
    return d


def valid_node(**kw):
    d = {"id": "CCTV1", "online": True, "battery": 87, "buffer_available": 72,
         "reliability": 0.95, "load": 0.30, "role": "source"}
    d.update(kw)
    return d


def expect_value_error(fn, label):
    try:
        fn()
    except ValueError:
        return
    except Exception as e:
        raise AssertionError(f"{label}: expected ValueError, got {type(e).__name__}: {e}")
    raise AssertionError(f"{label}: no ValueError raised")


TESTS = []


def test(tid, title):
    def deco(fn):
        TESTS.append((tid, title, fn))
        return fn
    return deco


# --------------------------------------------------------------------------
# topology.py
# --------------------------------------------------------------------------
def build(m, node=None, link=None):
    return m.topology.build_topology(make_nodes(node), make_links(link))


@test("TOP-001", "Graph has 8 nodes and 11 edges")
def _(m):
    G = build(m)
    assert G.number_of_nodes() == 8, f"nodes: {G.number_of_nodes()}"
    assert G.number_of_edges() == 11, f"edges: {G.number_of_edges()}"


@test("TOP-002", "Edge set equals the declared edge list")
def _(m):
    G = build(m)
    got = {frozenset(e) for e in G.edges()}
    want = {frozenset(e) for e in EDGES}
    assert got == want, f"missing {want - got}, unexpected {got - want}"


@test("TOP-003", "Undeclared edges CCTV1-Gateway / CCTV4-Server are rejected")
def _(m):
    for a, b in (("CCTV1", "Gateway"), ("CCTV4", "Server")):
        expect_value_error(
            lambda: m.topology.build_topology(make_nodes(), make_links() + [link_dict(a, b)]),
            f"{a}-{b}")


@test("TOP-004", "Self-loop link (source == destination) is rejected")
def _(m):
    expect_value_error(
        lambda: m.topology.build_topology(make_nodes(), make_links() + [link_dict("CCTV1", "CCTV1")]),
        "self-loop")


@test("TOP-005", "Link referencing an unknown node is rejected")
def _(m):
    expect_value_error(
        lambda: m.topology.build_topology(make_nodes(), make_links() + [link_dict("CCTV1", "CCTV9")]),
        "unknown node")


@test("TOP-006", "Every node can reach the Server")
def _(m):
    G = build(m)
    for start in NODE_IDS:
        seen, stack = {start}, [start]
        while stack:
            for nb in G.neighbors(stack.pop()):
                if nb not in seen:
                    seen.add(nb)
                    stack.append(nb)
        assert "Server" in seen, f"{start} cannot reach Server"


@test("TOP-007", "Offline node / inactive link excluded from active view, data retained")
def _(m):
    t = m.topology
    G = build(m)
    t.set_node_online(G, "CCTV3", False)
    A = t.get_active_graph(G)
    assert "CCTV3" not in A.nodes, "offline CCTV3 still in active graph"
    assert A.number_of_nodes() == 7 and A.number_of_edges() == 6, \
        f"active graph has {A.number_of_nodes()} nodes / {A.number_of_edges()} edges (want 7/6)"
    assert G.number_of_nodes() == 8 and G.number_of_edges() == 11, "original graph was modified"
    assert G.nodes["CCTV3"]["online"] is False, "online flag not stored on node"

    G2 = build(m)
    t.set_link_active(G2, "CCTV1", "CCTV2", False)
    A2 = t.get_active_graph(G2)
    assert not A2.has_edge("CCTV1", "CCTV2"), "inactive link still in active graph"
    assert A2.number_of_edges() == 10 and A2.number_of_nodes() == 8
    assert G2.has_edge("CCTV1", "CCTV2") and G2.edges["CCTV1", "CCTV2"]["active"] is False


# --------------------------------------------------------------------------
# nodes.py
# --------------------------------------------------------------------------
@test("NODE-001", "CameraNode stores fields unchanged")
def _(m):
    d = valid_node()
    n = m.nodes.CameraNode(copy.deepcopy(d))
    assert n.id == "CCTV1"
    assert n.to_dict() == d, f"to_dict() = {n.to_dict()}"


@test("NODE-002", "update_state returns exactly node_id/online/buffer_available/load")
def _(m):
    n = m.nodes.CameraNode(valid_node())
    out = n.update_state(buffer_available=40, load=0.5)
    assert set(out) == {"node_id", "online", "buffer_available", "load"}, f"keys {sorted(out)}"
    assert out["node_id"] == "CCTV1" and out["online"] is True
    approx(out["buffer_available"], 40, "buffer_available")
    approx(out["load"], 0.5, "load")
    d = n.to_dict()
    approx(d["buffer_available"], 40, "stored buffer")
    approx(d["load"], 0.5, "stored load")


@test("NODE-003", "Invalid node fields rejected (battery, reliability, load, role)")
def _(m):
    for bad in ({"battery": 120}, {"reliability": 1.2}, {"load": -0.1}, {"role": "router"}):
        expect_value_error(lambda b=bad: m.nodes.CameraNode(valid_node(**b)), f"invalid {bad}")


@test("NODE-004", "set_online(False) is reflected in to_dict and update_state")
def _(m):
    n = m.nodes.CameraNode(valid_node())
    n.set_online(False)
    assert n.to_dict()["online"] is False
    assert n.update_state(load=0.4)["online"] is False


@test("NODE-005", "update_state rejects out-of-range buffer / load")
def _(m):
    n = m.nodes.CameraNode(valid_node())
    expect_value_error(lambda: n.update_state(buffer_available=120), "buffer 120")
    expect_value_error(lambda: n.update_state(load=1.5), "load 1.5")


# --------------------------------------------------------------------------
# links.py
# --------------------------------------------------------------------------
@test("LINK-001", "update_quality returns exactly source/destination/quality/active")
def _(m):
    l = m.links.Link(link_dict("CCTV1", "CCTV2"))
    out = l.update_quality(0.5)
    assert set(out) == {"source", "destination", "quality", "active"}, f"keys {sorted(out)}"
    assert out["source"] == "CCTV1" and out["destination"] == "CCTV2" and out["active"] is True
    approx(out["quality"], 0.5, "quality")
    approx(l.to_dict()["quality"], 0.5, "stored quality")


@test("LINK-002", "Invalid link fields rejected")
def _(m):
    cases = [{"quality": 1.5}, {"quality": -0.1}, {"bandwidth": 0}, {"latency": -1},
             {"packet_loss": 1.5}]
    for bad in cases:
        expect_value_error(lambda b=bad: m.links.Link(link_dict("CCTV1", "CCTV2", **b)), f"invalid {bad}")
    expect_value_error(lambda: m.links.Link(link_dict("CCTV1", "CCTV1")), "source == destination")


@test("LINK-003", "set_active(False) is reflected in to_dict and update_quality")
def _(m):
    l = m.links.Link(link_dict("CCTV1", "CCTV2"))
    l.set_active(False)
    assert l.to_dict()["active"] is False
    assert l.update_quality(0.6)["active"] is False


@test("LINK-004", "update_quality rejects values outside 0-1")
def _(m):
    l = m.links.Link(link_dict("CCTV1", "CCTV2"))
    expect_value_error(lambda: l.update_quality(1.2), "quality 1.2")
    expect_value_error(lambda: l.update_quality(-0.2), "quality -0.2")


# --------------------------------------------------------------------------
# simulation.py
# --------------------------------------------------------------------------
@test("SIM-001", "CCTV1, 10 packets, lossless: 10 sent, 10 delivered, 0 lost")
def _(m):
    out, _ = run(m, time_s=100, sources=["CCTV1"], max_packets=10)
    mt = out["metrics"]
    assert METRIC_KEYS <= set(mt), f"missing metrics {sorted(METRIC_KEYS - set(mt))}"
    assert (mt["packets_sent"], mt["packets_delivered"], mt["packets_lost"]) == (10, 10, 0), \
        f"sent/delivered/lost = {mt['packets_sent']}/{mt['packets_delivered']}/{mt['packets_lost']}"
    approx(mt["delivery_rate"], 1.0, "delivery_rate")
    approx(mt["average_hops"], 3.0, "average_hops")


@test("SIM-002", "FIX-FAIL: CCTV3 fails at 20 s -> reroute via CCTV2, packets continue")
def _(m):
    fail = [{"failure_type": "relay_node", "failed_element": "CCTV3", "failure_time": 20.0}]
    out, _ = run(m, time_s=40, sources=["CCTV1"], link=fix_fail_links(), failures=fail)
    changes = evs(out, "route_changed")
    first = [e for e in changes if e["old_route"] is None]
    assert first and first[0]["new_route"] == R(1, 3, "G", "S"), \
        f"initial route should be via CCTV3, got {first[0]['new_route'] if first else None}"
    sw = [e for e in changes if e["old_route"] == R(1, 3, "G", "S")]
    assert sw, "no route_changed from the CCTV3 route"
    sw = sw[0]
    assert sw["new_route"] == R(1, 2, "G", "S"), f"new route {sw['new_route']}"
    assert 20.0 <= sw["timestamp"] <= 22.5, f"route change at {sw['timestamp']} s"
    after = [e for e in evs(out, "packet_sent") if e["timestamp"] > sw["timestamp"]]
    assert after, "no packets sent after the reroute"
    assert all(e["next_node"] == "CCTV2" for e in after), "packets still sent toward CCTV3"
    late = [e for e in evs(out, "packet_received") if e["timestamp"] > 22.5]
    assert late, "no packets delivered after the failure"


@test("SIM-003", "Same config twice (seed 42) -> identical events and metrics")
def _(m):
    a, _ = run(m, time_s=20, loss=0.1, seed=42)
    b, _ = run(m, time_s=20, loss=0.1, seed=42)
    assert a["events"] == b["events"], "event logs differ between identical runs"
    assert a["metrics"] == b["metrics"], "metrics differ between identical runs"


@test("SIM-004", "Random loss is on and each seed is reproducible (seeds 42, 43)")
def _(m):
    results = {}
    for seed in (42, 43):
        a, _ = run(m, time_s=20, sources=["CCTV4"], link={"CCTV4-Gateway": {"packet_loss": 0.3}}, seed=seed)
        b, _ = run(m, time_s=20, sources=["CCTV4"], link={"CCTV4-Gateway": {"packet_loss": 0.3}}, seed=seed)
        assert a["events"] == b["events"], f"seed {seed} not reproducible"
        assert a["metrics"]["packets_lost"] > 0, f"seed {seed}: no losses despite 30% link loss"
        results[seed] = a["metrics"]["packets_lost"]


@test("SIM-005", "All six cameras deliver to the Server")
def _(m):
    out, _ = run(m, time_s=100, max_packets=5)
    mt = out["metrics"]
    assert mt["packets_sent"] == 30 and mt["packets_delivered"] == 30, \
        f"sent {mt['packets_sent']}, delivered {mt['packets_delivered']} (want 30/30)"
    got = {e["source"] for e in evs(out, "packet_received")}
    assert got == {f"CCTV{i}" for i in range(1, 7)}, f"sources delivering: {sorted(got)}"


@test("SIM-006", "sent = delivered + lost + buffered, and event counts match metrics")
def _(m):
    fail = [{"failure_type": "relay_node", "failed_element": "CCTV3", "failure_time": 10.0}]
    out, _ = run(m, time_s=30, sources=["CCTV1", "CCTV2"], link=fix_fail_links(),
                 loss=0.1, failures=fail)
    mt = out["metrics"]
    total = mt["packets_delivered"] + mt["packets_lost"] + mt.get("packets_buffered", 0)
    assert mt["packets_sent"] == total, \
        f"sent {mt['packets_sent']} != delivered+lost+buffered {total}"
    assert mt["packets_sent"] == len(evs(out, "packet_sent"))
    assert mt["packets_delivered"] == len(evs(out, "packet_received"))
    assert mt["packets_lost"] == len(evs(out, "packet_lost"))
    approx(mt["delivery_rate"], mt["packets_delivered"] / mt["packets_sent"], "delivery_rate", 1e-9)


@test("SIM-007", "Event timestamps never decrease")
def _(m):
    fail = [{"failure_type": "relay_node", "failed_element": "CCTV3", "failure_time": 10.0}]
    out, _ = run(m, time_s=30, link=fix_fail_links(), loss=0.05, failures=fail)
    ts = [e["timestamp"] for e in out["events"]]
    assert ts, "no events produced"
    for i in range(1, len(ts)):
        assert ts[i] >= ts[i - 1], f"event {i} at {ts[i]} comes after {ts[i - 1]}"


@test("SIM-008", "Delivery time = send time + sum of link latencies (15+25+5 ms)")
def _(m):
    link = {"CCTV1-CCTV2": {"latency": 15}, "CCTV2-Gateway": {"latency": 25},
            "Gateway-Server": {"latency": 5}}
    out, _ = run(m, time_s=5, sources=["CCTV1"], max_packets=3, link=link)
    sent = {e["packet_id"]: e for e in evs(out, "packet_sent")}
    recv = {e["packet_id"]: e for e in evs(out, "packet_received")}
    assert recv, "nothing delivered"
    for pid, r in recv.items():
        approx(r["timestamp"] - sent[pid]["timestamp"], 0.045, f"transit of {pid}")


@test("SIM-009", "Every packet follows a valid, loop-free route ending at Server")
def _(m):
    out, _ = run(m, time_s=10)
    declared = {frozenset(e) for e in EDGES}
    paths = packet_paths(out)
    delivered = {e["packet_id"] for e in evs(out, "packet_received")}
    assert delivered, "nothing delivered"
    for pid in delivered:
        p = paths[pid]
        assert len(p) == len(set(p)), f"{pid} revisits a node: {p}"
        assert p[-1] == "Server", f"{pid} path ends at {p[-1]}"
        for e in zip(p, p[1:]):
            assert frozenset(e) in declared, f"{pid} uses undeclared link {e}"


@test("SIM-010", "100 s at 10 packets/s -> 1000 packets from one source")
def _(m):
    out, _ = run(m, time_s=100, rate=10, sources=["CCTV1"])
    sent = evs(out, "packet_sent")
    assert len(sent) == 1000, f"generated {len(sent)}"
    assert all(e["timestamp"] < 100 for e in sent), "packet generated at/after simulation_time"


@test("SIM-011", "Offline CCTV3 sends, receives and forwards nothing")
def _(m):
    out, _ = run(m, time_s=10, node={"CCTV3": {"online": False}})
    for e in out["events"]:
        if not e["event_type"].startswith("packet_"):
            continue   # control events (route_changed etc.) may mention the node
        for k in ("source", "current_node", "next_node"):
            assert e.get(k) != "CCTV3", f"{e['event_type']} involves offline CCTV3 ({k})"
    assert out["metrics"]["packets_delivered"] > 0, "other cameras should still deliver"


@test("SIM-012", "Inactive link CCTV1-CCTV2 carries no packets")
def _(m):
    out, _ = run(m, time_s=10, sources=["CCTV1"], link={"CCTV1-CCTV2": {"active": False}})
    bad = frozenset(("CCTV1", "CCTV2"))
    for e in out["events"]:
        if e["event_type"] in ("packet_sent", "packet_forwarded") and e["next_node"]:
            assert frozenset((e["current_node"], e["next_node"])) != bad, "packet crossed inactive link"
    assert out["metrics"]["packets_delivered"] > 0, "CCTV1 should use CCTV3"


@test("SIM-013", "All events pass the schema check")
def _(m):
    fail = [{"failure_type": "link", "failed_element": "CCTV1-CCTV3", "failure_time": 5.0}]
    out, _ = run(m, time_s=10, sources=["CCTV1"], link=fix_fail_links(), loss=0.05, failures=fail)
    assert out["events"], "no events"
    for e in out["events"]:
        assert isinstance(e, dict) and "event_type" in e, f"bad event {e}"
        assert e["event_type"] in ALLOWED_EVENTS, f"unknown event_type {e['event_type']!r}"
        assert isinstance(e.get("timestamp"), (int, float)), f"{e['event_type']}: bad timestamp"
        missing = REQUIRED_KEYS.get(e["event_type"], set()) - set(e)
        assert not missing, f"{e['event_type']} missing {sorted(missing)}"


@test("SIM-014", "No failure -> route_changes metric is 0")
def _(m):
    out, _ = run(m, time_s=20)
    assert out["metrics"]["route_changes"] == 0, f"route_changes = {out['metrics']['route_changes']}"
    assert not evs(out, "failure_detected"), "failure_detected without any failure"
    assert all(e["old_route"] is None for e in evs(out, "route_changed")), \
        "route_changed with an old route although nothing failed"


@test("SIM-015", "100 s, all six sources (6000 packets) finishes under 30 s")
def _(m):
    t0 = time.perf_counter()
    out, _ = run(m, time_s=100, rate=10)
    dt = time.perf_counter() - t0
    assert out["metrics"]["packets_sent"] == 6000, f"sent {out['metrics']['packets_sent']}"
    assert dt < 30, f"took {dt:.1f} s (limit 30 s)"


@test("SIM-016", "30% loss on CCTV4-Gateway -> lost fraction within 0.25-0.35")
def _(m):
    out, _ = run(m, time_s=100, rate=10, sources=["CCTV4"],
                 link={"CCTV4-Gateway": {"packet_loss": 0.3}})
    mt = out["metrics"]
    frac = mt["packets_lost"] / mt["packets_sent"]
    assert 0.25 <= frac <= 0.35, f"lost fraction {frac:.3f}"


@test("SIM-017", "Router called per route event, not per packet")
def _(m):
    out, router = run(m, time_s=100, sources=["CCTV1"])
    assert out["metrics"]["packets_sent"] == 1000
    assert router.calls <= 2, f"router called {router.calls} times for 1000 packets"
    fail = [{"failure_type": "relay_node", "failed_element": "CCTV3", "failure_time": 20.0}]
    out, router = run(m, time_s=40, sources=["CCTV1"], link=fix_fail_links(), failures=fail)
    assert router.calls <= 3, f"router called {router.calls} times with one failure"


@test("SIM-018", "Invalid config values rejected")
def _(m):
    for bad in ({"simulation_time": 0}, {"simulation_time": -5}, {"packet_rate": 0}, {"packet_rate": -1}):
        cfg, _ = sim_config()
        cfg.update(bad)
        expect_value_error(lambda c=cfg: m.simulation.run_simulation(c), f"config {bad}")


@test("SIM-019", "Packet IDs are unique")
def _(m):
    out, _ = run(m, time_s=20)
    ids = [e["packet_id"] for e in evs(out, "packet_sent")]
    assert ids and len(ids) == len(set(ids)), "duplicate packet_id values"


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------
def load_modules(args):
    dirs = []
    for a in args:
        p = os.path.abspath(a)
        if os.path.isdir(p):
            d = p
        elif os.path.isfile(p):
            d = os.path.dirname(p)
        else:
            sys.exit(f"ERROR: not found: {a}")
        if d not in dirs:
            dirs.append(d)
    for d in reversed(dirs):
        sys.path.insert(0, d)
    mods = {}
    for name in ("topology", "nodes", "links", "simulation"):
        if not any(os.path.isfile(os.path.join(d, name + ".py")) for d in dirs):
            sys.exit(f"ERROR: {name}.py not found in {', '.join(dirs)}")
        mods[name] = importlib.import_module(name)
    return SimpleNamespace(**mods)


def main(argv):
    flags = [a for a in argv[1:] if a.startswith("-")]
    args = [a for a in argv[1:] if not a.startswith("-")]
    verbose = "-v" in flags
    ids = {a for a in args if a[:4] in ("TOP-", "NODE", "LINK", "SIM-")}
    paths = [a for a in args if a not in ids]
    if not paths:
        print(__doc__.split("API CONTRACT")[0])
        return 2

    print("=" * 70)
    print(" P3 SIMULATION ENGINEER - TEST SUITE")
    print(" topology.py | nodes.py | links.py | simulation.py")
    print("=" * 70)
    try:
        m = load_modules(paths)
    except SystemExit:
        raise
    except Exception:
        print("ERROR: could not import modules")
        traceback.print_exc()
        return 2

    tests = [t for t in TESTS if not ids or t[0] in ids]
    order = {"TOP": 0, "NODE": 1, "LINK": 2, "SIM": 3}
    tests.sort(key=lambda t: (order[t[0].split("-")[0]], t[0]))

    passed, failed, errors = 0, [], []
    for tid, title, fn in tests:
        try:
            fn(m)
            print(f"[PASS]  {tid:<9}{title}")
            passed += 1
        except AssertionError as e:
            print(f"[FAIL]  {tid:<9}{title}\n          -> {e}")
            failed.append(tid)
        except Exception as e:
            print(f"[ERROR] {tid:<9}{title}\n          -> {type(e).__name__}: {e}")
            if verbose:
                traceback.print_exc()
            errors.append(tid)

    total = len(tests)
    print("-" * 70)
    print(f" Total {total} | Passed {passed} | Failed {len(failed)} | Errors {len(errors)}")
    if failed:
        print(" Failed:", ", ".join(failed))
    if errors:
        print(" Errors:", ", ".join(errors))
    print(" RESULT:", "ALL PASS - P3 acceptance met" if passed == total else "NOT DONE")
    print("=" * 70)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
