<<<<<<< HEAD
"""Variable-host server launcher for the Self-Healing CCTV Mesh Dashboard.

Run this on whichever machine you choose as the main server (Windows or macOS).
Remote camera devices connect to this server over the LAN via camera_agent.py.
"""
from __future__ import annotations

import argparse
from collections import deque
=======
"""Server launcher for the Self-Healing CCTV Mesh Dashboard (any laptop can be the server).

Single server:
    python ui/server.py --port 5050

Failover group (run on every laptop, each listing the OTHER laptops):
    laptop A:  python ui/server.py --priority 1 --peers http://B:5050,http://C:5050
    laptop B:  python ui/server.py --priority 2 --peers http://A:5050,http://C:5050
    laptop C:  python ui/server.py --priority 3 --peers http://A:5050,http://B:5050
The live node with the lowest priority is the active server; if it dies the next one takes over.
"""

import argparse
>>>>>>> 29049ca (Improved Dashboard with different TABS for routes and camera)
import os
import socket
import subprocess
import threading
<<<<<<< HEAD
import time

import api
import dashboard


def default_topology(cameras_online: bool = True):
    """Build the Handbook §3.1 8-node, 11-link mesh topology."""
    nodes = []
    for nid in dashboard.NODE_IDS:
        is_cam = nid.startswith("CCTV")
        nodes.append({
            "id": nid,
            "online": cameras_online if is_cam else True,
            "battery": 100.0,
            "buffer_available": 100.0,
            "reliability": 0.95,
            "load": 0.15 if is_cam else 0.10,
            "role": "source" if is_cam else "relay",
        })
    links = [
        {
            "source": a,
            "destination": b,
            "quality": 0.90,
            "bandwidth": 100.0,
            "latency": 10.0,
            "packet_loss": 0.01,
            "active": True,
        }
        for a, b in dashboard.EDGES
    ]
    return nodes, links


class MeshCoordinator:
    """Tracks live remote camera heartbeats and drives event-based self-healing routes."""

    def __init__(self, state: dashboard.DashboardState, heartbeat_timeout_s: float = 2.0):
        self.state = state
        self.heartbeat_timeout_s = max(0.5, float(heartbeat_timeout_s))
        self.agents: dict[str, dict] = {}
        self.failed_at: dict[str, float] = {}
        self._lock = threading.RLock()

    def reset(self):
        with self._lock:
            self.agents.clear()
            self.failed_at.clear()

    def agents_snapshot(self):
        now = time.time()
        with self._lock:
            snap = {}
            for nid, info in self.agents.items():
                copy = dict(info)
                copy["seconds_since_heartbeat"] = round(max(0.0, now - info["last_seen"]), 2)
                copy["timeout_s"] = self.heartbeat_timeout_s
                snap[nid] = copy
            return snap

    def _compute_route(self, source: str, destination: str = "Server", exclude_node: str | None = None):
        """Find shortest active simple path from source to destination (<= 6 hops)."""
        if self.state._node_state.get(source) != "ONLINE" or source == exclude_node:
            return None
        adj: dict[str, list[str]] = {n: [] for n in self.state.nodes}
        for link in self.state.links:
            a, b = link["source"], link["destination"]
            if a == exclude_node or b == exclude_node:
                continue
            if self.state._node_state.get(a) != "ONLINE" or self.state._node_state.get(b) != "ONLINE":
                continue
            if not self.state._link_active.get(dashboard._edge_key(a, b), False):
                continue
            adj[a].append(b)
            adj[b].append(a)
        queue = deque([[source]])
        visited = {source}
        while queue:
            path = queue.popleft()
            if path[-1] == destination:
                return path if len(path) - 1 <= 6 else None
            if len(path) - 1 >= 6:
                continue
            for nxt in sorted(adj.get(path[-1], [])):
                if nxt not in visited:
                    visited.add(nxt)
                    queue.append(path + [nxt])
        return None

    def _route_valid(self, path: list[str] | None) -> bool:
        if not path or len(path) < 2:
            return False
        for node in path:
            if self.state._node_state.get(node) != "ONLINE":
                return False
        for a, b in zip(path, path[1:]):
            if not self.state._link_active.get(dashboard._edge_key(a, b), False):
                return False
        return True

    def _reroute_affected_cameras(self, now: float, force_all_online: bool = False):
        """Generate route_changed + route_verified events for cameras needing a route."""
        events = []
        for nid in self.state.nodes:
            if not nid.startswith("CCTV"):
                continue
            if self.state._node_state.get(nid) != "ONLINE":
                continue
            current = self.state.current_route(nid)
            if not force_all_online and self._route_valid(current):
                continue
            t0 = time.perf_counter()
            new_route = self._compute_route(nid)
            compute_ms = round((time.perf_counter() - t0) * 1000.0, 2)
            if new_route and new_route != current:
                events.append({
                    "event_type": "route_changed",
                    "timestamp": now,
                    "source": nid,
                    "old_route": current,
                    "new_route": new_route,
                    "route_computation_time_ms": compute_ms,
                    "control_messages_sent": max(2, len(new_route) * 2),
                })
                events.append({
                    "event_type": "route_verified",
                    "timestamp": now + 0.001,
                    "source": nid,
                    "route": new_route,
                })
        return events

    def record_heartbeat(self, payload: dict):
        now = time.time()
        node_id = payload["node_id"]
        with self._lock:
            prev_state = self.state._node_state.get(node_id, "ONLINE")
            existing = self.agents.get(node_id)
            info = {
                "node_id": node_id,
                "hostname": payload.get("hostname") or (existing["hostname"] if existing else "unknown"),
                "os": payload.get("os") or (existing["os"] if existing else "unknown"),
                "ip": payload.get("ip") or (existing["ip"] if existing else "unknown"),
                "first_seen": existing["first_seen"] if existing else now,
                "last_seen": now,
                "status": "ONLINE",
            }
            self.agents[node_id] = info
            events = []

            was_failed = prev_state != "ONLINE"
            if was_failed:
                # Restore node online first so route calculation includes it.
                self.state._set_node(node_id, "online")
                failed_ts = self.failed_at.pop(node_id, None)
                recovery_ms = round((now - failed_ts) * 1000.0, 1) if failed_ts else 0.0
                new_route = self._compute_route(node_id)
                events.append({
                    "event_type": "recovery_complete",
                    "timestamp": now,
                    "node_id": node_id,
                    "failed_element": node_id,
                    "node_status": "online",
                    "new_route": new_route,
                    "recovery_time_ms": recovery_ms,
                    "route_verified": bool(new_route),
                })
                if new_route:
                    events.append({
                        "event_type": "route_changed",
                        "timestamp": now + 0.001,
                        "source": node_id,
                        "new_route": new_route,
                        "route_computation_time_ms": 1.0,
                        "control_messages_sent": len(new_route) * 2,
                    })
                    events.append({
                        "event_type": "route_verified",
                        "timestamp": now + 0.002,
                        "source": node_id,
                        "route": new_route,
                    })
            elif not self._route_valid(self.state.current_route(node_id)):
                new_route = self._compute_route(node_id)
                if new_route:
                    events.append({
                        "event_type": "route_changed",
                        "timestamp": now,
                        "source": node_id,
                        "new_route": new_route,
                        "route_computation_time_ms": 1.0,
                        "control_messages_sent": len(new_route) * 2,
                    })
                    events.append({
                        "event_type": "route_verified",
                        "timestamp": now + 0.001,
                        "source": node_id,
                        "route": new_route,
                    })
            return events, dict(info)

    def on_event(self, event: dict):
        """React to externally posted failure/recovery events with automatic rerouting."""
        typ = event.get("event_type")
        now = event.get("timestamp")
        if not isinstance(now, (int, float)):
            now = time.time()
        with self._lock:
            if typ == "failure_detected":
                elem = event.get("failed_element")
                if isinstance(elem, str) and elem in self.state.nodes:
                    self.failed_at[elem] = time.time()
                    if elem in self.agents:
                        self.agents[elem]["status"] = "FAILED"
                return self._reroute_affected_cameras(now)
            if typ == "recovery_complete":
                node = event.get("node_id") or event.get("failed_element")
                if isinstance(node, str):
                    self.failed_at.pop(node, None)
                    if node in self.agents:
                        self.agents[node]["status"] = "ONLINE"
                return self._reroute_affected_cameras(now)
            return []

    def check_timeouts(self):
        """Detect expired remote camera heartbeats and emit failure + reroute events."""
        now = time.time()
        events = []
        with self._lock:
            for nid, info in list(self.agents.items()):
                if info["status"] != "ONLINE":
                    continue
                if now - info["last_seen"] <= self.heartbeat_timeout_s:
                    continue
                info["status"] = "FAILED"
                self.failed_at[nid] = now
                old_route = self.state.current_route(nid)
                self.state._set_node(nid, "offline")

                # Check if any other camera depended on this node and compute alternate route
                t0 = time.perf_counter()
                reroute_events = self._reroute_affected_cameras(now + 0.005)
                compute_ms = round((time.perf_counter() - t0) * 1000.0, 2)
                sample_new = next((e["new_route"] for e in reroute_events if e["event_type"] == "route_changed"), None)
                sample_old = next((e.get("old_route") for e in reroute_events if e["event_type"] == "route_changed"), old_route)

                events.append({
                    "event_type": "failure_detected",
                    "timestamp": now,
                    "failure_type": "relay_node" if nid == "CCTV3" else "source_camera",
                    "failed_element": nid,
                    "old_route": sample_old,
                    "new_route": sample_new,
                    "route_computation_time_ms": compute_ms,
                    "control_messages_sent": 4,
                    "description": f"Heartbeat timeout ({self.heartbeat_timeout_s}s) from {info['hostname']} ({info['os']})",
                })
                events.extend(reroute_events)
        return events


def detect_lan_ips() -> list[str]:
=======

import api
import cluster as cluster_mod
import dashboard
import mesh


def detect_lan_ips():
>>>>>>> 29049ca (Improved Dashboard with different TABS for routes and camera)
    ips = set()
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        ips.add(sock.getsockname()[0])
        sock.close()
    except OSError:
        pass
    try:
        for res in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
<<<<<<< HEAD
            ip = res[4][0]
            if not ip.startswith("127."):
                ips.add(ip)
=======
            if not res[4][0].startswith("127."):
                ips.add(res[4][0])
>>>>>>> 29049ca (Improved Dashboard with different TABS for routes and camera)
    except OSError:
        pass
    return sorted(ips)


<<<<<<< HEAD
def ensure_self_signed_cert(cert_dir: str, lan_ips: list[str]) -> tuple[str, str]:
    """Generate a temporary self-signed TLS cert via openssl so mobile/LAN browsers allow webcam access."""
=======
def ensure_self_signed_cert(cert_dir, lan_ips):
    """Self-signed TLS cert so LAN browsers allow webcam access (needs openssl)."""
>>>>>>> 29049ca (Improved Dashboard with different TABS for routes and camera)
    cert_path = os.path.join(cert_dir, ".server-cert.pem")
    key_path = os.path.join(cert_dir, ".server-key.pem")
    if os.path.exists(cert_path) and os.path.exists(key_path):
        return cert_path, key_path
<<<<<<< HEAD
    san_items = ["DNS:localhost", "IP:127.0.0.1"] + [f"IP:{ip}" for ip in lan_ips]
    san = ",".join(san_items)
    cmd = [
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
        "-keyout", key_path, "-out", cert_path, "-days", "30",
        "-subj", "/CN=cctv-mesh-local",
        "-addext", f"subjectAltName={san}",
    ]
=======
    san = ",".join(["DNS:localhost", "IP:127.0.0.1"] + [f"IP:{ip}" for ip in lan_ips])
    cmd = ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key_path,
           "-out", cert_path, "-days", "30", "-subj", "/CN=cctv-mesh-local",
           "-addext", f"subjectAltName={san}"]
>>>>>>> 29049ca (Improved Dashboard with different TABS for routes and camera)
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return cert_path, key_path


<<<<<<< HEAD
def main():
    parser = argparse.ArgumentParser(description="Run the Self-Healing CCTV Mesh Dashboard server.")
    parser.add_argument("--host", default=os.environ.get("CCTV_HOST", "0.0.0.0"), help="Bind host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=int(os.environ.get("CCTV_PORT", "5050")), help="Bind port (default: 5050)")
    parser.add_argument("--heartbeat-timeout", type=float, default=float(os.environ.get("CCTV_HEARTBEAT_TIMEOUT", "2.0")),
                        help="Seconds before missing camera heartbeat marks node FAILED (default: 2.0)")
    parser.add_argument("--wait-for-agents", action="store_true",
                        help="Start CCTV1-CCTV6 offline until remote camera agents connect")
    parser.add_argument("--https", action="store_true",
                        help="Serve over self-signed HTTPS (required by browsers for remote laptop webcam access)")
    args = parser.parse_args()

    nodes, links = default_topology(cameras_online=not args.wait_for_agents)
    state = dashboard.DashboardState(nodes, links)
    coordinator = MeshCoordinator(state, heartbeat_timeout_s=args.heartbeat_timeout)
    flask_app = api.create_app(state, heartbeat_monitor=coordinator)

    # Seed initial routes for online cameras so topology & routes are visible immediately.
    if not args.wait_for_agents:
        flask_app.emit_events(coordinator._reroute_affected_cameras(time.time(), force_all_online=True))

    stop_event = threading.Event()

    def monitor_loop():
        while not stop_event.is_set():
            events = coordinator.check_timeouts()
            if events:
                flask_app.emit_events(events)
            stop_event.wait(0.4)

    t = threading.Thread(target=monitor_loop, daemon=True)
    t.start()

    lan_ips = detect_lan_ips()
    primary_ip = lan_ips[0] if lan_ips else "127.0.0.1"
    scheme = "https" if args.https else "http"
    ssl_context = ensure_self_signed_cert(os.path.dirname(os.path.abspath(__file__)), lan_ips) if args.https else None

    print("=" * 64)
    print("Self-Healing CCTV Mesh Dashboard Server")
    print("=" * 64)
    print(f"Local URL : {scheme}://127.0.0.1:{args.port}")
    for ip in lan_ips:
        print(f"LAN URL   : {scheme}://{ip}:{args.port}")
    print(f"Heartbeat timeout: {args.heartbeat_timeout}s")
    print("\nBrowser Camera URL (for live webcam footage from other laptops):")
    print(f"  {scheme}://{primary_ip}:{args.port}/camera?node=CCTV1")
    print(f"  {scheme}://{primary_ip}:{args.port}/camera?node=CCTV2")
    if not args.https:
        print("  (Tip: if remote browsers block webcam on HTTP, restart with --https)")
    print("\nOr connect Python telemetry agents:")
    print(f"  python camera_agent.py --server {scheme}://{primary_ip}:{args.port} --node CCTV1")
    print("=" * 64)

    try:
        flask_app.run(host=args.host, port=args.port, threaded=True, use_reloader=False, ssl_context=ssl_context)
    finally:
        stop_event.set()
=======
def build_server(args, url):
    """Wire state, coordinator, cluster and Flask app together. Returns (app, cluster, coordinator)."""
    nodes, links = mesh.build_topology(cameras_online=not args.wait_for_agents)
    state = dashboard.DashboardState(nodes, links)
    coordinator = mesh.MeshCoordinator(state, heartbeat_timeout_s=args.heartbeat_timeout,
                                       cameras_online=not args.wait_for_agents)
    holder = {}
    need_full = [True]  # after being active, copy everything again from the new active server

    def sync(peer_url):
        app = holder["app"]
        epoch = -1 if need_full[0] else app.epoch[0]
        since = 0 if need_full[0] else len(app.event_log)
        data = cluster_mod.http_get_json(f"{peer_url}/api/replicate?since={since}&epoch={epoch}", timeout=2.0)
        if data:
            app.apply_replica(data)
            need_full[0] = False
        return bool(data)

    def on_promote():
        app = holder["app"]
        with app.lock:
            app.emit_events(coordinator.adopt(app.replica_meta["agents"], app.replica_meta["forced_down"]))

    def on_demote():
        need_full[0] = True

    peers = [p for p in args.peers.split(",") if p.strip()]
    node_id = args.id or f"{socket.gethostname()}:{args.port}"
    clu = cluster_mod.Cluster(node_id, url, peers, priority=args.priority, interval=1.0,
                              fail_after=args.election_timeout, sync=sync,
                              on_promote=on_promote, on_demote=on_demote)
    app = api.create_app(state, heartbeat_monitor=coordinator, cluster=clu)
    holder["app"] = app
    return app, clu, coordinator


def main():
    parser = argparse.ArgumentParser(description="Run the Self-Healing CCTV Mesh Dashboard server.")
    parser.add_argument("--host", default=os.environ.get("CCTV_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("CCTV_PORT", "5050")))
    parser.add_argument("--heartbeat-timeout", type=float, default=2.0,
                        help="seconds of silence before a camera is marked FAILED (default 2.0)")
    parser.add_argument("--wait-for-agents", action="store_true",
                        help="start CCTV1-CCTV6 offline until real cameras connect")
    parser.add_argument("--https", action="store_true", help="self-signed HTTPS (needed for webcams)")
    parser.add_argument("--peers", default="", help="comma-separated URLs of the OTHER server laptops")
    parser.add_argument("--priority", type=int, default=100, help="lower number = preferred server")
    parser.add_argument("--id", default="", help="name of this laptop (default hostname:port)")
    parser.add_argument("--advertise", default="", help="URL other laptops use to reach this one")
    parser.add_argument("--election-timeout", type=float, default=3.0,
                        help="seconds without an answer before a peer is considered dead (default 3.0)")
    args = parser.parse_args()

    lan_ips = detect_lan_ips()
    primary_ip = lan_ips[0] if lan_ips else "127.0.0.1"
    scheme = "https" if args.https else "http"
    url = args.advertise or f"{scheme}://{primary_ip}:{args.port}"

    app, clu, coordinator = build_server(args, url)
    clu.tick()  # first election round before serving, so a lone node starts as active
    clu.start()

    stop = threading.Event()

    def monitor_loop():
        while not stop.is_set():
            if clu.role == "active" and not clu.crashed:
                events = coordinator.check_timeouts()
                if events:
                    app.emit_events(events)
            stop.wait(0.4)

    threading.Thread(target=monitor_loop, daemon=True).start()

    here = os.path.dirname(os.path.abspath(__file__))
    ssl_context = ensure_self_signed_cert(here, lan_ips) if args.https else None
    print("=" * 64)
    print(f"CCTV Mesh server '{clu.id}'  role={clu.role}  priority={args.priority}")
    print(f"Dashboard : {url}/")
    print(f"Camera    : {url}/camera?node=CCTV1")
    print(f"Agent     : python ui/camera_agent.py --server {url} --node CCTV1")
    if clu.peers:
        print(f"Peers     : {', '.join(clu.peers)}")
        print("Agents/cameras: pass every server URL, comma-separated, so they can follow a failover.")
    print("=" * 64)
    try:
        app.run(host=args.host, port=args.port, threaded=True, use_reloader=False, ssl_context=ssl_context)
    finally:
        stop.set()
        clu.stop()
>>>>>>> 29049ca (Improved Dashboard with different TABS for routes and camera)


if __name__ == "__main__":
    main()
