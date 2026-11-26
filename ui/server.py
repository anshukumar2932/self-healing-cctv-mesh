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
import os
import socket
import subprocess
import threading

import api
import cluster as cluster_mod
import dashboard
import mesh


def detect_lan_ips():
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
            if not res[4][0].startswith("127."):
                ips.add(res[4][0])
    except OSError:
        pass
    return sorted(ips)


def ensure_self_signed_cert(cert_dir, lan_ips):
    """Self-signed TLS cert so LAN browsers allow webcam access (needs openssl)."""
    cert_path = os.path.join(cert_dir, ".server-cert.pem")
    key_path = os.path.join(cert_dir, ".server-key.pem")
    if os.path.exists(cert_path) and os.path.exists(key_path):
        return cert_path, key_path
    san = ",".join(["DNS:localhost", "IP:127.0.0.1"] + [f"IP:{ip}" for ip in lan_ips])
    cmd = ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key_path,
           "-out", cert_path, "-days", "30", "-subj", "/CN=cctv-mesh-local",
           "-addext", f"subjectAltName={san}"]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return cert_path, key_path


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


if __name__ == "__main__":
    main()
