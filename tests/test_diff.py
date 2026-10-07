"""Tests for diff.py: statuses, tolerance, order-insensitivity, FP traps."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from diff import (
    STATUS_ARG_DRIFT, STATUS_EXTRA, STATUS_MATCHED, STATUS_MISSING,
    STATUS_RESULT_DRIFT, STATUS_TOOL_DRIFT,
    diff_traces, values_equal,
)
from trace import Step, Trace


def _trace(steps):
    t = Trace(meta={"scenario": "s"})
    for i, (tool, args, result) in enumerate(steps, start=1):
        t.steps.append(Step(seq=i, observation=f"o{i}", tool=tool, args=args,
                            result=result, timestamp="T", request_id="R",
                            duration_ms=1.0))
    return t


class TestValuesEqual(unittest.TestCase):
    def test_dict_key_order_ignored(self):
        self.assertTrue(values_equal({"a": 1, "b": 2}, {"b": 2, "a": 1}))

    def test_nested_key_order_ignored(self):
        self.assertTrue(values_equal({"x": {"a": 1, "b": 2}}, {"x": {"b": 2, "a": 1}}))

    def test_list_of_dicts_order_insensitive(self):
        a = [{"name": "http_200", "ok": True}, {"name": "db_ping", "ok": True}]
        b = [{"name": "db_ping", "ok": True}, {"name": "http_200", "ok": True}]
        self.assertTrue(values_equal(a, b))

    def test_plain_lists_order_sensitive(self):
        self.assertFalse(values_equal([1, 2, 3], [3, 2, 1]))

    def test_numeric_tolerance_within(self):
        self.assertTrue(values_equal(1.0, 1.0 + 5e-7))   # rel diff 5e-7 < 1e-6
        self.assertTrue(values_equal({"v": 812.0}, {"v": 812.0004}))

    def test_numeric_tolerance_outside(self):
        self.assertFalse(values_equal(1.0, 1.0 + 2e-6))  # rel diff 2e-6 > 1e-6
        self.assertFalse(values_equal({"v": 0.038}, {"v": 0.041}))

    def test_int_float_mix(self):
        self.assertTrue(values_equal(3, 3.0))

    def test_bool_is_not_number(self):
        # Python's True == 1 trap must not leak into the comparator.
        self.assertFalse(values_equal(True, 1))
        self.assertFalse(values_equal(False, 0.0))
        self.assertTrue(values_equal(True, True))

    def test_none_and_strings_exact(self):
        self.assertTrue(values_equal(None, None))
        self.assertTrue(values_equal("ok", "ok"))
        self.assertFalse(values_equal("ok", "OK"))
        self.assertFalse(values_equal("1", 1))

    def test_list_of_dicts_with_float_wobble(self):
        # Order-insensitive AND tolerance must compose (not exact string compare).
        a = [{"name": "x", "latency_ms": 180.0}, {"name": "y", "latency_ms": 12.0}]
        b = [{"name": "y", "latency_ms": 12.0000001}, {"name": "x", "latency_ms": 180.0000001}]
        self.assertTrue(values_equal(a, b))


class TestDiffTraces(unittest.TestCase):
    def test_identical_traces_all_matched(self):
        steps = [("get_metric", {"service": "x"}, {"value": 1.0}),
                 ("restart_service", {"service": "x"}, {"status": "ok"})]
        d = diff_traces(_trace(steps), _trace(steps))
        self.assertFalse(d.drifted)
        self.assertEqual(d.counts, {STATUS_MATCHED: 2})

    def test_volatile_differences_never_drift(self):
        g = _trace([("get_metric", {"a": 1}, {"v": 1.0})])
        c = _trace([("get_metric", {"a": 1}, {"v": 1.0})])
        c.steps[0].timestamp = "completely different"
        c.steps[0].request_id = "different"
        c.steps[0].duration_ms = 12345.0
        c.meta["trace_id"] = "different"
        d = diff_traces(g, c)
        self.assertFalse(d.drifted)

    def test_tool_drift(self):
        g = _trace([("get_metric", {"a": 1}, {"v": 1})])
        c = _trace([("scale_replicas", {"a": 1}, {"v": 1})])
        d = diff_traces(g, c)
        self.assertTrue(d.drifted)
        self.assertEqual(d.steps[0].status, STATUS_TOOL_DRIFT)

    def test_arg_drift(self):
        g = _trace([("scale_replicas", {"service": "s", "replicas": 4}, {})])
        c = _trace([("scale_replicas", {"service": "s", "replicas": 6}, {})])
        d = diff_traces(g, c)
        self.assertEqual(d.steps[0].status, STATUS_ARG_DRIFT)
        self.assertIn("replicas", d.steps[0].detail)

    def test_arg_key_order_is_not_drift(self):
        g = _trace([("scale_replicas", {"service": "s", "replicas": 4}, {"ok": True})])
        c = _trace([("scale_replicas", {"replicas": 4, "service": "s"}, {"ok": True})])
        d = diff_traces(g, c)
        self.assertFalse(d.drifted)

    def test_result_drift(self):
        g = _trace([("get_metric", {}, {"value": 0.038})])
        c = _trace([("get_metric", {}, {"value": 0.041})])
        d = diff_traces(g, c)
        self.assertEqual(d.steps[0].status, STATUS_RESULT_DRIFT)
        self.assertIn("value", d.steps[0].detail)

    def test_result_float_wobble_is_not_drift(self):
        g = _trace([("get_metric", {}, {"value": 812.0})])
        c = _trace([("get_metric", {}, {"value": 812.0004})])
        d = diff_traces(g, c)
        self.assertFalse(d.drifted)

    def test_missing_step(self):
        g = _trace([("a", {}, {}), ("b", {}, {}), ("c", {}, {})])
        c = _trace([("a", {}, {}), ("b", {}, {})])
        d = diff_traces(g, c)
        self.assertTrue(d.drifted)
        self.assertEqual(d.steps[2].status, STATUS_MISSING)
        self.assertEqual(d.steps[2].seq, 3)

    def test_extra_step(self):
        g = _trace([("a", {}, {})])
        c = _trace([("a", {}, {}), ("b", {}, {})])
        d = diff_traces(g, c)
        self.assertTrue(d.drifted)
        self.assertEqual(d.steps[1].status, STATUS_EXTRA)
        self.assertEqual(d.steps[1].seq, 2)

    def test_empty_traces(self):
        d = diff_traces(_trace([]), _trace([]))
        self.assertFalse(d.drifted)
        self.assertEqual(d.counts, {})

    def test_diff_report_summary_lists_only_drift(self):
        g = _trace([("a", {}, {"v": 1}), ("b", {}, {"v": 2})])
        c = _trace([("a", {}, {"v": 1}), ("b", {}, {"v": 99})])
        d = diff_traces(g, c)
        s = d.summary()
        self.assertIn("seq=2", s)
        self.assertNotIn("seq=1 result_drift", s)

    def test_to_dict_round_trip(self):
        g = _trace([("a", {}, {"v": 1})])
        d = diff_traces(g, g)
        dd = d.to_dict()
        self.assertFalse(dd["drifted"])
        self.assertEqual(dd["counts"], {STATUS_MATCHED: 1})


if __name__ == "__main__":
    unittest.main()


class TestFirstMismatchPath(unittest.TestCase):
    def test_reordered_list_points_at_true_drift(self):
        from diff import _first_mismatch
        a = {"checks": [{"name": "x", "v": 1}, {"name": "y", "v": 2}]}
        b = {"checks": [{"name": "y", "v": 2}, {"name": "x", "v": 99}]}
        where = _first_mismatch(a, b, "$.result")
        # The real drift is x's v (1 -> 99); the report must say so,
        # not blame the reordering itself.
        self.assertIn("v", where)
        self.assertIn("99", where)
        self.assertNotIn("'x' != 'y'", where)
