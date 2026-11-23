"""P4 - Recovery engine: reroute traffic after a failure.

The engine does not compute routes itself.  P2's router is injected as a
callable, together with a verify callable that reports whether the new
route answered with an ACK in time.

State machine on success:
    NORMAL -> DETECT -> ANALYZE -> FIND_ALT -> SCORE -> SWITCH -> VERIFY -> NORMAL
A failed verification loops back to FIND_ALT with that route excluded.  When
no verified route is left the engine reports recovery_failed with fallback
"store_locally" so the source buffers packets (see storage.py).

Owner: P4 (Failure + Storage Engineer).  Checked by REC-001 .. REC-008.
"""

import itertools
import time

DEFAULT_CONFIG = {
    "ack_timeout_s": 1.0,  # cost of every verification that fails
    "control_message_delay_ms": 10,  # one-way delay of a control message
    "max_allowed_hops": 6,
    "max_attempts": 50,  # safety stop for the retry loop
}

FALLBACK_STORE = "store_locally"


def _route_edges(route):
    return {frozenset(pair) for pair in itertools.pairwise(route)}


def _route_affected(failure, route):
    """Does the failure touch the route the source is currently using?"""
    if not route:
        return True
    ftype = failure.get("failure_type")
    element = failure.get("failed_element", "")
    if ftype == "link":
        if "-" not in element:
            return False
        a, b = element.split("-", 1)
        return frozenset((a, b)) in _route_edges(route)
    return element in route


class RecoveryEngine:
    """Reroute a source after a failure, verifying the new path before use."""

    def __init__(self, router, verify, config=None):
        self.router = router
        self.verify = verify
        self.config = dict(DEFAULT_CONFIG)
        if config:
            self.config.update(config)
        self.route_changes = 0
        self.history = []

    # ------------------------------------------------------------------ helpers
    def _result(self, failure, now, source, current_route, packets_lost):
        return {
            "event_type": "recovery_failed",
            "timestamp": now,
            "failure_type": failure.get("failure_type"),
            "failed_element": failure.get("failed_element"),
            "source": source,
            "old_route": list(current_route) if current_route else [],
            "new_route": None,
            "route_verified": False,
            "recovery_time_ms": 0.0,
            "route_computation_time_ms": 0.0,
            "control_messages_sent": 0,
            "packets_lost": packets_lost,
            "state_log": ["NORMAL", "DETECT", "ANALYZE"],
            "fallback": None,
            "events": [],
        }

    def _finish(self, res):
        self.history.append(res)
        return res

    # ------------------------------------------------------------------ API
    def recover(self, failure, nodes, links, source, current_route, now=None, packets_lost=0):
        """Handle one failure for one source camera.

        failure        -- failure_detected event (or the raw failure dict)
        nodes, links   -- current topology, already mutated by failure.py
        source         -- id of the camera whose traffic we protect
        current_route  -- list of node ids the source is using right now
        Returns the recovery result dict described in the module docstring.
        """
        cfg = self.config
        if now is None:
            now = failure.get("timestamp", failure.get("failure_time", 0.0))
        res = self._result(failure, now, source, current_route, packets_lost)
        log = res["state_log"]
        events = res["events"]

        # --- ANALYZE: is there anything to do? --------------------------------
        source_node = next((n for n in nodes if n.get("id") == source), None)
        source_failed = (
            failure.get("failed_element") == source
            and failure.get("failure_type") in ("source_camera", "relay_node")
        ) or (source_node is not None and not source_node.get("online", True))

        if source_failed:
            # Nothing can be rerouted when the camera itself is gone.
            res["fallback"] = FALLBACK_STORE
            res["recovery_time_ms"] = float(cfg["control_message_delay_ms"])
            res["control_messages_sent"] = 1
            events.append(
                {
                    "event_type": "buffering_started",
                    "timestamp": now,
                    "node_id": source,
                    "reason": "source_offline",
                }
            )
            return self._finish(res)

        if not _route_affected(failure, current_route):
            # Current route still works: no action, no router call.
            res["event_type"] = "recovery_complete"
            res["new_route"] = list(current_route)
            res["route_verified"] = True
            log.append("NORMAL")
            return self._finish(res)

        # --- FIND_ALT / SCORE / SWITCH / VERIFY loop --------------------------
        destination = current_route[-1] if current_route else "Server"
        excluded = []
        failed_verifications = 0
        comp_ms = 0.0
        control = 0
        verified_route = None

        for _ in range(cfg["max_attempts"]):
            log.append("FIND_ALT")
            routing_input = {
                "source": source,
                "destination": destination,
                "nodes": nodes,
                "links": links,
                "max_allowed_hops": cfg["max_allowed_hops"],
                "excluded_routes": [list(r) for r in excluded],
                "failure": failure,
                "timestamp": now,
            }
            t0 = time.perf_counter()
            routing = self.router(routing_input) or {}
            comp_ms += (time.perf_counter() - t0) * 1000.0
            control += 1  # route request to the routing service

            candidate = routing.get("selected_route")
            if not candidate or list(candidate) in excluded:
                events.append(
                    {
                        "event_type": "no_route",
                        "timestamp": now,
                        "source": source,
                        "reason": routing.get("reason", "no_route"),
                    }
                )
                break
            candidate = list(candidate)

            log.append("SCORE")
            log.append("SWITCH")
            log.append("VERIFY")
            control += 1  # verification probe along the candidate
            if self.verify(candidate):
                verified_route = candidate
                break
            failed_verifications += 1
            excluded.append(candidate)
            events.append(
                {
                    "event_type": "route_rejected",
                    "timestamp": now,
                    "route": candidate,
                    "reason": "no_ack",
                }
            )

        res["route_computation_time_ms"] = comp_ms
        res["control_messages_sent"] = max(control, 1)
        res["recovery_time_ms"] = (
            comp_ms
            + cfg["control_message_delay_ms"]
            + failed_verifications * cfg["ack_timeout_s"] * 1000.0
        )

        if verified_route is None:
            res["fallback"] = FALLBACK_STORE
            events.append(
                {
                    "event_type": "buffering_started",
                    "timestamp": now,
                    "node_id": source,
                    "reason": "no_verified_route",
                }
            )
            return self._finish(res)

        res["event_type"] = "recovery_complete"
        res["new_route"] = verified_route
        res["route_verified"] = True
        if verified_route != list(current_route or []):
            self.route_changes += 1
            events.append(
                {
                    "event_type": "route_changed",
                    "timestamp": now,
                    "source": source,
                    "old_route": list(current_route or []),
                    "new_route": verified_route,
                }
            )
        events.append(
            {
                "event_type": "route_verified",
                "timestamp": now,
                "source": source,
                "route": verified_route,
            }
        )
        log.append("NORMAL")
        return self._finish(res)
