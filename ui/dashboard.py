"""P6 dashboard state and PNG renderer for the Self-Healing CCTV Mesh Network."""
from __future__ import annotations

<<<<<<< HEAD
import json
import os
=======
import itertools
import json
>>>>>>> 29049ca (Improved Dashboard with different TABS for routes and camera)
from collections import OrderedDict

NODE_IDS = ["CCTV1", "CCTV2", "CCTV3", "CCTV4", "CCTV5", "CCTV6", "Gateway", "Server"]
EDGES = [
    ("CCTV1", "CCTV2"), ("CCTV1", "CCTV3"), ("CCTV2", "CCTV3"),
    ("CCTV3", "CCTV4"), ("CCTV2", "CCTV5"), ("CCTV3", "CCTV6"),
    ("CCTV5", "CCTV6"), ("CCTV2", "Gateway"), ("CCTV3", "Gateway"),
    ("CCTV4", "Gateway"), ("Gateway", "Server"),
]
KNOWN_EVENTS = {
    "failure_detected", "recovery_complete", "recovery_failed", "route_changed",
    "route_verified", "packet_sent", "packet_forwarded", "packet_received",
    "packet_lost", "buffer_drained", "metric_snapshot",
}


def _edge_key(a, b):
    return frozenset((a, b))


class DashboardState:
    def __init__(self, nodes, links):
        self._initial_nodes = [dict(n) for n in nodes]
        self._initial_links = [dict(l) for l in links]
        self.reset()

    def reset(self):
        self.nodes = {n["id"]: dict(n) for n in self._initial_nodes if isinstance(n, dict) and "id" in n}
        self.links = [dict(l) for l in self._initial_links]
        self._node_state = {n: ("ONLINE" if d.get("online", True) else "FAILED") for n, d in self.nodes.items()}
        self._link_active = {_edge_key(l.get("source"), l.get("destination")): bool(l.get("active", True)) for l in self.links}
        self._routes = {}
        self._packets = OrderedDict()
        self._failure = None
        self._failures = 0
        self._successful_recoveries = 0
        self._recovery_times = []
        self._last_timestamp = None
        self._last_snapshot = None
        self._route_change_count = 0
        self._route_compute_values = []
        self._control_messages_total = 0
        self._control_messages_seen = False
        self._events_applied = 0
        self.ignored_events = []
        self.warnings = []
        return self

    def topology(self):
        return {"nodes": list(self.nodes), "edges": [(l["source"], l["destination"]) for l in self.links]}

    def node_states(self):
        return dict(self._node_state)

    def link_states(self):
        return [{"source": l["source"], "destination": l["destination"],
                 "state": "ACTIVE" if self._link_active.get(_edge_key(l["source"], l["destination"]), False) else "INACTIVE"}
                for l in self.links]

    def _set_node(self, node, status):
        if node not in self.nodes:
            return False
        state = {"online": "ONLINE", "offline": "FAILED", "recovering": "RECOVERING"}.get(str(status).lower())
        if state is None:
            return True
        self._node_state[node] = state
        self.nodes[node]["online"] = state != "FAILED"
        for l in self.links:
            if node in (l["source"], l["destination"]):
                self._link_active[_edge_key(l["source"], l["destination"])] = state == "ONLINE" and bool(l.get("active", True))
        return True

    def _set_failed(self, element, failure_type):
        if failure_type == "link":
            if isinstance(element, (list, tuple)) and len(element) == 2:
                key = _edge_key(*element)
            elif isinstance(element, str) and "-" in element:
                key = _edge_key(*element.split("-", 1))
            else:
                return False
            if key not in self._link_active:
                return False
            self._link_active[key] = False
            return True
        if element not in self.nodes:
            return False
        self._set_node(element, "offline")
        return True

    def apply_event(self, event):
        if not isinstance(event, dict) or not event or not isinstance(event.get("event_type"), str):
            self.ignored_events.append(event)
            return False
        typ = event["event_type"]
        if typ not in KNOWN_EVENTS:
            self.ignored_events.append(event)
            return False
        # Validate named entities before changing any state.
        for key in ("node_id", "source", "current_node", "next_node", "destination"):
            value = event.get(key)
            if value is not None and value not in self.nodes:
                self.ignored_events.append(event)
                return False
        element = event.get("failed_element")
        ftype = event.get("failure_type")
        if typ in ("failure_detected", "recovery_failed") and element is not None:
            if ftype == "link":
                valid = self._known_link_element(element)
            else:
                valid = element in self.nodes
            if not valid:
                self.ignored_events.append(event)
                return False
        ts = event.get("timestamp")
        if isinstance(ts, (int, float)):
            if self._last_timestamp is not None and ts < self._last_timestamp:
                self.warnings.append({"type": "out_of_order", "timestamp": ts, "last_timestamp": self._last_timestamp})
            self._last_timestamp = ts if self._last_timestamp is None else max(self._last_timestamp, ts)
        try:
            if typ == "failure_detected":
                if element is None or not self._set_failed(element, ftype):
                    self.ignored_events.append(event); return False
                self._failures += 1
                self._failure = {k: event.get(k) for k in ("failed_element", "failure_type", "timestamp", "old_route", "new_route", "recovery_time_ms", "packets_lost", "route_computation_time_ms", "control_messages_sent", "fallback")}
            elif typ == "recovery_failed":
                if self._failure is None or self._failure.get("failed_element") == element:
                    self._merge_failure(event)
            elif typ == "recovery_complete":
                node = event.get("node_id") or element
                status = event.get("node_status")
<<<<<<< HEAD
=======
                if event.get("link_status") == "active" and self._known_link_element(element):
                    pair = element if isinstance(element, (list, tuple)) else element.split("-", 1)
                    self._link_active[_edge_key(*pair)] = True
>>>>>>> 29049ca (Improved Dashboard with different TABS for routes and camera)
                if status is not None and node is not None:
                    if not self._set_node(node, status): self.ignored_events.append(event); return False
                elif node in self.nodes and not event.get("new_route"):
                    self._set_node(node, "online")
                if event.get("route_verified") is True:
                    self._successful_recoveries += 1
                    if isinstance(event.get("recovery_time_ms"), (int, float)):
                        self._recovery_times.append(float(event["recovery_time_ms"]))
                if self._failure is not None and (element is None or self._failure.get("failed_element") == element):
                    self._merge_failure(event)
            elif typ == "route_changed":
                src = event.get("source")
                route = event.get("new_route")
                if src not in self.nodes or not isinstance(route, list) or not route or any(n not in self.nodes for n in route):
                    self.ignored_events.append(event); return False
                had_route = src in self._routes
                self._routes[src] = {"path": list(route), "verified": False}
                if had_route:
                    self._route_change_count += 1
            elif typ == "route_verified":
                src, route = event.get("source"), event.get("route")
                if src not in self.nodes:
                    self.ignored_events.append(event); return False
                if src in self._routes and self._routes[src]["path"] == route:
                    self._routes[src]["verified"] = True
            elif typ in ("packet_sent", "packet_forwarded", "packet_received", "packet_lost"):
                pid = event.get("packet_id")
                if pid is None:
                    self.ignored_events.append(event); return False
                p = self._packets.setdefault(str(pid), {"path": [], "status": "in_flight"})
                cur = event.get("current_node")
                if typ == "packet_sent":
                    p["path"] = [x for x in (cur, event.get("next_node")) if x is not None]
                elif typ == "packet_forwarded":
                    nxt = event.get("next_node")
                    if cur is not None and (not p["path"] or p["path"][-1] != cur): p["path"].append(cur)
                    if nxt is not None and (not p["path"] or p["path"][-1] != nxt): p["path"].append(nxt)
                elif typ == "packet_received":
                    dest = event.get("destination")
                    if dest and (not p["path"] or p["path"][-1] != dest): p["path"].append(dest)
                    p["status"] = "delivered"
                else:
                    if cur and (not p["path"] or p["path"][-1] != cur): p["path"].append(cur)
                    p["status"] = "lost"
            elif typ == "buffer_drained":
                pass
            elif typ == "metric_snapshot":
                self._last_snapshot = dict(event)
            if isinstance(event.get("route_computation_time_ms"), (int, float)):
                self._route_compute_values.append(float(event["route_computation_time_ms"]))
            if isinstance(event.get("control_messages_sent"), (int, float)):
                self._control_messages_total += event["control_messages_sent"]
                self._control_messages_seen = True
            self._events_applied += 1
            return True
<<<<<<< HEAD
        except Exception:
=======
        except Exception:  # noqa: BLE001 - a malformed event must never crash the dashboard
>>>>>>> 29049ca (Improved Dashboard with different TABS for routes and camera)
            self.ignored_events.append(event)
            return False

    def _known_link_element(self, element):
        if isinstance(element, (list, tuple)) and len(element) == 2:
            key = _edge_key(*element)
        elif isinstance(element, str) and "-" in element:
            key = _edge_key(*element.split("-", 1))
        else:
            return False
        return key in self._link_active

    def _merge_failure(self, event):
        if self._failure is None:
            self._failure = {k: None for k in ("failed_element", "failure_type", "timestamp", "old_route", "new_route", "recovery_time_ms", "packets_lost", "route_computation_time_ms", "control_messages_sent", "fallback")}
        for k in self._failure:
            if k == "timestamp" and self._failure.get("timestamp") is not None:
                continue
            if k in event and event[k] is not None:
                self._failure[k] = event[k]

    def apply_events(self, events):
        if not isinstance(events, list):
            self.ignored_events.append(events); return
        for event in events:
            self.apply_event(event)

    def current_route(self, source):
        info = self._routes.get(source)
        return list(info["path"]) if info else None

    def current_route_info(self, source):
        info = self._routes.get(source)
        return {"path": list(info["path"]), "verified": info["verified"]} if info else None

    def packet_paths(self):
        return {pid: list(p["path"]) for pid, p in self._packets.items()}

    def packet_status(self, packet_id):
        p = self._packets.get(str(packet_id))
        return p["status"] if p else None

    def failure_panel(self):
        return dict(self._failure) if self._failure is not None else None

    def summary(self):
        cameras = [n for n in self.nodes if n.startswith("CCTV")]
        active = sum(self._node_state.get(n) == "ONLINE" for n in cameras)
        failed = [n for n, st in self._node_state.items() if st == "FAILED"]
        return {"active_nodes": active, "total_nodes": len(cameras), "active_nodes_text": f"{active}/{len(cameras)}",
                "failed_nodes": failed, "active_links": sum(x["state"] == "ACTIVE" for x in self.link_states()),
                "failures": self._failures, "successful_recoveries": self._successful_recoveries,
                "average_recovery_ms": sum(self._recovery_times) / len(self._recovery_times) if self._recovery_times else 0}

    def metrics(self):
        if self._last_snapshot is not None:
            e = self._last_snapshot
            sent, delivered, lost = (e.get("packets_sent", 0), e.get("packets_delivered", 0), e.get("packets_lost", 0))
            rate = delivered / sent if sent else 0
            return {"packets_sent": sent, "packets_delivered": delivered, "packets_lost": lost,
                    "delivery_rate": rate, "delivery_rate_pct": rate * 100,
                    "average_hops": e.get("average_hops", 0), "route_changes": e.get("route_changes", 0),
                    "network_load": e.get("network_load", 0),
                    "route_computation_time_ms": e.get("route_computation_time_ms", 0),
                    "control_messages": e.get("control_messages", e.get("control_messages_sent", 0))}
        sent = len(self._packets)
        delivered = sum(p["status"] == "delivered" for p in self._packets.values())
        lost = sum(p["status"] == "lost" for p in self._packets.values())
        hops = [max(0, len(p["path"]) - 1) for p in self._packets.values() if p["status"] == "delivered"]
        rate = delivered / sent if sent else 0
        return {"packets_sent": sent, "packets_delivered": delivered, "packets_lost": lost,
                "delivery_rate": rate, "delivery_rate_pct": rate * 100,
                "average_hops": sum(hops) / len(hops) if hops else 0,
                "route_changes": self._route_change_count, "network_load": 0,
                "route_computation_time_ms": sum(self._route_compute_values) / len(self._route_compute_values) if self._route_compute_values else 0,
                "control_messages": self._control_messages_total}

    def node_info(self, node_id):
        if node_id not in self.nodes: return None
        n = self.nodes[node_id]
        return {"id": node_id, "role": n.get("role"), "state": self._node_state[node_id],
                "load": n.get("load"), "battery": n.get("battery"),
                "buffer_available": n.get("buffer_available"), "reliability": n.get("reliability")}


def load_events(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"Invalid JSON event file: {exc}") from exc
    if not isinstance(data, list):
<<<<<<< HEAD
        raise ValueError("Event JSON must contain a list")
=======
        raise ValueError("Event JSON must contain a list")  # noqa: TRY004
>>>>>>> 29049ca (Improved Dashboard with different TABS for routes and camera)
    return data


def render_png(state, path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    positions = {"CCTV1": (0, 2), "CCTV2": (2, 2), "CCTV3": (1, 1), "CCTV4": (0, 0),
                 "CCTV5": (3, 1), "CCTV6": (2, 0), "Gateway": (4, 1), "Server": (5, 1)}
    fig, ax = plt.subplots(figsize=(11, 6))
    route_edges = set()
    for info in state._routes.values():
        p = info["path"]
<<<<<<< HEAD
        route_edges.update(_edge_key(a, b) for a, b in zip(p, p[1:]))
=======
        route_edges.update(_edge_key(a, b) for a, b in itertools.pairwise(p))
>>>>>>> 29049ca (Improved Dashboard with different TABS for routes and camera)
    for l in state.links:
        a, b = l["source"], l["destination"]
        active = state._link_active.get(_edge_key(a, b), False)
        is_route = _edge_key(a, b) in route_edges
        x1, y1 = positions[a]; x2, y2 = positions[b]
        ax.plot([x1, x2], [y1, y2], linewidth=3 if is_route else 1.5,
                linestyle="-" if active else "--", alpha=1 if active else .35)
    for node, (x, y) in positions.items():
        st = state._node_state[node]
        marker = {"ONLINE": "o", "FAILED": "X", "RECOVERING": "s"}.get(st, "o")
        ax.scatter([x], [y], s=900, marker=marker, edgecolors="black", linewidths=1.5)
        ax.text(x, y, node, ha="center", va="center", fontsize=8)
    sm = state.summary(); mt = state.metrics()
    ax.set_title(f"Self-Healing CCTV Mesh Network | Active cameras: {sm['active_nodes_text']} | Packets delivered: {mt['packets_delivered']}")
    ax.axis("off"); ax.set_xlim(-.7, 5.7); ax.set_ylim(-.7, 2.7)
    fig.tight_layout(); fig.savefig(path, dpi=140); plt.close(fig)
