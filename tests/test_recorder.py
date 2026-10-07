"""Tests for recorder.py: context manager, decorator, streaming, error capture."""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import ScriptedToolBackend
from recorder import TraceRecorder, record_trace
from trace import Trace


class Boom(Exception):
    pass


class FlakyBackend(ScriptedToolBackend):
    def _tool_get_metric(self, args):
        raise Boom("backend exploded")


class TestContextManager(unittest.TestCase):
    def test_records_steps_in_order(self):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
            path = fh.name
        try:
            backend = ScriptedToolBackend()
            with TraceRecorder(path, agent="a", scenario="s") as rec:
                rec.call(backend, "get_service_status", {"service": "payments-api"},
                         observation="obs1")
                rec.call(backend, "get_metric", {"service": "payments-api", "metric": "error_rate"},
                         observation="obs2")
                self.assertEqual(len(rec), 2)
            t = Trace.load(path)
            self.assertEqual(len(t), 2)
            self.assertEqual([s.tool for s in t.steps],
                             ["get_service_status", "get_metric"])
            self.assertEqual(t.steps[0].observation, "obs1")
            self.assertEqual(t.steps[0].seq, 1)
            self.assertEqual(t.meta["agent"], "a")
            self.assertEqual(t.meta["scenario"], "s")
            # volatile fields are populated raw
            self.assertIsNotNone(t.steps[0].timestamp)
            self.assertIsNotNone(t.steps[0].request_id)
            self.assertNotEqual(t.steps[0].request_id, t.steps[1].request_id)
        finally:
            os.unlink(path)

    def test_backend_error_recorded_not_raised(self):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
            path = fh.name
        try:
            with TraceRecorder(path) as rec:
                result = rec.call(FlakyBackend(), "get_metric", {"service": "x", "metric": "y"})
            self.assertIn("Boom", result["error"])
            t = Trace.load(path)
            self.assertIn("Boom", t.steps[0].result["error"])
        finally:
            os.unlink(path)

    def test_partial_trace_survives_exception(self):
        # A crash mid-run must still leave a valid trace file behind.
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
            path = fh.name
        try:
            backend = ScriptedToolBackend()
            with self.assertRaises(RuntimeError):
                with TraceRecorder(path, scenario="crash") as rec:
                    rec.call(backend, "get_service_status", {"service": "payments-api"})
                    raise RuntimeError("agent died")
            t = Trace.load(path)  # must parse, not raise
            self.assertEqual(len(t), 1)
            self.assertEqual(t.steps[0].tool, "get_service_status")
        finally:
            os.unlink(path)

    def test_meta_header_is_first_line(self):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
            path = fh.name
        try:
            with TraceRecorder(path, agent="a", scenario="s") as rec:
                rec.record("get_metric", {"a": 1}, {"b": 2})
            with open(path) as fh2:
                first = json.loads(fh2.readline())
            self.assertEqual(first["kind"], "meta")
            self.assertEqual(first["scenario"], "s")
        finally:
            os.unlink(path)


class TestDecorator(unittest.TestCase):
    def test_decorator_injects_recorder(self):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
            path = fh.name
        try:
            @record_trace(path, agent="dec", scenario="ds")
            def run(recorder=None):
                recorder.call(ScriptedToolBackend(), "get_service_status",
                              {"service": "node-07"})
                return "done"

            self.assertEqual(run(), "done")
            t = Trace.load(path)
            self.assertEqual(len(t), 1)
            self.assertEqual(t.meta["agent"], "dec")
        finally:
            os.unlink(path)

    def test_decorator_passes_through_explicit_recorder(self):
        # If the caller already has a recorder, the decorator must not
        # shadow it or open a second file.
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
            path = fh.name
        try:
            @record_trace("/tmp/should-not-be-created.jsonl")
            def run(recorder=None):
                return recorder

            with TraceRecorder(path) as rec:
                self.assertIs(run(recorder=rec), rec)
            self.assertFalse(os.path.exists("/tmp/should-not-be-created.jsonl"))
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
