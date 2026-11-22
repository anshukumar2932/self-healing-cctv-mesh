"""P4 - Local packet storage for the self-healing CCTV mesh.

When a camera has no verified route to the Server it keeps its packets in a
local buffer and drains them once a route is back.  One FIFO buffer per node,
shared counters for delivered / lost packets.

Owner: P4 (Failure + Storage Engineer).  Checked by STO-001 .. STO-012.
"""

from collections import deque


class StorageManager:
    """Per-node FIFO packet buffers with a fixed capacity.

    capacity   -- maximum number of packets each node may hold (> 0)
    delivered  -- packets successfully handed to send_fn across all nodes
    lost       -- packets dropped because a buffer was full
    """

    def __init__(self, capacity):
        if capacity is None or capacity <= 0:
            raise ValueError(f"capacity must be > 0, got {capacity!r}")
        self.capacity = int(capacity)
        self.delivered = 0
        self.lost = 0
        self._buffers = {}

    # ------------------------------------------------------------------ helpers
    def _buffer(self, node_id):
        return self._buffers.setdefault(node_id, deque())

    # ------------------------------------------------------------------ API
    def store(self, node_id, packet, now):
        """Buffer one packet at node_id.

        Returns {"stored": True, "event": None} on success, or
        {"stored": False, "event": packet_lost_event} when the buffer is full.
        """
        buf = self._buffer(node_id)
        if len(buf) >= self.capacity:
            self.lost += 1
            event = {
                "event_type": "packet_lost",
                "timestamp": now,
                "packet_id": packet.get("packet_id"),
                "source": packet.get("source"),
                "current_node": node_id,
                "reason": "buffer_full",
            }
            return {"stored": False, "event": event}
        buf.append(packet)
        return {"stored": True, "event": None}

    def count(self, node_id):
        """Number of packets waiting at node_id."""
        return len(self._buffer(node_id))

    def buffer_available(self, node_id):
        """Percent of the node's buffer that is free, 0.0 .. 100.0."""
        free = self.capacity - len(self._buffer(node_id))
        free = max(0, min(self.capacity, free))
        return 100.0 * free / self.capacity

    def drain(self, node_id, send_fn, now):
        """Send buffered packets oldest-first until send_fn returns False.

        The packet that failed to send stays at the head of the queue so the
        order is preserved for the next drain.  Returns a buffer_drained event.
        """
        buf = self._buffer(node_id)
        sent_ids = []
        while buf:
            packet = buf[0]
            if not send_fn(packet):
                break
            buf.popleft()
            self.delivered += 1
            sent_ids.append(packet.get("packet_id"))
        return {
            "event_type": "buffer_drained",
            "timestamp": now,
            "node_id": node_id,
            "count": len(sent_ids),
            "packet_ids": sent_ids,
        }

    def buffered(self):
        """Total packets waiting across all nodes."""
        return sum(len(b) for b in self._buffers.values())
