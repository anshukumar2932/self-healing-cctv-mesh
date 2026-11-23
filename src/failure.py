"""P4 - Failure detection and failure injection for the CCTV mesh.

Nodes are dicts (handbook 3.2) with at least "id", "online", "load",
"last_heartbeat".  Links are dicts (handbook 3.3) with "source",
"destination", "quality", "packet_loss", "active".  All functions mutate the
dicts in place and return event dicts.

Owner: P4 (Failure + Storage Engineer).  Checked by FAIL-001 .. FAIL-017.
"""

DEFAULT_CONFIG = {
    "warning_threshold": 0.60,  # load >= this  -> WARNING
    "congestion_threshold": 0.80,  # load >= this  -> CONGESTED
    "packet_loss_threshold": 0.50,  # packet_loss > this -> link inactive
    "quality_threshold": 0.30,  # quality < this     -> link inactive
    "heartbeat_timeout_s": 2.0,  # silence > this     -> node offline
}

FAILURE_TYPES = ("relay_node", "source_camera", "link", "congestion")


def _cfg(config):
    merged = dict(DEFAULT_CONFIG)
    if config:
        merged.update(config)
    return merged


def _find_node(nodes, node_id):
    for node in nodes:
        if node.get("id") == node_id:
            return node
    return None


def _find_link(links, element):
    """element is 'A-B'; direction does not matter."""
    if "-" not in element:
        return None
    a, b = element.split("-", 1)
    wanted = frozenset((a, b))
    for link in links:
        if frozenset((link.get("source"), link.get("destination"))) == wanted:
            return link
    return None


def _event(event_type, timestamp, failure_type, failed_element, **extra):
    event = {
        "event_type": event_type,
        "timestamp": timestamp,
        "failure_type": failure_type,
        "failed_element": failed_element,
    }
    event.update(extra)
    return event


# --------------------------------------------------------------------------
# Load classification
# --------------------------------------------------------------------------
def classify_load(load, config=None):
    """Return "NORMAL", "WARNING" or "CONGESTED" for a node load in 0..1."""
    cfg = _cfg(config)
    if load >= cfg["congestion_threshold"]:
        return "CONGESTED"
    if load >= cfg["warning_threshold"]:
        return "WARNING"
    return "NORMAL"


# --------------------------------------------------------------------------
# Link health
# --------------------------------------------------------------------------
def evaluate_link(link, config=None):
    """Deactivate a link whose packet loss or quality is out of bounds.

    A link that is already inactive is never re-activated here.
    Returns the link's (possibly updated) active flag.
    """
    cfg = _cfg(config)
    if (
        link.get("packet_loss", 0.0) > cfg["packet_loss_threshold"]
        or link.get("quality", 1.0) < cfg["quality_threshold"]
    ):
        link["active"] = False
    return bool(link.get("active", True))


# --------------------------------------------------------------------------
# Heartbeats
# --------------------------------------------------------------------------
def record_heartbeat(node, t):
    """Remember that node was alive at simulation time t."""
    node["last_heartbeat"] = t
    return node


def check_heartbeats(nodes, now, config=None):
    """Mark online nodes that have been silent too long as offline.

    A node is failed when now - last_heartbeat is strictly greater than
    heartbeat_timeout_s.  Each node is reported once: nodes already offline
    are skipped, so repeated calls never duplicate an event.
    """
    cfg = _cfg(config)
    timeout = cfg["heartbeat_timeout_s"]
    events = []
    for node in nodes:
        if not node.get("online", False):
            continue
        last = node.get("last_heartbeat", 0.0)
        if now - last > timeout:
            node["online"] = False
            ftype = "source_camera" if node.get("role") == "source" else "relay_node"
            events.append(
                _event(
                    "failure_detected",
                    now,
                    ftype,
                    node["id"],
                    last_heartbeat=last,
                    detected_by="heartbeat",
                )
            )
    return events


# --------------------------------------------------------------------------
# Injection and restoration
# --------------------------------------------------------------------------
def inject_failure(nodes, links, failure):
    """Apply a scripted failure to the topology and return its event.

    failure = {"failure_type": relay_node | source_camera | link | congestion,
               "failed_element": "CCTV3" or "CCTV1-CCTV3",
               "failure_time": 20.0,
               "load": 0.85}            # congestion only
    Raises ValueError for an unknown failure_type or element.
    """
    ftype = failure.get("failure_type")
    element = failure.get("failed_element")
    when = failure.get("failure_time", failure.get("timestamp", 0.0))

    if ftype not in FAILURE_TYPES:
        raise ValueError(f"unknown failure_type {ftype!r}; expected one of {FAILURE_TYPES}")
    if not element:
        raise ValueError("failed_element is required")

    if ftype in ("relay_node", "source_camera"):
        node = _find_node(nodes, element)
        if node is None:
            raise ValueError(f"unknown node {element!r}")
        node["online"] = False
        return _event("failure_detected", when, ftype, element)

    if ftype == "link":
        link = _find_link(links, element)
        if link is None:
            raise ValueError(f"unknown link {element!r}")
        link["active"] = False
        return _event("failure_detected", when, ftype, element)

    # congestion: the node is penalised, never taken offline
    node = _find_node(nodes, element)
    if node is None:
        raise ValueError(f"unknown node {element!r}")
    load = failure.get("load")
    if load is None:
        raise ValueError("congestion failure needs a 'load' value")
    node["load"] = load
    return _event("failure_detected", when, ftype, element, load=load, state=classify_load(load))


def restore_element(nodes, links, element, now):
    """Bring a node or a link back into service and return a recovery event."""
    link = _find_link(links, element)
    if link is not None:
        link["active"] = True
        return {
            "event_type": "recovery_complete",
            "timestamp": now,
            "failed_element": element,
            "element_type": "link",
        }
    node = _find_node(nodes, element)
    if node is None:
        raise ValueError(f"unknown element {element!r}")
    node["online"] = True
    node["last_heartbeat"] = now
    return {
        "event_type": "recovery_complete",
        "timestamp": now,
        "failed_element": element,
        "element_type": "node",
    }
