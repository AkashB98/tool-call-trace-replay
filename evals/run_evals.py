"""Golden evals: ~12 scenarios proving the harness works end to end.

Every eval is deterministic: run_evals.py writes eval_report.json, and the
file must be byte-identical across runs (checked via md5 in the quality
gate). Timestamps/request-ids are normalized out of every comparison, so
re-recorded goldens compare cleanly.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import ScriptedToolBackend
from cli import EXIT_DRIFT, EXIT_ERROR, EXIT_MATCH, main as cli_main
from demo_agent import ALERTS, record_scenario
from diff import diff_traces
from replayer import replay
from trace import Trace, canonical

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="trace-replay-evals-")
RESULTS = []


def check(name, passed, detail=""):
    RESULTS.append({"name": name, "passed": bool(passed), "detail": detail})
    print(f"  [{'PASS' if passed else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return passed


def golden_path(scenario, fv=1):
    path = os.path.join(TMP, f"{scenario}.v{fv}.golden.jsonl")
    record_scenario(scenario, path, fixture_version=fv)
    return path


def shuffle_keys(obj):
    """Recursively rebuild dicts with reversed key order (same content)."""
    if isinstance(obj, dict):
        return {k: shuffle_keys(v) for k, v in reversed(list(obj.items()))}
    if isinstance(obj, list):
        return [shuffle_keys(x) for x in obj]
    return obj


def main():
    print("== golden evals: tool-call trace replay ==")

    # E1-E3: exact replay matches for every scenario
    goldens = {sc: Trace.load(golden_path(sc)) for sc in sorted(ALERTS)}
    for sc, trace in goldens.items():
        report = replay(trace, ScriptedToolBackend(fixture_version=1))
        check(f"E1-E3 exact replay matches [{sc}]",
              report.verdict == "match" and
              report.counts["matched"] == len(trace),
              f"{report.counts}")

    # E4: drifted fixtures flag exactly the right steps
    trace = goldens["payments-error-spike"]
    report = replay(trace, ScriptedToolBackend(fixture_version=2))
    drifted = [(s.seq, s.tool) for s in report.steps if s.status == "result_drift"]
    check("E4 drifted fixtures flag exactly the right steps",
          report.verdict == "drift" and drifted == [(3, "get_metric"), (4, "restart_service")],
          f"drifted={drifted}")

    # E5: arg key-order shuffle must NOT drift
    t2 = Trace.from_jsonl(trace.to_jsonl())
    for s in t2.steps:
        s.args = shuffle_keys(s.args)
        s.result = shuffle_keys(s.result)
    d = diff_traces(trace, t2)
    check("E5 shuffled dict key order is not drift", not d.drifted, f"{d.counts}")

    # E6: volatile-field skew must NOT drift
    t3 = Trace.from_jsonl(trace.to_jsonl())
    for i, s in enumerate(t3.steps):
        s.timestamp = f"1999-12-31T00:00:{i:02d}Z"
        s.request_id = "deadbeef"
        s.duration_ms = 424242.0
    t3.meta["trace_id"] = "other"
    d = diff_traces(trace, t3)
    check("E6 timestamp/request-id skew is not drift", not d.drifted, f"{d.counts}")

    # E7: numeric tolerance boundary pinned (rel_tol=1e-6)
    t4 = Trace.from_jsonl(trace.to_jsonl())
    for s in t4.steps:
        if s.tool == "get_metric" and s.args.get("metric") == "error_rate":
            s.result = copy.deepcopy(s.result)
            s.result["value"] = 0.038 * (1 + 5e-7)   # inside tolerance
    d = diff_traces(trace, t4)
    within_ok = not d.drifted
    t5 = Trace.from_jsonl(trace.to_jsonl())
    for s in t5.steps:
        if s.tool == "get_metric" and s.args.get("metric") == "error_rate":
            s.result = copy.deepcopy(s.result)
            s.result["value"] = 0.038 * (1 + 2e-6)   # outside tolerance
    d2 = diff_traces(trace, t5)
    outside_ok = (d2.drifted and
                  [s.seq for s in d2.steps if s.status == "result_drift"] == [3])
    check("E7 numeric tolerance boundary pinned", within_ok and outside_ok,
          f"within={within_ok} outside={outside_ok}")

    # E8: missing step flagged at the right seq
    t6 = Trace.from_jsonl(trace.to_jsonl())
    dropped = t6.steps.pop()
    d = diff_traces(trace, t6)
    missing = [s for s in d.steps if s.status == "missing"]
    check("E8 missing step flagged",
          len(missing) == 1 and missing[0].seq == dropped.seq,
          f"missing seq={[s.seq for s in missing]}")

    # E9: extra step flagged
    t7 = Trace.from_jsonl(trace.to_jsonl())
    t7.steps.append(copy.deepcopy(t7.steps[-1]))
    t7.steps[-1].seq = len(t7.steps)
    d = diff_traces(trace, t7)
    extra = [s for s in d.steps if s.status == "extra"]
    check("E9 extra step flagged",
          len(extra) == 1 and extra[0].seq == len(t7.steps),
          f"extra seq={[s.seq for s in extra]}")

    # E10: tool swap flagged as tool_drift, not arg/result drift
    t8 = Trace.from_jsonl(trace.to_jsonl())
    t8.steps[0].tool = "scale_replicas"
    d = diff_traces(trace, t8)
    check("E10 tool swap -> tool_drift",
          d.steps[0].status == "tool_drift", f"status={d.steps[0].status}")

    # E11: determinism — record twice -> byte-identical normalized traces;
    # replay twice -> byte-identical reports
    p1 = golden_path("latency-p99")
    p2 = golden_path("latency-p99")
    h1 = hashlib.md5(Trace.load(p1).normalized_jsonl().encode()).hexdigest()
    h2 = hashlib.md5(Trace.load(p2).normalized_jsonl().encode()).hexdigest()
    r1 = canonical(replay(Trace.load(p1), ScriptedToolBackend()).to_dict())
    r2 = canonical(replay(Trace.load(p2), ScriptedToolBackend()).to_dict())
    m1 = hashlib.md5(r1.encode()).hexdigest()
    m2 = hashlib.md5(r2.encode()).hexdigest()
    check("E11 determinism (record + replay byte-identical)",
          h1 == h2 and m1 == m2, f"trace_md5={h1[:8]} report_md5={m1[:8]}")

    # E12: CLI verify exit codes 0 / 1 / 2
    gp = golden_path("disk-space-warning")
    c0 = cli_main(["verify", "--golden", gp, "--trace", gp])
    c1 = cli_main(["replay", "--trace", golden_path("payments-error-spike"),
                   "--fixture-version", "2"])
    c2 = cli_main(["verify", "--golden", "/tmp/no-such-golden-xyz.jsonl"])
    check("E12 CLI exit codes 0/1/2",
          (c0, c1, c2) == (EXIT_MATCH, EXIT_DRIFT, EXIT_ERROR),
          f"got {(c0, c1, c2)}")

    passed = sum(1 for r in RESULTS if r["passed"])
    total = len(RESULTS)
    summary = {"passed": passed, "total": total,
               "all_passed": passed == total, "evals": RESULTS}
    # Timestamps/request-ids never enter the report: only names + booleans.
    with open(os.path.join(HERE, "eval_report.json"), "w", encoding="utf-8") as fh:
        fh.write(canonical(summary) + "\n")
    print(f"\n{passed}/{total} evals passed -> evals/eval_report.json")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
