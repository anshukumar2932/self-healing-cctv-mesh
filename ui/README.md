<<<<<<< HEAD
P6 Dashboard files
==================

Files
- dashboard.py: DashboardState, standard-event processing, metrics, PNG renderer.
- api.py: Flask API implementing the routes in testcase_dashboard.py plus CORS & /api/heartbeat.
- server.py: Variable-host server launcher (run on whichever Windows/macOS machine acts as the main server).
- camera_agent.py: Cross-platform Windows/macOS camera client (stdlib only, no extra installs).
- index.html: Live web dashboard with variable server URL switcher, route highlighting, and agent status.

Install dependencies on the main server machine:
    python -m pip install matplotlib flask

1. Start the Variable Main Server (on whichever laptop/PC is acting as the server):
    python server.py --host 0.0.0.0 --port 5050
   (The startup banner prints the LAN URL, e.g. http://192.168.1.42:5050)
   Optional: add --wait-for-agents to start CCTV1-CCTV6 offline until remote devices connect.

2. Open the Dashboard on ANY device (browser on Windows, macOS, etc.):
    Open http://<SERVER_IP>:5050/
    (Or switch servers anytime using the URL box in the dashboard header.)

3. Connect 2+ Windows or macOS devices as live cameras (no pip install needed):
    python camera_agent.py --server http://<SERVER_IP>:5050 --node CCTV1
    python camera_agent.py --server http://<SERVER_IP>:5050 --node CCTV2
   In the agent terminal you can type: status | burst 5 | fail | recover | quit
   Pressing Ctrl+C simulates an unexpected camera disconnect; the server detects the
   2.0s heartbeat timeout, logs failure_detected, and reroutes affected cameras.

Run the supplied P6 test suite from the project root:
    python testcase_dashboard.py ui/
    python testcase_dashboard.py ui/ DASH
    python testcase_dashboard.py ui/ API
=======
# Dashboard (`ui/`)

Three tabs: **CCTV View** (topology, node status), **Cameras** (connect laptop cameras, live feeds)
and **Failure Testing** (fail or recover any node or link, server failover, latest failure, routes, events).

## Connecting laptop cameras

Each laptop is one camera slot (CCTV1 to CCTV6, so up to six laptops). Open the **Cameras** tab, press
**Copy link** next to a free slot, open that link on the laptop and press Start Streaming. The link
already lists every known server, so the camera keeps working after a failover. Use `--https` on the
servers so remote laptops are allowed to use the webcam.

| File | Purpose |
|---|---|
| `dashboard.py` | `DashboardState`: consumes standard events, metrics, PNG renderer |
| `api.py` | Flask API (`create_app(state, heartbeat_monitor=None, cluster=None)`) |
| `mesh.py` | `MeshCoordinator`: heartbeats, failure detection, recovery via `src/failure.py` and `src/recovery.py` |
| `cluster.py` | Failover: any laptop can be the server |
| `server.py` | Launcher |
| `camera_agent.py` / `camera.html` | Camera client (terminal / browser webcam) |
| `index.html` | The two-tab dashboard |

`mesh.route_for` is a stand-in router (fewest hops, best link quality). Replace it with
`src/routing.py` when that exists.

## Run one server

    pip install -r requirements.txt
    python ui/server.py --port 5050
    python ui/camera_agent.py --server http://<SERVER_IP>:5050 --node CCTV1

Open `http://<SERVER_IP>:5050/`. Webcams in remote browsers need `--https`.

## Run several servers (failover)

Run on every laptop; each lists the **other** laptops. Lowest priority number is preferred.

    laptop A: python ui/server.py --id A --priority 1 --peers http://B:5050,http://C:5050
    laptop B: python ui/server.py --id B --priority 2 --peers http://A:5050,http://C:5050
    laptop C: python ui/server.py --id C --priority 3 --peers http://A:5050,http://B:5050

Point agents and cameras at all servers so they follow the active one:

    python ui/camera_agent.py --server http://A:5050,http://B:5050,http://C:5050 --node CCTV1
    # browser camera: /camera?node=CCTV1&server=http://A:5050,http://B:5050

How it works:

- Each server polls its peers every second. A peer silent for 3 s (`--election-timeout`) is dead.
- The live server with the lowest `(priority, id)` is **active**. The others are **standby**: they
  copy the active server's events (`GET /api/replicate`) and answer reads, but refuse writes (HTTP 409
  with `active_url`).
- When the active server dies, the next one promotes itself, adopts the copied state, gives known
  agents a fresh heartbeat window and re-plans routes. Agents and the dashboard switch servers by themselves.
- When a better server comes back it copies the state from the current active server first, then takes over.
- Limit: a simple LAN election, not Raft. If the network splits, each side may elect its own active server.
- Live video frames are not copied; cameras re-send them to the new server.

Test it from the Failure Testing tab ("Crash this server (simulate)", then "Revive (sim)"),
or stop `server.py` with Ctrl+C.

## Failure testing routes

| Route | Meaning |
|---|---|
| `POST /api/inject` `{"element": "CCTV3"}` or `"CCTV1-CCTV2"` | fail a node or link and run recovery |
| `POST /api/recover` `{"element": ...}` | restore it |
| `GET /api/frames/meta`, `GET /api/frame/<node>` | camera frame timestamps and the latest JPEG |
| `GET /api/role` | this server's role, peers, history |
| `GET /api/replicate?since=N&epoch=E` | events for standbys |
| `POST /api/admin/crash`, `/api/admin/revive` | simulate a server crash |
>>>>>>> 29049ca (Improved Dashboard with different TABS for routes and camera)
