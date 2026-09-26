---
status: active
role: discovery-note
kind: decision
date: 2026-09-26
last_reviewed: 2026-09-26
superseded_by: null
topic: mcp-launcher-akosha
---

# Akosha Launcher-Mode-Dispatch Discovery (Phase 4b Task 4b.1)

> **For agentic workers:** This is a **READ-ONLY** discovery note for Phase 4b Task 4b.1 (`docs/plans/2026-09-26-mcp-launcher-standardization.md` §5). The actual code change is gated on landing this note first.
> **Companion files:** `mahavishnu/docs/plans/2026-09-26-mcp-launcher-standardization.md` (the parent plan), `mcp-common/docs/mcp/launcher-cookbook.md` Example 3.
> **Cross-references:** REQ-001..008 (launcher shape), REQ-013 (backward-compat test matrix), REQ-014 (signal-handling smoke).

## Decision rule

Akosha's existing `_start_server` mode dispatch fits **Outcome A — straightforward** of the cookbook's Example 3 (mode is known at CLI entry; closure captures `mode_instance` from the enclosing scope). No launcher-kwargs extension is needed; the migration is a thin refactor of `_start_server` (~50 → ~80 LOC).

If the implementer encounters `mode` becoming late-bound (read from runtime config rather than the CLI flag), they should escalate to the parent plan's author before opening a PR — the cookbook's Outcome B would force an API extension.

## 1. Mode dispatch surface (what I observed)

### 1.1 Two entry points, one MCP factory

| Entry point | File:lines | Calls | Transport |
|---|---|---|---|
| Typer subcommand `akosha mcp start` (and legacy top-level `akosha start`) | `akosha/akosha/cli.py:368-440` (`_start_server`) | `create_app(mode=mode_instance)` then `app_instance.run(transport="streamable-http", ...)` | `streamable-http` |
| Local dev / dev-only | `akosha/akosha/mcp/__main__.py:1-22` | `create_app()` (no mode) then `uvicorn.run(app.http_app, ...)` | raw uvicorn ASGI |

The launchd plist (`/Users/les/Library/LaunchAgents/com.mcp.akosha.plist:36-48`) supervises the **Typer path**, never `__main__.py`. Migration targets only the Typer path. `__main__.py` is unchanged.

### 1.2 The mode registry (verbatim)

`akosha/akosha/modes/__init__.py:17-20`:

```python
_MODE_REGISTRY: dict[str, type[BaseMode]] = {
    "lite": LiteMode,
    "standard": StandardMode,
}
```

Verbatim mode list: **`["lite", "standard"]`** (also re-asserted at `akosha/akosha/cli.py:410` `valid_modes = ["lite", "standard"]`).

### 1.3 Mode dispatch shape (Typer path)

`akosha/akosha/cli.py:368-440` (`_start_server`):

```
Step 1. Typer --mode flag validation               (cli.py:410-414)
Step 2. config_dict = _load_config(config)         (cli.py:419)
Step 3. mode_instance = _init_mode(mode, config)   (cli.py:420 → _init_mode at cli.py:324-349)
Step 4. _configure_logging(verbose)                (cli.py:421)
Step 5. app_instance = create_app(mode=mode_instance)   (cli.py:424-426)
Step 6. app_instance.run(transport="streamable-http", host=host, port=port, path="/mcp",
                          uvicorn_config={"timeout_graceful_shutdown": 30})  (cli.py:434-440)
```

**Dispatch key**: validation against a `["lite", "standard"]` list at step 1, then `_init_mode()` (function dispatch on the `_MODE_REGISTRY` dict) at step 3. The mode is selected **before** `create_app` is called and is passed in as a kwarg.

### 1.4 What varies per mode

`akosha/akosha/mcp/server.py:415` captures `mode_instance = mode` in `create_app`'s enclosing scope. The lifespan then conditionally calls (server.py:493-535):

- `mode_instance.requires_external_services` (gate for Redis / cold storage init at line 493-495)
- `mode_instance.initialize_cache()` (line 504-505, returns the cache client)
- `mode_instance.initialize_cold_storage()` (line 532-533, returns the cold storage backend)

So **mode changes service wiring inside the lifespan** (`LiteMode` skips Redis/cold; `StandardMode` connects them). The dispatcher shape is identical across modes — only `initialize_*` calls fire or no-op. There is **no transport change** per mode, no FastMCP-instance-per-mode, no separate middleware.

### 1.5 FastMCP factory signature (verified)

`akosha/akosha/mcp/server.py:377`:

```python
def create_app(mode: Any | None = None) -> FastMCP:
```

- **Sync** (not `async def`). Returns a `FastMCP` instance.
- `mode` is a positional/keyword arg with `None` default.
- The lifespan inside `create_app` (server.py:426-1090) closes over `mode_instance` (the local copy of `mode` at line 415), NOT module globals. This matches the cookbook's REQ-003 variadic-closure contract cleanly.
- **Variadic closures with sync `create_app` work** with `mcp_common.server.launcher.launch()` per its docstring (`Callable[..., Any]` duck-typed; sync is fine because the launcher invokes `build_server()` synchronously at line 258 — `server = build_server()` — then awaits `run_with_uvicorn_config(server, ...)`).
- The same module exposes `__getattr__` at `akosha/mcp/__init__.py:18-24` which auto-calls `create_app()` with `mode=None` when `akosha.mcp.http_app` is referenced — but that path is not used by `_start_server` (it imports `create_app` directly).

### 1.6 Closure shape — does it work pre-bound?

Yes. `_start_server` runs the mode dispatch **before** `create_app` is called, so the post-migration rewrite can construct a `def build_server()` inside `_start_server` whose enclosing scope has `mode_instance` already bound. This is **Outcome A — straightforward** per the cookbook Example 3 contract.

## 2. Cookbook Example 3 mismatch analysis

The cookbook (Example 3, "ak (akosha) (mode dispatch collapse)") was written assuming mode selection happens in a wrapper script (`akosha/scripts/launch_mcp.py`). Reality differs:

| Cookbook assumed | Actual akosha shape | Resolution |
|---|---|---|
| Mode is selected in wrapper script before `launch()` | Mode is selected inside `_start_server` (Typer subcommand body) | Migrate `_start_server` instead of adding a wrapper. Closure is constructed inside `_start_server` after `_init_mode` returns `mode_instance`. |
| Wrapper replaces `_start_server` entirely | launchd plist (`com.mcp.akosha.plist:46-47`) invokes `python -m akosha mcp start`, so `_start_server` must still be the entry point | Refactor `_start_server` body to call `launch(build_server=...)`. CLI surface (`akosha mcp start [--mode lite|standard] [--host H] [--port P] [--config FILE] [--verbose]`) is preserved. |
| Closure factory `build_server(mode=...)` returns an inner `_build()` | Ak's `create_app(mode=...)` already takes mode as kwarg | Skip the indirection: closure is just `def build_server(): return create_app(mode=<captured>)` |
| Uses `transport="http"` | Ak today uses `transport="streamable-http"` | Plan REQ-007 normalizes to `transport="http"` (mahavishnu/oneiric/vishnu/cj all use this). See §5 Risks for client compatibility. |

Cookbook Example 3's "Outcome A" maps directly: the closure is built inside the dispatch branch (or, in our case, after the `_init_mode` call returns), and `launch()` is called once with the variadic closure. **No launcher-kwargs extension needed** (Outcome C not triggered). **Mode is not late-bound** (Outcome B not triggered).

## 3. Proposed migration shape

### 3.1 `_start_server` rewrite sketch (informative — NOT to be applied until this note is committed and PR is opened against akosha's repo)

```python
# akosha/akosha/cli.py:_start_server — proposed post-migration
def _start_server(host, port, mode, config, verbose):
    if verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    valid_modes = ["lite", "standard"]
    if mode not in valid_modes:
        typer.echo(f"❌ Invalid mode: {mode}", err=True)
        typer.echo(f"   Valid modes: {', '.join(valid_modes)}", err=True)
        raise typer.Exit(code=1)

    logger.info(f"Starting Akosha MCP server in {mode} mode on {host}:{port}")

    config_dict = _load_config(config) if config else {}
    mode_instance = _init_mode(mode, config_dict)
    _configure_logging(verbose)

    from akosha.mcp import create_app
    from mcp_common.server import launch

    # Closure binds mode_instance from enclosing scope (variadic Callable contract).
    def build_server() -> FastMCP:
        return create_app(mode=mode_instance)

    logger.info(f"✅ Akosha ready in {mode} mode")
    logger.info(f"   Mode: {mode_instance.mode_config.description}")
    logger.info(f"   External services required: {mode_instance.requires_external_services}")

    # The launcher handles transport="http" + uvicorn_config={"timeout_graceful_shutdown": 30}
    # (REQ-007) and pre-warms the `settings` health feed when settings_path is provided
    # (REQ-004 — ak currently has no settings.yaml so this is a no-op for us).
    asyncio.run(
        launch(
            build_server=build_server,
            component_name="akosha",
            secrets_path=Path("~/.config/secrets.env"),
            settings_path=None,            # ak has no settings.yaml
            host=host,
            port=port,
            timeout_graceful_shutdown=30,
        )
    )
```

**NET LOC change**: +`asyncio` import, +`Path` import (already imported), +`from mcp_common.server import launch`, +`asyncio.run(launch(...))` block, +3 imports reorder. Roughly +6 net lines vs. the bespoke `app_instance.run(...)` 6-line block. The total `_start_server` body grows ~10 LOC.

### 3.2 Migration cost summary

- **Files touched**: 1 (only `akosha/akosha/cli.py`)
- **New imports**: `asyncio` (already imported at module top? — verify before PR); `Path` is already imported (line 15); `from mcp_common.server import launch` (new)
- **Logic touched**: only the `app_instance.run(...)` 6-line block becomes `asyncio.run(launch(build_server=..., ...))`
- **Closure shape**: one closure per `_start_server` invocation; captures `mode_instance` from enclosing Typer function scope
- **Launcher kwargs for ak**: `secrets_path=Path("~/.config/secrets.env")`, `settings_path=None`, `host=host`, `port=port`. Mode is **not** a launcher kwarg — it's pre-bound in the closure. No launcher-kwargs extension needed.

## 4. Backward Compatibility Test Matrix row for ak

| Component | Public CLI commands | launchd plist `ProgramArguments` | Smoke test command |
|---|---|---|---|
| **ak (akosha)** | `akosha mcp start [--mode lite\|standard] [--host H] [--port P] [--config FILE] [--verbose]`; legacy top-level alias `akosha start [...]` | `/Users/les/Library/LaunchAgents/com.mcp.akosha.plist:36-48` — invokes `/Users/les/.local/state/mcp/scripts/launch_with_healthcheck.sh` with `--timeout 120 --` followed by `<akosha>/.venv/bin/python -m akosha mcp start` | `curl -fsS http://127.0.0.1:8682/health | python -m json.tool` returns HTTP 200, body contains `"launcher": "mcp_common.server.launcher@<version>"` |

The plist **does not change** — it invokes `python -m akosha mcp start` and `_start_server` still receives the `--mode` flag from the launchd environment (note: the plist does not pass `--mode`; the default `lite` at `cli.py:372-373` fires. Verify this is still the case after migration.)

**Two extra smoke tests** the implementer should add:

1. **Mode-specific smoke**: run `akosha mcp start --mode standard --port 8682` and `curl /health` returns 200 with `mode: "standard"` (or whatever the `/health` body reports). Repeat for `--mode lite`. The cookbook notes that mode is locked in the closure, so this test verifies the closure-capture path works for both modes.
2. **Signal handling smoke (REQ-014)**: `kill -TERM <pid>` exits cleanly within `timeout_graceful_shutdown=30s` (the launcher now enforces this; before migration, ak already passed `uvicorn_config={"timeout_graceful_shutdown": 30}` at `cli.py:439`, so this should be a no-regression test).

## 5. Risks

| Risk | Detail | Mitigation |
|---|---|---|
| **`create_app` is sync, launcher expects variadic Callable** | `create_app(mode: Any \| None = None) -> FastMCP` is a synchronous function. The launcher (mcp-common `launcher.py:258`) calls `server = build_server()` synchronously before awaiting `run_with_uvicorn_config`. | No risk — sync closures are supported. The launcher's docstring (`Callable[..., Any]`) is explicit. |
| **`transport="streamable-http"` vs `transport="http"`** | Ak currently uses `transport="streamable-http"` (`cli.py:435`). The launcher uses `transport="http"` per REQ-007. These may not be equivalent in FastMCP's transport matrix. | Verify FastMCP's `transport="http"` alias maps to `StreamableHTTPSessionManager` (which is what ak has been getting with `"streamable-http"`). If FastMCP rejects `"http"` for an ASGI path built via `lifespan=`, the implementer should escalate to the plan author — this is a launcher API change, not an ak local change. |
| **`/health` route does not include `"launcher"` field** | REQ-005 requires every consumer to register a `/health` route that emits `"launcher": "mcp_common.server.launcher@<version>"`. Ak already has a `/health` route at `server.py:1164` (comment: "HTTP health endpoint for Claude Code compatibility") plus a custom-route registration. **MUST VERIFY** whether the existing route handler emits `"launcher"` — if not, the migration needs to add it. | Verify during implementation: open `server.py` lines around 1164 and confirm the response JSON includes `launcher`; if absent, add `"launcher": f"mcp_common.server.launcher@{mcp_common.__version__}"` to the response dict. Reference: cookbook §"What the launcher gives you for free" #6. |
| **No `settings.yaml` consumed by ak** | Ak uses `--config` (a Yaml file at any path), not a fixed `settings.yaml`. The launcher's `warm_settings_feed` only fires when `settings_path` is provided, so this is opt-out cleanly: pass `settings_path=None`. | No risk — the launcher treats `None` as a no-op (mcp-common `launcher.py:255`). |
| **`secrets.env` location** | The launcher's default `DEFAULT_SECRETS_PATH = Path.home() / ".config" / "secrets.env"` matches the convention already used by mahavishnu's launch_mcp_with_secrets.py. Ak's plist doesn't preload secrets via shell init (it inherits the launchd environment), so the launcher's `load_secrets` is the right path. | Pass `secrets_path=Path("~/.config/secrets.env")` explicitly. Verify `/Users/les/.config/secrets.env` exists before smoke-testing. |
| **`RunAtLoad=true` on plist + macOS-reboot-cascade memory** | Plist has `RunAtLoad=true` and `KeepAlive.Crashed=true`, `KeepAlive.SuccessfulExit=false`. The macOS-reboot-cascade memory notes that `com.mcp.*` plists can thrash after a macOS update reboot. The discovery note does NOT modify the plist; this risk pre-exists. | Out of scope for this migration. The operator mitigates via `launchctl unload` after a known-bad reboot (per the memory). |
| **Two entry points: Typer vs `python -m akosha.mcp`** | The Typer path is the one in scope; `akosha/mcp/__main__.py` is a separate dev path that uses raw `uvicorn.run(app.http_app, ...)`. If a developer runs `python -m akosha.mcp`, they bypass the migration entirely. | Document this in the cookbook follow-up: the `__main__.py` path is dev-only and uses raw uvicorn for fast iteration; it does NOT use the launcher. Optionally add a comment to `akosha/mcp/__main__.py` clarifying this. |
| **Variadic closure captures `mode_instance` but not the asyncio loop** | `_init_mode(...)` constructs `LiteMode(config_dict)` synchronously; the lifespan that consumes `mode_instance` runs inside `app_instance.run(...)`'s event loop. The mode init must complete before the loop starts, which the synchronous `_init_mode` ensures. | No risk — same ordering as today's bespoke code. |

## 6. Migration steps (for the implementer)

After this note is committed (per the plan's "Block migration until note is committed"), the implementer:

1. **Verify `/health` route shape.** Open `akosha/akosha/mcp/server.py:1164` and confirm the registered `/health` handler emits `"launcher"` field. If absent, add it (REQ-005). Cite mcp-common `__version__` at runtime, not at module load.
2. **Edit `akosha/akosha/cli.py:_start_server`** to import `asyncio` (verify it's already imported; if not, add `import asyncio` near the other stdlib imports at the module top) and `from mcp_common.server import launch`. Replace lines 432-440 (the bespoke `app_instance.run(...)` call) with `asyncio.run(launch(build_server=lambda: create_app(mode=mode_instance), component_name="akosha", secrets_path=Path("~/.config/secrets.env"), settings_path=None, host=host, port=port, timeout_graceful_shutdown=30))`. Optionally make `build_server` a named inner function for readability.
3. **Preserve all CLI args.** The Typer decorator stack on `start` (`cli.py:443-454`) and `mcp_app.command("start")` (`cli.py:457-468`) does not change — the launchd plist passes no extra args beyond the Typer defaults.
4. **Smoke-test each mode.** `akosha mcp start --mode lite` and `akosha mcp start --mode standard --port 8683`, then `curl /health` on each port. Verify HTTP 200 and `"launcher"` field present.
5. **Verify launchd plist is unchanged.** The plist (`/Users/les/Library/LaunchAgents/com.mcp.akosha.plist`) must NOT be modified. `ProgramArguments` continue to invoke `python -m akosha mcp start`.
6. **Run the Backward Compatibility Test Matrix row** (per §4). All three columns green before the PR merges.
7. **Signal-handling smoke (REQ-014):** `kill -TERM <pid>` of the running server; assert exit code 0 within `timeout_graceful_shutdown + 5s`. The launcher already enforces this; before migration, ak also passed `uvicorn_config={"timeout_graceful_shutdown": 30}` at `cli.py:439`, so this should be a no-regression.

## 7. Commit sketch

```bash
# Inside akosha repo (no version bump, no tag — REQ-013 only):
git -c user.email=les@wedgwoodwebworks.com -c user.name=les \
    add akosha/akosha/cli.py
git -c user.email=les@wedgwoodwebworks.com -c user.name=les \
    commit -m "feat(akosha): migrate MCP server startup to mcp-common launcher (REQ-013)"
```

If step 1 above required adding the `"launcher"` field to `/health`, also include `akosha/akosha/mcp/server.py` in the same commit (it's part of the same REQ-005/REQ-013 contract).

## 8. Cross-references

- **Parent plan**: `mahavishnu/docs/plans/2026-09-26-mcp-launcher-standardization.md` §5 Phase 4b Task 4b.1 (this note is the deliverable); §4 Findings table row 4 (the "ak" row that lists `akosha/akosha/cli.py:368-440` as the bespoke entry); §7 Validation Matrix; §8 Risks.
- **Cookbook reference**: `mcp-common/docs/mcp/launcher-cookbook.md` Example 3 ("ak (akosha) (mode dispatch collapse)") — the "Before" pseudo-code at lines 333-367 matches `cli.py:368-440` verbatim; the "After" pseudo-code at lines 369-420 shows the wrong wrapper-script assumption (see §2 mismatch analysis); this note revises Example 3's "After" to migrate `_start_server` directly.
- **Launcher API**: `mcp-common/mcp_common/server/launcher.py` (`Callable[..., Any]` variadic at line 201; sync closure support at line 258; `transport="http"` enforced at line 187; `uvicorn_config={"timeout_graceful_shutdown": 30}` at line 190).
- **Phase REQs**: REQ-001..008 (launcher shape), REQ-013 (backward-compat matrix — the matrix row this note defines is a green-gate for Phase 4b Task 4b.3), REQ-014 (signal-handling smoke).

## 9. Decision status

- **Status**: active (this discovery note is committed; Phase 4b Task 4b.3 implementer can now open the migration PR)
- **Owner**: Phase 4b implementer for ak
- **Blockers**: none identified
- **Open question for implementer**: confirm `transport="http"` in the launcher maps to the same wire protocol as akosha's current `transport="streamable-http"` (verify against FastMCP 4.0.3 source before PR; if not, file a launcher-kwargs-extension issue per Outcome C)
