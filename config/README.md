# Akosha Legacy Configuration Directory — ARCHIVED

> **⚠️ ARCHIVED 2026-09-09** — This directory contains **legacy** Akosha
> configuration templates (`akosha.yaml`, `lite.yaml`, `standard.yaml`) that
> predate the Oneiric layered-config adoption. They are retained for
> provenance but are **not loaded by Akosha**.

## Canonical configuration

The **single source of truth** for runtime configuration is:

- **`settings/akosha.yaml`** — Oneiric layered-config (defaults → `settings/akosha.yaml` → `settings/local.yaml` → env vars prefixed with `AKOSHA_`)

Loaded by `akosha/config.py`. See `ARCHITECTURE.md` § Configuration for the
authoritative documentation.

## Why these files exist (legacy context)

The three files in this directory — `akosha.yaml`, `lite.yaml`, `standard.yaml` —
were used in the pre-Oneiric architecture (Jan–Feb 2026). They use port numbers
that predate the Bodai port convention:

| File | Legacy ports | Notes |
|------|--------------|-------|
| `akosha.yaml` | `api.port: 8000`, `mcp.port: 3001` | Pre-Oneiric defaults |
| `lite.yaml` | `mcp_port: 3002` | Lite-mode template |
| `standard.yaml` | `mcp_port: 3002` | Standard-mode template |

## Do not use these files

If you apply these or set environment variables from them, Akosha will not
read them. The canonical port is **8682** (see `akosha/config.py:DEFAULT_MCP_PORT`).