# Self-Healing CCTV Mesh Network

A Python-based simulation and evaluation project for a self-healing CCTV mesh network.

## Project structure

- `src/` — core simulator and network logic
- `ui/` — dashboard and API
- `tests/` — tests organized by project module/person ownership
- `fixtures/` — fixed reproducibility fixtures such as FIX-BASE and FIX-FAIL
- `docs/` — architecture, testing, and API documentation
- `experiments/` — experiment definitions and scripts
- `results/` — generated experiment/test results
- `.github/` — CI/CD, issue templates, and pull-request standards

## Team test ownership

The project has test cases assigned to different team members. Keep the ownership visible in test files and PRs, while CI runs the complete test suite together.

Recommended ownership:

| Person | Area |
|---|---|
| P1 | Integration, schemas, config |
| P2 | Routing |
| P3 | Topology, nodes, links, simulation |
| P4 | Failure, recovery, storage |
| P5 | QA, documentation, experiments |
| P6 | Dashboard, API |

## Reproducibility

The project documentation specifies a fixed seed of `42` for reproducible test and simulation behavior.

Do not invent new data formats. Shared contracts belong in `src/schemas.py`.

Availability convention:

- nodes: `node["online"]`
- links: `link["active"]`

## Local setup

```bash
python -m venv .venv
```

Activate the environment and install dependencies:

```bash
pip install -r requirements.txt
pip install -r requirements-dev.txt
```

Run the test suite:

```bash
pytest -v
```

Run linting:

```bash
ruff check .
```

## Fixed fixtures

`fixtures/fix_base.json` and `fixtures/fix_fail.json` are placeholders for the shared fixed fixtures described in the project test documentation. Replace them with the authoritative fixture values used by the team.

## Important

This repository is a professional scaffold. The source modules and test modules contain starter placeholders so the team can migrate the existing P1–P6 implementation and acceptance tests without changing their IDs or expected behavior.
