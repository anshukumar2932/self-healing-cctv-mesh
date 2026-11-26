"""Flask JSON API for the P6 dashboard. Consumes standard dashboard events only."""
from flask import Flask, jsonify, request, Response, send_from_directory
import io
import json
import threading
import time
import dashboard


def create_app(state, heartbeat_monitor=None):
    app = Flask(__name__)
    event_log = []
    latest_frames = {}
    lock = threading.RLock()

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

    app.emit_events = emit_events

    @app.after_request
    def add_cors_headers(response):
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        return response

    @app.route("/api/<path:_subpath>", methods=["OPTIONS"])
    def cors_preflight(_subpath):
        return ("", 204)

    def error(message, code):
        return jsonify({"error": str(message)}), code

    def valid_int_arg(name, default, minimum=0):
        raw = request.args.get(name)
        if raw is None: return default
        try: value = int(raw)
        except (TypeError, ValueError): raise ValueError(f"{name} must be an integer")
        if value < minimum: raise ValueError(f"{name} must be >= {minimum}")
        return value

    @app.errorhandler(404)
    def not_found(_e):
        return error("Not found", 404)

    @app.errorhandler(405)
    def method_not_allowed(_e):
        return error("Method not allowed", 405)

    @app.get("/")
    def home():
        return send_from_directory(app.root_path, "index.html")

    @app.get("/camera")
    def camera_page():
        return send_from_directory(app.root_path, "camera.html")

    @app.get("/api/health")
    def health():
        return jsonify({"status": "ok", "events_applied": len(event_log)})

    @app.get("/api/topology")
    def topology():
        nodes = []
        for nid in state.topology()["nodes"]:
            info = state.node_info(nid)
            nodes.append(info)
        return jsonify({"nodes": nodes, "edges": state.link_states()})

    @app.get("/api/nodes")
    def nodes():
        return jsonify({"nodes": [state.node_info(n) for n in state.topology()["nodes"]]})

    @app.get("/api/nodes/<node_id>")
    def node(node_id):
        info = state.node_info(node_id)
        if info is None: return error("Unknown node", 404)
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
        if info is None: return error("Route not found", 404)
        return jsonify(info)

    @app.get("/api/packets")
    def packets():
        try: limit = valid_int_arg("limit", 50, 1)
        except ValueError as exc: return error(exc, 400)
        ids = list(state._packets)[-limit:]
        return jsonify({"packets": [{"packet_id": pid, "path": state.packet_paths()[pid], "status": state.packet_status(pid)} for pid in ids]})

    @app.get("/api/packets/<packet_id>")
    def packet(packet_id):
        if packet_id not in state._packets: return error("Unknown packet", 404)
        return jsonify({"packet_id": packet_id, "path": state.packet_paths()[packet_id], "status": state.packet_status(packet_id)})

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
        except ValueError as exc: return error(exc, 400)
        order = (request.args.get("order") or "asc").lower()
        filtered = [x for x in event_log if x["seq"] > since]
        if order == "desc":
            selected = filtered[-limit:][::-1]
        else:
            selected = filtered[:limit]
        return jsonify({"events": selected, "last_seq": len(event_log)})

    @app.get("/api/warnings")
    def warnings():
        return jsonify({"warnings": state.warnings, "ignored": len(state.ignored_events)})

    @app.get("/api/render.png")
    def render():
        from dashboard import render_png
        # render_png accepts a path; use a temporary file and read its bytes.
        import tempfile, os
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            path = tmp.name
        try:
            render_png(state, path)
            with open(path, "rb") as image_file: data = image_file.read()
        finally:
            try: os.unlink(path)
            except OSError: pass
        return Response(data, mimetype="image/png")

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
                ok = _apply_and_log(event)
                if ok:
                    applied += 1
                    if heartbeat_monitor is not None:
                        generated = heartbeat_monitor.on_event(event)
                        emit_events(generated)
                else:
                    ignored += 1
        return jsonify({"applied": applied, "ignored": ignored})

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
                route_info = state.current_route_info(node_id)
                return jsonify({"status": "ok", "node_id": node_id, "agent": agent_info, "route": route_info})
            state._set_node(node_id, "online")
            return jsonify({"status": "ok", "node_id": node_id, "route": state.current_route_info(node_id)})

    @app.get("/api/frames")
    def frames():
        with lock:
            return jsonify({"frames": dict(latest_frames)})

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
            route_info = None
            if heartbeat_monitor is not None:
                payload = {
                    "node_id": node_id,
                    "hostname": body.get("hostname") or "browser-cam",
                    "os": body.get("os") or "Browser",
                    "ip": request.remote_addr,
                }
                generated, _ = heartbeat_monitor.record_heartbeat(payload)
                emit_events(generated)
                route_info = state.current_route_info(node_id)
            else:
                state._set_node(node_id, "online")
                route_info = state.current_route_info(node_id)
        return jsonify({"status": "ok", "node_id": node_id, "route": route_info})

    @app.post("/api/reset")
    def reset():
        with lock:
            state.reset(); event_log.clear(); latest_frames.clear()
            if heartbeat_monitor is not None:
                heartbeat_monitor.reset()
        return jsonify({"status": "reset"})

    return app
