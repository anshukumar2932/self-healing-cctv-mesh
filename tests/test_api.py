"""P6: ui/api.py acceptance tests (API-xxx).

The expected values live in testcase_dashboard.py next to this file.
This module only exposes them to pytest under their original IDs."""

from _p6_acceptance import register

register(globals(), "API")
