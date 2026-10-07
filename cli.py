"""CLI: record | replay | diff | verify | serve.

Exit codes (CI gate):
  0 = match (no drift)
  1 = drift detected
  2 = error (bad file, unknown scenario, backend blew up, ...)
"""

from __future__ import annotations

import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, Optional

from backend import ScriptedToolBackend
from demo_agent import ALERTS, record_scenario
from diff import DiffReport, diff_traces
from replayer import replay
from trace import Trace, canonical

EXIT_MATCH, EXIT_DRIFT, EXIT_ERROR = 0, 1, 2


def _load_trace(path: str) -> Optional[Trace]:
    try:
        return Trace.load(path)
    except FileNotFoundError:
        print(f"error: trace file not found: {path}", file=sys.stderr)
    except (ValueError, OSError) as exc:
        print(f"error: cannot read trace {path}: {exc}", file=sys.stderr)
    return None


def cmd_record(args: argparse.Namespace) -> int:
    try:
        path = record_scenario(args.scenario, args.out,
                               fixture_version=args.fixture_version)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    trace = Trace.load(path)
    print(f"recorded {len(trace)} steps -> {path}")
    return EXIT_MATCH


def cmd_replay(args: argparse.Namespace) -> int:
    trace = _load_trace(args.trace)
    if trace is None:
        return EXIT_ERROR
    backend = ScriptedToolBackend(fixture_version=args.fixture_version)
    report = replay(trace, backend)
    payload = canonical(report.to_dict())
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(payload + "\n")
        print(f"replay report -> {args.out}")
    print(f"verdict={report.verdict} counts={report.counts}")
    for s in report.steps:
        if s.status != "matched":
            print(f"  seq={s.seq} {s.status}: {s.detail}")
    return {"match": EXIT_MATCH, "drift": EXIT_DRIFT, "error": EXIT_ERROR}[report.verdict]


def cmd_diff(args: argparse.Namespace) -> int:
    golden = _load_trace(args.golden)
    candidate = _load_trace(args.candidate)
    if golden is None or candidate is None:
        return EXIT_ERROR
    report = diff_traces(golden, candidate)
    payload = canonical(report.to_dict())
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(payload + "\n")
        print(f"diff report -> {args.out}")
    print(report.summary())
    return EXIT_DRIFT if report.drifted else EXIT_MATCH


def cmd_verify(args: argparse.Namespace) -> int:
    """CI gate. With --trace: diff golden vs trace. Without: re-record the
    golden's scenario fresh and diff — catches nondeterminism and drift."""
    golden = _load_trace(args.golden)
    if golden is None:
        return EXIT_ERROR
    if args.trace:
        candidate = _load_trace(args.trace)
        if candidate is None:
            return EXIT_ERROR
    else:
        scenario = golden.meta.get("scenario", "")
        fixture_version = int(golden.meta.get("fixture_version", 1) or 1)
        if scenario not in ALERTS:
            print(f"error: golden has unknown scenario {scenario!r}", file=sys.stderr)
            return EXIT_ERROR
        import tempfile, os
        fd, tmp = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        try:
            record_scenario(scenario, tmp, fixture_version=fixture_version)
            candidate = Trace.load(tmp)
        finally:
            os.unlink(tmp)
    report = diff_traces(golden, candidate)
    print(report.summary())
    return EXIT_DRIFT if report.drifted else EXIT_MATCH


# ------------------------------------------------------------------- serve
_LAST_REPORT: Dict[str, Any] = {}


class _Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, payload: Dict[str, Any]) -> None:
        body = canonical(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/replay":
            return self._send(404, {"error": "unknown endpoint"})
        length = int(self.headers.get("Content-Length", 0) or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
            trace = Trace.from_jsonl("\n".join(
                canonical(line) for line in body["trace"]))
            backend = ScriptedToolBackend(
                fixture_version=int(body.get("fixture_version", 1)))
        except (KeyError, ValueError, TypeError) as exc:
            return self._send(400, {"error": f"bad request: {exc}"})
        report = replay(trace, backend)
        _LAST_REPORT.clear()
        _LAST_REPORT.update(report.to_dict())
        self._send(200, _LAST_REPORT)

    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/report":
            return self._send(404, {"error": "unknown endpoint"})
        if not _LAST_REPORT:
            return self._send(404, {"error": "no replay run yet"})
        self._send(200, _LAST_REPORT)

    def log_message(self, *a: Any) -> None:  # keep serve output quiet
        pass


def cmd_serve(args: argparse.Namespace) -> int:
    server = HTTPServer(("127.0.0.1", args.port), _Handler)
    print(f"serving on http://127.0.0.1:{args.port}  (POST /replay, GET /report)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return EXIT_MATCH


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="trace-replay",
                                description="Record, replay, and diff agent tool-call traces.")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("record", help="run the demo agent and record a trace")
    r.add_argument("--scenario", required=True, choices=sorted(ALERTS))
    r.add_argument("--out", required=True)
    r.add_argument("--fixture-version", type=int, default=1)
    r.set_defaults(func=cmd_record)

    r = sub.add_parser("replay", help="replay a trace against the scripted backend")
    r.add_argument("--trace", required=True)
    r.add_argument("--fixture-version", type=int, default=1)
    r.add_argument("--out", default=None)
    r.set_defaults(func=cmd_replay)

    r = sub.add_parser("diff", help="diff two traces step by step")
    r.add_argument("--golden", required=True)
    r.add_argument("--candidate", required=True)
    r.add_argument("--out", default=None)
    r.set_defaults(func=cmd_diff)

    r = sub.add_parser("verify", help="CI gate: diff golden vs trace (exit 0/1/2)")
    r.add_argument("--golden", required=True)
    r.add_argument("--trace", default=None)
    r.set_defaults(func=cmd_verify)

    r = sub.add_parser("serve", help="tiny JSON API: POST /replay, GET /report")
    r.add_argument("--port", type=int, default=8765)
    r.set_defaults(func=cmd_serve)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
