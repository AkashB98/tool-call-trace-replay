"""Tests for replayer.py: exact match, drift detection, error handling."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import ScriptedToolBackend, UnknownToolError
from demo_agent import record_scenario
from replayer import replay
from trace import Trace
import tempfile


def _record_tmp(scenario, fixture_version=1):
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    os.close(fd)
    record_scenario(scenario, path, fixture_version=fixture_version)
    return path


class TestReplayer(unittest.TestCase):
    def test_exact_replay_matches(self):
        path = _record_tmp("payments-error-spike")
        try:
            trace = Trace.load(path)
            report = replay(trace, ScriptedToolBackend(fixture_version=1))
            self.assertEqual(report.verdict, "match")
            self.assertEqual(report.counts["matched"], len(trace))
            self.assertEqual(report.scenario, "payments-error-spike")
        finally:
            os.unlink(path)

    def test_replay_all_scenarios_match(self):
        for scenario in ("payments-error-spike", "disk-space-warning", "latency-p99"):
            path = _record_tmp(scenario)
            try:
                report = replay(Trace.load(path), ScriptedToolBackend(fixture_version=1))
                self.assertEqual(report.verdict, "match", scenario)
            finally:
                os.unlink(path)

    def test_drifted_fixture_flags_exactly_two_steps(self):
        path = _record_tmp("payments-error-spike", fixture_version=1)
        try:
            trace = Trace.load(path)
            report = replay(trace, ScriptedToolBackend(fixture_version=2))
            self.assertEqual(report.verdict, "drift")
            drifted = [s for s in report.steps if s.status == "result_drift"]
            # v2 changes get_metric(error_rate) and restart_service timing
            self.assertEqual(len(drifted), 2)
            self.assertEqual([s.tool for s in drifted],
                             ["get_metric", "restart_service"])
            self.assertEqual([s.seq for s in drifted], [3, 4])
            for s in drifted:
                self.assertNotEqual(s.expected, s.actual)
        finally:
            os.unlink(path)

    def test_replay_is_deterministic(self):
        path = _record_tmp("latency-p99")
        try:
            trace = Trace.load(path)
            r1 = replay(trace, ScriptedToolBackend(fixture_version=1)).to_dict()
            r2 = replay(trace, ScriptedToolBackend(fixture_version=1)).to_dict()
            self.assertEqual(r1, r2)
        finally:
            os.unlink(path)

    def test_unknown_tool_becomes_error_not_crash(self):
        path = _record_tmp("disk-space-warning")
        try:
            trace = Trace.load(path)
            trace.steps[1].tool = "no_such_tool"
            report = replay(trace, ScriptedToolBackend(fixture_version=1))
            self.assertEqual(report.verdict, "error")
            self.assertEqual(report.steps[1].status, "error")
            self.assertIn("UnknownToolError", report.steps[1].detail)
            # replay continues past the error for the remaining steps
            self.assertEqual(len(report.steps), len(trace))
        finally:
            os.unlink(path)

    def test_replay_uses_recorded_args_verbatim(self):
        path = _record_tmp("latency-p99")
        try:
            trace = Trace.load(path)
            backend = ScriptedToolBackend(fixture_version=1)
            report = replay(trace, backend)
            self.assertEqual(report.verdict, "match")
            log = backend.call_log
            self.assertEqual([c["tool"] for c in log],
                             [s.tool for s in trace.steps])
            self.assertEqual([c["args"] for c in log],
                             [s.args for s in trace.steps])
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
