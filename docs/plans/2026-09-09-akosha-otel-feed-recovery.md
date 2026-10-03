---
status: draft
role: implementation
date: 2026-09-09
last_reviewed: 2026-09-09
superseded_by: null
topic: akosha-otel-feed-recovery
plan_kind: repair
replaces: null
related_decisions:
  - mahavishnu/.claude/decisions/wire-up-contract.md
  - mahavishnu/.claude/decisions/mcp-backend-wiring-discipline.md
  - akosha/docs/superpowers/plans/2026-09-06-otel-trace-ingester-impl.md
audited_by:
  - audit-2026-09-09-otel-correctness
  - audit-2026-09-09-bodai-policy
  - audit-2026-09-09-ops-risk
---

# Akosha OTel Feed Recovery Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

## 1. Outcome

Akosha's `/health` endpoint returns **200**, the `launch_with_healthcheck` wrapper stops killing the process every 60s, and `mcp__akosha__get_liveness` reports `ok` again — without masking the underlying trace-pipeline wiring. The OTel ingester speaks real OTLP/HTTP to the local Grafana Alloy collector, the collector's trace pipeline is wired end-to-end (with a debug exporter as a documented placeholder until a Tempo receiver is stood up), and the `/health` feed gate correctly distinguishes "producer running but feed empty because no upstream trace data has arrived yet" from "producer dead or unreachable."

**Concrete success signal:** `curl -fsS http://127.0.0.1:8682/health` returns `HTTP:200` with body `{"status":"ok", ...}` for at least 10 consecutive minutes under launchd supervision, AND `mcp__akosha__get_liveness` returns `{"status":"ok", ...}`, AND `claude mcp list` shows `akosha: http://localhost:8682/mcp (HTTP) - ✔ Connected`.

## 2. Goals

1. Restore `/health` 200 by repairing the actual root cause (OTLP/HTTP protocol mismatch on the OTel fetcher + missing trace pipeline in Alloy) rather than by relaxing the health gate.
2. Make the `local_traces_feed.ok` formula tolerate the "producer running but feed empty" steady state, with an explicit `feed_populated: false` signal so operators can grep for it.
3. Add structured per-poll logging so the next operator looking at the same symptom can distinguish "Alloy down" from "Alloy up but no exporter" from "Alloy up, exporter up, no spans produced yet."
4. Bring the akosha launchd plist in line with the safer `bootout`+`bootstrap` pattern documented in `2026-08-30-bodai-mcp-plist-stale-cli-subcommand`, and add `PYTHONUNBUFFERED=1` so future failures are visible.
5. Conform to `wire-up-contract.md` (Integration Contract blocks, REQ-IDs, `audit_orphans.py` gate, feature-tracking entry).

## 3. Non-Goals

1. Standing up a real Tempo receiver. The trace pipeline terminates in an `otelcol.exporter.debug` block; replacing that block with a `otelcol.exporter.otlp` to a real Tempo is a separate plan (`docs/ops/OTEL_TEMPO_HOOK.md` will be written in Phase 5 as a hand-off doc, but no Tempo wiring is in this plan).
2. Adding a query path from the Akosha hot store back to the OTel ingester. The hot store's `query_traces` API already uses `start_time`/`end_time` ISO8601; introducing `since_unix_nano` filtering into the hot store is out of scope.
3. Renaming the existing `AKOSHA_SKIP_OTEL_INGESTER` env var. We do not introduce a competing opt-out; the existing one continues to gate the lifespan.
4. Refactoring the `/health` assembly into a new testable helper. The `set_health_probe` / `build_app` pattern in `akosha/mcp/server.py:64-82` is already testable; we add a parity test for the `local_traces_ok` formula change instead.
5. Touching any plist, the launchd supervisor, or any other Bodai MCP server's plist as part of the code work. Phase 4 (plist edit) is a separate user-gated step.

## 4. Current Findings

The Akosha MCP server is alive (`launchctl print gui/501/com.mcp.akosha` reports `state = running` after a fresh `kickstart -k`), binds the FastMCP HTTP route on `127.0.0.1:8682`, but its `/health` endpoint returns **503** with body `{"status":"degraded", ...}`. The only failing sub-check is `local_traces_feed.ok=false`:

```json
"local_traces_feed": {
  "ok": false,
  "feed_entities_count": 0,
  "cycles_total": 1,
  "errors_total": 1,
  "feed_last_updated_timestamp": null,
  "otel_ingester_running": true,
  "otel_endpoint": "http://localhost:4318/v1/traces",
  "otel_cycles_total": 1,
  "otel_errors_total": 1,
  "source": "hot_store.query_traces (populated via kg_refresh + OtelTraceIngester)"
}
```

The 503 trips the `launch_with_healthcheck` wrapper after 60s, which SIGTERMs the process. The launchd supervisor (`KeepAlive.Crashed=true`) then respawns it, the new instance repeats the 503, and so on. `claude mcp list` shows `✔ Connected` because each `claude mcp list` invocation opens a fresh socket and sees the most recent process.

The three bugs that compose to produce the 503:

- **Bug #1 (Akosha fetcher):** `akosha/ingestion/otel_ingester.py:189` does `httpx.get(self.otlp_endpoint, params={"since": ...})`. The local Alloy OTLP receiver does not implement a `GET` route; only `POST /v1/traces` with a JSON or protobuf body. Verified by direct curl: `GET /v1/traces` → 404, `POST /v1/traces` with `{"resourceSpans":[]}` → 200.
- **Bug #2 (Alloy):** `/usr/local/etc/grafana-alloy/config.alloy:17-29` declares the OTLP receiver with HTTP on `:4318` and gRPC on `:4317`, but the receiver's `output { metrics = [...] }` block routes only metrics. Traces are accepted by the receiver and dropped at the receiver, with no downstream consumer.
- **Bug #3 (Akosha health assembly):** `akosha/mcp/server.py:770-774` defines `local_traces_ok = (local_traces_count > 0 or not any_producer_ever_cycled or not any_producer_alive)`. Once the OTel producer has run at least one cycle (which it has, with `otel_cycles_total=1` and an error), the formula reduces to `local_traces_count > 0`. If `kg_refresh` or the websocket subscriber has not yet written any rows, the feed stays `ok=false` even with a working fetcher.

The 404 we observe in the akosha log (`HTTP Request: GET http://localhost:4318/v1/traces?since=… "HTTP/1.1 404 Not Found"`) is the union of bug #1 and bug #2. Fixing only one of them does not restore `/health` 200.

## 4.5 Requirements

```yaml
requirements:
  - id: REQ-001
    title: "OTel ingester uses OTLP/HTTP POST with empty envelope, not GET-with-since"
  - id: REQ-002
    title: "Alloy config exposes a trace pipeline to a debug exporter (placeholder until Tempo)"
  - id: REQ-003
    title: "OtelTraceIngester logs method, path, status, bytes per poll cycle"
  - id: REQ-004
    title: "404/405/415 responses from the collector increment _errors_total and log at WARN"
  - id: REQ-005
    title: "local_traces_ok formula tolerates running-but-empty producer, with explicit feed_populated signal"
  - id: REQ-006
    title: "/health still returns 503 when any feed that has a producer reports ok=false for a non-collector reason"
  - id: REQ-007
    title: "Existing AKOSHA_SKIP_OTEL_INGESTER env var continues to gate the lifespan (no second opt-out)"
  - id: REQ-008
    title: "Plist edit uses bootout+bootstrap sequence with a backup file"
  - id: REQ-009
    title: "Plist has PYTHONUNBUFFERED=1 so restart failures are visible in akosha.err"
```

## 5. Implementation Phases

### Phase 1: Wire Alloy trace pipeline to a debug exporter

**Goal:** Give the local Grafana Alloy collector a real trace pipeline so `POST /v1/traces` is accepted end-to-end instead of being dropped at the receiver.

**Tasks:**
- Edit `/usr/local/etc/grafana-alloy/config.alloy`: add `otelcol.exporter.debug "cli_tools_traces"` and `otelcol.processor.batch "cli_tools_traces"`, and re-route the OTLP receiver's `traces` output through the batch to the debug exporter. The existing metrics pipeline is preserved unchanged.
- Validate with `alloy fmt` and a `brew services restart grafana-alloy` (or `launchctl kickstart -k gui/501/homebrew.mxcl.grafana-alloy`).
- Smoke-test with `curl -X POST http://127.0.0.1:4318/v1/traces -H 'Content-Type: application/json' -d '{"resourceSpans":[]}'` (expect 200) and a real one-span envelope (expect 200 and the span name visible in `alloy` logs via the debug exporter).

**Exit criteria:**
- `curl POST /v1/traces` returns HTTP 200 for both empty and non-empty envelopes.
- A real one-span envelope appears in the Alloy log via the debug exporter within 5 seconds.
- `python scripts/audit_orphans.py` (run in Phase 3 verification, but Phase 1 must not introduce new orphans) does not flag the new exporter or batch processor as zero-caller.

#### Integration Contract ← REQUIRED for every deliverable in this phase

- **Triggered from**: any OTLP/HTTP client (including the akosha ingester, Claude Code, Codex CLI, Qwen Code) that `POST`s to `http://127.0.0.1:4318/v1/traces`.
- **Returns to / updates**: alloy's debug log output (stdout / log file), until a real Tempo exporter is wired.
- **Demonstrable by**: `curl -fsS -X POST http://127.0.0.1:4318/v1/traces -H 'Content-Type: application/json' -d '{"resourceSpans":[{"resource":{"attributes":[{"key":"service.name","value":{"stringValue":"smoketest"}}]},"scopeSpans":[{"spans":[{"traceId":"0af7651916cd43dd8448eb211c80319c","spanId":"b7ad6b7169203331","name":"phase1-smoke","startTimeUnixNano":"1700000000000000000","endTimeUnixNano":"1700000000001000000"}]}]}]}'` returns `HTTP:200`, and `smoketest` / `phase1-smoke` appears in `~/.local/state/mcp/logs/alloy.log` (or wherever alloy writes logs) within 5 seconds. (REQs: REQ-002)
- **Rollback signal**: `brew services restart grafana-alloy` exits non-zero, OR the receiver starts returning 5xx, OR a real production tracer starts seeing dropped spans. In all cases, revert by re-applying the previous `config.alloy` from backup.
- **Observability added**: `otelcol.exporter.debug` emits one log line per received span; this is the observability surface for the placeholder. Will be replaced by `otelcol.exporter.otlp` to Tempo in a future plan.

### Phase 2: Real OTLP/HTTP POST in the OTel ingester

**Goal:** Fix the spec-violating `GET` in `_fetch_spans` so the ingester speaks real OTLP/HTTP to the local collector.

**Tasks:**
- Edit `akosha/ingestion/otel_ingester.py::_fetch_spans` to issue a `POST` with body `{"resourceSpans": []}` and `Content-Type: application/json`. Keep the `since_unix_nano` parameter for log/observability only — server-side filtering is not implemented at the collector.
- Add a `logger.info("OTel fetch method=POST path=%s status=%s bytes=%s", ...)` per poll cycle.
- Add `logger.warning("OTel fetch returned status=%s; treating as empty", ...)` and increment `_errors_total` for 404/405/415 responses.
- Add unit tests in `tests/unit/test_otel_trace_ingester.py` covering: (a) uses POST not GET (assert `.get` raises and `.post` is awaited), (b) unwraps a `resourceSpans` payload, (c) 404 returns empty + increments counter, (d) 5xx raises. (REQs: REQ-001, REQ-003, REQ-004)

**Exit criteria:**
- `pytest tests/unit/test_otel_trace_ingester.py -v` shows 4/4 pass.
- The full `pytest tests/akosha` suite remains green.

#### Integration Contract ← REQUIRED for every deliverable in this phase

- **Triggered from**: `OtelTraceIngester._polling_loop` (the `asyncio` task started in `akosha/mcp/server.py:574`).
- **Returns to / updates**: `_cycles_total`, `_errors_total`, `_last_poll_at` counters on the ingester; `_watermarks` dict on the ingester; `local_traces_feed` payload assembled in `akosha/mcp/server.py:738-770`.
- **Demonstrable by**: `pytest tests/unit/test_otel_trace_ingester.py -v` is 4/4 green. The `test_fetch_spans_uses_post_method_not_get` test asserts the protocol is `POST`. (REQs: REQ-001, REQ-003, REQ-004)
- **Rollback signal**: the lifespan-published `_otel_trace_ingester._errors_total` increases faster than `_cycles_total` after a clean install (would indicate the new POST is being rejected by something the old GET happened to be accepted by — should not happen, but if it does, revert via `git revert`). Also: `tail -n 200 /Users/les/.local/state/mcp/logs/akosha.err` shows repeated `OTel fetch returned status=…` warnings at >1 per minute.
- **Observability added**: the per-poll INFO line includes `method path status bytes`; the WARN line on 4xx responses is greppable via `rg "OTel fetch returned status" /Users/les/.local/state/mcp/logs/akosha.err`.

### Phase 3: Repair the `local_traces_ok` formula

**Goal:** Stop the formula from reporting `ok=false` when the OTel producer is alive but no spans have been ingested yet (because no real client has exported spans into Alloy yet).

**Tasks:**
- Capture a pre-change snapshot of `_build_health_payload` (or equivalent; the assembled `/health` body) into a JSON file in `tests/fixtures/health_snapshot_pre_otel_recovery.json`. (REQs: REQ-006, REQ-007)
- Edit `akosha/mcp/server.py` `local_traces_ok` calculation. New shape: `local_traces_ok = (otel_disabled) or (local_traces_count > 0) or (not any_producer_ever_cycled) or (not any_producer_alive)`. This is functionally the existing formula with one addition: when `AKOSHA_SKIP_OTEL_INGESTER=1` is set, `otel_disabled=True` short-circuits the gate. The existing `AKOSHA_SKIP_OTEL_INGESTER` env var continues to do the gating — no new env var. (REQs: REQ-005, REQ-007)
- Add a `feed_populated: bool` field to the `local_traces_feed` payload that is `local_traces_count > 0`. Operators can grep for `"feed_populated": false` to find steady-state-but-not-broken deployments. (REQs: REQ-005)
- Add a parity test in `tests/unit/test_mcp_server_lifespan.py` (or a new `tests/unit/test_local_traces_ok.py`) that:
  - Loads the pre-change snapshot JSON.
  - Invokes the modified health assembly.
  - Asserts: structural equality of every key other than `local_traces_feed`; for `local_traces_feed`, `ok` is preserved or improved; new `feed_populated` key matches the new contract. (REQs: REQ-005, REQ-006)
- Run `pytest tests/akosha` and confirm no regression.

**Exit criteria:**
- Parity test green.
- `pytest tests/akosha` fully green.
- `python scripts/audit_orphans.py` (from `akosha/`) reports no new zero-caller symbols.

#### Integration Contract ← REQUIRED for every deliverable in this phase

- **Triggered from**: `GET /health` on the akosha FastMCP HTTP route (`akosha/mcp/server.py:905-944`).
- **Returns to / updates**: the JSON body returned by `/health`, plus the `local_traces_feed.ok` value, plus the new `feed_populated` field.
- **Demonstrable by**: the parity test under `tests/unit/test_local_traces_ok.py::test_local_traces_ok_preserves_health_gate` (or equivalent name) passes. (REQs: REQ-005, REQ-006, REQ-007)
- **Rollback signal**: the parity test fails on a subsequent run, OR `claude mcp list` shows `akosha` as `Failed to connect` for any reason, OR `/health` returns 503 after a previously-green run.
- **Observability added**: the new `local_traces_feed.feed_populated` boolean is queryable via the same `/health` body; operators can `jq '.checks.local_traces_feed.feed_populated' /tmp/health.json` to confirm the steady state without reading log lines.

### Phase 4: Akosha plist hygiene + launchd recovery (USER-GATED)

**Goal:** Apply the safer `bootout`+`bootstrap` pattern, add `PYTHONUNBUFFERED=1`, and verify end-to-end that `/health` 200 persists under launchd supervision.

**This phase is separated because it touches `~/Library/LaunchAgents/com.mcp.akosha.plist` and a long-running supervisor. The user must explicitly approve this phase before it runs.**

**Tasks:**
- Backup the existing plist: `cp ~/Library/LaunchAgents/com.mcp.akosha.plist ~/Library/LaunchAgents/com.mcp.akosha.plist.bak-pre-phase4-2026-09-09`.
- Show the user a unified diff of the plist changes for approval. Two changes only:
  - Add `<key>EnvironmentVariables</key><dict><key>PYTHONUNBUFFERED</key><string>1</string></dict>` (or merge with existing `EnvironmentVariables` if any).
  - Do NOT change `StartInterval` (audit 3 flagged it as a separate follow-up).
  - Do NOT set `AKOSHA_SKIP_OTEL_INGESTER`.
- After user approval, apply the diff.
- Run `launchctl bootout gui/501/com.mcp.akosha` then `launchctl bootstrap gui/501 ~/Library/LaunchAgents/com.mcp.akosha.plist` then `launchctl kickstart -k gui/501/com.mcp.akosha`. (REQs: REQ-008, REQ-009)
- Wait up to 60s and poll `/health`. Verify HTTP 200. If 503 persists, run `tail -n 200 /Users/les/.local/state/mcp/logs/akosha.err` to determine whether the new code path has a bug, or whether the wrapper timed out before the lifespan published the OTel ingester.

**Exit criteria:**
- Plist diff approved by user before apply.
- `/health` returns HTTP 200 for 10 consecutive minutes under launchd supervision.
- `mcp__akosha__get_liveness` returns `ok`.
- `claude mcp list` shows `akosha` as `✔ Connected`.

#### Integration Contract ← REQUIRED for every deliverable in this phase

- **Triggered from**: user approval of the plist diff (the harness requires explicit confirmation before this phase runs).
- **Returns to / updates**: `~/Library/LaunchAgents/com.mcp.akosha.plist` (one-time edit), launchd state (reloaded), and the akosha process (restarted with the new env).
- **Demonstrable by**: `claude mcp list` shows `akosha: http://localhost:8682/mcp (HTTP) - ✔ Connected` AND `mcp__akosha__get_liveness` returns `{"status":"ok", ...}`. (REQs: REQ-008, REQ-009)
- **Rollback signal**: `/health` returns 503 for >2 consecutive minutes after the reload. Action: `cp ~/Library/LaunchAgents/com.mcp.akosha.plist.bak-pre-phase4-2026-09-09 ~/Library/LaunchAgents/com.mcp.akosha.plist` followed by `launchctl bootout` + `launchctl bootstrap` + `launchctl kickstart -k`.
- **Observability added**: `PYTHONUNBUFFERED=1` makes `akosha.err` stream live; future restart failures will be visible in the log instead of being buffered until the wrapper exits.

### Phase 5: Documentation hand-off (no code)

**Goal:** Document the Tempo hook for whoever wires a real OTel backend in the future.

**Tasks:**
- Create `akosha/docs/ops/OTEL_TEMPO_HOOK.md` describing: (1) the current `otelcol.exporter.debug` block is a placeholder, (2) the operator steps to install Tempo (Docker, native binary), (3) the River config snippet to replace the debug exporter with `otelcol.exporter.otlp` to Tempo, (4) the verification curl.
- Add a `docs/feature-tracking/2026-09-09-otel-feed-recovery.md` entry per `docs/feature-tracking/TEMPLATE.md`, tracking state `{built: true, wired: pending_phase4, adopted: false}`.
- Persist a one-line state summary via `mcp__session-buddy__store_reflection` with tags `["feature-tracking", "akosha-otel-feed-recovery", "wire-up-state"]`.

**Exit criteria:**
- Both files committed to akosha.
- Session-Buddy reflection stored.

#### Integration Contract ← REQUIRED for every deliverable in this phase

- **Triggered from**: completion of Phases 1–4.
- **Returns to / updates**: `akosha/docs/ops/OTEL_TEMPO_HOOK.md`, `akosha/docs/feature-tracking/2026-09-09-otel-feed-recovery.md`, and the Session-Buddy reflection store.
- **Demonstrable by**: `git -C akosha log --oneline -n 2` shows the new commits; `mcp__session-buddy__quick_search(query="akosha-otel-feed-recovery")` returns the reflection.
- **Rollback signal**: not applicable (documentation-only).
- **Observability added**: not applicable (documentation-only).

## 6. Required Code Changes

- [ ] `/Users/les/Projects/akosha/akosha/ingestion/otel_ingester.py` — replace GET with POST, add per-poll logging, add 4xx WARN+counter (Phase 2)
- [ ] `/Users/les/Projects/akosha/tests/unit/test_otel_trace_ingester.py` — add 4 new tests (Phase 2)
- [ ] `/Users/les/Projects/akosha/akosha/mcp/server.py` — extend `local_traces_ok` formula, add `feed_populated` field, no new env var (Phase 3)
- [ ] `/Users/les/Projects/akosha/tests/unit/test_local_traces_ok.py` — new parity test (Phase 3)
- [ ] `/Users/les/Projects/akosha/tests/fixtures/health_snapshot_pre_otel_recovery.json` — pre-change snapshot (Phase 3)
- [ ] `/Users/les/Projects/akosha/docs/ops/OTEL_TEMPO_HOOK.md` — operator hand-off doc (Phase 5)
- [ ] `/Users/les/Projects/akosha/docs/feature-tracking/2026-09-09-otel-feed-recovery.md` — feature-tracking entry (Phase 5)
- [ ] `/usr/local/etc/grafana-alloy/config.alloy` — add trace pipeline (Phase 1)
- [ ] `~/Library/LaunchAgents/com.mcp.akosha.plist` — add `PYTHONUNBUFFERED=1` (Phase 4, separately gated)

## 7. Validation Matrix

| Tool / command | Expected outcome | Evidence location |
|---|---|---|
| `alloy fmt /usr/local/etc/grafana-alloy/config.alloy` | No diff (config is canonical) | terminal output |
| `brew services restart grafana-alloy` | exit 0 | terminal output |
| `curl -fsS -X POST http://127.0.0.1:4318/v1/traces -d '{"resourceSpans":[]}'` | HTTP 200 | terminal output |
| `curl -fsS -X POST ... -d '{"resourceSpans":[{...,"name":"phase1-smoke"...}]}'` | HTTP 200, then `phase1-smoke` in alloy log within 5s | alloy log |
| `cd /Users/les/Projects/akosha && .venv/bin/pytest tests/unit/test_otel_trace_ingester.py -v` | 4/4 pass | terminal output |
| `cd /Users/les/Projects/akosha && .venv/bin/pytest tests/akosha` | all green | terminal output |
| `cd /Users/les/Projects/akosha && python scripts/audit_orphans.py` | no new zero-caller symbols | terminal output |
| `cd /Users/les/Projects/akosha && .venv/bin/pytest tests/unit/test_local_traces_ok.py` | parity test green | terminal output |
| `claude mcp list` (after Phase 4) | `akosha: ... ✔ Connected` | terminal output |
| `mcp__akosha__get_liveness` (after Phase 4) | `{"status":"ok", ...}` | tool output |
| `curl -fsS http://127.0.0.1:8682/health` (after Phase 4) | HTTP 200 with `"status":"ok"` | terminal output |

## 8. Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| Phase 1 Alloy config change is invalid syntax for the installed version (1.19.2 per audit 1) | Low | `alloy fmt` validates; commit only if `alloy fmt` returns no diff and `alloy --version` ≥ 1.4. |
| Phase 2 empty-POST returns 200 but the response is non-JSON or empty in a way the parser mishandles | Low | existing parser already handles `ValueError` from `response.json()`; new WARN line catches the case. |
| Phase 3 parity test misses a field that changed for an unrelated reason | Low | snapshot the pre-change payload first; assert key set equality, not just field equality. |
| Phase 4 plist edit introduces a typo that breaks launchd parsing | Low | backup plist before edit; user approves the diff before apply; `launchctl print` after reload. |
| `grafana/tap/tempo` is not a real Homebrew formula (the user already saw this), so Phase 5's Tempo hook doc must not depend on it | Known | Phase 5 doc uses Docker / native binary, not brew. |
| `local_traces_ok` is too permissive and hides a real wiring regression | Medium | `feed_populated: false` is greppable; `cycles_total` and `errors_total` remain on the payload; `AKOSHA_SKIP_OTEL_INGESTER` does not change. |
| `claude mcp list` shows `✔ Connected` while `/mcp` handshake fails (stale session socket) | Medium | the user must run `/mcp` → pick `akosha` to reinitialize if the in-session MCP call fails after restart. |
| The plist edit at Phase 4 races with an in-flight `akosha mcp start` | Low | `bootout` returns when the process is gone; `bootstrap` re-creates the registration idempotently. |

## 9. Decision Rule

The plan is "done enough" when: `/health` returns HTTP 200 with `"status":"ok"` and the OTel producer is wired (REQ-002 + REQ-005 + REQ-006 are all demonstrable), AND `mcp__akosha__get_liveness` returns `ok` for at least 10 minutes under launchd supervision (REQ-008 + REQ-009 are demonstrable), AND `pytest tests/akosha` is fully green (REQs 1, 3, 4, 5, 6 are covered by tests), AND `python scripts/audit_orphans.py` reports no new zero-caller symbols. The debug exporter is acceptable as a placeholder if (and only if) the hand-off doc at `akosha/docs/ops/OTEL_TEMPO_HOOK.md` exists and the feature-tracking entry marks `wired: pending_tempo`. If scope pressure forces a cut, drop Phase 5 (documentation) and ship the wired behavior; do not drop any of Phases 1–4.

---

## Self-Review

- **Spec coverage:** The plan implements the goals in §2 with explicit REQ-IDs in §4.5 and Integration Contracts in each phase. The validation matrix in §7 ties each goal to a checkable command.
- **Placeholder scan:** No "TBD" / "TODO" / "implement later" in any step. The only "pending" marker is the debug exporter in Phase 1, which is named explicitly as a placeholder with a hand-off doc.
- **Type consistency:** REQ-IDs are stable across §4.5, the Integration Contracts, and the validation matrix. `feed_populated`, `_errors_total`, `AKOSHA_SKIP_OTEL_INGESTER` are referenced consistently.
- **Phases are independently testable:** Phases 1, 2, 3 each have their own pytest scope. Phase 4 is the runtime plist edit. Phase 5 is documentation.
- **User gating:** Phase 4 is the only phase that requires user approval beyond the initial plan approval, and that is stated in the phase description.
- **Out of scope:** Tempo wiring, hot-store filtering, env-var renames, `/health` helper extraction — all explicitly excluded in §3.

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-09-09-akosha-otel-feed-recovery.md`. Two execution options:

1. **Subagent-Driven (recommended)** — I dispatch a fresh subagent per phase, review between phases, fast iteration.
2. **Inline Execution** — Execute phases in this session using `executing-plans`, batch execution with checkpoints.

Which approach?
