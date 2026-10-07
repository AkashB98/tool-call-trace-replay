"""Trace and Step model with JSONL serialization.

A *trace* is the recorded history of one agent run: an ordered list of
(observation -> tool call -> tool result) steps. Traces are stored as JSONL:
one JSON object per line. The first line is the trace metadata header;
every following line is a step.

Volatile fields (wall-clock timestamps, request ids, measured durations)
are recorded raw but stripped by :func:`normalize` so that diffing two
traces compares *behavior*, not clock noise.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional

KIND_META = "meta"
KIND_STEP = "step"

#: Fields that change run-to-run without meaning anything about behavior.
VOLATILE_STEP_FIELDS = ("timestamp", "request_id", "duration_ms")
VOLATILE_META_FIELDS = ("trace_id", "created_at")


def canonical(obj: Any) -> str:
    """Deterministic JSON encoding: sorted keys, no whitespace."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


@dataclass
class Step:
    seq: int
    observation: str
    tool: str
    args: Dict[str, Any] = field(default_factory=dict)
    result: Dict[str, Any] = field(default_factory=dict)
    # --- volatile fields (stripped by normalize()) ---
    timestamp: Optional[str] = None
    request_id: Optional[str] = None
    duration_ms: Optional[float] = None

    def to_dict(self, include_volatile: bool = True) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "kind": KIND_STEP,
            "seq": self.seq,
            "observation": self.observation,
            "tool": self.tool,
            "args": copy.deepcopy(self.args),
            "result": copy.deepcopy(self.result),
        }
        if include_volatile:
            d["timestamp"] = self.timestamp
            d["request_id"] = self.request_id
            d["duration_ms"] = self.duration_ms
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Step":
        return cls(
            seq=int(d["seq"]),
            observation=str(d.get("observation", "")),
            tool=str(d["tool"]),
            args=copy.deepcopy(d.get("args", {})),
            result=copy.deepcopy(d.get("result", {})),
            timestamp=d.get("timestamp"),
            request_id=d.get("request_id"),
            duration_ms=d.get("duration_ms"),
        )


@dataclass
class Trace:
    meta: Dict[str, Any] = field(default_factory=dict)
    steps: List[Step] = field(default_factory=list)

    # ------------------------------------------------------------------ IO
    def to_jsonl(self) -> str:
        lines = [canonical({"kind": KIND_META, **self.meta})]
        for step in self.steps:
            lines.append(canonical(step.to_dict()))
        return "\n".join(lines) + "\n"

    @classmethod
    def from_jsonl(cls, text: str) -> "Trace":
        meta: Dict[str, Any] = {}
        steps: List[Step] = []
        for lineno, line in enumerate(text.splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON on line {lineno}: {exc}") from exc
            kind = obj.get("kind")
            if kind == KIND_META:
                meta = {k: v for k, v in obj.items() if k != "kind"}
            elif kind == KIND_STEP:
                steps.append(Step.from_dict(obj))
            else:
                raise ValueError(f"line {lineno}: unknown record kind {kind!r}")
        steps.sort(key=lambda s: s.seq)
        return cls(meta=meta, steps=steps)

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(self.to_jsonl())

    @classmethod
    def load(cls, path: str) -> "Trace":
        with open(path, "r", encoding="utf-8") as fh:
            return cls.from_jsonl(fh.read())

    # ------------------------------------------------------- normalization
    def normalized(self) -> "Trace":
        """Copy with every volatile field stripped — the diffable form."""
        meta = {k: v for k, v in self.meta.items() if k not in VOLATILE_META_FIELDS}
        steps = []
        for s in self.steps:
            d = s.to_dict(include_volatile=False)
            d.pop("kind", None)
            steps.append(
                Step(
                    seq=d["seq"],
                    observation=d["observation"],
                    tool=d["tool"],
                    args=d["args"],
                    result=d["result"],
                )
            )
        return Trace(meta=meta, steps=steps)

    def normalized_jsonl(self) -> str:
        lines = [canonical({"kind": KIND_META, **self.normalized().meta})]
        for step in self.normalized().steps:
            d = step.to_dict(include_volatile=False)
            lines.append(canonical(d))
        return "\n".join(lines) + "\n"

    def __iter__(self) -> Iterator[Step]:
        return iter(self.steps)

    def __len__(self) -> int:
        return len(self.steps)
