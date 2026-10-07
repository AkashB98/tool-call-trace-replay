# tool-call-trace-replay

**Record / replay / diff harness for agent tool-call traces — the CI gate for catching agent behavior drift before customers do.**

Every team shipping AI agents to customers has the same nightmare: the agent worked in the demo, then a "small" backend change quietly altered what it does — different tool, different args, different result — and nobody noticed until a customer did. This repo is the fix for that: record a golden agent session as a trace, replay it against every new backend build, and diff step-by-step. Exit code 0 means behavior is pinned; exit 1 means something drifted, with the exact step named.

Built to pair with two sibling projects: [mcp-tool-server](https://github.com/AkashB98/mcp-tool-server) (where the agent loop runs *live* against tools) and the [AI red-teaming harness](https://github.com/AkashB98/ai-red-teaming-harness) + [prompt-injection firewall](https://github.com/AkashB98/prompt-injection-firewall) (which grade and defend the agent *under attack*). This one pins the agent's behavior *over time*.

Stdlib-only Python. Zero network, zero API keys, zero personal data — the demo backend is a fictional seeded "Helios Cloud" devops toolset, so the whole thing runs offline.

## Quickstart

```bash
# 1. Record a golden trace: run the demo incident-responder, capture every step
python3 cli.py record --scenario payments-error-spike --out golden.jsonl

# 2. Replay it against the current backend (exact match -> exit 0)
python3 cli.py replay --trace golden.jsonl --fixture-version 1

# 3. Replay against changed fixtures (drift -> exit 1, names the steps)
python3 cli.py replay --trace golden.jsonl --fixture-version 2

# 4. CI gate: re-record the golden's scenario fresh, diff vs golden
python3 cli.py verify --golden golden.jsonl   # exit 0 = match, 1 = drift, 2 = error

# 5. Diff any two traces step by step
python3 cli.py diff --golden golden.jsonl --candidate candidate.jsonl

# 6. One-command end-to-end demo
python3 demo.py

# 7. Tiny JSON API (POST /replay, GET /report)
python3 cli.py serve --port 8765
```

## What a trace looks like

One JSON object per line. First line is metadata, then one step per line:

```json
{"kind":"meta","agent":"devops-demo-agent","scenario":"payments-error-spike","fixture_version":1,"format":"tool-call-trace/1", ...}
{"kind":"step","seq":3,"observation":"logs show upstream gateway timeouts; measure error rate","tool":"get_metric","args":{"service":"payments-api","metric":"error_rate","window":"15m"},"result":{"service":"payments-api","metric":"error_rate","window":"15m","value":0.038,"unit":"ratio"},"timestamp":"...","request_id":"...","duration_ms":0.04}
```

## Architecture

| Module | Job |
|---|---|
| `trace.py` | `Step` / `Trace` model, JSONL serialization, `normalized()` — strips volatile fields (timestamps, request ids, durations) so diffs compare *behavior*, not clock noise |
| `recorder.py` | `TraceRecorder` context manager + `@record_trace` decorator; wraps any tool-calling loop. `rec.call(backend, tool, args, observation)` executes one tool call and records the step. Streams to disk as it goes, so a crashed run still leaves a valid partial trace |
| `backend.py` | `ScriptedToolBackend` — fictional seeded devops toolset (8 tools: `get_service_status`, `get_metric`, `tail_logs`, `restart_service`, `scale_replicas`, `run_healthcheck`, `create_incident`, `ack_incident`). Deterministic per fixture version; v2 deliberately drifts two values for drift-detection demos |
| `demo_agent.py` | Rule-based incident responder over three scenarios (`payments-error-spike`, `disk-space-warning`, `latency-p99`). Branches on tool results, but deterministically — same alert always yields the same trace. Optional `policy=` hook is the LLM seam (never used in tests/evals) |
| `replayer.py` | Re-executes a trace's tool calls in order against a fresh backend; compares each actual result to the recorded one → `match` / `drift` / `error` verdict per step and overall |
| `diff.py` | Structured trace-vs-trace diff: `matched` / `tool_drift` / `arg_drift` / `result_drift` / `missing` / `extra`. Comparison rules: volatile fields normalized away; dict key order ignored; **lists of dicts compared order-insensitively**; numbers compared with relative tolerance (1e-6); bools never equal numbers (the `True == 1` trap) |
| `cli.py` | `record` / `replay` / `diff` / `verify` (exit 0/1/2 for CI) / `serve` (stdlib JSON API) |
| `demo.py` | End-to-end narrative: record goldens → exact replay → drifted replay → fresh-record diff → exit codes |

## Evals

`python3 evals/run_evals.py` → 12/12 golden evals, report committed at `evals/eval_report.json` (byte-identical across runs):

- **E1–E3** exact replay matches for all three scenarios
- **E4** drifted fixtures flag *exactly* steps 3 (`get_metric`) and 4 (`restart_service`) — no more, no fewer
- **E5** shuffled dict key order → no drift (false-positive trap)
- **E6** timestamp/request-id/duration skew → no drift (false-positive trap)
- **E7** numeric tolerance boundary pinned: relative change 5e-7 → match, 2e-6 → drift
- **E8/E9** missing step flagged at the right seq; extra step flagged
- **E10** tool swap → `tool_drift` (not misclassified as arg/result drift)
- **E11** determinism: re-record → byte-identical normalized traces; re-replay → byte-identical reports
- **E12** CLI exit codes 0/1/2

Unit tests: `python3 -m unittest discover -s tests` → 77 hermetic tests, including a no-network enforcement test (sockets stubbed) and a 20k-case fuzz of the comparator (zero crashes, reflexive, symmetric).

## Scope and honest limits

- **Positional step alignment.** The differ aligns steps by position, not by content. If the agent inserts a genuinely new step in the *middle* of a trace, downstream steps report as `tool_drift` rather than one `extra` + matches. For incident-response style linear traces this is the right trade-off (order *is* behavior); a sequence-alignment differ is future work.
- **Scripted backend.** Replay re-executes recorded tool calls against a deterministic backend — it tests *backend/tool behavior drift*, not agent-policy drift. If your agent is LLM-driven and nondeterministic, record several goldens and treat any single diff as a signal, not a verdict.
- **No wall-clock inside results.** The backend contract requires results free of wall-clock timestamps (enforced by a test); a backend that embeds `datetime.now()` in results will false-positive on every replay. That's by design — nondeterministic results are the bug.
- **Demo agent is deliberately simple.** A fixed rule-based policy exists to make goldens meaningful. The `policy=` hook is where a real planner plugs in.

## Dev loop: bugs the tests actually caught

1. **Order-insensitive compare defeated numeric tolerance.** The first version of `values_equal` sorted lists-of-dicts by their canonical JSON *strings* and then compared the strings exactly — so `180.0` vs `180.0000001` in a reordered `checks` list flagged drift. Fix: sort the original dicts by canonical key, compare pairwise with tolerance. Caught by `test_list_of_dicts_with_float_wobble` (verified: fails on the old code, passes on the new).
2. **Drift location blamed the reordering, not the drift.** `_first_mismatch` (which writes the human-readable `$.result.checks[0].v` path in diff reports) compared reordered lists positionally, so a real value change inside a reordered list was reported at the wrong element. Fix: sort both sides by canonical key before walking. Caught by `test_reordered_list_points_at_true_drift` (verified: fails on the old code).
3. **Fuzzing the comparator.** 20,000 randomized value pairs: zero crashes, and reflexivity (`equal(a, a)`) + symmetry held throughout — the ordering/tolerance composition doesn't have a hidden asymmetry.
4. **Recorder cleanup.** A leftover dead line in `TraceRecorder.record` (a no-op write expression from an early draft) was removed during review; the crash-safety test (`test_partial_trace_survives_exception`) confirms a dying agent still leaves a parseable trace.

## Why this project

Every forward-deployed team shipping agents to customers needs three things: a way to run agents against tools (mcp-tool-server), a way to grade them under attack (red-teaming harness + firewall), and a way to *pin their behavior in CI* so last week's working agent is still this week's working agent. This repo is the third leg — golden trace replay with step-level diffs and CI exit codes.
