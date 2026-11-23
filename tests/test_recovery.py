"""P4: recovery.py acceptance tests (REC-001 .. REC-008).

The expected values live in testcase_failure.py at the repository root.
This module only exposes them to pytest under their original IDs.
"""

from _p4_acceptance import register

register(globals(), "REC")
