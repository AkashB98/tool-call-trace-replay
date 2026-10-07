"""Adversarial edge tests: symmetric drift, CLI seams, serve error paths."""

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import ScriptedToolBackend
from cli import EXIT_DRIFT, EXIT_ERROR, EXIT_MATCH, main
from demo_agent import record_scenario
from diff import diff_traces
from replayer import replay
from trace import Trace, canonical


def _tmp(suffix=".jsonl"):
    fd, path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    return path


class TestEdge(unittest.TestCase):
    def setUp(self):
        self.paths = []

    def tearDown(self):
        for p in self.paths:
            if os.path.exists(p):
                os.unlink(p)

    def _rec(self, scenario="payments-error-spike", fv=1):
        p = _tmp()
        self.paths.append(p)
        record_scenario(scenario, p, fixture_version=fv)
        return p

    def test_drift_is_symmetric(self):
        # Golden recorded on v2 fixtures, replayed on v1: drift must ALSO fire.
        p = self._rec(fv=2)
        report = replay(Trace.load(p), ScriptedToolBackend(fixture_version=1))
        self.assertEqual(report.verdict, "drift")
        drifted = [s.tool for s in report.steps if s.status == "result_drift"]
        self.assertEqual(drifted, ["get_metric", "restart_service"])

    def test_diff_out_writes_report(self):
        g = self._rec()
        out = _tmp(suffix=".json")
        self.paths.append(out)
        code = main(["diff", "--golden", g, "--candidate", g, "--out", out])
        self.assertEqual(code, EXIT_MATCH)
        with open(out) as fh:
            report = json.load(fh)
        self.assertFalse(report["drifted"])

    def test_verify_unknown_scenario_is_error(self):
        g = self._rec()
        t = Trace.load(g)
        t.meta["scenario"] = "not-a-real-scenario"
        t.save(g)
        code = main(["verify", "--golden", g])
        self.assertEqual(code, EXIT_ERROR)

    def test_verify_corrupt_golden_is_error(self):
        p = _tmp()
        self.paths.append(p)
        with open(p, "w") as fh:
            fh.write("this is not jsonl\n")
        code = main(["verify", "--golden", p])
        self.assertEqual(code, EXIT_ERROR)

    def test_diff_of_different_scenarios_flags_tool_drift(self):
        g = Trace.load(self._rec("payments-error-spike"))
        c = Trace.load(self._rec("disk-space-warning"))
        d = diff_traces(g, c)
        self.assertTrue(d.drifted)
        statuses = {s.status for s in d.steps}
        self.assertIn("tool_drift", statuses)

    def test_empty_trace_replay_is_match(self):
        t = Trace(meta={"scenario": "empty"})
        report = replay(t, ScriptedToolBackend())
        self.assertEqual(report.verdict, "match")
        self.assertEqual(report.counts["matched"], 0)

    def test_deeply_nested_tolerant_compare(self):
        from diff import values_equal
        a = {"checks": [{"name": "db", "lat": {"p50": 10.0, "p99": 100.0}}], "ok": True}
        b = {"checks": [{"lat": {"p99": 100.00005, "p50": 10.0}, "name": "db"}], "ok": True}
        self.assertTrue(values_equal(a, b))
        c = {"checks": [{"name": "db", "lat": {"p50": 10.0, "p99": 150.0}}], "ok": True}
        self.assertFalse(values_equal(a, c))


class TestServeErrors(unittest.TestCase):
    def _server(self):
        from cli import _Handler
        from http.server import HTTPServer
        server = HTTPServer(("127.0.0.1", 0), _Handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread, port

    def _post(self, port, payload):
        body = canonical(payload).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{port}/replay", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())

    def test_serve_bad_body_is_400(self):
        server, thread, port = self._server()
        try:
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self._post(port, {"nope": 1})
            self.assertEqual(ctx.exception.code, 400)
        finally:
            server.shutdown()
            thread.join(timeout=5)

    def test_serve_replay_then_report(self):
        server, thread, port = self._server()
        fd, gp = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        try:
            record_scenario("latency-p99", gp, fixture_version=1)
            trace = Trace.load(gp)
            lines = [json.loads(l) for l in trace.to_jsonl().splitlines()]
            status, payload = self._post(port, {"trace": lines, "fixture_version": 2})
            self.assertEqual(status, 200)
            # v2 fixtures drift the payments metric but latency-p99 doesn't
            # touch those tools -> still a match. Drift is per-trace.
            self.assertEqual(payload["verdict"], "match")
        finally:
            os.unlink(gp)
            server.shutdown()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
