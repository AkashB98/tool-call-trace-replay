"""Tests for backend.py: determinism, fixtures, arg validation."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import ScriptedToolBackend, ToolArgError, UnknownToolError


class TestBackend(unittest.TestCase):
    def test_unknown_tool(self):
        b = ScriptedToolBackend()
        with self.assertRaises(UnknownToolError):
            b.call("delete_everything", {})

    def test_missing_arg(self):
        b = ScriptedToolBackend()
        with self.assertRaises(ToolArgError):
            b.call("get_service_status", {})

    def test_bad_replicas(self):
        b = ScriptedToolBackend()
        with self.assertRaises(ToolArgError):
            b.call("scale_replicas", {"service": "search-api", "replicas": 99})

    def test_deterministic_across_instances(self):
        seq = [("get_service_status", {"service": "payments-api"}),
               ("get_metric", {"service": "payments-api", "metric": "error_rate", "window": "15m"}),
               ("create_incident", {"service": "payments-api", "severity": "sev2", "title": "t"})]
        outs_a = [ScriptedToolBackend().call(t, a) for t, a in seq]
        outs_b = [ScriptedToolBackend().call(t, a) for t, a in seq]
        self.assertEqual(outs_a, outs_b)
        # incident ids are deterministic counters, not random
        self.assertEqual(outs_a[2]["incident_id"], "INC-0001")

    def test_incident_ids_increment_within_instance(self):
        b = ScriptedToolBackend()
        r1 = b.call("create_incident", {"service": "a", "severity": "sev3", "title": "t"})
        r2 = b.call("create_incident", {"service": "a", "severity": "sev3", "title": "t"})
        self.assertEqual((r1["incident_id"], r2["incident_id"]), ("INC-0001", "INC-0002"))

    def test_fixture_v2_drifts_exactly_two_values(self):
        v1 = ScriptedToolBackend(fixture_version=1)
        v2 = ScriptedToolBackend(fixture_version=2)
        m1 = v1.call("get_metric", {"service": "payments-api", "metric": "error_rate", "window": "15m"})
        m2 = v2.call("get_metric", {"service": "payments-api", "metric": "error_rate", "window": "15m"})
        self.assertNotEqual(m1["value"], m2["value"])
        r1 = v1.call("restart_service", {"service": "payments-api"})
        r2 = v2.call("restart_service", {"service": "payments-api"})
        self.assertNotEqual(r1["restarted_in_s"], r2["restarted_in_s"])
        # everything else identical
        for tool, args in [
            ("get_service_status", {"service": "payments-api"}),
            ("tail_logs", {"service": "payments-api", "lines": 50}),
            ("run_healthcheck", {"service": "payments-api"}),
            ("get_metric", {"service": "node-07", "metric": "disk_usage_pct", "window": "5m"}),
        ]:
            self.assertEqual(v1.call(tool, args), v2.call(tool, args), tool)

    def test_no_wall_clock_in_results(self):
        # Results must never embed wall-clock time, or replays couldn't match.
        import re
        b = ScriptedToolBackend()
        for tool, args in [
            ("get_service_status", {"service": "payments-api"}),
            ("get_metric", {"service": "payments-api", "metric": "error_rate"}),
            ("tail_logs", {"service": "payments-api", "lines": 50}),
            ("restart_service", {"service": "payments-api"}),
            ("run_healthcheck", {"service": "payments-api"}),
        ]:
            blob = str(b.call(tool, args))
            self.assertIsNone(re.search(r"2026-1[01]|2027|20:..:..", blob), tool)

    def test_unknown_fixture_version(self):
        with self.assertRaises(ValueError):
            ScriptedToolBackend(fixture_version=99)


if __name__ == "__main__":
    unittest.main()
