#!/usr/bin/env python3
"""
P4 (Failure + Storage Engineer) test suite  --  Self-Healing CCTV Mesh Network
Covers: failure.py, recovery.py, storage.py

USAGE
    python testcase_failure.py src/                          # folder with the 3 files
    python testcase_failure.py failure.py recovery.py storage.py
    python testcase_failure.py src/ -v                       # full tracebacks
    python testcase_failure.py src/ FAIL-001 STO-005         # selected tests only

Needs only the Python standard library.

API CONTRACT THE TESTS EXPECT
  nodes = list of node dicts (handbook 3.2), links = list of link dicts (3.3).
  Functions mutate these lists' dicts in place (online / active / load).

  failure.py
    classify_load(load, config=None) -> "NORMAL" | "WARNING" | "CONGESTED"
        load < 0.60 NORMAL; 0.60 <= load < 0.80 WARNING; load >= 0.80 CONGESTED
    evaluate_link(link, config=None) -> bool
        sets link["active"]=False if packet_loss > 0.50 or quality < 0.30
        (never re-activates); returns link["active"]
    record_heartbeat(node, t)            # node["last_heartbeat"] = t
    check_heartbeats(nodes, now, config=None) -> [failure_detected events]
        online node with (now - last_heartbeat) > heartbeat_timeout_s (2.0)
        is set online=False and reported exactly once
    inject_failure(nodes, links, failure) -> failure_detected event
        failure = {"failure_type": relay_node|source_camera|link|congestion,
                   "failed_element": "CCTV3" or "CCTV1-CCTV3",
                   "failure_time": 20.0, "load": 0.85 (congestion only)}
        event keys: event_type, timestamp (= failure_time), failure_type,
                    failed_element; congestion adds "state".
        Raises ValueError for unknown failure_type / unknown element.
    restore_element(nodes, links, element, now) -> recovery_complete event
        node: online=True and last_heartbeat=now; link: active=True

  recovery.py
    RecoveryEngine(router, verify, config)
        router(routing_input)->route dict (selected_route; supports
              "excluded_routes"), verify(route)->bool (True = ACK in time)
        config: ack_timeout_s (1.0), control_message_delay_ms (10),
                max_allowed_hops (6)
        .route_changes  (count of successful route switches)
        .recover(failure, nodes, links, source, current_route, now=None,
                 packets_lost=0) -> dict:
            event_type: "recovery_complete" | "recovery_failed"
            timestamp, failure_type, failed_element, old_route, new_route,
            route_verified, recovery_time_ms, route_computation_time_ms,
            control_messages_sent, packets_lost,
            state_log   (NORMAL, DETECT, ANALYZE, FIND_ALT, SCORE, SWITCH,
                         VERIFY, NORMAL on success),
            fallback    (None, or "store_locally" when no verified route),
            events      (route_changed, route_verified, ...)
        recovery_time_ms = route computation + control-message delay
                           + ack_timeout for every failed verification
        If current_route is not affected by the failure -> no action, no
        router call.  If the SOURCE camera itself failed -> no router call,
        fallback "store_locally".

  storage.py
    StorageManager(capacity)             # packets per node buffer; ValueError if <= 0
        .store(node_id, packet, now) -> {"stored": bool, "event": None | packet_lost event}
              packet = {"packet_id", "source", "capture_time"}
              full buffer -> stored False, event reason "buffer_full"
        .count(node_id) -> int
        .buffer_available(node_id) -> float   # percent free, 0-100
        .drain(node_id, send_fn, now) -> buffer_drained event
              send_fn(packet)->bool; packets leave FIFO; stops at the first
              False (that packet stays at the head).
              event: event_type, timestamp, node_id, count, packet_ids
        .delivered / .lost (int attributes), .buffered() -> int (all nodes)

NOTES (P1 to confirm)
  * Handbook allowed event types need "recovery_failed" added.
  * FAIL-012 checks the failure event; the full handbook 3.5 field set is
    checked on the recovery result (REC-005).
  * Traffic returning to the better route after a node recovers (FAIL-014)
    is not tested here; only element restoration is.
"""

import importlib
import os
import re
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
ENGINE_CFG = {"ack_timeout_s": 1.0, "control_message_delay_ms": 10, "max_allowed_hops": 6}


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
             "reliability": 0.95, "load": 0.30, "last_heartbeat": 0.0,
             "role": "source" if nid.startswith("CCTV") else "relay"}
        n.update((node or {}).get(nid, {}))
        out.append(n)
    return out


def make_links(link=None):
    over = {frozenset(k.split("-")): v for k, v in (link or {}).items()}
    out = []
    for a, b in EDGES:
        l = {"source": a, "destination": b, "quality": 0.90, "bandwidth": 20,
             "latency": 10, "packet_loss": 0.02, "active": True}
        l.update(over.get(frozenset((a, b)), {}))
        out.append(l)
    return out


def fix_fail_links():
    return {"CCTV1-CCTV3": {"quality": 0.95}, "CCTV3-Gateway": {"quality": 0.95},
            "Gateway-Server": {"quality": 0.95}}


def get_node(nodes, nid):
    return next(n for n in nodes if n["id"] == nid)


def get_link(links, a, b):
    return next(l for l in links if frozenset((l["source"], l["destination"])) == frozenset((a, b)))


def fail(ftype, element, t=20.0, **kw):
    d = {"failure_type": ftype, "failed_element": element, "failure_time": t}
    d.update(kw)
    return d


class StubRouter:
    """Fewest hops, then highest summed link quality, then alphabetical.
    Honors excluded_routes. Counts calls."""

    def __init__(self):
        self.calls = 0

    def __call__(self, inp):
        self.calls += 1
        nodes = {n["id"]: n for n in inp["nodes"]}
        res = {"selected_route": None, "score": 0.0, "candidate_routes": [],
               "route_verified": False}
        src, dst = inp["source"], inp["destination"]
        excl = [tuple(p) for p in inp.get("excluded_routes", [])]
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
        found = [p for p in found if len(p) - 1 <= inp["max_allowed_hops"] and tuple(p) not in excl]
        if not found:
            res["reason"] = "no_route"
            return res
        best = min(found, key=lambda p: (len(p) - 1,
                   -sum(q[frozenset(e)] for e in zip(p, p[1:])), p))
        res["selected_route"] = best
        return res


def new_engine(m, verify=None):
    router = StubRouter()
    engine = m.recovery.RecoveryEngine(router=router, verify=verify or (lambda route: True),
                                       config=dict(ENGINE_CFG))
    return engine, router


def expect_value_error(fn, label):
    try:
        fn()
    except ValueError:
        return
    except Exception as e:
        raise AssertionError(f"{label}: expected ValueError, got {type(e).__name__}: {e}")
    raise AssertionError(f"{label}: no ValueError raised")


def approx(actual, expected, label, tol=0.001):
    assert actual is not None, f"{label}: got None, expected {expected}"
    assert abs(actual - expected) <= tol, f"{label}: expected {expected}, got {actual}"


def pkt(i, t=None):
    return {"packet_id": f"PKT{i:03d}", "source": "CCTV1", "capture_time": float(i if t is None else t)}


TESTS = []


def test(tid, title):
    def deco(fn):
        TESTS.append((tid, title, fn))
        return fn
    return deco


# --------------------------------------------------------------------------
# failure.py
# --------------------------------------------------------------------------
@test("FAIL-001", "relay_node CCTV3 fails: offline, route [1,3,G,S] -> [1,2,G,S]")
def _(m):
    nodes, links = make_nodes(), make_links(fix_fail_links())
    ev = m.failure.inject_failure(nodes, links, fail("relay_node", "CCTV3", 20.0))
    assert get_node(nodes, "CCTV3")["online"] is False, "CCTV3 still online"
    assert all(n["online"] for n in nodes if n["id"] != "CCTV3"), "other nodes were affected"
    assert ev["event_type"] == "failure_detected"
    assert ev["failure_type"] == "relay_node" and ev["failed_element"] == "CCTV3"
    approx(ev["timestamp"], 20.0, "timestamp")
    engine, _ = new_engine(m)
    res = engine.recover(ev, nodes, links, "CCTV1", R(1, 3, "G", "S"), now=20.0)
    assert res["old_route"] == R(1, 3, "G", "S"), f"old_route {res['old_route']}"
    assert res["new_route"] == R(1, 2, "G", "S"), f"new_route {res['new_route']}"


@test("FAIL-002", "source_camera CCTV1 fails: offline, nothing to reroute, store locally")
def _(m):
    nodes, links = make_nodes(), make_links(fix_fail_links())
    ev = m.failure.inject_failure(nodes, links, fail("source_camera", "CCTV1", 20.0))
    assert get_node(nodes, "CCTV1")["online"] is False
    assert ev["failure_type"] == "source_camera"
    engine, router = new_engine(m)
    res = engine.recover(ev, nodes, links, "CCTV1", R(1, 3, "G", "S"), now=20.0)
    assert res["fallback"] == "store_locally", f"fallback = {res['fallback']!r}"
    assert res["new_route"] is None, f"new_route {res['new_route']}"
    assert router.calls == 0, f"router called {router.calls}x for a source failure"
    assert engine.route_changes == 0


@test("FAIL-003", "link CCTV1-CCTV3 fails: active=False and no new route uses it")
def _(m):
    nodes, links = make_nodes(), make_links(fix_fail_links())
    ev = m.failure.inject_failure(nodes, links, fail("link", "CCTV1-CCTV3", 20.0))
    assert get_link(links, "CCTV1", "CCTV3")["active"] is False
    assert sum(1 for l in links if not l["active"]) == 1, "other links were affected"
    engine, _ = new_engine(m)
    res = engine.recover(ev, nodes, links, "CCTV1", R(1, 3, "G", "S"), now=20.0)
    assert res["new_route"] == R(1, 2, "G", "S"), f"new_route {res['new_route']}"
    bad = frozenset(("CCTV1", "CCTV3"))
    assert bad not in {frozenset(e) for e in zip(res["new_route"], res["new_route"][1:])}


@test("FAIL-004", "congestion (load 0.85): state CONGESTED, node stays online")
def _(m):
    nodes, links = make_nodes(), make_links()
    ev = m.failure.inject_failure(nodes, links, fail("congestion", "CCTV3", 5.0, load=0.85))
    n = get_node(nodes, "CCTV3")
    approx(n["load"], 0.85, "load")
    assert n["online"] is True, "congested node must stay online (penalized, not excluded)"
    assert ev["state"] == "CONGESTED", f"state {ev.get('state')!r}"
    assert ev["failure_type"] == "congestion"


@test("FAIL-005", "Load boundaries: <0.60 NORMAL, 0.60-<0.80 WARNING, >=0.80 CONGESTED")
def _(m):
    want = [(0.30, "NORMAL"), (0.59, "NORMAL"), (0.60, "WARNING"), (0.70, "WARNING"),
            (0.79, "WARNING"), (0.80, "CONGESTED"), (0.85, "CONGESTED"), (1.0, "CONGESTED")]
    for load, state in want:
        got = m.failure.classify_load(load)
        assert got == state, f"load {load}: expected {state}, got {got}"
    got = m.failure.classify_load(0.85, {"congestion_threshold": 0.90})
    assert got == "WARNING", f"custom threshold 0.90, load 0.85: got {got}"


def _link(**kw):
    l = {"source": "CCTV1", "destination": "CCTV2", "quality": 0.9, "bandwidth": 20,
         "latency": 10, "packet_loss": 0.02, "active": True}
    l.update(kw)
    return l


@test("FAIL-006", "Link packet_loss 0.51 -> inactive")
def _(m):
    l = _link(packet_loss=0.51)
    assert m.failure.evaluate_link(l) is False and l["active"] is False


@test("FAIL-007", "Link packet_loss 0.50 -> stays active (rule is > 0.50)")
def _(m):
    l = _link(packet_loss=0.50)
    assert m.failure.evaluate_link(l) is True and l["active"] is True


@test("FAIL-008", "Link quality 0.29 -> inactive")
def _(m):
    l = _link(quality=0.29)
    assert m.failure.evaluate_link(l) is False and l["active"] is False


@test("FAIL-009", "Link quality 0.30 -> stays active (rule is < 0.30)")
def _(m):
    l = _link(quality=0.30)
    assert m.failure.evaluate_link(l) is True and l["active"] is True


@test("FAIL-010", "Failure at 20.0 s, timeout 2.0 s: detected once, in (20.0, 22.5]")
def _(m):
    nodes = make_nodes()
    detections = []
    for i in range(1, 61):                      # t = 0.5 ... 30.0
        t = i * 0.5
        for n in nodes:
            if n["id"] == "CCTV3" and t >= 20.0:
                continue                         # CCTV3 stops sending heartbeats at 20.0
            m.failure.record_heartbeat(n, t)
        detections += m.failure.check_heartbeats(nodes, t)
    assert len(detections) == 1, f"expected 1 detection, got {len(detections)}: " \
                                 f"{[d['failed_element'] for d in detections]}"
    d = detections[0]
    assert d["failed_element"] == "CCTV3", f"detected {d['failed_element']}"
    assert d["event_type"] == "failure_detected"
    assert 20.0 < d["timestamp"] <= 22.5, f"detected at {d['timestamp']} s"
    assert get_node(nodes, "CCTV3")["online"] is False


@test("FAIL-011", "Heartbeat gap 1.0 s / exactly 2.0 s not a failure; 2.1 s is")
def _(m):
    for gap, expect_fail in ((1.0, False), (2.0, False), (2.1, True)):
        nodes = make_nodes({"CCTV3": {"last_heartbeat": 20.0 - gap}})
        evs = m.failure.check_heartbeats(nodes, 20.0)
        got = any(e["failed_element"] == "CCTV3" for e in evs)
        assert got == expect_fail, f"gap {gap}s: failure reported = {got}, expected {expect_fail}"
        assert get_node(nodes, "CCTV3")["online"] is (not expect_fail)


@test("FAIL-012", "failure_detected event has event_type, timestamp, failure_type, failed_element")
def _(m):
    nodes, links = make_nodes(), make_links()
    ev = m.failure.inject_failure(nodes, links, fail("relay_node", "CCTV3", 20.0))
    missing = {"event_type", "timestamp", "failure_type", "failed_element"} - set(ev)
    assert not missing, f"missing keys {sorted(missing)}"
    assert ev["failure_type"] in {"relay_node", "source_camera", "link", "congestion"}
    assert isinstance(ev["timestamp"], (int, float))


@test("FAIL-013", "Two failures (CCTV3 at 10 s, CCTV4 at 15 s) give two ordered events")
def _(m):
    nodes, links = make_nodes(), make_links()
    e1 = m.failure.inject_failure(nodes, links, fail("relay_node", "CCTV3", 10.0))
    e2 = m.failure.inject_failure(nodes, links, fail("relay_node", "CCTV4", 15.0))
    assert (e1["failed_element"], e2["failed_element"]) == ("CCTV3", "CCTV4")
    assert e1["timestamp"] < e2["timestamp"]
    assert not get_node(nodes, "CCTV3")["online"] and not get_node(nodes, "CCTV4")["online"]


@test("FAIL-014", "Recovery at 40 s: node back online (heartbeat reset), link active again")
def _(m):
    nodes, links = make_nodes(), make_links()
    m.failure.inject_failure(nodes, links, fail("relay_node", "CCTV3", 20.0))
    ev = m.failure.restore_element(nodes, links, "CCTV3", 40.0)
    n = get_node(nodes, "CCTV3")
    assert n["online"] is True, "node not restored"
    approx(n["last_heartbeat"], 40.0, "last_heartbeat")
    assert ev["event_type"] == "recovery_complete" and ev["failed_element"] == "CCTV3"
    approx(ev["timestamp"], 40.0, "timestamp")
    m.failure.inject_failure(nodes, links, fail("link", "CCTV1-CCTV3", 41.0))
    m.failure.restore_element(nodes, links, "CCTV1-CCTV3", 50.0)
    assert get_link(links, "CCTV1", "CCTV3")["active"] is True, "link not restored"


@test("FAIL-015", "Link failure with an alternative: rerouted, recovery_time_ms > 0")
def _(m):
    nodes, links = make_nodes(), make_links(fix_fail_links())
    ev = m.failure.inject_failure(nodes, links, fail("link", "CCTV3-Gateway", 12.0))
    engine, _ = new_engine(m)
    res = engine.recover(ev, nodes, links, "CCTV1", R(1, 3, "G", "S"), now=12.0)
    assert res["event_type"] == "recovery_complete", f"event_type {res['event_type']}"
    assert res["new_route"] == R(1, 2, "G", "S"), f"new_route {res['new_route']}"
    assert res["recovery_time_ms"] > 0, f"recovery_time_ms {res['recovery_time_ms']}"


@test("FAIL-016", "Unknown failure_type rejected")
def _(m):
    expect_value_error(lambda: m.failure.inject_failure(
        make_nodes(), make_links(), fail("meteor", "CCTV3")), "failure_type meteor")


@test("FAIL-017", "Unknown node or non-existent link rejected")
def _(m):
    expect_value_error(lambda: m.failure.inject_failure(
        make_nodes(), make_links(), fail("relay_node", "CCTV9")), "node CCTV9")
    expect_value_error(lambda: m.failure.inject_failure(
        make_nodes(), make_links(), fail("link", "CCTV1-Server")), "link CCTV1-Server")


# --------------------------------------------------------------------------
# recovery.py
# --------------------------------------------------------------------------
@test("REC-001", "ACK received: route verified, back to NORMAL, route_changed + route_verified")
def _(m):
    nodes, links = make_nodes(), make_links(fix_fail_links())
    ev = m.failure.inject_failure(nodes, links, fail("relay_node", "CCTV3", 20.0))
    engine, _ = new_engine(m, verify=lambda r: True)
    res = engine.recover(ev, nodes, links, "CCTV1", R(1, 3, "G", "S"), now=20.0)
    assert res["event_type"] == "recovery_complete"
    assert res["route_verified"] is True
    assert res["new_route"] == R(1, 2, "G", "S")
    assert res["state_log"][-1] == "NORMAL", f"final state {res['state_log'][-1]}"
    types = [e["event_type"] for e in res["events"]]
    assert "route_changed" in types and "route_verified" in types, f"events {types}"
    rc = next(e for e in res["events"] if e["event_type"] == "route_changed")
    assert rc["old_route"] == R(1, 3, "G", "S") and rc["new_route"] == R(1, 2, "G", "S")
    assert engine.route_changes == 1


def cctv2_down(m, verify):
    nodes, links = make_nodes(), make_links()
    ev = m.failure.inject_failure(nodes, links, fail("relay_node", "CCTV2", 20.0))
    engine, router = new_engine(m, verify=verify)
    res = engine.recover(ev, nodes, links, "CCTV1", R(1, 2, "G", "S"), now=20.0)
    return res, engine, router


@test("REC-002", "No ACK on best route: marked failed, next-best tried")
def _(m):
    tried = []

    def verify(route):
        tried.append(route)
        return route != R(1, 3, "G", "S")        # best candidate gets no ACK
    res, engine, router = cctv2_down(m, verify)
    assert tried and tried[0] == R(1, 3, "G", "S"), f"first route tried: {tried[:1]}"
    assert res["new_route"] == R(1, 3, 4, "G", "S"), f"new_route {res['new_route']}"
    assert res["route_verified"] is True and res["event_type"] == "recovery_complete"
    assert res["recovery_time_ms"] >= 1000, f"recovery_time_ms {res['recovery_time_ms']} (one ACK timeout = 1000)"
    assert router.calls >= 2, f"router called {router.calls}x"


@test("REC-003", "Every candidate fails verification: fallback store_locally, no crash")
def _(m):
    res, engine, _ = cctv2_down(m, lambda route: False)
    assert res["event_type"] == "recovery_failed", f"event_type {res['event_type']}"
    assert res["route_verified"] is False
    assert res["new_route"] is None
    assert res["fallback"] == "store_locally", f"fallback {res['fallback']!r}"
    assert res["recovery_time_ms"] >= 2000, f"recovery_time_ms {res['recovery_time_ms']} (two timeouts)"
    assert engine.route_changes == 0


@test("REC-004", "CCTV2 and CCTV3 down: CCTV1/5/6 isolated, CCTV4 still reaches Server")
def _(m):
    nodes, links = make_nodes(), make_links()
    ev2 = m.failure.inject_failure(nodes, links, fail("relay_node", "CCTV2", 10.0))
    ev = m.failure.inject_failure(nodes, links, fail("relay_node", "CCTV3", 10.0))
    engine, _ = new_engine(m)
    # each source is handled with the failure that broke its current route
    for src, cur, f in (("CCTV1", R(1, 3, "G", "S"), ev), ("CCTV5", R(5, 2, "G", "S"), ev2),
                        ("CCTV6", R(6, 3, "G", "S"), ev)):
        res = engine.recover(f, nodes, links, src, cur, now=10.0)
        assert res["new_route"] is None, f"{src} should be isolated, got {res['new_route']}"
        assert res["fallback"] == "store_locally", f"{src} fallback {res['fallback']!r}"
    res = engine.recover(ev, nodes, links, "CCTV4", R(4, 3, "G", "S"), now=10.0)
    assert res["new_route"] == R(4, "G", "S"), f"CCTV4 new_route {res['new_route']}"
    assert res["route_verified"] is True


@test("REC-005", "Result carries every handbook 3.5 field, none null")
def _(m):
    nodes, links = make_nodes(), make_links(fix_fail_links())
    ev = m.failure.inject_failure(nodes, links, fail("relay_node", "CCTV3", 20.0))
    engine, _ = new_engine(m)
    res = engine.recover(ev, nodes, links, "CCTV1", R(1, 3, "G", "S"), now=20.0, packets_lost=7)
    keys = ["event_type", "timestamp", "failure_type", "failed_element", "old_route", "new_route",
            "recovery_time_ms", "packets_lost", "route_computation_time_ms", "control_messages_sent"]
    missing = [k for k in keys if k not in res]
    assert not missing, f"missing {missing}"
    nulls = [k for k in keys if res[k] is None]
    assert not nulls, f"null fields {nulls}"
    assert res["packets_lost"] == 7, f"packets_lost {res['packets_lost']}"
    assert res["recovery_time_ms"] > 0
    assert res["route_computation_time_ms"] >= 0
    assert isinstance(res["control_messages_sent"], int) and res["control_messages_sent"] >= 1


@test("REC-006", "No alternative path: route unchanged, buffering starts, no exception")
def _(m):
    nodes, links = make_nodes(), make_links()
    m.failure.inject_failure(nodes, links, fail("relay_node", "CCTV2", 10.0))
    ev = m.failure.inject_failure(nodes, links, fail("relay_node", "CCTV3", 10.0))
    engine, _ = new_engine(m)
    cur = R(1, 3, "G", "S")
    res = engine.recover(ev, nodes, links, "CCTV1", cur, now=10.0)
    assert res["fallback"] == "store_locally"
    assert res["new_route"] is None and res["old_route"] == cur
    assert all(e["event_type"] != "route_changed" for e in res["events"]), "route_changed without a new route"
    assert engine.route_changes == 0


@test("REC-007", "State machine order on success")
def _(m):
    nodes, links = make_nodes(), make_links(fix_fail_links())
    ev = m.failure.inject_failure(nodes, links, fail("relay_node", "CCTV3", 20.0))
    engine, _ = new_engine(m)
    res = engine.recover(ev, nodes, links, "CCTV1", R(1, 3, "G", "S"), now=20.0)
    want = ["NORMAL", "DETECT", "ANALYZE", "FIND_ALT", "SCORE", "SWITCH", "VERIFY", "NORMAL"]
    assert res["state_log"] == want, f"state_log {res['state_log']}"


@test("REC-008", "Same failure handled twice: one route change, no extra routing")
def _(m):
    nodes, links = make_nodes(), make_links(fix_fail_links())
    ev = m.failure.inject_failure(nodes, links, fail("relay_node", "CCTV3", 20.0))
    engine, router = new_engine(m)
    first = engine.recover(ev, nodes, links, "CCTV1", R(1, 3, "G", "S"), now=20.0)
    calls = router.calls
    second = engine.recover(ev, nodes, links, "CCTV1", first["new_route"], now=20.5)
    assert engine.route_changes == 1, f"route_changes {engine.route_changes}"
    assert second["old_route"] == second["new_route"] == first["new_route"]
    assert all(e["event_type"] != "route_changed" for e in second["events"])
    assert router.calls == calls, "router called again for an unaffected route"


# --------------------------------------------------------------------------
# storage.py
# --------------------------------------------------------------------------
def fill(sm, node, n, start=1):
    for i in range(start, start + n):
        sm.store(node, pkt(i), now=float(i))


@test("STO-001", "5 packets buffered at failed CCTV1: kept locally (count 5, 75% free)")
def _(m):
    sm = m.storage.StorageManager(20)
    for i in range(1, 6):
        r = sm.store("CCTV1", pkt(i), now=float(i))
        assert r["stored"] is True and r["event"] is None, f"store {i}: {r}"
    assert sm.count("CCTV1") == 5, f"count {sm.count('CCTV1')}"
    approx(sm.buffer_available("CCTV1"), 75.0, "buffer_available")


@test("STO-002", "Recovery: 5 buffered packets drained and delivered")
def _(m):
    sm = m.storage.StorageManager(20)
    fill(sm, "CCTV1", 5)
    ev = sm.drain("CCTV1", lambda p: True, now=30.0)
    assert ev["event_type"] == "buffer_drained", f"event_type {ev['event_type']}"
    assert ev["count"] == 5 and ev["node_id"] == "CCTV1", f"event {ev}"
    approx(ev["timestamp"], 30.0, "timestamp")
    assert sm.count("CCTV1") == 0 and sm.delivered == 5
    approx(sm.buffer_available("CCTV1"), 100.0, "buffer_available")


@test("STO-003", "Drain order is first in, first out (capture-time order)")
def _(m):
    sm = m.storage.StorageManager(20)
    fill(sm, "CCTV1", 5)
    seen = []
    ev = sm.drain("CCTV1", lambda p: seen.append(p["packet_id"]) or True, now=30.0)
    want = [f"PKT{i:03d}" for i in range(1, 6)]
    assert seen == want, f"send order {seen}"
    assert ev["packet_ids"] == want, f"event packet_ids {ev['packet_ids']}"


@test("STO-004", "Gateway-Server down: data held; delivered after reconnect")
def _(m):
    sm = m.storage.StorageManager(20)
    fill(sm, "CCTV1", 5)
    net = {"up": False}
    ev = sm.drain("CCTV1", lambda p: net["up"], now=10.0)
    assert ev["count"] == 0 and sm.count("CCTV1") == 5 and sm.delivered == 0, \
        f"while down: count {ev['count']}, buffered {sm.count('CCTV1')}, delivered {sm.delivered}"
    net["up"] = True
    ev = sm.drain("CCTV1", lambda p: net["up"], now=20.0)
    assert ev["count"] == 5 and sm.count("CCTV1") == 0 and sm.delivered == 5


@test("STO-005", "Buffer full: extra packets lost with reason buffer_full")
def _(m):
    sm = m.storage.StorageManager(20)
    lost = []
    for i in range(1, 26):
        r = sm.store("CCTV1", pkt(i), now=float(i))
        if not r["stored"]:
            lost.append(r["event"])
    assert sm.count("CCTV1") == 20 and sm.lost == 5, f"count {sm.count('CCTV1')}, lost {sm.lost}"
    approx(sm.buffer_available("CCTV1"), 0.0, "buffer_available")
    assert len(lost) == 5
    for e in lost:
        assert e["event_type"] == "packet_lost" and e["reason"] == "buffer_full", f"event {e}"
        assert {"packet_id", "source", "current_node", "timestamp"} <= set(e), f"keys {sorted(e)}"


@test("STO-006", "buffer_available falls on store, rises on drain, stays within 0-100")
def _(m):
    sm = m.storage.StorageManager(10)
    last = sm.buffer_available("CCTV1")
    approx(last, 100.0, "empty buffer")
    for i in range(1, 15):                       # overfill on purpose
        sm.store("CCTV1", pkt(i), now=float(i))
        cur = sm.buffer_available("CCTV1")
        assert 0.0 <= cur <= 100.0, f"out of range: {cur}"
        assert cur <= last, f"buffer_available rose on store ({last} -> {cur})"
        last = cur
    approx(last, 0.0, "full buffer")
    sm.drain("CCTV1", lambda p: True, now=20.0)
    approx(sm.buffer_available("CCTV1"), 100.0, "after drain")


@test("STO-007", "Invariant: stored+lost accounting (sent = delivered + lost + buffered)")
def _(m):
    sm = m.storage.StorageManager(10)
    sent = 15
    for i in range(1, sent + 1):
        sm.store("CCTV1", pkt(i), now=float(i))
    n = {"i": 0}

    def send(p):
        n["i"] += 1
        return n["i"] <= 4                       # network drops after 4 packets
    sm.drain("CCTV1", send, now=30.0)
    assert (sm.delivered, sm.lost, sm.buffered()) == (4, 5, 6), \
        f"delivered/lost/buffered = {sm.delivered}/{sm.lost}/{sm.buffered()} (want 4/5/6)"
    assert sent == sm.delivered + sm.lost + sm.buffered()


@test("STO-008", "Server unavailable 30 s at 10 pkt/s (300 packets): nothing dropped")
def _(m):
    sm = m.storage.StorageManager(500)
    for i in range(300):
        sm.store("CCTV1", pkt(i, t=i / 10), now=i / 10)
    assert sm.lost == 0, f"lost {sm.lost}"
    assert sm.count("CCTV1") == 300
    approx(sm.buffer_available("CCTV1"), 40.0, "buffer_available")
    ev = sm.drain("CCTV1", lambda p: True, now=31.0)
    assert ev["count"] == 300 and sm.delivered == 300 and sm.lost == 0


@test("STO-009", "No duplicate delivery after repeated drains")
def _(m):
    sm = m.storage.StorageManager(20)
    fill(sm, "CCTV1", 5)
    got = []
    sm.drain("CCTV1", lambda p: got.append(p["packet_id"]) or True, now=10.0)
    ev2 = sm.drain("CCTV1", lambda p: got.append(p["packet_id"]) or True, now=11.0)
    assert ev2["count"] == 0, f"second drain delivered {ev2['count']}"
    assert len(got) == 5 and len(set(got)) == 5, f"deliveries {got}"
    assert sm.delivered == 5


@test("STO-010", "Drain stops at first failure; unsent packets keep their order")
def _(m):
    sm = m.storage.StorageManager(20)
    fill(sm, "CCTV1", 5)
    n = {"i": 0}

    def send(p):
        n["i"] += 1
        return n["i"] <= 2
    ev = sm.drain("CCTV1", send, now=10.0)
    assert ev["count"] == 2 and sm.count("CCTV1") == 3, f"count {ev['count']}, left {sm.count('CCTV1')}"
    seen = []
    sm.drain("CCTV1", lambda p: seen.append(p["packet_id"]) or True, now=11.0)
    assert seen == ["PKT003", "PKT004", "PKT005"], f"remaining order {seen}"


@test("STO-011", "Buffers are independent per node")
def _(m):
    sm = m.storage.StorageManager(20)
    fill(sm, "CCTV1", 5)
    fill(sm, "CCTV2", 2, start=10)
    assert sm.count("CCTV1") == 5 and sm.count("CCTV2") == 2
    approx(sm.buffer_available("CCTV2"), 90.0, "CCTV2 buffer_available")
    sm.drain("CCTV1", lambda p: True, now=20.0)
    assert sm.count("CCTV2") == 2, "draining CCTV1 changed CCTV2"
    assert sm.buffered() == 2


@test("STO-012", "Capacity <= 0 rejected")
def _(m):
    for bad in (0, -5):
        expect_value_error(lambda b=bad: m.storage.StorageManager(b), f"capacity {bad}")


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
    for name in ("failure", "recovery", "storage"):
        if not any(os.path.isfile(os.path.join(d, name + ".py")) for d in dirs):
            sys.exit(f"ERROR: {name}.py not found in {', '.join(dirs)}")
        mods[name] = importlib.import_module(name)
    return SimpleNamespace(**mods)


def main(argv):
    flags = [a for a in argv[1:] if a.startswith("-")]
    args = [a for a in argv[1:] if not a.startswith("-")]
    verbose = "-v" in flags
    idpat = re.compile(r"^(FAIL|REC|STO)-\d+$")
    ids = {a for a in args if idpat.match(a)}
    paths = [a for a in args if a not in ids]
    if not paths:
        print(__doc__.split("API CONTRACT")[0])
        return 2

    print("=" * 70)
    print(" P4 FAILURE + STORAGE ENGINEER - TEST SUITE")
    print(" failure.py | recovery.py | storage.py")
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
    order = {"FAIL": 0, "REC": 1, "STO": 2}
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
    print(" RESULT:", "ALL PASS - P4 acceptance met" if passed == total else "NOT DONE")
    print("=" * 70)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
