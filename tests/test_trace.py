"""Tests for trace.py: model, JSONL round-trip, normalization, determinism."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trace import Step, Trace, canonical


def _sample_trace() -> Trace:
    t = Trace(meta={"agent": "demo", "scenario": "s1"})
    t.steps.append(Step(seq=1, observation="o1", tool="get_metric",
                        args={"service": "x", "metric": "error_rate"},
                        result={"value": 0.038}, timestamp="2026-10-03T00:00:00Z",
                        request_id="abc", duration_ms=1.5))
    t.steps.append(Step(seq=2, observation="o2", tool="restart_service",
                        args={"service": "x"}, result={"status": "ok"},
                        timestamp="2026-10-03T00:00:01Z",
                        request_id="def", duration_ms=2.5))
    return t


class TestJsonlRoundTrip(unittest.TestCase):
    def test_round_trip_preserves_everything(self):
        t = _sample_trace()
        t2 = Trace.from_jsonl(t.to_jsonl())
        self.assertEqual(len(t2), 2)
        self.assertEqual(t2.steps[0].tool, "get_metric")
        self.assertEqual(t2.steps[0].args, {"service": "x", "metric": "error_rate"})
        self.assertEqual(t2.steps[0].timestamp, "2026-10-03T00:00:00Z")
        self.assertEqual(t2.steps[1].request_id, "def")

    def test_steps_sorted_by_seq_on_load(self):
        t = _sample_trace()
        text = t.to_jsonl()
        lines = text.splitlines()
        # swap the two step lines; loader must re-sort
        text = "\n".join([lines[0], lines[2], lines[1]]) + "\n"
        t2 = Trace.from_jsonl(text)
        self.assertEqual([s.seq for s in t2.steps], [1, 2])

    def test_blank_lines_ignored(self):
        t = _sample_trace()
        t2 = Trace.from_jsonl("\n\n" + t.to_jsonl() + "\n\n")
        self.assertEqual(len(t2), 2)

    def test_bad_json_raises(self):
        with self.assertRaises(ValueError):
            Trace.from_jsonl('{"kind": "meta"}\nnot json\n')

    def test_unknown_kind_raises(self):
        with self.assertRaises(ValueError):
            Trace.from_jsonl('{"kind": "weird"}\n')

    def test_save_load_file(self):
        t = _sample_trace()
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
            path = fh.name
        try:
            t.save(path)
            t2 = Trace.load(path)
            self.assertEqual(len(t2), 2)
            self.assertEqual(t2.meta["scenario"], "s1")
        finally:
            os.unlink(path)


class TestNormalization(unittest.TestCase):
    def test_volatile_fields_stripped(self):
        n = _sample_trace().normalized()
        for s in n.steps:
            self.assertIsNone(s.timestamp)
            self.assertIsNone(s.request_id)
            self.assertIsNone(s.duration_ms)

    def test_behavioral_fields_kept(self):
        n = _sample_trace().normalized()
        self.assertEqual(n.steps[0].tool, "get_metric")
        self.assertEqual(n.steps[0].args["metric"], "error_rate")
        self.assertEqual(n.steps[0].result["value"], 0.038)
        self.assertEqual(n.steps[0].observation, "o1")

    def test_normalized_jsonl_byte_stable(self):
        # Same behavior, different volatile noise -> identical normalized form.
        a = _sample_trace()
        b = _sample_trace()
        b.steps[0].timestamp = "1999-01-01T00:00:00Z"
        b.steps[0].request_id = "zzz"
        b.steps[0].duration_ms = 999.0
        b.meta["trace_id"] = "different"
        b.meta["created_at"] = "whenever"
        self.assertEqual(a.normalized_jsonl(), b.normalized_jsonl())

    def test_canonical_is_key_order_stable(self):
        self.assertEqual(canonical({"b": 1, "a": 2}), canonical({"a": 2, "b": 1}))


if __name__ == "__main__":
    unittest.main()
