"""Deterministic scripted tool backend (fictional "Helios Cloud" devops toolset).

This is the world the demo agent acts in. Every tool returns seeded,
hard-coded fixtures — no network, no clock, no randomness — so the same
call sequence always produces the same results. That is what makes replay
meaningful: if replaying a recorded trace against the backend produces
different results, the *backend* (i.e. the code under test) changed.

``fixture_version`` simulates that change for drift-detection demos and
evals: version 2 alters two tool results relative to version 1.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List

BASE_TIME = "2026-10-03T09:00:00Z"  # fictional fixed clock; never wall time


class UnknownToolError(Exception):
    pass


class ToolArgError(Exception):
    pass


class ScriptedToolBackend:
    """Seeded devops/incident toolset. Fully deterministic per (version, call order)."""

    TOOLS = (
        "get_service_status",
        "get_metric",
        "tail_logs",
        "restart_service",
        "scale_replicas",
        "run_healthcheck",
        "create_incident",
        "ack_incident",
    )

    def __init__(self, fixture_version: int = 1):
        if fixture_version not in (1, 2):
            raise ValueError(f"unknown fixture_version {fixture_version!r}")
        self.fixture_version = fixture_version
        self._incident_seq = 0
        self._call_log: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------ API
    def call(self, tool: str, args: Dict[str, Any]) -> Dict[str, Any]:
        handler = getattr(self, f"_tool_{tool}", None)
        if handler is None or tool not in self.TOOLS:
            raise UnknownToolError(f"unknown tool {tool!r}")
        args = copy.deepcopy(args or {})
        result = handler(args)
        self._call_log.append({"tool": tool, "args": args, "result": copy.deepcopy(result)})
        return result

    @property
    def call_log(self) -> List[Dict[str, Any]]:
        return copy.deepcopy(self._call_log)

    # ---------------------------------------------------------------- tools
    def _tool_get_service_status(self, args: Dict[str, Any]) -> Dict[str, Any]:
        service = self._require(args, "service")
        statuses = {
            "payments-api": {"status": "degraded", "uptime_s": 864120, "version": "v2.14.3"},
            "node-07": {"status": "ok", "uptime_s": 2592000, "version": "n/a"},
            "search-api": {"status": "degraded", "uptime_s": 432000, "version": "v1.9.0"},
        }
        info = statuses.get(service, {"status": "ok", "uptime_s": 100000, "version": "v0.0.0"})
        return {"service": service, **info}

    def _tool_get_metric(self, args: Dict[str, Any]) -> Dict[str, Any]:
        service = self._require(args, "service")
        metric = self._require(args, "metric")
        window = args.get("window", "15m")
        # v2 fixture drifts two values: the drift-detection demo.
        v2 = self.fixture_version == 2
        table = {
            ("payments-api", "error_rate"): 0.041 if v2 else 0.038,
            ("payments-api", "latency_p99_ms"): 812.0,
            ("node-07", "disk_usage_pct"): 91.4,
            ("search-api", "latency_p99_ms"): 1240.0,
            ("search-api", "error_rate"): 0.012,
        }
        value = table.get((service, metric), 0.0)
        unit = {"error_rate": "ratio", "latency_p99_ms": "ms", "disk_usage_pct": "pct"}.get(metric, "x")
        return {"service": service, "metric": metric, "window": window,
                "value": value, "unit": unit}

    def _tool_tail_logs(self, args: Dict[str, Any]) -> Dict[str, Any]:
        service = self._require(args, "service")
        lines = int(args.get("lines", 50))
        scripted = {
            "payments-api": [
                "ERROR gateway timeout upstream=ledger-svc attempt=3",
                "ERROR gateway timeout upstream=ledger-svc attempt=2",
                "WARN  circuit breaker half-open upstream=ledger-svc",
                "ERROR gateway timeout upstream=ledger-svc attempt=3",
                "INFO  healthcheck ok latency=210ms",
            ],
            "node-07": [
                "WARN  disk usage 91% on /var/lib/docker",
                "WARN  disk usage 91% on /var/lib/docker",
                "INFO  logrotate completed",
            ],
            "search-api": [
                "WARN  p99 latency 1240ms exceeds slo=800ms",
                "WARN  p99 latency 1310ms exceeds slo=800ms",
                "INFO  autoscaler evaluating",
            ],
        }
        pool = scripted.get(service, ["INFO  nominal"])
        out = [pool[i % len(pool)] for i in range(min(lines, 5))]
        return {"service": service, "lines": out, "truncated": lines > 5}

    def _tool_restart_service(self, args: Dict[str, Any]) -> Dict[str, Any]:
        service = self._require(args, "service")
        v2 = self.fixture_version == 2
        return {"service": service, "status": "ok",
                "restarted_in_s": 4.2 if v2 else 3.9}

    def _tool_scale_replicas(self, args: Dict[str, Any]) -> Dict[str, Any]:
        service = self._require(args, "service")
        replicas = int(self._require(args, "replicas"))
        if not 1 <= replicas <= 32:
            raise ToolArgError("replicas must be 1..32")
        return {"service": service, "replicas": replicas, "status": "ok"}

    def _tool_run_healthcheck(self, args: Dict[str, Any]) -> Dict[str, Any]:
        service = self._require(args, "service")
        checks = [
            {"name": "http_200", "ok": True, "latency_ms": 180.0},
            {"name": "db_ping", "ok": True, "latency_ms": 12.0},
            {"name": "upstream_ledger", "ok": True, "latency_ms": 240.0},
        ]
        return {"service": service, "healthy": True, "checks": checks}

    def _tool_create_incident(self, args: Dict[str, Any]) -> Dict[str, Any]:
        service = self._require(args, "service")
        severity = self._require(args, "severity")
        title = self._require(args, "title")
        self._incident_seq += 1
        return {"incident_id": f"INC-{self._incident_seq:04d}", "service": service,
                "severity": severity, "title": title, "status": "open"}

    def _tool_ack_incident(self, args: Dict[str, Any]) -> Dict[str, Any]:
        incident_id = self._require(args, "incident_id")
        return {"incident_id": incident_id, "status": "acknowledged"}

    # ------------------------------------------------------------------ util
    @staticmethod
    def _require(args: Dict[str, Any], key: str) -> Any:
        if key not in args:
            raise ToolArgError(f"missing required arg {key!r}")
        return args[key]
