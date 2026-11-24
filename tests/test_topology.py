"""P3: topology.py acceptance tests (TOP-001 .. TOP-007).

The expected values live in testcase_sim.py next to this file.
This module only exposes them to pytest under their original IDs."""

from _p3_acceptance import register

register(globals(), "TOP")
