from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_core_project_structure_exists():
    required = [
        "src/schemas.py",
        "src/config.py",
        "src/topology.py",
        "src/nodes.py",
        "src/links.py",
        "src/routing.py",
        "src/simulation.py",
        "src/failure.py",
        "src/recovery.py",
        "src/storage.py",
        "ui/dashboard.py",
        "ui/api.py",
        "fixtures/fix_base.json",
        "fixtures/fix_fail.json",
    ]
    missing = [path for path in required if not (ROOT / path).exists()]
    assert not missing, f"Missing project files: {missing}"
