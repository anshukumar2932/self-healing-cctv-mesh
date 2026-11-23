# P4 Workflow — Failure, Recovery and Storage

This document describes how I (P4, Failure + Storage Engineer) built and verified my
part of the Self-Healing CCTV Mesh Network. It covers the four files I created:

| File | Purpose |
|---|---|
| `src/failure.py` | Load classification, link health, heartbeat detection, scripted failure injection and element restoration |
| `src/recovery.py` | `RecoveryEngine`: reroutes a camera after a failure, verifies the new route, falls back to local storage |
| `src/storage.py` | `StorageManager`: per-node FIFO packet buffers with delivered / lost accounting |
| `tests/_p4_acceptance.py` | Bridge that exposes the fixed acceptance cases in `testcase_failure.py` to pytest |

The authoritative acceptance suite is `testcase_failure.py` (37 cases: FAIL-001..017,
REC-001..008, STO-001..012). I never edit its expected values; the code is changed until
the suite passes.

---

## 1. Start from the contract, not from the code

`testcase_failure.py` begins with an **API CONTRACT** docstring. I treated that block as the
specification and wrote each function signature down before writing any logic:

- `failure.py`: `classify_load`, `evaluate_link`, `record_heartbeat`, `check_heartbeats`,
  `inject_failure`, `restore_element`
- `recovery.py`: `RecoveryEngine(router, verify, config)` with `.recover(...)` and `.route_changes`
- `storage.py`: `StorageManager(capacity)` with `.store`, `.count`, `.buffer_available`,
  `.drain`, `.buffered()`, `.delivered`, `.lost`

Two team conventions from the README drive every function:

- a node is usable when `node["online"]` is `True`
- a link is usable when `link["active"]` is `True`

All functions mutate the node / link dicts **in place** and return event dicts. No new data
formats are invented; shared contracts belong to P1 in `src/schemas.py`.

## 2. Run the suite red first

Before implementing anything I ran the runner against the scaffold to see the full list of
failures and confirm the loader found the three modules:

```bash
python testcase_failure.py src/          # all cases
python testcase_failure.py src/ -v       # full tracebacks on errors
python testcase_failure.py src/ FAIL-010 # one case while iterating
```

The runner needs only the standard library, so it works before any dependency is installed.

## 3. Implement module by module, in dependency order

### 3.1 `failure.py` (FAIL-001 .. FAIL-017)

Order of work: pure functions first, then stateful ones.

1. `classify_load` – thresholds live in `DEFAULT_CONFIG` and are overridable per call
   (`{"congestion_threshold": 0.90}` is checked by FAIL-005). Boundaries are
   `>= 0.60 WARNING`, `>= 0.80 CONGESTED`.
2. `evaluate_link` – deactivates on `packet_loss > 0.50` **or** `quality < 0.30`. The rule
   is strict (`0.50` and `0.30` stay active) and a link is never re-activated here.
3. `record_heartbeat` / `check_heartbeats` – a node fails when `now - last_heartbeat` is
   **strictly greater** than `heartbeat_timeout_s` (2.0 s), so a 2.0 s gap is not a failure
   (FAIL-011). Already-offline nodes are skipped, which is what makes "reported exactly once"
   hold across repeated polling (FAIL-010).
4. `inject_failure` – validates `failure_type` against `FAILURE_TYPES` and the element
   against the topology, raising `ValueError` otherwise (FAIL-016/017). Node failures set
   `online=False`, link failures set `active=False`, congestion only sets `load` and adds a
   `state` key; the node stays online (FAIL-004).
5. `restore_element` – links are matched first (the `A-B` form), then nodes. Restoring a node
   also resets `last_heartbeat = now` so the heartbeat checker does not immediately fail it
   again (FAIL-014).

### 3.2 `recovery.py` (REC-001 .. REC-008)

The engine does **not** compute routes. P2's router is injected as a callable, and a
`verify(route) -> bool` callable stands in for the ACK. This keeps P4 testable with the
`StubRouter` in the suite and lets P2 swap in the real router later.

The state machine is `NORMAL → DETECT → ANALYZE → FIND_ALT → SCORE → SWITCH → VERIFY → NORMAL`
(REC-007). Decisions made in `recover()`:

- **Source camera failed** → no router call, `fallback="store_locally"` (FAIL-002).
- **Current route not affected** by the failure → no router call, route returned unchanged,
  no `route_changed` event, `route_changes` not incremented (REC-008).
- **Verification fails** → the candidate is appended to `excluded_routes`, the router is
  called again, and one `ack_timeout_s` is added to `recovery_time_ms` (REC-002).
- **No verified route left** → `event_type="recovery_failed"`, `new_route=None`,
  `fallback="store_locally"`, no crash (REC-003, REC-006).
- `recovery_time_ms = route_computation_ms + control_message_delay_ms + failed_verifications × ack_timeout_s × 1000`.
- `max_attempts` (50) is a safety stop so a misbehaving router cannot loop forever.

Every handbook 3.5 field is present and non-null in the result (REC-005): `old_route`,
`new_route`, `recovery_time_ms`, `route_computation_time_ms`, `control_messages_sent`,
`packets_lost`, plus `state_log`, `fallback` and `events`.

### 3.3 `storage.py` (STO-001 .. STO-012)

- One `deque` per node, created lazily, so buffers are independent (STO-011).
- `store` rejects when `len(buf) >= capacity`, increments `lost` and returns a
  `packet_lost` event with `reason="buffer_full"` (STO-005).
- `drain` is FIFO and stops at the **first** `send_fn` that returns `False`; that packet
  stays at the head so ordering survives a partial drain (STO-010). This also gives
  `sent == delivered + lost + buffered()` (STO-007).
- `buffer_available` is a percentage clamped to `0..100`.
- `capacity <= 0` raises `ValueError` (STO-012).

## 4. Iterate until the runner is green

Each change was followed by the targeted runner command for the IDs I was working on, then
the full run. Final state:

```
Total 37 | Passed 37 | Failed 0 | Errors 0
RESULT: ALL PASS - P4 acceptance met
```

## 5. Bridge the acceptance suite into pytest

`docs/testing.md` makes `pytest -v` the common CI entry point but says the existing runners
must be kept. Rather than copy 37 expected values into pytest files, I wrote
`tests/_p4_acceptance.py`:

1. Puts the repo root and `src/` on `sys.path`.
2. Imports `testcase_failure` once and the three modules once.
3. `register(namespace, prefix)` creates one pytest function per acceptance case, named
   after its ID (`test_FAIL_001`, `test_REC_002`, `test_STO_012`), so IDs stay visible in
   CI output.

The three pytest files are then three lines each:

```python
from _p4_acceptance import register
register(globals(), "FAIL")   # or "REC" / "STO"
```

For the bridge to import the suite, `testcase_failure.py` lives at the **repository root**
(the same place the documented `python testcase_failure.py src/` command assumes).

## 6. Lint the way CI does

CI runs `ruff check .` before the tests. Two things had to be handled:

- The acceptance suite uses single-letter loop names and one unused import. It is kept
  verbatim, so `pyproject.toml` has a per-file-ignore for `testcase_failure.py` (E741, F401)
  instead of edits to the authoritative file.
- The scaffold's placeholder modules (`config.py`, `schemas.py`, `simulation.py`,
  `topology.py`, `nodes.py`, `links.py`, `routing.py`) contained a literal `\n` sequence
  instead of real newlines, which ruff reports as a syntax error. These were normalised so
  lint passes for the whole repo; their content (a docstring and a TODO) is unchanged.

## 7. Full local check before a PR

```bash
pip install -r requirements.txt -r requirements-dev.txt
ruff check .                        # must print "All checks passed!"
pytest -v                           # 43 passed (37 P4 + 6 other scaffolds)
python testcase_failure.py src/     # 37/37, the authoritative runner
python -c "import src; import ui"   # CI import check
```

Commit on a feature branch and open a PR against `dev`; `.github/CODEOWNERS` and the PR
template apply. P4 files only: `src/failure.py`, `src/recovery.py`, `src/storage.py`,
`tests/_p4_acceptance.py`, `tests/test_{failure,recovery,storage}.py`,
`testcase_failure.py`, and the ruff config lines in `pyproject.toml`.

## 8. Hand-offs and open items for the team

- **P1 (schemas)**: the handbook's allowed event types need `recovery_failed` added. The
  `failure_detected` event is checked by FAIL-012; the full 3.5 field set is checked on the
  recovery result (REC-005).
- **P2 (routing)**: the real router must accept `routing_input["excluded_routes"]` (list of
  routes) and return `{"selected_route": [...]}` or `selected_route=None` with a `reason`.
  The `StubRouter` in the suite documents the expected shape.
- **P3 / P5**: traffic returning to the better route after a node recovers (FAIL-014 only
  checks element restoration) is not covered by P4 tests.
- `fixtures/fix_fail.json` and `fixtures/fix_base.json` are still placeholders; the suite
  builds its own FIX-FAIL link qualities in `fix_fail_links()`.
