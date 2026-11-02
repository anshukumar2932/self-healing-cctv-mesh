# Testing

## Canonical command

The canonical CI/local command is:

```bash
pytest -v
```

## Ownership

- P1: integration/schema/config tests
- P2: routing tests
- P3: topology/node/link/simulation tests
- P4: failure/recovery/storage tests
- P5: QA, documentation, and experiment validation
- P6: dashboard/API tests

## Existing project runners

If the team already uses custom acceptance-test runners such as:

```bash
python testcase_sim.py src/
python testcase_failure.py src/
```

keep those runners during migration. The goal is to make `pytest` the common CI entry point without deleting the existing acceptance checks.

## Fixed test IDs

Preserve the existing test IDs exactly. When converting a fixed case into pytest, use a clear test name such as:

```python
def test_SIM_002():
    ...
```

Do not silently change expected values, fixture definitions, topology, seed, or tolerances.
