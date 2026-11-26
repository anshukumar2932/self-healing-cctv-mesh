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
