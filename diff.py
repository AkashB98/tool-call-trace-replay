"""Structured diff between two traces (golden vs candidate).

Comparison rules — the whole point of this module:
  * volatile fields (timestamps, request ids, durations) are normalized
    away before comparison; they can never cause drift.
  * dict key order is ignored (JSON objects are unordered).
  * lists of dicts are compared order-insensitively (tool-arg lists like
    ``checks=[...]`` shouldn't drift just because the backend reordered
    them). All other lists are order-sensitive.
  * numbers compare with a relative tolerance (default 1e-6): tiny float
    wobble is not drift, real value changes are.
  * bools, strings, and None compare exactly; a bool is never equal to a
    number (Python's ``True == 1`` trap).

Step statuses: ``matched`` | ``tool_drift`` | ``arg_drift`` |
``result_drift`` | ``missing`` | ``extra``.
"""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple

from trace import Trace, canonical

DEFAULT_REL_TOL = 1e-6
DEFAULT_ABS_TOL = 1e-9

STATUS_MATCHED = "matched"
STATUS_TOOL_DRIFT = "tool_drift"
STATUS_ARG_DRIFT = "arg_drift"
STATUS_RESULT_DRIFT = "result_drift"
STATUS_MISSING = "missing"
STATUS_EXTRA = "extra"


def _is_num(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def values_equal(a: Any, b: Any, rel_tol: float = DEFAULT_REL_TOL,
                 abs_tol: float = DEFAULT_ABS_TOL) -> bool:
    """Tolerant deep equality: order-insensitive dicts/lists-of-dicts,
    numeric tolerance, exact everything else."""
    if _is_num(a) and _is_num(b):
        return math.isclose(a, b, rel_tol=rel_tol, abs_tol=abs_tol)
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, dict) and isinstance(b, dict):
        if set(a.keys()) != set(b.keys()):
            return False
        return all(values_equal(a[k], b[k], rel_tol, abs_tol) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return False
        if all(isinstance(x, dict) for x in a) and all(isinstance(x, dict) for x in b):
            # Order-insensitive: sort by canonical encoding, then compare
            # the original dicts pairwise so numeric tolerance still applies.
            sa = sorted(a, key=canonical)
            sb = sorted(b, key=canonical)
            return all(values_equal(x, y, rel_tol, abs_tol) for x, y in zip(sa, sb))
        return all(values_equal(x, y, rel_tol, abs_tol) for x, y in zip(a, b))
    return a == b


@dataclass
class StepDiff:
    seq: int
    status: str
    tool: str = ""
    detail: str = ""
    expected: Any = None
    actual: Any = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "seq": self.seq,
            "status": self.status,
            "tool": self.tool,
            "detail": self.detail,
            "expected": copy.deepcopy(self.expected),
            "actual": copy.deepcopy(self.actual),
        }


@dataclass
class DiffReport:
    golden_meta: Dict[str, Any] = field(default_factory=dict)
    candidate_meta: Dict[str, Any] = field(default_factory=dict)
    steps: List[StepDiff] = field(default_factory=list)

    @property
    def counts(self) -> Dict[str, int]:
        c: Dict[str, int] = {}
        for s in self.steps:
            c[s.status] = c.get(s.status, 0) + 1
        return c

    @property
    def drifted(self) -> bool:
        return any(s.status != STATUS_MATCHED for s in self.steps)

    def summary(self) -> str:
        lines = [f"steps={len(self.steps)} counts={self.counts}"]
        for s in self.steps:
            if s.status != STATUS_MATCHED:
                lines.append(f"  seq={s.seq} {s.status}: {s.detail}")
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "golden_meta": copy.deepcopy(self.golden_meta),
            "candidate_meta": copy.deepcopy(self.candidate_meta),
            "drifted": self.drifted,
            "counts": self.counts,
            "steps": [s.to_dict() for s in self.steps],
        }


def _first_mismatch(a: Any, b: Any, path: str = "$",
                    rel_tol: float = DEFAULT_REL_TOL) -> str:
    """Human-readable location of the first difference (for reports)."""
    if values_equal(a, b, rel_tol=rel_tol):
        return ""
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            sub = _first_mismatch(a.get(k), b.get(k), f"{path}.{k}", rel_tol)
            if sub:
                return sub
        return f"{path}: dicts differ"
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return f"{path}: length {len(a)} != {len(b)}"
        xs, ys = a, b
        if all(isinstance(x, dict) for x in xs) and all(isinstance(y, dict) for y in ys):
            xs = sorted(xs, key=canonical)
            ys = sorted(ys, key=canonical)
        for i, (x, y) in enumerate(zip(xs, ys)):
            sub = _first_mismatch(x, y, f"{path}[{i}]", rel_tol)
            if sub:
                return sub
        return f"{path}: lists differ"
    return f"{path}: {a!r} != {b!r}"


def diff_traces(golden: Trace, candidate: Trace,
                rel_tol: float = DEFAULT_REL_TOL) -> DiffReport:
    """Align steps by position and classify each one."""
    g = golden.normalized()
    c = candidate.normalized()
    report = DiffReport(golden_meta=g.meta, candidate_meta=c.meta)
    n = max(len(g.steps), len(c.steps))
    for i in range(n):
        gs = g.steps[i] if i < len(g.steps) else None
        cs = c.steps[i] if i < len(c.steps) else None
        if gs is None and cs is not None:
            report.steps.append(StepDiff(seq=cs.seq, status=STATUS_EXTRA, tool=cs.tool,
                                         detail=f"candidate has extra step calling {cs.tool!r}"))
            continue
        if cs is None and gs is not None:
            report.steps.append(StepDiff(seq=gs.seq, status=STATUS_MISSING, tool=gs.tool,
                                         detail=f"candidate is missing golden step calling {gs.tool!r}"))
            continue
        assert gs is not None and cs is not None
        if gs.tool != cs.tool:
            report.steps.append(StepDiff(
                seq=gs.seq, status=STATUS_TOOL_DRIFT, tool=gs.tool,
                detail=f"tool changed: {gs.tool!r} -> {cs.tool!r}",
                expected=gs.tool, actual=cs.tool))
        elif not values_equal(gs.args, cs.args, rel_tol=rel_tol):
            where = _first_mismatch(gs.args, cs.args, "$.args", rel_tol)
            report.steps.append(StepDiff(
                seq=gs.seq, status=STATUS_ARG_DRIFT, tool=gs.tool,
                detail=f"args differ at {where}",
                expected=gs.args, actual=cs.args))
        elif not values_equal(gs.result, cs.result, rel_tol=rel_tol):
            where = _first_mismatch(gs.result, cs.result, "$.result", rel_tol)
            report.steps.append(StepDiff(
                seq=gs.seq, status=STATUS_RESULT_DRIFT, tool=gs.tool,
                detail=f"result differs at {where}",
                expected=gs.result, actual=cs.result))
        else:
            report.steps.append(StepDiff(seq=gs.seq, status=STATUS_MATCHED, tool=gs.tool))
    return report
