"""P3: nodes.py acceptance tests (NODE-001 .. NODE-005).

The expected values live in testcase_sim.py next to this file.
This module only exposes them to pytest under their original IDs."""

from _p3_acceptance import register

register(globals(), "NODE")
