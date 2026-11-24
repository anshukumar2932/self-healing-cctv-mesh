"""Bridge between the P3 acceptance runner and pytest.

testcase_sim.py stays the authoritative acceptance suite (see
docs/testing.md).  This helper imports it once and exposes each fixed case
as a pytest test named after its ID, e.g. test_SIM_001, so IDs stay visible
in CI output without copying any expected values.
"""

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
for p in (HERE, ROOT, ROOT / "src"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

acceptance = importlib.import_module("testcase_sim")

MODULES = SimpleNamespace(
    topology=importlib.import_module("topology"),
    nodes=importlib.import_module("nodes"),
    links=importlib.import_module("links"),
    simulation=importlib.import_module("simulation"),
)


def register(namespace, prefix):
    """Add one test function per acceptance case with the given ID prefix."""
    for tid, title, fn in acceptance.TESTS:
        if not tid.startswith(prefix + "-"):
            continue

        def make(case):
            def test_case():
                case(MODULES)

            return test_case

        test = make(fn)
        test.__name__ = "test_" + tid.replace("-", "_")
        test.__doc__ = f"{tid}: {title}"
        namespace[test.__name__] = test
