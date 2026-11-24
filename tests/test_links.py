"""P3: links.py acceptance tests (LINK-001 .. LINK-004).

The expected values live in testcase_sim.py next to this file.
This module only exposes them to pytest under their original IDs."""

from _p3_acceptance import register

register(globals(), "LINK")
