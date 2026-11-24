## Unimplemented modules (xfail while building)

`tests/conftest.py` marks the P3 and P6 acceptance tests as expected-to-fail
(xfail) only while their target module is still a placeholder. This keeps CI
green during the build, and the tests still run and are listed by ID.

| Test file | Marked xfail until it defines |
|-----------|-------------------------------|
| `test_topology.py` | `src/topology.py: build_topology` |
| `test_nodes.py` | `src/nodes.py: CameraNode` |
| `test_links.py` | `src/links.py: Link` |
| `test_simulation.py` | `src/simulation.py: run_simulation` |
| `test_dashboard.py` | `ui/dashboard.py: DashboardState` |
| `test_api.py` | `ui/api.py: create_app` |

Once your module defines that name, its tests run for real and failures turn
CI red. Use the exact names from the API contract at the top of
`tests/testcase_sim.py` / `tests/testcase_dashboard.py`; a different name keeps
the tests xfailed.

### Running and debugging

```bash
pytest tests/test_topology.py -v             # shows XFAIL while a placeholder
pytest tests/test_topology.py --runxfail -v  # real failures and tracebacks
python tests/testcase_sim.py src/            # P3 runner, clear PASS/FAIL messages
python tests/testcase_dashboard.py ui/       # P6 runner
python tests/testcase_failure.py src/        # P4 runner
```

When all six modules are implemented, delete `tests/conftest.py`.