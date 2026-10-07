"""Recorder: wraps any tool-calling agent loop and appends steps to a trace.

Two ways to use it:

1. Context manager — the agent loop calls ``rec.record(...)`` / ``rec.call(...)``
   explicitly::

       with TraceRecorder("run.jsonl", agent="devops-agent", scenario="x") as rec:
           result = rec.call(backend, "get_service_status",
                             {"service": "payments-api"},
                             observation="alert: error-rate spike")

2. Decorator — injects a recorder into a function that accepts a ``recorder``
   keyword argument::

       @record_trace("run.jsonl", agent="devops-agent", scenario="x")
       def handle_alert(recorder, alert): ...
"""

from __future__ import annotations

import functools
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

from trace import Trace, Step, canonical


class TraceRecorder:
    def __init__(self, path: str, agent: str = "agent", scenario: str = "",
                 fixture_version: int = 1):
        self.path = path
        self.trace = Trace(
            meta={
                "trace_id": uuid.uuid4().hex,
                "agent": agent,
                "scenario": scenario,
                "fixture_version": fixture_version,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "format": "tool-call-trace/1",
            }
        )
        self._seq = 0
        self._file = None

    # ------------------------------------------------------------ recording
    def record(self, tool: str, args: Dict[str, Any], result: Dict[str, Any],
               observation: str = "", duration_ms: Optional[float] = None) -> Step:
        self._seq += 1
        step = Step(
            seq=self._seq,
            observation=observation,
            tool=tool,
            args=args or {},
            result=result or {},
            timestamp=datetime.now(timezone.utc).isoformat(),
            request_id=uuid.uuid4().hex,
            duration_ms=duration_ms,
        )
        self.trace.steps.append(step)
        if self._file is not None:
            # Stream steps to disk as they happen: a crashed run still
            # leaves a partial-but-valid trace on disk.
            self._file.write(canonical(step.to_dict()) + "\n")
            self._file.flush()
        return step

    def call(self, backend: Any, tool: str, args: Dict[str, Any],
             observation: str = "") -> Dict[str, Any]:
        """Execute one tool call against *backend* and record the step."""
        started = time.perf_counter()
        try:
            result = backend.call(tool, args)
        except Exception as exc:  # record the failure as the step's result
            result = {"error": f"{type(exc).__name__}: {exc}"}
        duration_ms = (time.perf_counter() - started) * 1000.0
        self.record(tool, args, result, observation=observation, duration_ms=duration_ms)
        return result

    # ------------------------------------------------------- context manager
    def __enter__(self) -> "TraceRecorder":
        self._file = open(self.path, "w", encoding="utf-8")
        self._file.write(canonical({"kind": "meta", **self.trace.meta}) + "\n")
        self._file.flush()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if self._file is not None:
                self._file.close()
        finally:
            self._file = None
        # Rewrite the file from the in-memory trace so the on-disk copy is
        # exactly what Trace.load() would round-trip (canonical form).
        self.trace.save(self.path)

    # ------------------------------------------------------------------ misc
    def __len__(self) -> int:
        return len(self.trace.steps)


def record_trace(path: str, agent: str = "agent", scenario: str = "",
                 fixture_version: int = 1) -> Callable:
    """Decorator injecting a ``TraceRecorder`` as the ``recorder`` kwarg."""

    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if "recorder" in kwargs:
                return fn(*args, **kwargs)
            with TraceRecorder(path, agent=agent, scenario=scenario,
                               fixture_version=fixture_version) as rec:
                kwargs["recorder"] = rec
                return fn(*args, **kwargs)

        return wrapper

    return decorator
