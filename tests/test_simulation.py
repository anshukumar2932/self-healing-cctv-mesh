"""P3: simulation.py acceptance tests (SIM-001 .. SIM-019).

The expected values live in testcase_sim.py next to this file.
This module only exposes them to pytest under their original IDs."""

from _p3_acceptance import register

register(globals(), "SIM")
