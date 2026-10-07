"""End-to-end demo: record goldens, replay exact, replay drifted, diff.

Runs fully offline. Prints a short narrative of what happened at each stage.
"""

from __future__ import annotations

import os
import tempfile

from backend import ScriptedToolBackend
from cli import EXIT_DRIFT, EXIT_MATCH
from demo_agent import ALERTS, record_scenario
from diff import diff_traces
from replayer import replay
from trace import Trace

SCENARIOS = sorted(ALERTS)


def main() -> None:
    tmp = tempfile.mkdtemp(prefix="trace-replay-demo-")
    goldens = {}
    print("== 1. record golden traces (fixture v1) ==")
    for sc in SCENARIOS:
        path = os.path.join(tmp, f"{sc}.golden.jsonl")
        record_scenario(sc, path, fixture_version=1)
        goldens[sc] = Trace.load(path)
        print(f"   {sc}: {len(goldens[sc])} steps -> {path}")

    print("\n== 2. replay goldens against the same fixtures (expect match) ==")
    ok = True
    for sc in SCENARIOS:
        report = replay(goldens[sc], ScriptedToolBackend(fixture_version=1))
        print(f"   {sc}: verdict={report.verdict} counts={report.counts}")
        ok = ok and report.verdict == "match"

    print("\n== 3. replay against drifted fixtures (v2: metric + restart timing changed) ==")
    for sc in SCENARIOS:
        report = replay(goldens[sc], ScriptedToolBackend(fixture_version=2))
        print(f"   {sc}: verdict={report.verdict} counts={report.counts}")
        for s in report.steps:
            if s.status != "matched":
                print(f"      seq={s.seq} {s.status}: {s.tool}")

    print("\n== 4. record a fresh trace and diff vs golden (expect no drift) ==")
    fresh_path = os.path.join(tmp, "payments-error-spike.fresh.jsonl")
    record_scenario("payments-error-spike", fresh_path, fixture_version=1)
    d = diff_traces(goldens["payments-error-spike"], Trace.load(fresh_path))
    print(f"   drifted={d.drifted} counts={d.counts}")

    print("\n== 5. CLI verify exit codes ==")
    from cli import main as cli_main
    code = cli_main(["verify", "--golden", os.path.join(tmp, "payments-error-spike.golden.jsonl"),
                     "--trace", fresh_path])
    print(f"   verify golden vs fresh -> exit {code} (expect {EXIT_MATCH})")
    print(f"\nDemo artifacts left in {tmp} (inspect freely; nothing leaves your machine).")
    assert ok, "exact replay should match"
    assert code == EXIT_MATCH


if __name__ == "__main__":
    main()
