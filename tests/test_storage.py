"""P4: storage.py acceptance tests (STO-001 .. STO-012).

The expected values live in testcase_failure.py at the repository root.
This module only exposes them to pytest under their original IDs.
"""

from _p4_acceptance import register

register(globals(), "STO")
