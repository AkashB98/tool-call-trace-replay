"""No-network enforcement: with sockets blocked, the whole record/replay/diff
pipeline must still work. (The `serve` command is the only intentional
exception — it binds localhost on demand.)"""

import os
import socket
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class _Blocker:
    def __init__(self, *a, **k):
        raise RuntimeError("network access blocked in hermetic test")


class TestNoNetwork(unittest.TestCase):
    def setUp(self):
        self._real_socket = socket.socket
        socket.socket = _Blocker
        # urllib builds on socket; block that layer too.
        import urllib.request as urlreq
        self._real_urlopen = urlreq.urlopen
        urlreq.urlopen = lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("network access blocked in hermetic test"))

    def tearDown(self):
        socket.socket = self._real_socket
        import urllib.request as urlreq
        urlreq.urlopen = self._real_urlopen

    def test_full_pipeline_offline(self):
        from demo_agent import record_scenario
        from backend import ScriptedToolBackend
        from replayer import replay
        from diff import diff_traces
        from trace import Trace

        fd, path = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        try:
            record_scenario("payments-error-spike", path)
            trace = Trace.load(path)
            report = replay(trace, ScriptedToolBackend())
            self.assertEqual(report.verdict, "match")
            d = diff_traces(trace, trace)
            self.assertFalse(d.drifted)
        finally:
            os.unlink(path)

    def test_no_module_opens_sockets_at_import(self):
        # Importing every module must not touch the network.
        for mod in ("trace", "recorder", "backend", "diff", "replayer",
                    "demo_agent", "cli"):
            if mod in sys.modules:
                del sys.modules[mod]
        import trace, recorder, backend, diff, replayer, demo_agent, cli  # noqa: F401
        self.assertTrue(True)


if __name__ == "__main__":
    unittest.main()
