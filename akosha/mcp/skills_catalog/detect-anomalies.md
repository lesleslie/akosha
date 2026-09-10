---
name: detect-anomalies
description: Use ONLY when the user explicitly types `/akosha:detect-anomalies` or selects this Skill from the picker to surface statistical anomalies in a tracked metric. Do not auto-trigger. Routes through `mcp__akosha__akosha_detect_anomalies` and explains Z-score interpretation, threshold tuning, and the difference between "noise" and "structural break." Useful when the user reports a sudden spike, a sudden drop, or wants to confirm whether a recent value is genuinely anomalous.
allowed-tools: mcp__akosha__akosha_detect_anomalies, Read
---

# detect-anomalies

## When to use

This Skill fires when the user has a tracked time-series metric and
wants to know which data points deviate from baseline. Common cases:

- "Did error rate spike yesterday?"
- "Are there any anomalous p99 latency readings in the last week?"
- "Find anomalous events around 2026-09-08."

The Skill drives `mcp__akosha__akosha_detect_anomalies` with sensible
defaults and walks the user through interpretation.

## The Z-score model

`akosha_detect_anomalies` uses Z-score anomaly detection:

- Compute `mean` and `stdev` over the time window.
- For each data point, compute `z = (value - mean) / stdev`.
- Flag points where `|z| > threshold_std`.

The default `threshold_std` is **3.0**, which under a normal
distribution captures ~99.7% of "normal" values inside ±3σ. Lower
thresholds (2.0) flag more anomalies — useful for noisy metrics where
you want sensitivity. Higher thresholds (4.0+) flag only extreme
outliers.

## Tuning

- `time_window_days`: 7 by default. Shorter windows adapt faster but
  have noisier baselines. 3-14 days is the sweet spot for most
  operational metrics.
- `threshold_std`: 3.0 default. If the user is seeing too many false
  positives, raise to 4.0. If they're missing real anomalies, lower
  to 2.0.
- `system_id`: optional filter to focus on one system.

## Interpreting the response

The tool returns:

```json
{
  "metric_name": "error_rate",
  "anomaly_count": 2,
  "total_points": 168,
  "anomaly_rate": 0.0119,
  "threshold": 3.0,
  "anomalies": [
    {"timestamp": "...", "value": 0.42, "z_score": 4.2, "deviation": 0.31},
    ...
  ]
}
```

- `anomaly_count`: how many points exceeded the threshold.
- `anomaly_rate`: `anomaly_count / total_points`. <5% is typical.
- `anomalies`: up to 10 of the worst offenders with their `z_score`.
- `z_score`: how many standard deviations from the mean. >5 is
  usually a structural break, not noise.

## When NOT to use

- For changepoint / structural break detection (segment-level
  transitions), use `mcp__akosha__akosha_analyze_changepoints` instead.
- For trend direction only (no anomaly scoring), use
  `mcp__akosha__akosha_analyze_trends`.

## Example flow

User: "Did error rate spike yesterday?"

Skill action:

```python
result = await mcp__akosha__akosha_detect_anomalies(
    metric_name="error_rate",
    time_window_days=2,
    threshold_std=3.0,
)
```

If `anomaly_count > 0`, surface the worst offender's `timestamp`,
`value`, and `z_score` to the user with a one-line interpretation.
