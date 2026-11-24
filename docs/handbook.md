# Self-Healing CCTV Mesh Network — Team Handbook

*Simulation and Evaluation of a Self-Healing CCTV Mesh Network Using Multi-Factor Routing*
Based on Project Documentation Rev. 2 · 6 members · 8 weeks · Python, NetworkX, SimPy, Matplotlib

---

## 1. Purpose and Ground Rules

This handbook is the single reference for who owns what, what each module must accept and return, and when a task counts as finished. Rev. 2 is the design authority; this handbook turns it into working agreements.

**Rules**

1. One repository, one integrated simulator. No isolated versions.
2. No one invents their own data format. Everything follows the contracts in Section 3 and lives in `src/schemas.py`.
3. Availability is always `node["online"]` and `link["active"]`. Never `status`.
4. Randomness uses a fixed seed (`42`) so results are reproducible.
5. A module is done only when its fixed test cases pass (Section 6).
6. Merge to `dev` continuously; integrate into `main` every Friday and run the full system end to end.

---

## 2. Roles and Ownership

| ID | Role | Owns | Deliverables |
| --- | --- | --- | --- |
| P1 | Project Manager + Integration | Schemas, interfaces, integration, Git, acceptance | `schemas.py`, `config.py`, integrated system |
| P2 | Routing Engineer | Path generation, scoring, verification | `routing.py` |
| P3 | Simulation Engineer | Topology, nodes, links, packets, SimPy loop | `topology.py`, `nodes.py`, `links.py`, `simulation.py` |
| P4 | Failure + Storage Engineer | Failure injection/detection, recovery, buffering | `failure.py`, `recovery.py`, `storage.py` |
| P5 | QA + Documentation | Test matrix, experiments, results, report | `docs/`, `experiments/`, `results/` |
| P6 | Dashboard Engineer | Visualization and metrics display | `ui/dashboard.py` |

**Note:** Rev. 2 §4 lists a different split (P5 = CCTV/Storage, P6 = UI + Testing + Docs). This handbook supersedes it; update §4 of the main document to match.

**Dependency flow:** P2, P3, P4 build the core simulator. P1 integrates. P5 and P6 consume its outputs and events.

---

## 3. Data Contracts (frozen)

### 3.1 Topology (Rev. 2 §3.1)

Nodes: `CCTV1`–`CCTV6`, `Gateway`, `Server`.
Edges: CCTV1–CCTV2, CCTV1–CCTV3, CCTV2–CCTV3, CCTV3–CCTV4, CCTV2–CCTV5, CCTV3–CCTV6, CCTV5–CCTV6, CCTV2–Gateway, CCTV3–Gateway, CCTV4–Gateway, Gateway–Server.
Only these edges may appear in examples, tests and the dashboard.

### 3.2 Node

| Field | Type | Range | Meaning |
| --- | --- | --- | --- |
| id | str | — | e.g. `CCTV1` |
| online | bool | — | availability |
| battery | float | 0–100 | percent |
| buffer_available | float | 0–100 | free store-and-forward buffer, % |
| reliability | float | 0–1 |  |
| load | float | 0–1 |  |
| role | enum | source / relay |  |

### 3.3 Link

| Field | Type | Range |
| --- | --- | --- |
| source, destination | str | valid node IDs |
| quality | float | 0–1 |
| bandwidth | float | > 0 |
| latency | float | ≥ 0 |
| packet_loss | float | 0–1 |
| active | bool | — |

### 3.4 Route (output of routing)

```python
route = {
  "selected_route": ["CCTV1", "CCTV2", "Gateway", "Server"],
  "score": 0.68,
  "hops": 3,
  "link_quality": 0.80,        # MIN over links
  "reliability": 0.90,         # MIN over nodes
  "load": 0.80,                # MAX over nodes/links
  "hop_score": 0.50,           # 1 - hops / max_allowed_hops
  "buffer_availability": 0.70,
  "candidate_routes": [],
  "route_verified": False      # True only after test packet is ACKed
}
```

**Score formula (initial weights, to be tuned):**
`0.35·LinkQuality + 0.25·Reliability + 0.15·HopScore + 0.15·(1 − Load) + 0.10·Buffer`

### 3.5 Standard event (everything the dashboard consumes)

Every event carries `event_type` and `timestamp`. Allowed types:
`packet_sent`, `packet_received`, `packet_lost`, `failure_detected`, `route_changed`, `route_verified`, `recovery_complete`, `buffer_drained`, `metric_snapshot`.

```python
failure_detected = {
  "event_type": "failure_detected",
  "timestamp": 22.0,
  "failure_type": "relay_node",   # relay_node | source_camera | link | congestion
  "failed_element": "CCTV3",
  "old_route": ["CCTV1", "CCTV3", "Gateway", "Server"],
  "new_route": ["CCTV1", "CCTV2", "Gateway", "Server"],
  "recovery_time_ms": 3500,
  "packets_lost": 4,
  "route_computation_time_ms": 8,
  "control_messages_sent": 6
}
```

### 3.6 Metric snapshot

`packets_sent, packets_delivered, packets_lost, delivery_rate, average_hops, route_changes, network_load, route_computation_time_ms, control_messages`

---

## 4. Module Contracts

| Module | Input | Output | Must guarantee |
| --- | --- | --- | --- |
| routing (P2) | source, destination, nodes, links, max_allowed_hops, weights | Route object (3.4) | Simple paths only (`all_simple_paths`); offline nodes and inactive links excluded; never returns only a path |
| simulation (P3) | nodes, links, simulation_time, packet_rate, packet_size, seed | Ordered stream of standard events | Deterministic for a given seed; calls routing only on setup or when a problem is reported |
| failure (P4) | failure_event (type, element, time) | `failure_detected` event; updated `online`/`active` | Heartbeat timeout marks `online=False`; link threshold marks `active=False` |
| recovery (P4) | failure_detected | `route_changed`, `route_verified` or fallback | Verify new route with test packet and ACK; on failure try next-best route |
| storage (P4) | packet, node state | buffered / drained events | Source failure: stop new packets, keep captured data, drain after recovery; no route: store locally, retry |
| dashboard (P6) | event stream + metric snapshots | Views only | Reads standard events; no reaching into simulator internals |

**Event-driven recalculation:** establish a route once, stream packets, recalculate only on a reported problem (Rev. 2 §8.7).

---

## 5. Configuration (no hard-coding)

```python
CONFIG = {
  "seed": 42, "node_count": 6, "max_allowed_hops": 6,
  "weights": {"link_quality": .35, "reliability": .25, "hop": .15, "load": .15, "buffer": .10},
  "packet_loss_threshold": 0.50,   # sweep 0.30 / 0.50 / 0.70
  "link_quality_threshold": 0.30,  # sweep 0.2 / 0.3 / 0.4
  "congestion_threshold": 0.80,    # sweep 0.70 / 0.80 / 0.90
  "warning_threshold": 0.60,
  "heartbeat_timeout_s": 2.0
}
```

Thresholds and weights are experimental starting points. State this in the report and justify final values with sensitivity experiments.

---

## 6. Fixed Test Cases

Format: Input → Expected → Pass condition.

### Routing

| ID | Input | Expected | Pass if |
| --- | --- | --- | --- |
| RT-001 | Route A (Q .80, R .90, hop .50, load .80, buffer .70) vs a lower-scoring Route B | A selected, score 0.68 | route = A and score = 0.68 ± 0.001 |
| RT-002 | CCTV2 `online=False` | CCTV2 absent from every candidate | not in `selected_route` or candidates |
| RT-003 | Link CCTV1–CCTV2 `active=False` | No route uses that link | edge absent |
| RT-004 | Candidate CCTV1→CCTV2→CCTV3→CCTV2→Gateway→Server | Rejected | repeated-node path never generated |
| RT-005 | Path with link qualities .95, .90, .30 | link_quality = .30 | MIN, not average |
| RT-006 | 2 hops, max_allowed_hops 4 | hop_score = 0.50 | formula exact |

*Check for RT-001:* 0.28 + 0.225 + 0.075 + 0.03 + 0.07 = 0.68.

### Simulation

| ID | Input | Expected |
| --- | --- | --- |
| SIM-001 | 6 nodes, no failure, 10 packets, seed 42 | 10 generated, 10 delivered, 0 lost |
| SIM-002 | CCTV3 fails at 20 s | CCTV3 offline, new route generated, packets continue via alternate |
| SIM-003 | Same config run twice, seed 42 | identical event streams |

### Failure and storage

| ID | Input | Expected |
| --- | --- | --- |
| FAIL-001 | relay_node CCTV3 | offline; CCTV1→CCTV3→Gateway→Server becomes CCTV1→CCTV2→Gateway→Server |
| FAIL-002 | source_camera CCTV1 | no new packets; captured data buffered; drained after recovery |
| FAIL-003 | link CCTV1–CCTV3 | `active=False`; no route uses it |
| FAIL-004 | congestion, load 0.85 | state CONGESTED; route penalized, not excluded |
| FAIL-005 | New route's test packet gets no ACK | route marked failed; next-best tried |
| FAIL-006 | Gateway unreachable | data stored locally; upload after reconnection |

### Dashboard

| ID | Input | Expected |
| --- | --- | --- |
| DASH-001 | Mock `failure_detected` JSON | failed node shown, old/new route, recovery time |
| DASH-002 | Metric snapshot | all metrics in 3.6 displayed |

P6 builds against a mock JSON file of these events from Week 1, before the backend exists.

---

## 7. Definition of Done

A task is done only when every box is ticked:

- [ ] Code implemented on a feature branch
- [ ] Inputs and outputs documented, matching `schemas.py`
- [ ] Unit tests written; fixed test cases pass
- [ ] Error handling tested (bad input, missing node, no route)
- [ ] Pull request reviewed by P1
- [ ] Integration test passed on `dev`
- [ ] Docs updated by the owner, checked by P5

---

## 8. Master Test Matrix (P5 maintains)

| Test ID | Module | Owner | Expected | Actual | Status | Date |
| --- | --- | --- | --- | --- | --- | --- |
| RT-001 | Routing | P2 | Route A, 0.68 |  | ⬜ |  |
| RT-002 | Routing | P2 | Exclude CCTV2 |  | ⬜ |  |
| SIM-001 | Simulation | P3 | 10/10 delivered |  | ⬜ |  |
| FAIL-001 | Failure | P4 | Alternate route |  | ⬜ |  |
| FAIL-002 | Storage | P4 | Buffer then drain |  | ⬜ |  |
| DASH-001 | Dashboard | P6 | Failure shown |  | ⬜ |  |

Add every ID from Section 6.

---

## 9. Experiments (Rev. 2 §11)

| # | Condition | Measure |
| --- | --- | --- |
| 1 | No failure | Baseline delivery |
| 2 | CCTV3 fails | Recovery time |
| 3 | Link fails | New route selected |
| 4 | Congestion | Packet loss |
| 5 | Server unavailable | Local storage behavior |
| 6 | Multiple failures | Network resilience |
| 7 | Threshold sweeps (Section 5) | Sensitivity |

Log per run: recovery time, packet loss, hop count, route changes, network load, delivery rate, route computation time, control/heartbeat overhead. Compare the proposed algorithm against plain shortest-path routing on the same seed.

---

## 10. Weekly Cadence and Git

- **Monday:** each person states plan, dependencies, blockers.
- **Wednesday:** 15 minutes, done / doing / blocked.
- **Friday:** merge to `main`, run `python src/simulation.py` end to end, record results.

**Branches:** `main` (protected), `dev`, `feature/routing`, `feature/simulation`, `feature/failure-storage`, `feature/dashboard`, `docs/testing`. Nobody pushes experimental code to `main`.

| Week | Team milestone |
| --- | --- |
| 1 | Contracts frozen, repo, 6-node graph, mock dashboard data |
| 2 | Packets flow CCTV → mesh → Server |
| 3 | Multi-factor routing picks best route |
| 4 | Failure detection and automatic rerouting |
| 5 | Packets, buffering, local/server storage |
| 6 | Six experiments run, results collected |
| 7 | Dashboard integrated, 2–3 minute demo |
| 8 | Freeze, final report, presentation |

**Final demo:** nodes connected → packets flowing → fail CCTV3 → failure detected → route recalculated → packets take new route → recovery time shown.

---

## 11. Known Issues in Rev. 2 to Fix

1. §7 example route: link_quality 0.30, reliability 0.85, load 0.70, hop_score 0.5 cannot produce score 0.82 (maximum is about 0.54). Recompute or change the inputs.
2. §9.7 says "Contrast with source-camera failure (Section 9.7)". It should point to §9.8.
3. §4 role table differs from this handbook (see Section 2).
4. Team-plan topology sketches must match the declared edge list in Section 3.1.
5. Exact heartbeat `TIMEOUT` is undefined. Set it in `CONFIG`.
6. §7 example path `CCTV1 → CCTV3 → CCTV4 → Server` uses an undeclared edge (CCTV4–Server). CCTV4 reaches the Server only through the Gateway.
7. With `max_allowed_hops = 4`, a 3-hop route has hop_score 0.25, not 0.50. The default is now 6 so RT-001 (3 hops, hop_score 0.50) is consistent.
