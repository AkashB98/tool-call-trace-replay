"""Replayer: re-execute a recorded trace against a (fresh) tool backend.

Replay is the CI primitive. Record a trace once against a known-good
backend, commit it as the *golden*. On every later change, replay the
golden's tool calls — same tools, same args, in order — against the new
backend and compare each actual result to the recorded one.

Divergence means the backend's behavior changed: exactly the signal you
want before customers see it.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from diff import values_equal
from trace import Trace

STATUS_MATCHED = "matched"
STATUS_RESULT_DRIFT = "result_drift"
STATUS_ERROR = "error"


@dataclass
class StepReplay:
    seq: int
    tool: str
    status: str
    expected: Dict[str, Any] = field(default_factory=dict)
    actual: Dict[str, Any] = field(default_factory=dict)
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "seq": self.seq,
            "tool": self.tool,
            "status": self.status,
            "expected": copy.deepcopy(self.expected),
            "actual": copy.deepcopy(self.actual),
            "detail": self.detail,
        }


@dataclass
class ReplayReport:
    scenario: str
    fixture_version: int
    steps: List[StepReplay] = field(default_factory=list)

    @property
    def counts(self) -> Dict[str, int]:
        c = {STATUS_MATCHED: 0, STATUS_RESULT_DRIFT: 0, STATUS_ERROR: 0}
        for s in self.steps:
            c[s.status] = c.get(s.status, 0) + 1
        return c

    @property
    def verdict(self) -> str:
        if any(s.status == STATUS_ERROR for s in self.steps):
            return "error"
        if any(s.status == STATUS_RESULT_DRIFT for s in self.steps):
            return "drift"
        return "match"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "scenario": self.scenario,
            "fixture_version": self.fixture_version,
            "verdict": self.verdict,
            "counts": self.counts,
            "steps": [s.to_dict() for s in self.steps],
        }


def replay(trace: Trace, backend: Any, rel_tol: float = 1e-6) -> ReplayReport:
    """Re-execute every recorded step; compare actual results to recorded ones."""
    report = ReplayReport(
        scenario=str(trace.meta.get("scenario", "")),
        fixture_version=int(getattr(backend, "fixture_version", 0) or 0),
    )
    for step in trace.steps:
        args = copy.deepcopy(step.args)
        try:
            actual = backend.call(step.tool, args)
        except Exception as exc:
            report.steps.append(StepReplay(
                seq=step.seq, tool=step.tool, status=STATUS_ERROR,
                expected=copy.deepcopy(step.result),
                actual={"error": f"{type(exc).__name__}: {exc}"},
                detail=f"backend raised {type(exc).__name__}",
            ))
            continue
        if values_equal(step.result, actual, rel_tol=rel_tol):
            status, detail = STATUS_MATCHED, ""
        else:
            status = STATUS_RESULT_DRIFT
            detail = f"result differs from recorded golden (tool {step.tool})"
        report.steps.append(StepReplay(
            seq=step.seq, tool=step.tool, status=status,
            expected=copy.deepcopy(step.result),
            actual=copy.deepcopy(actual),
            detail=detail,
        ))
    return report
