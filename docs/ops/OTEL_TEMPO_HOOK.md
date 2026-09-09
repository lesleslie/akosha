---
status: active
role: operations
date: 2026-09-09
last_reviewed: 2026-09-09
topic: otel-tempo-hook
superseded_by: null
related_decisions:
  - mahavishnu/.claude/decisions/wire-up-contract.md
  - akosha/docs/superpowers/plans/2026-09-09-akosha-otel-feed-recovery.md
audited_by: []
---

# OTel → Tempo Hand-off

Replaces the `otelcol.exporter.debug` placeholder in `/usr/local/etc/grafana-alloy/config.alloy` with a real `otelcol.exporter.otlp` to a Tempo backend. Until this hook is exercised, traces received by Alloy are emitted to Alloy's stdout and discarded — there is no queryable history.

## Current state (placeholder)

In `/usr/local/etc/grafana-alloy/config.alloy`:

```river
otelcol.processor.batch "cli_tools_traces" {
    timeout = "2s"
}

otelcol.exporter.debug "cli_tools_traces" { }

// OTLP receiver's `output` block:
otelcol.receiver.otlp "cli_tools_traces" {
    http { endpoint = "127.0.0.1:4318" }
    grpc { endpoint = "127.0.0.1:4317" }
    output {
        metrics = [...]
        traces  = [otelcol.processor.batch.cli_tools_traces.input]
    }
}
```

The `debug` exporter writes one log line per received span to the Alloy stdout/log. It is the only sink in the current pipeline. There is no persistence; there is no query path.

## Why this matters

- Akosha's `/health` `local_traces_feed.feed_populated` will stay `false` until a real client exports a span into Alloy **and** a downstream consumer (Tempo) makes it queryable. The current pipeline receives spans, then drops them.
- The OTel ingester in `akosha/ingestion/otel_ingester.py` polls Alloy's OTLP endpoint with empty envelopes; when a real trace arrives at Alloy and a consumer (Tempo) is configured, the consumer can be queried independently. Akosha's hot-store OTel ingester is the **consumer-of-the-Aggregator** path; this doc covers the **receiver-to-storage** path inside Alloy.

## Tempo install options

Pick one. Both run Tempo listening on the standard OTLP ports (4317 gRPC, 4318 HTTP) on localhost.

### Option A: Docker

```bash
docker run -d --name tempo \
  -p 4317:4317 -p 4318:4318 -p 3200:3200 \
  -v /tmp/tempo-data:/var/tempo \
  grafana/tempo:latest \
  --target=all --storage.trace.backend=local --storage.trace.local.path=/var/tempo
```

Data lives at `/tmp/tempo-data`; replace with a durable path in production.

### Option B: brew (macOS)

```bash
brew install tempo
tempo --target=all --storage.trace.backend=local \
  --storage.trace.local.path=/usr/local/var/tempo &
```

Tempo listens on `:4317` (gRPC), `:4318` (HTTP), `:3200` (UI/API).

### Option C: Native binary

Download from <https://github.com/grafana/tempo/releases> and run with the same flags.

## River config swap

Edit `/usr/local/etc/grafana-alloy/config.alloy`:

### 1. Replace the debug exporter with an OTLP exporter to Tempo

Replace:

```river
otelcol.exporter.debug "cli_tools_traces" { }
```

With:

```river
otelcol.exporter.otlp "cli_tools_traces" {
    client {
        endpoint = "127.0.0.1:4317"  // Tempo gRPC OTLP
        // tls { insecure = true }  // uncomment if Tempo is on a non-localhost host
    }
}
```

If you want HTTP (4318) instead of gRPC (4317):

```river
otelcol.exporter.otlphttp "cli_tools_traces" {
    client {
        endpoint = "http://127.0.0.1:4318"
    }
}
```

### 2. Re-route the receiver's `traces` output through batch → otlp

Replace the existing `traces = [...]` line in the receiver's `output` block:

```river
traces = [otelcol.exporter.debug.cli_tools_traces.input]
```

With:

```river
traces = [otelcol.processor.batch.cli_tools_traces.input]
```

(The `processor.batch` block stays as-is.)

### 3. Validate, format, restart

```bash
alloy fmt /usr/local/etc/grafana-alloy/config.alloy
plutil -lint /usr/local/etc/grafana-alloy/config.alloy || echo "alloy fmt OK"
launchctl kickstart -k gui/$(id -u)/homebrew.mxcl.grafana-alloy
```

## Verification

Smoke-test the pipeline end-to-end:

```bash
curl -fsS -X POST http://127.0.0.1:4318/v1/traces \
  -H 'Content-Type: application/json' \
  -d '{
    "resourceSpans":[{
      "resource":{"attributes":[{"key":"service.name","value":{"stringValue":"tempo-hook-smoketest"}}]},
      "scopeSpans":[{"spans":[{
        "traceId":"0af7651916cd43dd8448eb211c80319c",
        "spanId":"b7ad6b7169203331",
        "name":"phase1-smoke",
        "startTimeUnixNano":"1700000000000000000",
        "endTimeUnixNano":"1700000000001000000"
      }]}]
    }]
  }'
```

Expected: HTTP 200, span visible in Tempo's trace search within 5s:

```bash
curl -fsS "http://127.0.0.1:3200/api/search?service=tempo-hook-smoketest" | jq .
```

Or open <http://localhost:3200> and search for `service.name = tempo-hook-smoketest`.

## Akosha-side verification (cross-check)

After the Tempo exporter is live, Akosha's OTel ingester will continue to poll `http://localhost:4318/v1/traces` with empty envelopes and receive 200s. That is expected: the ingester is a *consumer-of-the-receiver*, not a *consumer-of-Tempo*. The Tempo-exported traces are queryable via Tempo's API/UI, not via Akosha's hot store. A future plan (out of scope here) is needed to wire Akosha's hot store to query Tempo directly.

## Rollback

If the new pipeline drops spans or Alloy fails to start:

1. `cp /usr/local/etc/grafana-alloy/config.alloy.bak-pre-otel-feed-recovery /usr/local/etc/grafana-alloy/config.alloy` (or whichever backup filename is current).
2. `launchctl kickstart -k gui/$(id -u)/homebrew.mxcl.grafana-alloy`.
3. Confirm `curl -fsS http://127.0.0.1:4318/v1/traces` (POST with empty envelope) returns 200 within 5s.

## Out of scope

- Akosha hot-store ↔ Tempo query bridge (separate plan).
- Tempo backend tuning (S3/GCS storage, retention, search concurrency).
- Grafana ↔ Tempo datasource wiring (operator-level, not in this hook).
- Authentication / mTLS for the OTLP exporter (only relevant if Tempo leaves localhost).
