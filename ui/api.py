"""Flask JSON API for the P6 dashboard. Consumes standard dashboard events only.

Optional extras (off unless passed to create_app):
  heartbeat_monitor -- ui/mesh.py MeshCoordinator: heartbeats, failure detection, recovery
  cluster           -- ui/cluster.py Cluster: several laptops, any of which can be the server
"""

import base64
import binascii
import os
import tempfile
import threading
import time

import dashboard
from flask import Flask, Response, jsonify, request, send_from_directory

WRITE_PATHS = ("/api/heartbeat", "/api/events", "/api/reset", "/api/inject", "/api/recover")


def create_app(state, heartbeat_monitor=None, cluster=None):
    app = Flask(__name__)
    event_log = []
    latest_frames = {}
    lock = threading.RLock()
    epoch = [0]  # bumped on reset so standbys know to re-copy everything
    replica_meta = {"agents": {}, "forced_down": []}

    def _apply_and_log(event):
        with lock:
            ok = state.apply_event(event)
            if ok:
                event_log.append({"seq": len(event_log) + 1, "event": event})
            return ok

    def emit_events(events):
        applied = 0
        with lock:
            for ev in events or []:
                if _apply_and_log(ev):
                    applied += 1
        return applied

    def reset_state():
        with lock:
            state.reset()
            event_log.clear()
            latest_frames.clear()
            epoch[0] += 1
            if heartbeat_monitor is not None:
                heartbeat_monitor.reset()

    def apply_replica(payload):
        """Standby: copy the active server's new events (or everything after a reset)."""
        with lock:
            if payload.get("full") or payload.get("epoch") != epoch[0]:
                state.reset()
                event_log.clear()
                latest_frames.clear()
                epoch[0] = payload.get("epoch", 0)
            for item in payload.get("events", []):
                if item["seq"] == len(event_log) + 1:
                    _apply_and_log(item["event"])
            replica_meta["agents"] = payload.get("agents", {})
            replica_meta["forced_down"] = payload.get("forced_down", [])

    app.emit_events = emit_events
    app.reset_state = reset_state
    app.apply_replica = apply_replica
    app.replica_meta = replica_meta
    app.epoch = epoch
    app.event_log = event_log
    app.state = state
    app.lock = lock

    # ------------------------------------------------------------------ cross-cutting rules
    @app.before_request
    def gate():
        if request.method == "OPTIONS":
            return ("", 204)
        if cluster is not None:
            if cluster.crashed and request.path != "/api/admin/revive":
                return jsonify({"error": "server crashed (simulated)"}), 503
            is_write = request.method == "POST" and (
                request.path in WRITE_PATHS or request.path.startswith("/api/camera/"))
            if is_write and cluster.role != "active":
                return jsonify({"error": "standby", "active_url": cluster.active_url()}), 409
        return None

    @app.after_request
    def add_cors_headers(response):
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        return response

    def error(message, code):
        return jsonify({"error": str(message)}), code

    def valid_int_arg(name, default, minimum=0):
        raw = request.args.get(name)
        if raw is None:
            return default
        try:
            value = int(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} must be an integer") from exc
        if value < minimum:
            raise ValueError(f"{name} must be >= {minimum}")
        return value

    @app.errorhandler(404)
    def not_found(_e):
        return error("Not found", 404)

    @app.errorhandler(405)
    def method_not_allowed(_e):
        return error("Method not allowed", 405)

    # ------------------------------------------------------------------ pages
    @app.get("/")
    def home():
        return send_from_directory(app.root_path, "index.html")

    @app.get("/camera")
    def camera_page():
        return send_from_directory(app.root_path, "camera.html")

    # ------------------------------------------------------------------ read-only state
    @app.get("/api/health")
    def health():
        return jsonify({"status": "ok", "events_applied": len(event_log)})

    @app.get("/api/topology")
    def topology():
        nodes = [state.node_info(nid) for nid in state.topology()["nodes"]]
        return jsonify({"nodes": nodes, "edges": state.link_states()})

    @app.get("/api/nodes")
    def nodes():
        return jsonify({"nodes": [state.node_info(n) for n in state.topology()["nodes"]]})

    @app.get("/api/nodes/<node_id>")
    def node(node_id):
        info = state.node_info(node_id)
        if info is None:
            return error("Unknown node", 404)
        return jsonify(info)

    @app.get("/api/links")
    def links():
        return jsonify({"links": state.link_states()})

    @app.get("/api/routes")
    def routes():
        return jsonify({"routes": {src: state.current_route_info(src) for src in state._routes}})

    @app.get("/api/routes/<source>")
    def route(source):
        info = state.current_route_info(source)
        if info is None:
            return error("Route not found", 404)
        return jsonify(info)

    @app.get("/api/packets")
    def packets():
        try:
            limit = valid_int_arg("limit", 50, 1)
        except ValueError as exc:
            return error(exc, 400)
        ids = list(state._packets)[-limit:]
        paths = state.packet_paths()
        return jsonify({"packets": [
            {"packet_id": pid, "path": paths[pid], "status": state.packet_status(pid)} for pid in ids]})

    @app.get("/api/packets/<packet_id>")
    def packet(packet_id):
        if packet_id not in state._packets:
            return error("Unknown packet", 404)
        return jsonify({"packet_id": packet_id, "path": state.packet_paths()[packet_id],
                        "status": state.packet_status(packet_id)})

    @app.get("/api/failure")
    def failure():
        return jsonify({"failure": state.failure_panel()})

    @app.get("/api/summary")
    def summary():
        return jsonify(state.summary())

    @app.get("/api/metrics")
    def metrics():
        return jsonify(state.metrics())

    @app.get("/api/events")
    def events():
        try:
            since = valid_int_arg("since", 0, 0)
            limit = valid_int_arg("limit", len(event_log) or 1, 1)
        except ValueError as exc:
            return error(exc, 400)
        order = (request.args.get("order") or "asc").lower()
        filtered = [x for x in event_log if x["seq"] > since]
        selected = filtered[-limit:][::-1] if order == "desc" else filtered[:limit]
        return jsonify({"events": selected, "last_seq": len(event_log)})

    @app.get("/api/warnings")
    def warnings():
        return jsonify({"warnings": state.warnings, "ignored": len(state.ignored_events)})

    @app.get("/api/render.png")
    def render():
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            path = tmp.name
        try:
            dashboard.render_png(state, path)
            with open(path, "rb") as image_file:
                data = image_file.read()
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
        return Response(data, mimetype="image/png")

    # ------------------------------------------------------------------ events in
    @app.post("/api/events")
    def post_events():
        if not request.is_json:
            return error("Request body must be valid JSON", 400)
        body = request.get_json(silent=True)
        if not isinstance(body, (dict, list)):
            return error("JSON body must be an object or list", 400)
        items = body if isinstance(body, list) else [body]
        applied = ignored = 0
        with lock:
            for event in items:
                if _apply_and_log(event):
                    applied += 1
                    if heartbeat_monitor is not None:
                        emit_events(heartbeat_monitor.on_event(event))
                else:
                    ignored += 1
        return jsonify({"applied": applied, "ignored": ignored})

    # ------------------------------------------------------------------ live agents
    @app.get("/api/agents")
    def agents():
        if heartbeat_monitor is None:
            return jsonify({"agents": {}})
        with lock:
            return jsonify({"agents": heartbeat_monitor.agents_snapshot()})

    @app.post("/api/heartbeat")
    def heartbeat():
        if not request.is_json:
            return error("Request body must be valid JSON", 400)
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or not body.get("node_id"):
            return error("JSON body must include node_id", 400)
        node_id = body["node_id"]
        with lock:
            if node_id not in state.nodes:
                return error(f"Unknown node: {node_id}", 404)
            payload = dict(body)
            payload.setdefault("ip", request.remote_addr)
            if heartbeat_monitor is not None:
                generated, agent_info = heartbeat_monitor.record_heartbeat(payload)
                emit_events(generated)
                return jsonify({"status": "ok", "node_id": node_id, "agent": agent_info,
                                "route": state.current_route_info(node_id)})
            state._set_node(node_id, "online")
            return jsonify({"status": "ok", "node_id": node_id,
                            "route": state.current_route_info(node_id)})

    # ------------------------------------------------------------------ camera frames
    @app.get("/api/frames")
    def frames():
        with lock:
            return jsonify({"frames": dict(latest_frames)})

    @app.get("/api/frames/meta")
    def frames_meta():
        """Frame timestamps only (the dashboard fetches each image separately)."""
        with lock:
            return jsonify({"frames": {k: v["timestamp"] for k, v in latest_frames.items()}})

    @app.get("/api/frame/<node_id>")
    def frame_image(node_id):
        with lock:
            frame = latest_frames.get(node_id)
        if not frame:
            return error("No frame", 404)
        header, _, data = frame["image"].partition(",")
        try:
            raw = base64.b64decode(data)
        except (binascii.Error, ValueError):
            return error("Bad frame", 500)
        mime = header[5:].split(";")[0] or "image/jpeg"
        return Response(raw, mimetype=mime, headers={"Cache-Control": "no-store"})

    @app.post("/api/camera/<node_id>/frame")
    def post_frame(node_id):
        if node_id not in state.nodes:
            return error(f"Unknown node: {node_id}", 404)
        if not request.is_json:
            return error("Request body must be valid JSON", 400)
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or not isinstance(body.get("image"), str):
            return error("JSON body must include image data URL", 400)
        if not body["image"].startswith("data:image/"):
            return error("image must be a data:image/* URL", 400)
        now = time.time()
        with lock:
            latest_frames[node_id] = {"image": body["image"], "timestamp": now}
            if heartbeat_monitor is not None:
                payload = {"node_id": node_id, "hostname": body.get("hostname") or "browser-cam",
                           "os": body.get("os") or "Browser", "ip": request.remote_addr}
                generated, _ = heartbeat_monitor.record_heartbeat(payload)
                emit_events(generated)
            else:
                state._set_node(node_id, "online")
            route_info = state.current_route_info(node_id)
        return jsonify({"status": "ok", "node_id": node_id, "route": route_info})

    @app.post("/api/reset")
    def reset():
        reset_state()
        return jsonify({"status": "reset"})

    # ------------------------------------------------------------------ failure testing
    def _run(action):
        if heartbeat_monitor is None:
            return error("failure testing needs the mesh coordinator (run server.py)", 400)
        body = request.get_json(silent=True) if request.is_json else None
        if not isinstance(body, dict) or not body.get("element"):
            return error("JSON body must include element", 400)
        try:
            with lock:
                evs = getattr(heartbeat_monitor, action)(body["element"])
                applied = emit_events(evs)
        except ValueError as exc:
            return error(exc, 400)
        return jsonify({"applied": applied, "events": [e["event_type"] for e in evs]})

    @app.post("/api/inject")
    def inject():
        return _run("inject")

    @app.post("/api/recover")
    def recover():
        return _run("recover")

    # ------------------------------------------------------------------ cluster (failover)
    @app.get("/api/role")
    def role():
        if cluster is None:
            return jsonify({"role": "active", "active_url": None, "peers": [], "me": None,
                            "crashed": False, "history": []})
        return jsonify(cluster.info())

    @app.get("/api/replicate")
    def replicate():
        """Standbys call this on the active server to copy its events."""
        try:
            since = valid_int_arg("since", 0, 0)
        except ValueError as exc:
            return error(exc, 400)
        want_epoch = request.args.get("epoch", type=int)
        with lock:
            full = want_epoch != epoch[0]
            start = 0 if full else since
            mon = heartbeat_monitor
            return jsonify({
                "epoch": epoch[0], "full": full, "last_seq": len(event_log),
                "events": [x for x in event_log if x["seq"] > start],
                "agents": mon.agents_snapshot() if mon else {},
                "forced_down": sorted(mon.forced_down) if mon else [],
            })

    @app.post("/api/admin/crash")
    def admin_crash():
        if cluster is None:
            return error("single-server mode: stop the process with Ctrl+C instead", 400)
        cluster.crash()
        return jsonify({"status": "crashed"})

    @app.post("/api/admin/revive")
    def admin_revive():
        if cluster is None:
            return error("single-server mode", 400)
        cluster.revive()
        return jsonify({"status": "revived", "role": cluster.role})

    return app
