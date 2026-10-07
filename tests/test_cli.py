"""Tests for cli.py: exit codes, record/replay/diff/verify, serve smoke test."""

import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cli import EXIT_DRIFT, EXIT_ERROR, EXIT_MATCH, build_parser, main
from demo_agent import record_scenario
from trace import Trace, canonical


def _tmp(suffix=".jsonl"):
    fd, path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    return path


class TestCli(unittest.TestCase):
    def setUp(self):
        self.paths = []

    def tearDown(self):
        for p in self.paths:
            if os.path.exists(p):
                os.unlink(p)

    def _golden(self, scenario="payments-error-spike", fv=1):
        p = _tmp()
        self.paths.append(p)
        record_scenario(scenario, p, fixture_version=fv)
        return p

    def test_record_writes_valid_trace(self):
        out = _tmp()
        self.paths.append(out)
        code = main(["record", "--scenario", "disk-space-warning", "--out", out])
        self.assertEqual(code, EXIT_MATCH)
        t = Trace.load(out)
        self.assertEqual(t.meta["scenario"], "disk-space-warning")
        self.assertGreater(len(t), 0)

    def test_record_unknown_scenario_is_error(self):
        # argparse choices guard; also exercises the ValueError path directly
        with self.assertRaises(SystemExit):
            main(["record", "--scenario", "nope", "--out", _tmp()])

    def test_replay_exit_match(self):
        g = self._golden()
        code = main(["replay", "--trace", g, "--fixture-version", "1"])
        self.assertEqual(code, EXIT_MATCH)

    def test_replay_exit_drift(self):
        g = self._golden()
        code = main(["replay", "--trace", g, "--fixture-version", "2"])
        self.assertEqual(code, EXIT_DRIFT)

    def test_replay_missing_file_is_error(self):
        code = main(["replay", "--trace", "/tmp/does-not-exist-xyz.jsonl"])
        self.assertEqual(code, EXIT_ERROR)

    def test_replay_writes_report_file(self):
        g = self._golden()
        out = _tmp(suffix=".json")
        self.paths.append(out)
        code = main(["replay", "--trace", g, "--fixture-version", "1", "--out", out])
        self.assertEqual(code, EXIT_MATCH)
        with open(out) as fh:
            report = json.load(fh)
        self.assertEqual(report["verdict"], "match")

    def test_diff_exit_match(self):
        g = self._golden()
        code = main(["diff", "--golden", g, "--candidate", g])
        self.assertEqual(code, EXIT_MATCH)

    def test_diff_exit_drift(self):
        g = self._golden()
        t = Trace.load(g)
        t.steps[-1].result["status"] = "CHANGED"
        c = _tmp()
        self.paths.append(c)
        t.save(c)
        code = main(["diff", "--golden", g, "--candidate", c])
        self.assertEqual(code, EXIT_DRIFT)

    def test_verify_with_explicit_trace(self):
        g = self._golden()
        code = main(["verify", "--golden", g, "--trace", g])
        self.assertEqual(code, EXIT_MATCH)

    def test_verify_rerecords_when_no_trace(self):
        # verify with no --trace re-records the golden's scenario and diffs:
        # deterministic pipeline => match.
        g = self._golden("latency-p99")
        code = main(["verify", "--golden", g])
        self.assertEqual(code, EXIT_MATCH)

    def test_verify_missing_golden_is_error(self):
        code = main(["verify", "--golden", "/tmp/does-not-exist-xyz.jsonl"])
        self.assertEqual(code, EXIT_ERROR)


class TestServe(unittest.TestCase):
    def test_post_replay_and_get_report(self):
        from cli import _Handler
        from http.server import HTTPServer

        server = HTTPServer(("127.0.0.1", 0), _Handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            fd, gp = tempfile.mkstemp(suffix=".jsonl")
            os.close(fd)
            try:
                record_scenario("disk-space-warning", gp, fixture_version=1)
                trace = Trace.load(gp)
                lines = [json.loads(line) for line in trace.to_jsonl().splitlines()]
                body = canonical({"trace": lines, "fixture_version": 1}).encode()
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/replay", data=body,
                    headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    payload = json.loads(resp.read())
                self.assertEqual(payload["verdict"], "match")
                self.assertGreater(payload["counts"]["matched"], 0)

                with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/report", timeout=10) as resp:
                    report = json.loads(resp.read())
                self.assertEqual(report["verdict"], "match")
            finally:
                os.unlink(gp)
        finally:
            server.shutdown()
            thread.join(timeout=5)

    def test_unknown_endpoint_404(self):
        from cli import _Handler
        from http.server import HTTPServer

        server = HTTPServer(("127.0.0.1", 0), _Handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/nope", timeout=10)
            self.assertEqual(ctx.exception.code, 404)
        finally:
            server.shutdown()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
