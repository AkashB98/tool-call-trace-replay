"""Demo agent: deterministic rule-based incident responder.

The agent is deliberately *not* clever: a fixed policy maps an incoming
alert to a sequence of tool calls, branching only on the tool results it
sees. Because the backend is seeded, the same alert always produces the
same trace — which is exactly what makes the trace a useful golden.

Scenarios (all fictional, Helios Cloud):
  payments-error-spike — error-rate alert on payments-api
  disk-space-warning   — disk usage alert on node-07
  latency-p99          — p99 latency alert on search-api

Optional LLM hook: pass ``policy=<callable>`` to swap the rule-based
policy for your own (e.g. an LLM planner). The hook is a seam, not a
dependency — nothing here imports an LLM client, and tests/evals never
use it.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from backend import ScriptedToolBackend
from recorder import TraceRecorder


def _sev_for(metric_value: float, threshold: float) -> str:
    return "sev2" if metric_value >= threshold else "sev3"


def rule_policy(alert: Dict[str, Any], call: Callable) -> Dict[str, Any]:
    """Fixed policy. ``call(tool, args, observation)`` executes + records."""
    kind = alert["kind"]
    if kind == "payments-error-spike":
        service = "payments-api"
        call("get_service_status", {"service": service},
             observation="alert: error-rate spike on payments-api")
        logs = call("tail_logs", {"service": service, "lines": 50},
                    observation="status degraded; pull recent logs")
        metric = call("get_metric", {"service": service, "metric": "error_rate",
                                     "window": "15m"},
                      observation="logs show upstream gateway timeouts; measure error rate")
        restarts = [l for l in logs["lines"] if "timeout" in l]
        if len(restarts) >= 2:
            call("restart_service", {"service": service},
                 observation=f"{len(restarts)} timeout lines; restart to clear half-open breaker")
            health = call("run_healthcheck", {"service": service},
                          observation="verify service recovered after restart")
            sev = "sev2" if not health["healthy"] else "sev3"
        else:
            health = call("run_healthcheck", {"service": service},
                          observation="timeouts sparse; healthcheck before deciding")
            sev = "sev3"
        inc = call("create_incident", {"service": service, "severity": sev,
                                       "title": "payments-api error-rate spike"},
                   observation="open incident for the on-call trail")
        return {"incident_id": inc["incident_id"], "severity": sev}

    if kind == "disk-space-warning":
        service = "node-07"
        metric = call("get_metric", {"service": service, "metric": "disk_usage_pct",
                                     "window": "5m"},
                      observation="alert: disk usage high on node-07")
        call("get_service_status", {"service": service},
             observation="check node health before acting")
        sev = _sev_for(metric["value"], 95.0)
        inc = call("create_incident", {"service": service, "severity": sev,
                                       "title": "node-07 disk usage warning"},
                   observation="page storage on-call; no auto-remediation for disks")
        ack = call("ack_incident", {"incident_id": inc["incident_id"]},
                   observation="acknowledge so it stops paging")
        return {"incident_id": inc["incident_id"], "ack": ack["status"]}

    if kind == "latency-p99":
        service = "search-api"
        metric = call("get_metric", {"service": service, "metric": "latency_p99_ms",
                                     "window": "15m"},
                      observation="alert: p99 latency above SLO on search-api")
        call("get_service_status", {"service": service},
             observation="confirm service is up before scaling")
        call("scale_replicas", {"service": service, "replicas": 6},
             observation="add capacity; latency is load-shaped")
        health = call("run_healthcheck", {"service": service},
                      observation="verify new replicas serve traffic")
        sev = "sev2" if not health["healthy"] else "sev3"
        inc = call("create_incident", {"service": service, "severity": sev,
                                       "title": "search-api p99 latency breach"},
                   observation="record the scaling action for review")
        return {"incident_id": inc["incident_id"], "replicas": 6}

    raise ValueError(f"unknown alert kind {kind!r}")


class DemoAgent:
    """Runs one alert through the policy, recording every step."""

    def __init__(self, backend: Optional[ScriptedToolBackend] = None,
                 policy: Optional[Callable] = None):
        self.backend = backend or ScriptedToolBackend()
        self.policy = policy or rule_policy

    def handle(self, alert: Dict[str, Any], recorder: TraceRecorder) -> Dict[str, Any]:
        def call(tool: str, args: Dict[str, Any], observation: str = "") -> Dict[str, Any]:
            return recorder.call(self.backend, tool, args, observation=observation)

        return self.policy(alert, call)


ALERTS = {
    "payments-error-spike": {"kind": "payments-error-spike", "source": "slo-monitor"},
    "disk-space-warning": {"kind": "disk-space-warning", "source": "node-monitor"},
    "latency-p99": {"kind": "latency-p99", "source": "slo-monitor"},
}


def record_scenario(scenario: str, path: str, fixture_version: int = 1) -> str:
    """Record one scenario end-to-end. Returns the trace path."""
    if scenario not in ALERTS:
        raise ValueError(f"unknown scenario {scenario!r}; choose from {sorted(ALERTS)}")
    backend = ScriptedToolBackend(fixture_version=fixture_version)
    agent = DemoAgent(backend=backend)
    with TraceRecorder(path, agent="devops-demo-agent", scenario=scenario,
                       fixture_version=fixture_version) as rec:
        agent.handle(ALERTS[scenario], rec)
    return path
