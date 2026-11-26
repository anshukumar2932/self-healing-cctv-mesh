"""Mesh coordinator: heartbeats in, failure detection and verified recovery out.

Uses the project's own modules:
  src/failure.py   heartbeat timeout, link rules, failure injection, restoration
  src/recovery.py  RecoveryEngine: find a route, verify it, retry, fall back to local storage

The dashboard only ever sees standard events, so dashboard.py stays a plain event consumer.
route_for() is a stand-in until src/routing.py is implemented; swap it for the real router.
"""

import itertools
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

import dashboard

import failure
import recovery

ENGINE_CFG = {"ack_timeout_s": 1.0, "control_message_delay_ms": 10, "max_allowed_hops": 6}
ROUTE_EVENTS = ("route_changed", "route_verified")


def build_topology(cameras_online=True):
    """Handbook 3.1 topology (8 nodes, 11 links) in the project's data-contract format."""
    now = time.time()
    nodes = [
        {
            "id": nid,
            "online": cameras_online if nid.startswith("CCTV") else True,
            "battery": 100.0,
            "buffer_available": 100.0,
            "reliability": 0.95,
            "load": 0.15 if nid.startswith("CCTV") else 0.10,
            "last_heartbeat": now,
            "role": "source" if nid.startswith("CCTV") else "relay",
        }
        for nid in dashboard.NODE_IDS
    ]
    links = [
        {"source": a, "destination": b, "quality": 0.90, "bandwidth": 100.0, "latency": 10.0,
         "packet_loss": 0.01, "active": True}
        for a, b in dashboard.EDGES
    ]
    return nodes, links


def route_for(inp):
    """Stand-in router: fewest hops, then highest summed link quality."""
    nodes = {n["id"]: n for n in inp["nodes"]}
    src, dst = inp["source"], inp["destination"]
    excluded = [tuple(r) for r in inp.get("excluded_routes", [])]
    if not nodes.get(src, {}).get("online"):
        return {"selected_route": None, "reason": "source_offline"}
    adj, quality = {}, {}
    for link in inp["links"]:
        a, b = link["source"], link["destination"]
        if link["active"] and nodes[a]["online"] and nodes[b]["online"]:
            adj.setdefault(a, []).append(b)
            adj.setdefault(b, []).append(a)
            quality[frozenset((a, b))] = link["quality"]
    found = []

    def walk(cur, path):
        if cur == dst:
            found.append(list(path))
            return
        for nxt in sorted(adj.get(cur, [])):
            if nxt not in path and len(path) <= inp["max_allowed_hops"]:
                path.append(nxt)
                walk(nxt, path)
                path.pop()

    walk(src, [src])
    found = [p for p in found if tuple(p) not in excluded]
    if not found:
        return {"selected_route": None, "reason": "no_route"}
    best = min(
        found,
        key=lambda p: (len(p) - 1, -sum(quality[frozenset(e)] for e in itertools.pairwise(p)), p),
    )
    return {"selected_route": best}


class MeshCoordinator:
    """Runs on the ACTIVE server only; a standby keeps a replica of the dashboard state."""

    def __init__(self, state, heartbeat_timeout_s=2.0, cameras_online=True):
        self.state = state
        self.timeout = max(0.5, float(heartbeat_timeout_s))
        self.cameras_online = cameras_online
        self._lock = threading.RLock()
        self.engine = recovery.RecoveryEngine(router=route_for, verify=self.verify, config=dict(ENGINE_CFG))
        self.reset()

    # ------------------------------------------------------------------ lifecycle
    def reset(self):
        with self._lock:
            self.nodes, self.links = build_topology(self.cameras_online)
            self.node = {n["id"]: n for n in self.nodes}
            self.agents, self.failed_at, self.routes = {}, {}, {}
            self.forced_down = set()

    def sync_from_state(self):
        """Copy node/link health and routes from the dashboard state (standby -> active)."""
        with self._lock:
            for nid, st in self.state._node_state.items():
                self.node[nid]["online"] = st != "FAILED"
            for link in self.links:
                key = frozenset((link["source"], link["destination"]))
                link["active"] = self.state._link_active.get(key, True)
            self.routes = {src: list(info["path"]) for src, info in self.state._routes.items()}

    def adopt(self, expected_agents=None, forced_down=()):
        """Take over as server: trust the replicated state and give known agents a fresh window."""
        now = time.time()
        with self._lock:
            self.sync_from_state()
            self.forced_down = set(forced_down or ())
            for nid, info in (expected_agents or {}).items():
                if nid in self.node:
                    self.agents[nid] = {
                        "node_id": nid, "hostname": info.get("hostname", "unknown"),
                        "os": info.get("os", "unknown"), "ip": info.get("ip", "unknown"),
                        "first_seen": now, "last_seen": now,
                        "status": "FAILED" if nid in self.forced_down else "ONLINE",
                    }
                    self.node[nid]["last_heartbeat"] = now
            return self._replan_all(now)

    def agents_snapshot(self):
        now = time.time()
        with self._lock:
            snap = {}
            for nid, info in self.agents.items():
                copy = dict(info)
                copy["seconds_since_heartbeat"] = round(max(0.0, now - info["last_seen"]), 2)
                copy["timeout_s"] = self.timeout
                snap[nid] = copy
            return snap

    # ------------------------------------------------------------------ ACK check used by recovery.py
    def verify(self, route):
        now = time.time()
        for nid in route:
            if not self.node[nid]["online"]:
                return False
            agent = self.agents.get(nid)
            if agent and now - agent["last_seen"] > self.timeout + 0.5:
                return False
        return all(self._link(a, b)["active"] for a, b in itertools.pairwise(route))

    def _link(self, a, b):
        want = frozenset((a, b))
        return next(lk for lk in self.links if frozenset((lk["source"], lk["destination"])) == want)

    # ------------------------------------------------------------------ planning
    @staticmethod
    def _route_events(res):
        return [e for e in res["events"] if e["event_type"] in ROUTE_EVENTS]

    def _plan(self, cam, now):
        probe = {"event_type": "route_request", "failure_type": "link", "failed_element": "-", "timestamp": now}
        res = self.engine.recover(probe, self.nodes, self.links, cam, [], now=now)
        if res["event_type"] == "recovery_complete" and res["new_route"]:
            self.routes[cam] = list(res["new_route"])
            return self._route_events(res)
        return []

    def _replan_all(self, now):
        events = []
        for cam in self.node:
            if cam.startswith("CCTV") and self.node[cam]["online"] and cam not in self.routes:
                events += self._plan(cam, now)
        return events

    def seed_routes(self, now=None):
        with self._lock:
            return self._replan_all(now or time.time())

    # ------------------------------------------------------------------ failure handling
    def _handle_failure(self, event, now, emit_failure=True):
        """Run recovery for every camera whose route the failure touches; return dashboard events."""
        results = []
        for cam in [n for n in self.node if n.startswith("CCTV")]:
            if not self.node[cam]["online"]:
                self.routes.pop(cam, None)
                continue
            old = self.routes.get(cam)
            if old is None:
                continue
            res = self.engine.recover(event, self.nodes, self.links, cam, old, now=now)
            if res["event_type"] == "recovery_complete" and res["new_route"] != old:
                self.routes[cam] = list(res["new_route"])
                results.append(res)
            elif res["event_type"] == "recovery_failed":
                self.routes.pop(cam, None)
                results.append(res)
        events = []
        if emit_failure:
            first = next((r for r in results if r["event_type"] == "recovery_complete"), None)
            enriched = dict(event)
            if first:
                for key in ("old_route", "new_route", "recovery_time_ms",
                            "route_computation_time_ms", "control_messages_sent"):
                    enriched[key] = first[key]
            elif results:
                enriched["fallback"] = results[0].get("fallback")
            events.append(enriched)
        for res in results:
            events += self._route_events(res)
            events.append({k: v for k, v in res.items() if k not in ("state_log", "events")})
        return events

    def check_timeouts(self):
        """Detect silent laptops with failure.check_heartbeats and recover affected cameras."""
        now = time.time()
        events = []
        with self._lock:
            live = [self.node[n] for n in self.agents
                    if self.node[n]["online"] and n not in self.forced_down]
            for ev in failure.check_heartbeats(live, now, {"heartbeat_timeout_s": self.timeout}):
                nid = ev["failed_element"]
                self.agents[nid]["status"] = "FAILED"
                self.failed_at[nid] = now
                ev["description"] = f"Heartbeat timeout ({self.timeout}s) from {self.agents[nid]['hostname']}"
                events += self._handle_failure(ev, now)
        return events

    def _restore_node(self, nid, now, emit_recovery=True):
        failure.restore_element(self.nodes, self.links, nid, now)
        started = self.failed_at.pop(nid, None)
        events = []
        if emit_recovery:
            events.append({
                "event_type": "recovery_complete", "timestamp": now, "node_id": nid,
                "failed_element": nid, "node_status": "online", "route_verified": True,
                "recovery_time_ms": round((now - started) * 1000.0, 1) if started else 0.0,
            })
        return events + self._replan_all(now + 0.001)

    # ------------------------------------------------------------------ heartbeats
    def record_heartbeat(self, payload):
        now = time.time()
        nid = payload["node_id"]
        with self._lock:
            old = self.agents.get(nid)
            info = {
                "node_id": nid,
                "hostname": payload.get("hostname") or (old["hostname"] if old else "unknown"),
                "os": payload.get("os") or (old["os"] if old else "unknown"),
                "ip": payload.get("ip") or (old["ip"] if old else "unknown"),
                "first_seen": old["first_seen"] if old else now, "last_seen": now, "status": "ONLINE",
            }
            if nid in self.forced_down:  # failure injected from the testing tab: stay down
                info["status"] = "FAILED"
                self.agents[nid] = info
                return [], dict(info)
            self.agents[nid] = info
            if not self.node[nid]["online"]:
                return self._restore_node(nid, now), dict(info)
            failure.record_heartbeat(self.node[nid], now)
            return self._replan_all(now), dict(info)

    # ------------------------------------------------------------------ failure-testing actions
    def inject(self, element, now=None):
        """Fail a node ('CCTV3') or a link ('CCTV1-CCTV2') on purpose and run recovery."""
        now = now or time.time()
        with self._lock:
            if element == "Server":
                raise ValueError("Server is the destination; use 'Crash server' to test server failover")
            if element in self.node:
                if not self.node[element]["online"]:
                    raise ValueError(f"{element} is already failed")
                kind = "source_camera" if self.node[element]["role"] == "source" else "relay_node"
                self.forced_down.add(element)
            elif "-" in element:
                kind = "link"
            else:
                raise ValueError(f"unknown element {element!r}")
            try:
                ev = failure.inject_failure(
                    self.nodes, self.links,
                    {"failure_type": kind, "failed_element": element, "failure_time": now})
            except ValueError:
                self.forced_down.discard(element)
                raise
            ev["description"] = "Injected from the Failure Testing tab"
            self.failed_at[element] = now
            return self._handle_failure(ev, now)

    def recover(self, element, now=None):
        now = now or time.time()
        with self._lock:
            if element in self.node:
                self.forced_down.discard(element)
                if self.node[element]["online"]:
                    raise ValueError(f"{element} is not failed")
                return self._restore_node(element, now)
            failure.restore_element(self.nodes, self.links, element, now)
            return [{"event_type": "recovery_complete", "timestamp": now, "failed_element": element,
                     "failure_type": "link", "link_status": "active", "route_verified": True}
                    ] + self._replan_all(now + 0.001)

    def on_event(self, event):
        """Events posted by agents ('fail' / 'recover' commands): keep this model in step."""
        typ = event.get("event_type")
        ts = event.get("timestamp")
        now = ts if isinstance(ts, (int, float)) else time.time()
        elem = event.get("failed_element") or event.get("node_id")
        with self._lock:
            if typ == "failure_detected" and elem in self.node and self.node[elem]["online"]:
                kind = event.get("failure_type")
                if kind in ("source_camera", "relay_node"):
                    self.forced_down.add(elem)
                    ev = failure.inject_failure(self.nodes, self.links, {
                        "failure_type": kind, "failed_element": elem, "failure_time": now})
                    self.failed_at[elem] = now
                    return self._handle_failure(ev, now, emit_failure=False)
            if typ == "recovery_complete" and elem in self.node and not self.node[elem]["online"]:
                self.forced_down.discard(elem)
                return self._restore_node(elem, now, emit_recovery=False)
        return []
