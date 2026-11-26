"""Server failover: any laptop running server.py can become the active server.

Every laptop runs the same program and lists the OTHER laptops with --peers.  Once a
second each node asks its peers "GET /api/role".  A peer that has not answered for
`fail_after` seconds is dead.  Among the live nodes (including this one) the one with
the lowest (priority, id) is the active server; everyone else is a standby that keeps
a copy of the active server's events.  When the active server dies, the next standby
promotes itself, adopts the replicated state and re-plans routes.  When a better node
comes back it first copies the state from the current active server, then takes over.

Limit: this is a simple LAN election, not Raft.  If the network is split in two, each
side may elect its own active server until the split heals.
"""

import json
import ssl
import threading
import time
import urllib.error
import urllib.request


def http_get_json(url, timeout=0.8):
    """GET url and return parsed JSON, or None on any failure (used for peer polling)."""
    ctx = ssl._create_unverified_context() if url.startswith("https") else None  # self-signed LAN certs
    try:
        with urllib.request.urlopen(url, timeout=timeout, context=ctx) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


class Cluster:
    def __init__(self, node_id, url, peers=(), priority=100, interval=1.0, fail_after=3.0,
                 fetch=http_get_json, sync=None, on_promote=None, on_demote=None):
        self.id = node_id
        self.url = url.rstrip("/")
        self.priority = int(priority)
        self.interval = float(interval)
        self.fail_after = float(fail_after)
        self.fetch = fetch
        self.sync = sync or (lambda _url: True)
        self.on_promote = on_promote or (lambda: None)
        self.on_demote = on_demote or (lambda: None)
        self.peers = {
            u.rstrip("/"): {"id": u.rstrip("/"), "url": u.rstrip("/"), "priority": 100,
                            "role": "unknown", "last_ok": 0.0, "alive": False}
            for u in peers if u.strip() and u.rstrip("/") != self.url
        }
        self.role = "standby"
        self.crashed = False
        self.history = []
        self._lock = threading.RLock()
        self._stop = threading.Event()

    # ------------------------------------------------------------------ views
    def _log(self, kind, detail):
        self.history.append({"t": time.time(), "event": kind, "detail": detail})
        del self.history[:-30]

    def active_url(self):
        with self._lock:
            if self.role == "active":
                return self.url
            for p in self.peers.values():
                if p["alive"] and p["role"] == "active":
                    return p["url"]
            return None

    def info(self):
        with self._lock:
            active = self.active_url()
            peers = [{k: p[k] for k in ("id", "url", "priority", "role", "alive")}
                     for p in self.peers.values()]
            return {
                "role": self.role, "crashed": self.crashed, "active_url": active,
                "me": {"id": self.id, "url": self.url, "priority": self.priority},
                "peers": peers, "history": list(self.history),
            }

    # ------------------------------------------------------------------ election
    def _poll(self, now):
        for p in self.peers.values():
            data = self.fetch(p["url"] + "/api/role")
            was_alive = p["alive"]
            if isinstance(data, dict) and isinstance(data.get("me"), dict) and not data.get("crashed"):
                p.update(id=data["me"].get("id", p["id"]), priority=int(data["me"].get("priority", 100)),
                         role=data.get("role", "standby"), last_ok=now)
            p["alive"] = now - p["last_ok"] <= self.fail_after
            if was_alive and not p["alive"]:
                p["role"] = "unknown"
                self._log("peer_down", p["id"])
            elif p["alive"] and not was_alive:
                self._log("peer_up", p["id"])

    def winner(self):
        """Lowest (priority, id) among this node and the live peers."""
        pool = [(self.priority, self.id, None)]
        pool += [(p["priority"], p["id"], p) for p in self.peers.values() if p["alive"]]
        return min(pool, key=lambda t: (t[0], t[1]))[2]  # None means "me"

    def _active_peer(self):
        return next((p for p in self.peers.values() if p["alive"] and p["role"] == "active"), None)

    def tick(self, now=None):
        """One election round: poll peers, then promote, demote or follow."""
        if self.crashed:
            return
        now = now or time.time()
        with self._lock:
            self._poll(now)
            best = self.winner()
            active_peer = self._active_peer()
            if best is None:  # this node should be the active server
                if self.role != "active":
                    if active_peer:
                        self.sync(active_peer["url"])  # copy state before taking over
                    self.role = "active"
                    self._log("promoted", f"{self.id} is now the active server")
                    self.on_promote()
            else:
                if self.role == "active" and best["role"] == "active":
                    self.role = "standby"
                    self._log("demoted", f"{best['id']} took over")
                    self.on_demote()
                if self.role == "standby" and active_peer:
                    self.sync(active_peer["url"])

    # ------------------------------------------------------------------ simulated crash (testing tab)
    def crash(self):
        with self._lock:
            self.crashed = True
            if self.role == "active":
                self.on_demote()
            self.role = "standby"
            self._log("crashed", f"{self.id} (simulated)")

    def revive(self):
        with self._lock:
            self.crashed = False
            self.role = "standby"
            for p in self.peers.values():
                p["last_ok"], p["alive"], p["role"] = 0.0, False, "unknown"
            self._log("revived", self.id)

    # ------------------------------------------------------------------ thread
    def start(self):
        def loop():
            while not self._stop.is_set():
                self.tick()
                self._stop.wait(self.interval)

        threading.Thread(target=loop, daemon=True).start()

    def stop(self):
        self._stop.set()
