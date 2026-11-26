"""Cross-platform CCTV camera node agent (Windows & macOS, stdlib only).

Connects a remote laptop/desktop to any variable main server running server.py:
    python camera_agent.py --server http://<SERVER_IP>:5050 --node CCTV1
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

VALID_CAMERAS = ["CCTV1", "CCTV2", "CCTV3", "CCTV4", "CCTV5", "CCTV6"]


def normalize_server_url(raw: str) -> str:
    url = raw.strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        url = f"http://{url}"
    return url


def post_json(url: str, payload: dict | list, timeout: float = 3.0) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


class CameraAgent:
    """Streams live heartbeats and mesh packet events for a single CCTV node."""

    def __init__(self, server_url: str, node_id: str, interval: float = 0.7, packet_interval: float = 2.5):
        self.server_url = normalize_server_url(server_url)
        self.node_id = node_id
        self.interval = max(0.2, float(interval))
        self.packet_interval = float(packet_interval)
        self.hostname = socket.gethostname()
        self.os_name = f"{platform.system()} {platform.release()}".strip()
        self.current_route: list[str] | None = None
        self.paused = False
        self.stop_event = threading.Event()
        self._seq = 0
        self._lock = threading.Lock()
        self._last_error: str | None = None

    def send_heartbeat(self) -> dict | None:
        if self.paused:
            return None
        payload = {
            "node_id": self.node_id,
            "hostname": self.hostname,
            "os": self.os_name,
        }
        try:
            res = post_json(f"{self.server_url}/api/heartbeat", payload)
            route = res.get("route")
            with self._lock:
                self.current_route = route.get("path") if isinstance(route, dict) else None
                if self._last_error is not None:
                    print(f"[{self.node_id}] Reconnected to {self.server_url}")
                    self._last_error = None
            return res
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            with self._lock:
                msg = str(exc)
                if msg != self._last_error:
                    print(f"[{self.node_id}] Heartbeat error: {msg}")
                    self._last_error = msg
            return None

    def send_packet_flow(self) -> bool:
        if self.paused:
            return False
        with self._lock:
            path = list(self.current_route) if self.current_route else None
            self._seq += 1
            pid = f"{self.node_id}-pkt-{self._seq:04d}"
        now = time.time()
        if not path or len(path) < 2:
            events = [{
                "event_type": "packet_lost",
                "timestamp": now,
                "packet_id": pid,
                "current_node": self.node_id,
                "reason": "no_active_route",
            }]
        else:
            events = [{
                "event_type": "packet_sent",
                "timestamp": now,
                "packet_id": pid,
                "current_node": path[0],
                "next_node": path[1],
            }]
            for idx in range(1, len(path) - 1):
                events.append({
                    "event_type": "packet_forwarded",
                    "timestamp": now + idx * 0.01,
                    "packet_id": pid,
                    "current_node": path[idx],
                    "next_node": path[idx + 1],
                })
            events.append({
                "event_type": "packet_received",
                "timestamp": now + len(path) * 0.01,
                "packet_id": pid,
                "destination": path[-1],
            })
        try:
            post_json(f"{self.server_url}/api/events", events)
            return True
        except (urllib.error.URLError, TimeoutError, OSError):
            return False

    def trigger_failure(self):
        self.paused = True
        try:
            post_json(f"{self.server_url}/api/events", {
                "event_type": "failure_detected",
                "timestamp": time.time(),
                "failure_type": "relay_node" if self.node_id == "CCTV3" else "source_camera",
                "failed_element": self.node_id,
                "description": f"Manual failure triggered on {self.hostname} ({self.os_name})",
            })
            print(f"[{self.node_id}] Failure triggered; heartbeats paused.")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            print(f"[{self.node_id}] Failed to notify server: {exc}")

    def recover(self):
        self.paused = False
        res = self.send_heartbeat()
        print(f"[{self.node_id}] Resumed heartbeats -> route: {res.get('route') if res else 'pending'}")

    def start_background_loops(self):
        def hb_loop():
            while not self.stop_event.is_set():
                self.send_heartbeat()
                self.stop_event.wait(self.interval)

        def pkt_loop():
            while not self.stop_event.is_set():
                if self.packet_interval > 0:
                    self.send_packet_flow()
                    self.stop_event.wait(self.packet_interval)
                else:
                    self.stop_event.wait(1.0)

        t1 = threading.Thread(target=hb_loop, daemon=True)
        t2 = threading.Thread(target=pkt_loop, daemon=True)
        t1.start()
        t2.start()
        return t1, t2


def resolve_args():
    parser = argparse.ArgumentParser(description="Connect a Windows/macOS device as a live CCTV mesh node.")
    parser.add_argument("--server", default=os.environ.get("CCTV_SERVER", ""),
                        help="Variable main server URL, e.g. http://192.168.1.42:5050")
    parser.add_argument("--node", default=os.environ.get("CCTV_NODE", ""),
                        help="Camera node ID (CCTV1..CCTV6)")
    parser.add_argument("--interval", type=float, default=0.7, help="Heartbeat interval in seconds (default: 0.7)")
    parser.add_argument("--packet-interval", type=float, default=2.5,
                        help="Seconds between simulated packet flows (0 to disable, default: 2.5)")
    parser.add_argument("--no-interactive", action="store_true", help="Run without interactive stdin command prompt")
    args = parser.parse_args()

    server = args.server.strip()
    if not server:
        server = input("Main server URL (e.g. http://192.168.1.42:5050): ").strip()
    if not server:
        print("Error: --server URL is required.", file=sys.stderr)
        sys.exit(1)

    node = args.node.strip().upper()
    if not node:
        node = input(f"Camera node ID ({'/'.join(VALID_CAMERAS)}): ").strip().upper()
    if node not in VALID_CAMERAS:
        print(f"Error: --node must be one of {', '.join(VALID_CAMERAS)}.", file=sys.stderr)
        sys.exit(1)

    args.server = normalize_server_url(server)
    args.node = node
    return args


def main():
    args = resolve_args()
    agent = CameraAgent(args.server, args.node, interval=args.interval, packet_interval=args.packet_interval)
    print("=" * 60)
    print(f"CCTV Camera Agent | Node: {agent.node_id}")
    print(f"Device : {agent.hostname} ({agent.os_name})")
    print(f"Server : {agent.server_url}")
    print("Commands: status | burst [n] | fail | recover | quit")
    print("=" * 60)

    agent.start_background_loops()
    try:
        if args.no_interactive:
            while not agent.stop_event.is_set():
                time.sleep(0.5)
        else:
            while not agent.stop_event.is_set():
                try:
                    raw = input(f"{agent.node_id}> ").strip()
                except EOFError:
                    time.sleep(0.5)
                    continue
                if not raw:
                    continue
                parts = raw.split()
                cmd = parts[0].lower()
                if cmd in ("quit", "exit", "q"):
                    break
                if cmd == "status":
                    print(f"paused={agent.paused} server={agent.server_url} route={agent.current_route}")
                elif cmd == "fail":
                    agent.trigger_failure()
                elif cmd == "recover":
                    agent.recover()
                elif cmd == "burst":
                    count = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 5
                    ok = sum(1 for _ in range(count) if agent.send_packet_flow())
                    print(f"[{agent.node_id}] Sent {ok}/{count} packet flows")
                else:
                    print("Unknown command. Use: status | burst [n] | fail | recover | quit")
    except KeyboardInterrupt:
        print(f"\n[{agent.node_id}] Stopping agent (server will detect timeout if > heartbeat_timeout)...")
    finally:
        agent.stop_event.set()


if __name__ == "__main__":
    main()
