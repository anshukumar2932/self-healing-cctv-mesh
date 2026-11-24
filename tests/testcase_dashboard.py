#!/usr/bin/env python3
"""
P6 (Dashboard Engineer) test suite  --  Self-Healing CCTV Mesh Network
Covers: ui/dashboard.py  (state, event handling, PNG rendering)   DASH-xxx
        ui/api.py        (Flask JSON API on top of the state)      API-xxx

USAGE
    python testcase_dashboard.py ui/                 # folder with dashboard.py + api.py
    python testcase_dashboard.py ui/ -v              # full tracebacks
    python testcase_dashboard.py ui/ DASH-001 API-003    # selected tests only
    python testcase_dashboard.py ui/ API             # all API tests
    python testcase_dashboard.py ui/ DASH            # all DASH tests

Needs: pip install matplotlib flask
       (matplotlib: DASH-018, API-017.  flask: all API tests.)
       The API tests use Flask's built-in test client - no server needs to be
       running, no ports are opened.

WHY THIS DESIGN: the dashboard must be built and tested against mock JSON
events before the simulator exists. All logic therefore lives in a
DashboardState object that only consumes standard events; drawing is a thin
layer on top (render_png). Tests check the state, not pixels.

API CONTRACT THE TESTS EXPECT (dashboard.py)
  DashboardState(nodes, links)          # node / link dicts, handbook 3.2 / 3.3
    .apply_event(event)                 # never raises, whatever it is given
    .apply_events(list_of_events)
    .topology()      -> {"nodes": [ids], "edges": [(a, b), ...]}
    .node_states()   -> {node_id: "ONLINE" | "FAILED" | "RECOVERING"}
    .link_states()   -> [{"source", "destination", "state": "ACTIVE"|"INACTIVE"}]
    .current_route(source)       -> path list or None
    .current_route_info(source)  -> {"path": [...], "verified": bool} or None
    .packet_paths()  -> {packet_id: [node, ...]}
    .packet_status(packet_id) -> "in_flight" | "delivered" | "lost"
    .failure_panel() -> dict or None (latest failure, merged with its recovery):
          failed_element, failure_type, timestamp, old_route, new_route,
          recovery_time_ms, packets_lost, route_computation_time_ms,
          control_messages_sent, fallback      (missing values are None)
    .summary()  -> {active_nodes, total_nodes, active_nodes_text ("5/6"),
                    failed_nodes [ids], active_links, failures,
                    successful_recoveries, average_recovery_ms}
    .metrics()  -> {packets_sent, packets_delivered, packets_lost,
                    delivery_rate (0-1), delivery_rate_pct, average_hops,
                    route_changes, network_load, route_computation_time_ms,
                    control_messages}      (zeros, never None, when empty)
    .ignored_events (list)   .warnings (list of dicts with a "type" key)
    .reset()         -> back to the initial state (all online, no events,
                        no warnings, no ignored events)
    .node_info(id)   -> {"id", "role", "state", "load", "battery",
                         "buffer_available", "reliability"} or None
  load_events(path) -> list of event dicts (JSON file); ValueError if bad JSON
  render_png(state, path)   -> writes a PNG image of the current state

  Event rules the dashboard must follow
    failure_detected (relay_node / source_camera): node -> FAILED, its links
        -> INACTIVE.  failure_type "link": that link INACTIVE.
    Any event with node_id + node_status ("online"|"offline"|"recovering")
        sets that node to ONLINE / FAILED / RECOVERING.
    recovery_complete with route_verified True counts as a successful recovery.
        recovery_complete without new_route = element restored -> ONLINE/ACTIVE.
    route_changed sets the source's route (verified False);
    route_verified {source, route} sets verified True if route matches.
    "Active nodes" counts only CCTV1-CCTV6 (cameras).
    Out-of-order timestamps: event is still applied, and an
    {"type": "out_of_order", ...} entry is added to .warnings.
    Unknown event types, non-dict input, and events naming unknown nodes are
    ignored (added to .ignored_events), never raised.
    Metrics come from the latest metric_snapshot if one was received;
    otherwise they are derived from packet / route / recovery events.
    route_computation_time_ms = mean over events carrying it;
    control_messages = sum of control_messages_sent over events carrying it.

API ROUTES  (api.py:  create_app(state) -> flask.Flask;  all JSON unless noted)
  Errors are always JSON: {"error": "<message>"}  with 400 / 404 / 405.

  Method Route                     Returns (200)
  ------ ------------------------- -----------------------------------------
  GET    /                         HTML page (text/html)
  GET    /api/health               {"status": "ok", "events_applied": n}
  GET    /api/topology             {"nodes": [{id, role, state, ...}],
                                    "edges": [{source, destination, state}]}
  GET    /api/nodes                {"nodes": [{id, state, ...}]}
  GET    /api/nodes/<id>           node_info + "state"            404 unknown
  GET    /api/links                {"links": [{source, destination, state}]}
  GET    /api/routes               {"routes": {source: {path, verified}}}
  GET    /api/routes/<source>      {"path": [...], "verified": bool}  404 none
  GET    /api/packets?limit=N      {"packets": [{packet_id, path, status}]}
                                    last N, oldest first; default 50;
                                    N must be an integer >= 1 else 400
  GET    /api/packets/<id>         {"packet_id", "path", "status"}  404 unknown
  GET    /api/failure              {"failure": failure_panel dict or null}
  GET    /api/summary              exactly state.summary()
  GET    /api/metrics              exactly state.metrics()
  GET    /api/events?since=S&limit=N   {"events": [{"seq", "event"}],
                                    "last_seq": n}  applied events only,
                                    seq starts at 1; since default 0
  GET    /api/warnings             {"warnings": [...], "ignored": n}
  GET    /api/render.png           image/png of the current state
  POST   /api/events               body: one event object or a list of them
                                    -> {"applied": a, "ignored": i}
                                    400 if body is not valid JSON, or is
                                    neither an object nor a list
  POST   /api/reset                {"status": "reset"}; state, event log and
                                    seq counter start over
  GET routes never change state.  Wrong method -> 405.  Unknown path -> 404.

NOTES
    Handbook DASH-018 (screenshot legible on a projector) stays a manual
    check by P1; here it is an automated "renders and changes" check.
"""

import ast
import importlib.util
import json
import os
import re
import sys
import tempfile
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
METRIC_KEYS = {"packets_sent", "packets_delivered", "packets_lost", "delivery_rate",
               "delivery_rate_pct", "average_hops", "route_changes", "network_load",
               "route_computation_time_ms", "control_messages"}


# --------------------------------------------------------------------------
# Fixtures and event builders
# --------------------------------------------------------------------------
def R(*ids):
    m = {"G": "Gateway", "S": "Server"}
    return [m.get(str(i), "CCTV" + str(i)) for i in ids]


def make_nodes():
    return [{"id": n, "online": True, "battery": 100, "buffer_available": 100,
             "reliability": 0.95, "load": 0.30,
             "role": "source" if n.startswith("CCTV") else "relay"} for n in NODE_IDS]


def make_links():
    return [{"source": a, "destination": b, "quality": 0.9, "bandwidth": 20,
             "latency": 10, "packet_loss": 0.0, "active": True} for a, b in EDGES]


def new_state(m):
    return m.dashboard.DashboardState(make_nodes(), make_links())


def mock_failure():
    """The handbook's mock failure_detected event."""
    return {"event_type": "failure_detected", "timestamp": 20.0, "failed_element": "CCTV3",
            "failure_type": "relay_node", "old_route": R(1, 3, "G", "S"),
            "new_route": R(1, 2, "G", "S"), "packets_lost": 4, "recovery_time_ms": 3500,
            "route_computation_time_ms": 8, "control_messages_sent": 6}


def failure_ev(el, t=20.0, ftype="relay_node"):
    return {"event_type": "failure_detected", "timestamp": t, "failure_type": ftype,
            "failed_element": el}


def recovery_ok(el, t, ms, source="CCTV1", old=None, new=None):
    return {"event_type": "recovery_complete", "timestamp": t, "failure_type": "relay_node",
            "failed_element": el, "old_route": old or R(1, 3, "G", "S"),
            "new_route": new or R(1, 2, "G", "S"), "route_verified": True,
            "recovery_time_ms": ms, "packets_lost": 0, "route_computation_time_ms": 5,
            "control_messages_sent": 2}


def route_changed(src, old, new, t):
    return {"event_type": "route_changed", "timestamp": t, "source": src,
            "old_route": old, "new_route": new}


def packet_events(pid, route, t0, src="CCTV1", lost_at=None):
    """packet_sent + packet_forwarded... + packet_received (or packet_lost)."""
    evs = [{"event_type": "packet_sent", "timestamp": t0, "packet_id": pid, "source": src,
            "current_node": route[0], "next_node": route[1]}]
    for i in range(1, len(route) - 1):
        evs.append({"event_type": "packet_forwarded", "timestamp": t0 + 0.01 * i,
                    "packet_id": pid, "source": src, "current_node": route[i],
                    "next_node": route[i + 1]})
    if lost_at:
        evs.append({"event_type": "packet_lost", "timestamp": t0 + 0.02, "packet_id": pid,
                    "source": src, "current_node": lost_at, "reason": "link_loss"})
    else:
        evs.append({"event_type": "packet_received", "timestamp": t0 + 0.03,
                    "packet_id": pid, "source": src, "destination": "Server"})
    return evs


def approx(actual, expected, label, tol=0.001):
    assert actual is not None, f"{label}: got None, expected {expected}"
    assert abs(actual - expected) <= tol, f"{label}: expected {expected}, got {actual}"


TESTS = []


def test(tid, title):
    def deco(fn):
        TESTS.append((tid, title, fn))
        return fn
    return deco


# --------------------------------------------------------------------------
# Failure information
# --------------------------------------------------------------------------
@test("DASH-001", "Mock failure_detected (CCTV3): failure panel shows every field")
def _(m):
    s = new_state(m)
    s.apply_event(mock_failure())
    p = s.failure_panel()
    assert p is not None, "failure_panel() is None after a failure event"
    assert p["failed_element"] == "CCTV3" and p["failure_type"] == "relay_node"
    approx(p["timestamp"], 20.0, "timestamp")
    assert p["old_route"] == R(1, 3, "G", "S"), f"old_route {p['old_route']}"
    assert p["new_route"] == R(1, 2, "G", "S"), f"new_route {p['new_route']}"
    approx(p["recovery_time_ms"], 3500, "recovery_time_ms")
    assert p["packets_lost"] == 4
    approx(p["route_computation_time_ms"], 8, "route_computation_time_ms")
    assert p["control_messages_sent"] == 6
    assert s.node_states()["CCTV3"] == "FAILED"


@test("DASH-010", "Failure event with only the 4 base fields: no crash, missing values None")
def _(m):
    s = new_state(m)
    s.apply_event(failure_ev("CCTV3"))
    p = s.failure_panel()
    assert p["failed_element"] == "CCTV3"
    for k in ("old_route", "new_route", "recovery_time_ms", "packets_lost",
              "route_computation_time_ms", "control_messages_sent", "fallback"):
        assert p.get(k) is None, f"{k} should be None, got {p.get(k)!r}"
    assert s.node_states()["CCTV3"] == "FAILED"


@test("DASH-022", "recovery_failed merges into the panel (fallback store_locally)")
def _(m):
    s = new_state(m)
    s.apply_event(failure_ev("CCTV2", 10.0))
    s.apply_event({"event_type": "recovery_failed", "timestamp": 12.0, "failure_type": "relay_node",
                   "failed_element": "CCTV2", "old_route": R(1, 2, "G", "S"), "new_route": None,
                   "route_verified": False, "recovery_time_ms": 2000, "fallback": "store_locally",
                   "packets_lost": 3})
    p = s.failure_panel()
    assert p["failed_element"] == "CCTV2"
    assert p["fallback"] == "store_locally", f"fallback {p['fallback']!r}"
    assert p["new_route"] is None
    approx(p["recovery_time_ms"], 2000, "recovery_time_ms")
    assert s.summary()["successful_recoveries"] == 0, "recovery_failed counted as success"


@test("DASH-023", "recovery_complete merges into the failure panel")
def _(m):
    s = new_state(m)
    s.apply_event(failure_ev("CCTV3", 20.0))
    s.apply_event(recovery_ok("CCTV3", 20.1, 3500))
    p = s.failure_panel()
    assert p["new_route"] == R(1, 2, "G", "S") and p["old_route"] == R(1, 3, "G", "S")
    approx(p["recovery_time_ms"], 3500, "recovery_time_ms")
    approx(p["timestamp"], 20.0, "failure timestamp kept")


# --------------------------------------------------------------------------
# Topology and states
# --------------------------------------------------------------------------
@test("DASH-003", "Topology: 8 nodes, 11 edges, nothing undeclared")
def _(m):
    t = new_state(m).topology()
    assert sorted(t["nodes"]) == sorted(NODE_IDS), f"nodes {t['nodes']}"
    got = {frozenset(e) for e in t["edges"]}
    want = {frozenset(e) for e in EDGES}
    assert len(t["edges"]) == 11 and got == want, f"missing {want - got}, unexpected {got - want}"


@test("DASH-004", "Node states ONLINE / FAILED / RECOVERING via node_status")
def _(m):
    s = new_state(m)
    assert set(s.node_states().values()) == {"ONLINE"}, "all nodes should start ONLINE"
    s.apply_event({"event_type": "failure_detected", "timestamp": 5.0, "failure_type": "relay_node",
                   "failed_element": "CCTV3", "node_id": "CCTV3", "node_status": "offline"})
    assert s.node_states()["CCTV3"] == "FAILED"
    s.apply_event({"event_type": "recovery_complete", "timestamp": 30.0, "failed_element": "CCTV3",
                   "node_id": "CCTV3", "node_status": "recovering"})
    assert s.node_states()["CCTV3"] == "RECOVERING"
    s.apply_event({"event_type": "recovery_complete", "timestamp": 31.0, "failed_element": "CCTV3",
                   "node_id": "CCTV3", "node_status": "online"})
    assert s.node_states()["CCTV3"] == "ONLINE"
    assert len(set(s.node_states().values())) == 1, "other nodes changed state"


@test("DASH-005", "CCTV3 fails: node FAILED and its 5 links INACTIVE, others ACTIVE")
def _(m):
    s = new_state(m)
    s.apply_event(failure_ev("CCTV3"))
    assert s.node_states()["CCTV3"] == "FAILED"
    links = s.link_states()
    assert len(links) == 11, f"{len(links)} links listed"
    for l in links:
        touches = "CCTV3" in (l["source"], l["destination"])
        want = "INACTIVE" if touches else "ACTIVE"
        assert l["state"] == want, f"{l['source']}-{l['destination']}: {l['state']} (want {want})"
    assert s.summary()["active_links"] == 6


@test("DASH-014", "Active nodes counter counts cameras only: 5/6, 6/6, relay failure ignored")
def _(m):
    s = new_state(m)
    assert s.summary()["active_nodes_text"] == "6/6"
    s.apply_event(failure_ev("CCTV3"))
    sm = s.summary()
    assert sm["active_nodes_text"] == "5/6", f"got {sm['active_nodes_text']}"
    assert sm["active_nodes"] == 5 and sm["total_nodes"] == 6
    assert sm["failed_nodes"] == ["CCTV3"], f"failed_nodes {sm['failed_nodes']}"
    s.apply_event({"event_type": "recovery_complete", "timestamp": 40.0, "failed_element": "CCTV3"})
    assert s.summary()["active_nodes_text"] == "6/6", "restore did not bring the node back"
    assert s.node_states()["CCTV3"] == "ONLINE"
    s.apply_event(failure_ev("Gateway", 50.0))
    sm = s.summary()
    assert sm["active_nodes_text"] == "6/6", "Gateway is not a camera"
    assert "Gateway" in sm["failed_nodes"]


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------
@test("DASH-006", "route_changed updates the current route")
def _(m):
    s = new_state(m)
    s.apply_event(route_changed("CCTV1", None, R(1, 3, "G", "S"), 0.0))
    assert s.current_route("CCTV1") == R(1, 3, "G", "S")
    s.apply_event(route_changed("CCTV1", R(1, 3, "G", "S"), R(1, 2, "G", "S"), 20.0))
    assert s.current_route("CCTV1") == R(1, 2, "G", "S"), f"got {s.current_route('CCTV1')}"
    assert s.current_route("CCTV4") is None, "route shown for a source with no route"


@test("DASH-021", "New route is unverified until route_verified arrives")
def _(m):
    s = new_state(m)
    s.apply_event(route_changed("CCTV1", None, R(1, 2, "G", "S"), 1.0))
    info = s.current_route_info("CCTV1")
    assert info["path"] == R(1, 2, "G", "S") and info["verified"] is False
    s.apply_event({"event_type": "route_verified", "timestamp": 1.1, "source": "CCTV1",
                   "route": R(1, 3, "G", "S")})
    assert s.current_route_info("CCTV1")["verified"] is False, "verified a route that is not current"
    s.apply_event({"event_type": "route_verified", "timestamp": 1.2, "source": "CCTV1",
                   "route": R(1, 2, "G", "S")})
    assert s.current_route_info("CCTV1")["verified"] is True
    s.apply_event(route_changed("CCTV1", R(1, 2, "G", "S"), R(1, 3, "G", "S"), 5.0))
    assert s.current_route_info("CCTV1")["verified"] is False, "new route should reset verified"


# --------------------------------------------------------------------------
# Packets and metrics
# --------------------------------------------------------------------------
@test("DASH-009", "packet_sent -> forwarded -> received follows the event route")
def _(m):
    s = new_state(m)
    s.apply_events(packet_events("PKT001", R(1, 2, "G", "S"), 1.0))
    assert s.packet_paths()["PKT001"] == R(1, 2, "G", "S"), f"path {s.packet_paths().get('PKT001')}"
    assert s.packet_status("PKT001") == "delivered"
    s.apply_events(packet_events("PKT002", R(1, 3, "G", "S"), 2.0, lost_at="CCTV3"))
    assert s.packet_status("PKT002") == "lost"
    s.apply_event({"event_type": "packet_sent", "timestamp": 3.0, "packet_id": "PKT003",
                   "source": "CCTV1", "current_node": "CCTV1", "next_node": "CCTV2"})
    assert s.packet_status("PKT003") == "in_flight"


@test("DASH-002", "metric_snapshot 1000 sent / 960 delivered / 40 lost -> 96%")
def _(m):
    s = new_state(m)
    s.apply_event({"event_type": "metric_snapshot", "timestamp": 100.0, "packets_sent": 1000,
                   "packets_delivered": 960, "packets_lost": 40, "average_hops": 3.2,
                   "route_changes": 7, "network_load": 0.4, "route_computation_time_ms": 8,
                   "control_messages": 120})
    mt = s.metrics()
    approx(mt["delivery_rate_pct"], 96.0, "delivery_rate_pct")
    approx(mt["delivery_rate"], 0.96, "delivery_rate")
    assert (mt["packets_sent"], mt["packets_delivered"], mt["packets_lost"]) == (1000, 960, 40)
    approx(mt["average_hops"], 3.2, "average_hops")
    assert mt["route_changes"] == 7 and mt["control_messages"] == 120


@test("DASH-007", "5 failures, 4 successful recoveries -> counters 5 and 4")
def _(m):
    s = new_state(m)
    for i, el in enumerate(["CCTV3", "CCTV4", "CCTV5", "CCTV6", "CCTV2"]):
        s.apply_event(failure_ev(el, 10.0 + i))
    for i in range(4):
        s.apply_event(recovery_ok("CCTV3", 20.0 + i, 300))
    s.apply_event({"event_type": "recovery_failed", "timestamp": 30.0, "failed_element": "CCTV2",
                   "failure_type": "relay_node", "new_route": None, "route_verified": False,
                   "recovery_time_ms": 2000, "fallback": "store_locally"})
    s.apply_event({"event_type": "recovery_complete", "timestamp": 40.0, "failed_element": "CCTV4"})
    sm = s.summary()
    assert sm["failures"] == 5, f"failures {sm['failures']}"
    assert sm["successful_recoveries"] == 4, \
        f"successful_recoveries {sm['successful_recoveries']} (restores and failures must not count)"


@test("DASH-008", "Recovery times 300, 350, 400 ms -> average 350 ms (failed recovery excluded)")
def _(m):
    s = new_state(m)
    for i, ms in enumerate((300, 350, 400)):
        s.apply_event(recovery_ok("CCTV3", 20.0 + i, ms))
    s.apply_event({"event_type": "recovery_failed", "timestamp": 30.0, "failed_element": "CCTV2",
                   "failure_type": "relay_node", "recovery_time_ms": 2000, "fallback": "store_locally"})
    approx(s.summary()["average_recovery_ms"], 350.0, "average_recovery_ms")


@test("DASH-017", "Empty dashboard: all ten metric keys present, zeros, no division errors")
def _(m):
    mt = new_state(m).metrics()
    missing = METRIC_KEYS - set(mt)
    assert not missing, f"missing metrics {sorted(missing)}"
    for k in METRIC_KEYS:
        assert mt[k] is not None, f"{k} is None"
        assert mt[k] == 0, f"{k} should be 0 on an empty dashboard, got {mt[k]}"
    sm = new_state(m).summary()
    assert sm["average_recovery_ms"] == 0 and sm["failures"] == 0


# --------------------------------------------------------------------------
# Robustness
# --------------------------------------------------------------------------
@test("DASH-011", "Unknown event type ignored and logged, state unchanged")
def _(m):
    s = new_state(m)
    before = s.node_states()
    s.apply_event({"event_type": "banana", "timestamp": 1.0})
    assert len(s.ignored_events) == 1, f"ignored_events {len(s.ignored_events)}"
    assert s.node_states() == before


@test("DASH-012", "Out-of-order timestamp: applied and flagged in warnings")
def _(m):
    s = new_state(m)
    s.apply_event(failure_ev("CCTV4", 10.0))
    s.apply_event(failure_ev("CCTV3", 5.0))
    assert s.node_states()["CCTV3"] == "FAILED", "late event was dropped instead of applied"
    assert any(w.get("type") == "out_of_order" for w in s.warnings), f"warnings {s.warnings}"


@test("DASH-020", "Junk input (string, empty dict, None) ignored without raising")
def _(m):
    s = new_state(m)
    for junk in ("hello", {}, None, 42, {"timestamp": 1.0}):
        s.apply_event(junk)
    assert len(s.ignored_events) == 5, f"ignored {len(s.ignored_events)} of 5 junk inputs"
    assert set(s.node_states().values()) == {"ONLINE"}


@test("DASH-024", "Event naming an unknown node: ignored, no crash, no phantom node")
def _(m):
    s = new_state(m)
    s.apply_event(failure_ev("CCTV9"))
    assert "CCTV9" not in s.node_states(), "unknown node added to the display"
    assert len(s.ignored_events) == 1
    assert s.summary()["failures"] == 0, "unknown-node failure counted"


# --------------------------------------------------------------------------
# Whole-system behaviour
# --------------------------------------------------------------------------
def demo_events():
    ev = [route_changed("CCTV1", None, R(1, 3, "G", "S"), 0.0)]
    for i in range(5):
        ev += packet_events(f"PKT{i:03d}", R(1, 3, "G", "S"), 1.0 + i)
    ev.append(failure_ev("CCTV3", 20.0))
    ev.append(route_changed("CCTV1", R(1, 3, "G", "S"), R(1, 2, "G", "S"), 20.0))
    ev.append({"event_type": "route_verified", "timestamp": 20.05, "source": "CCTV1",
               "route": R(1, 2, "G", "S")})
    ev.append(recovery_ok("CCTV3", 20.1, 3500))
    for i in range(5, 10):
        ev += packet_events(f"PKT{i:03d}", R(1, 2, "G", "S"), 21.0 + i)
    return ev


@test("DASH-013", "Replay of the full demo scenario ends in the right state")
def _(m):
    s = new_state(m)
    for e in demo_events():
        s.apply_event(e)
    assert s.node_states()["CCTV3"] == "FAILED"
    info = s.current_route_info("CCTV1")
    assert info["path"] == R(1, 2, "G", "S") and info["verified"] is True, f"route info {info}"
    p = s.failure_panel()
    assert p["failed_element"] == "CCTV3"
    approx(p["recovery_time_ms"], 3500, "recovery_time_ms")
    mt = s.metrics()
    assert (mt["packets_sent"], mt["packets_delivered"], mt["packets_lost"]) == (10, 10, 0), \
        f"derived packet counts {mt['packets_sent']}/{mt['packets_delivered']}/{mt['packets_lost']}"
    approx(mt["average_hops"], 3.0, "average_hops")
    assert mt["route_changes"] == 1, f"route_changes {mt['route_changes']}"
    sm = s.summary()
    assert sm["failures"] == 1 and sm["successful_recoveries"] == 1
    assert not s.ignored_events, f"events ignored: {s.ignored_events[:2]}"
    assert not s.warnings, f"unexpected warnings: {s.warnings[:2]}"


@test("DASH-016", "5000+ events processed in under 2 s")
def _(m):
    s = new_state(m)
    events = []
    for i in range(1250):
        events += packet_events(f"P{i}", R(1, 2, "G", "S"), i * 0.01)
    assert len(events) >= 5000, f"only {len(events)} events built"
    t0 = time.perf_counter()
    s.apply_events(events)
    dt = time.perf_counter() - t0
    assert dt < 2.0, f"took {dt:.2f} s for {len(events)} events"
    assert s.metrics()["packets_delivered"] == 1250


@test("DASH-015", "Dashboard imports no simulator internals (reads events only)")
def _(m):
    forbidden = {"simulation", "routing", "failure", "recovery", "storage"}
    path = m.dashboard.__file__
    tree = ast.parse(open(path, encoding="utf-8").read())
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            bad += [a.name for a in node.names if a.name.split(".")[0] in forbidden]
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module.split(".")[0] in forbidden:
                bad.append(node.module)
    assert not bad, f"forbidden imports: {sorted(set(bad))}"


@test("DASH-019", "Mock JSON file loads and replays; bad JSON raises ValueError")
def _(m):
    with tempfile.TemporaryDirectory() as d:
        good = os.path.join(d, "mock.json")
        json.dump(demo_events(), open(good, "w"))
        events = m.dashboard.load_events(good)
        assert isinstance(events, list) and len(events) == len(demo_events()), \
            f"loaded {len(events)} events"
        s = new_state(m)
        s.apply_events(events)
        assert s.node_states()["CCTV3"] == "FAILED"
        bad = os.path.join(d, "bad.json")
        open(bad, "w").write("{not json")
        try:
            m.dashboard.load_events(bad)
        except ValueError:
            return
        except Exception as e:
            raise AssertionError(f"bad JSON: expected ValueError, got {type(e).__name__}: {e}")
        raise AssertionError("bad JSON: no ValueError raised")


@test("DASH-018", "render_png writes a real image, and a failure changes the picture")
def _(m):
    try:
        import matplotlib  # noqa: F401
    except ImportError:
        raise RuntimeError("matplotlib not installed (pip install matplotlib)")
    with tempfile.TemporaryDirectory() as d:
        s = new_state(m)
        s.apply_event(route_changed("CCTV1", None, R(1, 3, "G", "S"), 0.0))
        a = os.path.join(d, "healthy.png")
        m.dashboard.render_png(s, a)
        s.apply_events([failure_ev("CCTV3", 20.0),
                        route_changed("CCTV1", R(1, 3, "G", "S"), R(1, 2, "G", "S"), 20.0)])
        b = os.path.join(d, "failed.png")
        m.dashboard.render_png(s, b)
        da, db = open(a, "rb").read(), open(b, "rb").read()
        sig = b"\x89PNG\r\n\x1a\n"
        assert da[:8] == sig and db[:8] == sig, "output is not a PNG"
        assert len(da) > 1000 and len(db) > 1000, f"images too small ({len(da)}, {len(db)} bytes)"
        assert da != db, "healthy and failed states render identically"


# --------------------------------------------------------------------------
# State additions used by the API
# --------------------------------------------------------------------------
@test("DASH-025", "reset() returns the dashboard to its initial state")
def _(m):
    s = new_state(m)
    s.apply_events(demo_events())
    s.apply_event({"event_type": "banana"})
    s.apply_event(failure_ev("CCTV4", 1.0))          # out of order -> warning
    s.reset()
    assert set(s.node_states().values()) == {"ONLINE"}, "nodes not reset"
    assert s.failure_panel() is None and s.current_route("CCTV1") is None
    assert s.summary()["failures"] == 0 and s.summary()["active_nodes_text"] == "6/6"
    assert s.packet_paths() == {}, "packets not cleared"
    assert not s.ignored_events and not s.warnings, "ignored/warnings not cleared"
    assert all(v == 0 for v in s.metrics().values()), "metrics not reset"
    assert all(l["state"] == "ACTIVE" for l in s.link_states()), "links not reset"


@test("DASH-026", "node_info returns details; unknown node gives None")
def _(m):
    s = new_state(m)
    info = s.node_info("CCTV3")
    need = {"id", "role", "state", "load", "battery", "buffer_available", "reliability"}
    assert info is not None and need <= set(info), f"info {info}"
    assert info["id"] == "CCTV3" and info["state"] == "ONLINE" and info["role"] == "source"
    approx(info["load"], 0.30, "load")
    s.apply_event(failure_ev("CCTV3"))
    assert s.node_info("CCTV3")["state"] == "FAILED"
    assert s.node_info("CCTV9") is None


# --------------------------------------------------------------------------
# API routes (api.py)
# --------------------------------------------------------------------------
def api_client(m):
    if getattr(m, "api", None) is None:
        why = getattr(m, "api_error", None) or "ui/api.py not found next to dashboard.py"
        raise AssertionError(f"cannot load api.py: {why}")
    if not hasattr(m.api, "create_app"):
        raise AssertionError("api.py has no create_app(state)")
    state = new_state(m)
    app = m.api.create_app(state)
    app.config["TESTING"] = True
    return app.test_client(), state


def check_json(r, what):
    ct = r.content_type or ""
    assert "application/json" in ct, f"{what}: content-type {ct!r}, expected application/json"
    body = r.get_json(silent=True)
    assert body is not None, f"{what}: body is not valid JSON"
    return body


def jget(c, path, status=200):
    r = c.get(path)
    assert r.status_code == status, f"GET {path}: HTTP {r.status_code} (want {status}) {r.get_data(as_text=True)[:80]}"
    return check_json(r, f"GET {path}")


def jpost(c, path, obj, status=200):
    r = c.post(path, data=json.dumps(obj), content_type="application/json")
    assert r.status_code == status, f"POST {path}: HTTP {r.status_code} (want {status}) {r.get_data(as_text=True)[:80]}"
    return check_json(r, f"POST {path}")


def expect_error(r, status, what):
    assert r.status_code == status, f"{what}: HTTP {r.status_code} (want {status})"
    body = check_json(r, what)
    assert isinstance(body.get("error"), str) and body["error"], f"{what}: no 'error' message in {body}"


@test("API-001", "GET /api/health -> status ok, events_applied 0")
def _(m):
    c, _ = api_client(m)
    d = jget(c, "/api/health")
    assert d.get("status") == "ok", f"body {d}"
    assert d.get("events_applied") == 0, f"events_applied {d.get('events_applied')}"


@test("API-002", "GET /api/topology -> 8 nodes, 11 edges, all ONLINE / ACTIVE")
def _(m):
    c, _ = api_client(m)
    d = jget(c, "/api/topology")
    assert len(d["nodes"]) == 8 and len(d["edges"]) == 11, f"{len(d['nodes'])} nodes / {len(d['edges'])} edges"
    assert {n["id"] for n in d["nodes"]} == set(NODE_IDS)
    for n in d["nodes"]:
        assert {"id", "role", "state"} <= set(n), f"node keys {sorted(n)}"
        assert n["state"] == "ONLINE"
    assert {frozenset((e["source"], e["destination"])) for e in d["edges"]} == {frozenset(e) for e in EDGES}
    assert all(e["state"] == "ACTIVE" for e in d["edges"])


@test("API-003", "POST a failure, then topology / node routes show it")
def _(m):
    c, _ = api_client(m)
    r = jpost(c, "/api/events", failure_ev("CCTV3"))
    assert r == {"applied": 1, "ignored": 0}, f"response {r}"
    n = jget(c, "/api/nodes/CCTV3")
    assert n["state"] == "FAILED", f"CCTV3 state {n['state']}"
    d = jget(c, "/api/topology")
    assert next(x for x in d["nodes"] if x["id"] == "CCTV3")["state"] == "FAILED"
    down = [e for e in d["edges"] if e["state"] == "INACTIVE"]
    assert len(down) == 5, f"{len(down)} inactive edges (want 5)"
    assert all("CCTV3" in (e["source"], e["destination"]) for e in down)


@test("API-004", "POST a mixed batch: counts applied vs ignored")
def _(m):
    c, _ = api_client(m)
    batch = [failure_ev("CCTV3"), route_changed("CCTV1", None, R(1, 2, "G", "S"), 1.0),
             {"event_type": "banana", "timestamp": 2.0}, "junk",
             {"event_type": "packet_sent", "timestamp": 3.0, "packet_id": "P0", "source": "CCTV1",
              "current_node": "CCTV1", "next_node": "CCTV2"}]
    r = jpost(c, "/api/events", batch)
    assert r == {"applied": 3, "ignored": 2}, f"response {r} (want applied 3, ignored 2)"


@test("API-005", "POST bad bodies -> 400 JSON error; empty list is fine")
def _(m):
    c, _ = api_client(m)
    expect_error(c.post("/api/events", data="{not json", content_type="application/json"), 400, "invalid JSON")
    expect_error(c.post("/api/events", data='"hello"', content_type="application/json"), 400, "JSON string")
    expect_error(c.post("/api/events", data="42", content_type="application/json"), 400, "JSON number")
    expect_error(c.post("/api/events", data="", content_type="application/json"), 400, "empty body")
    assert jpost(c, "/api/events", []) == {"applied": 0, "ignored": 0}
    assert jget(c, "/api/health")["events_applied"] == 0, "bad requests must not change state"


@test("API-006", "GET /api/nodes and /api/nodes/<id>; unknown node is 404")
def _(m):
    c, _ = api_client(m)
    d = jget(c, "/api/nodes")
    assert len(d["nodes"]) == 8 and all({"id", "state"} <= set(n) for n in d["nodes"])
    one = jget(c, "/api/nodes/CCTV1")
    assert one["id"] == "CCTV1" and one["state"] == "ONLINE" and one["role"] == "source"
    expect_error(c.get("/api/nodes/CCTV9"), 404, "GET /api/nodes/CCTV9")


@test("API-007", "GET /api/links lists 11 links with state")
def _(m):
    c, _ = api_client(m)
    d = jget(c, "/api/links")
    assert len(d["links"]) == 11
    assert all({"source", "destination", "state"} <= set(l) for l in d["links"])
    jpost(c, "/api/events", failure_ev("CCTV1-CCTV3", ftype="link"))
    d = jget(c, "/api/links")
    down = [l for l in d["links"] if l["state"] == "INACTIVE"]
    assert len(down) == 1 and {down[0]["source"], down[0]["destination"]} == {"CCTV1", "CCTV3"}


@test("API-008", "Routes: empty, after route_changed, per-source lookup, 404")
def _(m):
    c, _ = api_client(m)
    assert jget(c, "/api/routes") == {"routes": {}}, "routes should start empty"
    jpost(c, "/api/events", route_changed("CCTV1", None, R(1, 2, "G", "S"), 1.0))
    d = jget(c, "/api/routes")
    assert d["routes"]["CCTV1"] == {"path": R(1, 2, "G", "S"), "verified": False}, f"routes {d}"
    one = jget(c, "/api/routes/CCTV1")
    assert one["path"] == R(1, 2, "G", "S") and one["verified"] is False
    jpost(c, "/api/events", {"event_type": "route_verified", "timestamp": 1.1, "source": "CCTV1",
                             "route": R(1, 2, "G", "S")})
    assert jget(c, "/api/routes/CCTV1")["verified"] is True
    expect_error(c.get("/api/routes/CCTV4"), 404, "GET /api/routes/CCTV4")


def post_packets(c, n=5):
    for i in range(n):
        jpost(c, "/api/events", packet_events(f"P{i}", R(1, 2, "G", "S"), float(i)))


@test("API-009", "Packets: limit, order, single lookup, bad limit, 404")
def _(m):
    c, _ = api_client(m)
    post_packets(c, 5)
    d = jget(c, "/api/packets")
    assert [p["packet_id"] for p in d["packets"]] == ["P0", "P1", "P2", "P3", "P4"], \
        f"default order {[p['packet_id'] for p in d['packets']]}"
    d = jget(c, "/api/packets?limit=2")
    assert [p["packet_id"] for p in d["packets"]] == ["P3", "P4"], "limit=2 should give the last two, oldest first"
    p = d["packets"][0]
    assert p["path"] == R(1, 2, "G", "S") and p["status"] == "delivered", f"packet {p}"
    one = jget(c, "/api/packets/P0")
    assert one["packet_id"] == "P0" and one["status"] == "delivered"
    expect_error(c.get("/api/packets?limit=abc"), 400, "limit=abc")
    expect_error(c.get("/api/packets?limit=0"), 400, "limit=0")
    expect_error(c.get("/api/packets/ZZZ"), 404, "unknown packet")


@test("API-010", "GET /api/failure: null at first, then the failure panel")
def _(m):
    c, _ = api_client(m)
    assert jget(c, "/api/failure") == {"failure": None}
    jpost(c, "/api/events", mock_failure())
    f = jget(c, "/api/failure")["failure"]
    assert f["failed_element"] == "CCTV3" and f["failure_type"] == "relay_node"
    assert f["old_route"] == R(1, 3, "G", "S") and f["new_route"] == R(1, 2, "G", "S")
    approx(f["recovery_time_ms"], 3500, "recovery_time_ms")
    assert f["packets_lost"] == 4


@test("API-011", "GET /api/summary equals the state summary (5/6 after a failure)")
def _(m):
    c, s = api_client(m)
    jpost(c, "/api/events", [failure_ev("CCTV3"), recovery_ok("CCTV3", 20.1, 350)])
    d = jget(c, "/api/summary")
    assert d == s.summary(), f"API {d} != state {s.summary()}"
    assert d["active_nodes_text"] == "5/6" and d["failures"] == 1 and d["successful_recoveries"] == 1


@test("API-012", "GET /api/metrics: zeros when empty, 96% after a snapshot")
def _(m):
    c, s = api_client(m)
    d = jget(c, "/api/metrics")
    assert METRIC_KEYS <= set(d) and all(d[k] == 0 for k in METRIC_KEYS), f"empty metrics {d}"
    jpost(c, "/api/events", {"event_type": "metric_snapshot", "timestamp": 9.0, "packets_sent": 1000,
                             "packets_delivered": 960, "packets_lost": 40, "average_hops": 3.2,
                             "route_changes": 7, "network_load": 0.4, "route_computation_time_ms": 8,
                             "control_messages": 120})
    d = jget(c, "/api/metrics")
    approx(d["delivery_rate_pct"], 96.0, "delivery_rate_pct")
    assert d == s.metrics(), "API metrics differ from state metrics"


@test("API-013", "Event log: seq numbers, since, limit, ignored events not logged")
def _(m):
    c, _ = api_client(m)
    assert jget(c, "/api/events") == {"events": [], "last_seq": 0}
    jpost(c, "/api/events", [failure_ev("CCTV3", 1.0), {"event_type": "banana"},
                             route_changed("CCTV1", None, R(1, 2, "G", "S"), 2.0)])
    jpost(c, "/api/events", failure_ev("CCTV4", 3.0))
    d = jget(c, "/api/events")
    assert [e["seq"] for e in d["events"]] == [1, 2, 3], f"seqs {[e['seq'] for e in d['events']]}"
    assert d["last_seq"] == 3
    assert d["events"][0]["event"]["failed_element"] == "CCTV3"
    d = jget(c, "/api/events?since=1")
    assert [e["seq"] for e in d["events"]] == [2, 3] and d["last_seq"] == 3
    assert jget(c, "/api/events?since=3") == {"events": [], "last_seq": 3}
    d = jget(c, "/api/events?since=0&limit=1")
    assert [e["seq"] for e in d["events"]] == [1], "limit should keep the first events after 'since'"
    expect_error(c.get("/api/events?since=x"), 400, "since=x")


@test("API-014", "GET /api/warnings: ignored count and out-of-order warning")
def _(m):
    c, _ = api_client(m)
    assert jget(c, "/api/warnings") == {"warnings": [], "ignored": 0}
    jpost(c, "/api/events", [{"event_type": "banana"}, failure_ev("CCTV4", 10.0), failure_ev("CCTV3", 5.0)])
    d = jget(c, "/api/warnings")
    assert d["ignored"] == 1, f"ignored {d['ignored']}"
    assert any(w.get("type") == "out_of_order" for w in d["warnings"]), f"warnings {d['warnings']}"


@test("API-015", "POST /api/reset clears state, log and seq counter")
def _(m):
    c, _ = api_client(m)
    jpost(c, "/api/events", demo_events())
    assert jget(c, "/api/health")["events_applied"] > 0
    assert jpost(c, "/api/reset", {}) == {"status": "reset"}
    assert jget(c, "/api/health")["events_applied"] == 0
    assert jget(c, "/api/events") == {"events": [], "last_seq": 0}
    assert jget(c, "/api/failure") == {"failure": None}
    assert all(n["state"] == "ONLINE" for n in jget(c, "/api/nodes")["nodes"])
    jpost(c, "/api/events", failure_ev("CCTV3"))
    assert jget(c, "/api/events")["last_seq"] == 1, "seq should restart at 1 after reset"


@test("API-016", "GET / serves an HTML page")
def _(m):
    c, _ = api_client(m)
    r = c.get("/")
    assert r.status_code == 200, f"HTTP {r.status_code}"
    assert "text/html" in (r.content_type or ""), f"content-type {r.content_type}"
    assert "<html" in r.get_data(as_text=True).lower(), "no <html> tag in the page"


@test("API-017", "GET /api/render.png returns a PNG that changes after a failure")
def _(m):
    try:
        import matplotlib  # noqa: F401
    except ImportError:
        raise RuntimeError("matplotlib not installed (pip install matplotlib)")
    c, _ = api_client(m)
    r1 = c.get("/api/render.png")
    assert r1.status_code == 200 and "image/png" in (r1.content_type or ""), \
        f"HTTP {r1.status_code}, content-type {r1.content_type}"
    a = r1.get_data()
    jpost(c, "/api/events", failure_ev("CCTV3"))
    b = c.get("/api/render.png").get_data()
    sig = b"\x89PNG\r\n\x1a\n"
    assert a[:8] == sig and b[:8] == sig, "not a PNG"
    assert len(a) > 1000 and a != b, "image too small or unchanged after a failure"


@test("API-018", "Unknown path -> 404 JSON, wrong method -> 405 JSON")
def _(m):
    c, _ = api_client(m)
    expect_error(c.get("/api/nope"), 404, "GET /api/nope")
    expect_error(c.post("/api/topology", data="{}", content_type="application/json"), 405, "POST /api/topology")
    expect_error(c.get("/api/reset"), 405, "GET /api/reset")
    expect_error(c.delete("/api/events"), 405, "DELETE /api/events")


@test("API-019", "Every JSON GET route returns application/json")
def _(m):
    c, _ = api_client(m)
    post_packets(c, 1)
    for path in ("/api/health", "/api/topology", "/api/nodes", "/api/nodes/CCTV1", "/api/links",
                 "/api/routes", "/api/failure", "/api/summary", "/api/metrics", "/api/events",
                 "/api/warnings", "/api/packets", "/api/packets/P0"):
        jget(c, path)


@test("API-020", "GET routes never change state")
def _(m):
    c, _ = api_client(m)
    jpost(c, "/api/events", [failure_ev("CCTV3", 1.0), route_changed("CCTV1", None, R(1, 2, "G", "S"), 2.0)])
    before = (jget(c, "/api/events"), jget(c, "/api/summary"), jget(c, "/api/health"))
    for _ in range(3):
        for path in ("/api/topology", "/api/nodes", "/api/routes", "/api/failure", "/api/metrics",
                     "/api/warnings", "/api/packets", "/api/events", "/"):
            c.get(path)
    after = (jget(c, "/api/events"), jget(c, "/api/summary"), jget(c, "/api/health"))
    assert before == after, "a GET request changed the dashboard state or the event log"


@test("API-021", "Full demo replayed through POST /api/events ends in the right state")
def _(m):
    c, _ = api_client(m)
    ev = demo_events()
    r = jpost(c, "/api/events", ev)
    assert r == {"applied": len(ev), "ignored": 0}, f"response {r}"
    assert jget(c, "/api/nodes/CCTV3")["state"] == "FAILED"
    route = jget(c, "/api/routes/CCTV1")
    assert route["path"] == R(1, 2, "G", "S") and route["verified"] is True
    assert jget(c, "/api/failure")["failure"]["failed_element"] == "CCTV3"
    mt = jget(c, "/api/metrics")
    assert (mt["packets_sent"], mt["packets_delivered"], mt["packets_lost"]) == (10, 10, 0)
    sm = jget(c, "/api/summary")
    assert sm["failures"] == 1 and sm["successful_recoveries"] == 1 and sm["active_nodes_text"] == "5/6"
    assert jget(c, "/api/health")["events_applied"] == len(ev)


@test("API-022", "One POST of 5000+ events handled in under 3 s")
def _(m):
    c, _ = api_client(m)
    events = []
    for i in range(1250):
        events += packet_events(f"P{i}", R(1, 2, "G", "S"), i * 0.01)
    t0 = time.perf_counter()
    r = jpost(c, "/api/events", events)
    dt = time.perf_counter() - t0
    assert r["applied"] == len(events) and r["ignored"] == 0, f"response {r}"
    assert dt < 3.0, f"took {dt:.2f} s for {len(events)} events"
    assert jget(c, "/api/metrics")["packets_delivered"] == 1250


@test("API-023", "api.py imports no simulator internals")
def _(m):
    if getattr(m, "api", None) is None:
        raise AssertionError("cannot load api.py: " + (getattr(m, "api_error", None) or "not found"))
    forbidden = {"simulation", "routing", "failure", "recovery", "storage"}
    tree = ast.parse(open(m.api.__file__, encoding="utf-8").read())
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            bad += [a.name for a in node.names if a.name.split(".")[0] in forbidden]
        elif isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] in forbidden:
            bad.append(node.module)
    assert not bad, f"forbidden imports: {sorted(set(bad))}"


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------
def load_dashboard(path):
    p = os.path.abspath(path)
    if os.path.isdir(p):
        for cand in (os.path.join(p, "dashboard.py"), os.path.join(p, "ui", "dashboard.py")):
            if os.path.isfile(cand):
                p = cand
                break
        else:
            sys.exit(f"ERROR: dashboard.py not found in {path}")
    if not os.path.isfile(p):
        sys.exit(f"ERROR: not found: {path}")
    d = os.path.dirname(p)
    sys.path.insert(0, d)
    spec = importlib.util.spec_from_file_location("dashboard", p)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["dashboard"] = mod
    spec.loader.exec_module(mod)
    api, api_error = None, None
    api_path = os.path.join(d, "api.py")
    if not os.path.isfile(api_path):
        api_error = f"{api_path} not found"
    else:
        try:
            aspec = importlib.util.spec_from_file_location("api", api_path)
            api = importlib.util.module_from_spec(aspec)
            sys.modules["api"] = api
            aspec.loader.exec_module(api)
        except Exception as e:           # e.g. Flask not installed
            api, api_error = None, f"{type(e).__name__}: {e}"
    return SimpleNamespace(dashboard=mod, api=api, api_error=api_error)


def main(argv):
    flags = [a for a in argv[1:] if a.startswith("-")]
    args = [a for a in argv[1:] if not a.startswith("-")]
    verbose = "-v" in flags
    ids = {a for a in args if re.match(r"^(DASH|API)(-\d+)?$", a)}
    paths = [a for a in args if a not in ids]
    if not paths:
        print(__doc__.split("API CONTRACT")[0])
        return 2

    print("=" * 70)
    print(" P6 DASHBOARD ENGINEER - TEST SUITE")
    print(" ui/dashboard.py | ui/api.py")
    print("=" * 70)
    try:
        m = load_dashboard(paths[0])
    except SystemExit:
        raise
    except Exception:
        print("ERROR: could not import dashboard")
        traceback.print_exc()
        return 2
    for need in ("DashboardState", "load_events", "render_png"):
        if not hasattr(m.dashboard, need):
            print(f"ERROR: dashboard.py has no {need}")
            return 2

    def wanted(tid):
        return not ids or tid in ids or tid.split("-")[0] in ids
    tests = sorted([t for t in TESTS if wanted(t[0])],
                   key=lambda t: (t[0].startswith("API"), t[0]))
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
    print(" RESULT:", "ALL PASS - P6 acceptance met" if passed == total else "NOT DONE")
    print("=" * 70)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
