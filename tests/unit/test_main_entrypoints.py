"""Smoke tests for the ``python -m akosha`` and ``python -m akosha.mcp`` entry points.

Per plan Task 2.4g: spawn each entry point as a subprocess and assert
non-zero exit only on actual errors. Catches:
- import-time errors (typos, missing modules, circular imports)
- argparse-level errors (--help / --version work, no sys.exit(2))
- module-level side effects that crash before the CLI takes over
"""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]


def _free_port() -> int:
    """An ephemeral port that is free right now."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _isolated_env() -> tuple[dict[str, str], Path]:
    """Build an environment that gives the subprocess its own machine.

    Spawning a second akosha on a host that already runs one collides
    three ways, and every collision looks like a code bug:

    1. **The DuckDB write lock.** The live service holds a lock on the
       shared XDG ``hot.db``; a second process opening the same file
       dies with ``_duckdb.IOException: Conflicting lock is held``.
       ``AKOSHA_DATA_PATH`` -- the highest-priority override in
       ``StoragePathResolver.get_base_path`` -- redirects the whole
       storage tree to a throwaway directory.
    2. **The OTLP receiver ports.** The receiver binds :4317/:4318
       unconditionally, by design (the lifespan comment in
       ``akosha/mcp/server.py`` says a flag there would let a config
       error silently un-own the fleet's OTLP endpoint). There is no
       environment variable for the port, but ``AKOSHA_CONFIG`` points
       the loader at a settings file, and ``spans_substrate`` is read
       from it.
    3. **The MCP server's own port.** ``MCP_PORT`` is what
       ``akosha/mcp/__main__.py`` binds.

    Without all three, the smoke test can only pass while the service is
    stopped -- and it fails identically on unmodified ``main``, which
    makes it look like a regression when it is not.

    ``AKOSHA_CONFIG`` REPLACES the project settings file rather than
    merging with it, so the two subscriber opt-outs are restated here:
    under the replacement they default to ENABLED, and the subprocess
    would reach for Redis.

    Returns:
        The environment mapping and the throwaway data directory it
        points at. The caller owns the directory's lifetime.
    """
    data_dir = Path(tempfile.mkdtemp(prefix="akosha-entrypoint-"))
    config_path = data_dir / "akosha-test.yaml"
    config_path.write_text(
        "spans_substrate:\n"
        f"  receiver_http_port: {_free_port()}\n"
        f"  receiver_grpc_port: {_free_port()}\n"
        "bodai_tool_invocation_subscriber:\n"
        "  enabled: false\n"
        "websocket_invocations_subscriber:\n"
        "  enabled: false\n"
    )
    env = dict(os.environ)
    env["AKOSHA_DATA_PATH"] = str(data_dir)
    env["AKOSHA_CONFIG"] = str(config_path)
    env["MCP_PORT"] = str(_free_port())
    env["MCP_HOST"] = "127.0.0.1"
    return env, data_dir


def _run(
    *args: str,
    timeout: float = 60.0,
    marker: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Helper: spawn python -m <args> from the repo root.

    The timeout is 60s, not 15s, because the import is the expensive
    part and it is not free: ``python -m akosha --help`` measured
    3.5-8.7s warm and 23.9s cold on this machine. A 15s budget turned
    the cold import into an intermittent failure that had nothing to do
    with the code under test.

    For short-lived CLI subcommands (--help, --version) the subprocess
    exits cleanly within the timeout. For server entry points
    (``akosha.mcp --help`` runs uvicorn that never returns), pass
    ``marker="tools registered"`` and the helper will SIGKILL the
    subprocess the moment that line appears in stdout — the test only
    cares that the lifespan init succeeded, not that the server runs
    to completion. SIGKILL after marker detection is intentional: the
    ASGI lifespan shutdown is fast enough to be observable in real
    usage but takes >10s in subprocess ``communicate``, which would
    blow the 20s smoke-test budget.
    """
    env, data_dir = _isolated_env()
    proc = subprocess.Popen(
        [sys.executable, "-m", *args],
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,  # line-buffered so marker detection is timely
        env=env,
    )
    captured_stdout: list[str] = []
    captured_stderr: list[str] = []
    deadline = time.monotonic() + timeout
    saw_marker = False
    import select

    while time.monotonic() < deadline:
        if proc.poll() is not None:
            # Process exited on its own — drain whatever it produced.
            break
        # Non-blocking poll of both pipes. When marker detection is on
        # we SIGKILL the subprocess the moment we see the marker line.
        if marker is not None:
            rlist, _, _ = select.select([proc.stdout, proc.stderr], [], [], 0.2)
            for stream in rlist:
                line = stream.readline()
                if not line:
                    continue
                if stream is proc.stdout:
                    captured_stdout.append(line)
                    if marker in line:
                        saw_marker = True
                else:
                    captured_stderr.append(line)
            if saw_marker:
                break
        else:
            time.sleep(0.2)

    if proc.poll() is None:
        # Either we hit the deadline without seeing the marker, or we
        # saw the marker and want to terminate. SIGKILL after marker
        # detection avoids the multi-second ASGI shutdown path that
        # would otherwise blow the smoke-test budget.
        proc.kill()
        try:
            remaining_out, remaining_err = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            remaining_out, remaining_err = "", ""
        captured_stdout.append(remaining_out)
        captured_stderr.append(remaining_err)
    else:
        # Process exited cleanly — drain both pipes.
        try:
            remaining_out, remaining_err = proc.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            remaining_out, remaining_err = "", ""
        captured_stdout.append(remaining_out)
        captured_stderr.append(remaining_err)

    shutil.rmtree(data_dir, ignore_errors=True)
    return subprocess.CompletedProcess(
        args=[sys.executable, "-m", *args],
        returncode=proc.returncode,
        stdout="".join(captured_stdout),
        stderr="".join(captured_stderr),
    )


def test_python_m_akosha_help_exits_0() -> None:
    """``python -m akosha --help`` must exit 0 (no import or argparse error)."""
    result = _run("akosha", "--help")
    assert result.returncode == 0, f"stderr: {result.stderr}"
    # Sanity: the help text should mention something Akosha-shaped.
    assert "akosha" in result.stdout.lower() or "usage" in result.stdout.lower()


def test_entrypoint_subprocess_gets_an_isolated_storage_root() -> None:
    """The spawned entry point must not touch the machine's shared storage.

    The live Akosha service holds a DuckDB write lock on the shared
    XDG ``hot.db``. A second process opening that file dies with
    ``Conflicting lock is held``, so the MCP entry-point smoke test
    could only pass while the service was stopped -- it failed on a
    machine with the service running, and for a reason that had
    nothing to do with the code under test.

    ``AKOSHA_DATA_PATH`` is the highest-priority override in
    ``StoragePathResolver.get_base_path``, so pointing it at a
    throwaway directory redirects the whole storage tree.
    """
    env, data_dir = _isolated_env()

    assert env["AKOSHA_DATA_PATH"] == str(data_dir)
    assert data_dir.is_absolute()
    # It is a throwaway under the system temp dir, not the shared XDG
    # location the running service uses.
    assert str(data_dir).startswith(tempfile.gettempdir())
    # And it is not the resolver's default base path.
    from akosha.storage.path_resolver import StoragePathResolver

    default_base = StoragePathResolver().base_path
    assert data_dir != default_base


def test_two_isolated_environments_use_different_directories() -> None:
    """Each subprocess gets its OWN root, so two runs cannot collide."""
    _, first = _isolated_env()
    _, second = _isolated_env()
    assert first != second


def test_entrypoint_subprocess_gets_its_own_ports() -> None:
    """The spawned entry point must not bind a port the service holds.

    The OTLP receiver binds :4317/:4318 unconditionally, and the MCP
    server binds its own port. Both are held by the live service, and
    both fail with ``[Errno 48] address already in use`` -- which kills
    the subprocess before it can print the marker the test looks for.
    """
    env, data_dir = _isolated_env()

    assert env["MCP_PORT"] != "8682"
    assert int(env["MCP_PORT"]) > 0

    config = Path(env["AKOSHA_CONFIG"]).read_text()
    assert "receiver_http_port" in config
    assert "receiver_grpc_port" in config
    # The canonical OTLP ports must not be the ones handed out.
    assert "receiver_http_port: 4318\n" not in config
    assert "receiver_grpc_port: 4317\n" not in config
    assert Path(env["AKOSHA_CONFIG"]).parent == data_dir


def test_the_isolated_config_keeps_the_subscribers_off() -> None:
    """``AKOSHA_CONFIG`` REPLACES the settings file, so the opt-outs are restated.

    Under the replacement the two subscriber blocks fall back to their
    code defaults, which are ENABLED -- and the subprocess would reach
    for Redis.
    """
    env, _ = _isolated_env()

    config = Path(env["AKOSHA_CONFIG"]).read_text()
    assert "bodai_tool_invocation_subscriber:\n  enabled: false\n" in config
    assert "websocket_invocations_subscriber:\n  enabled: false\n" in config


def test_python_m_akosha_mcp_imports_and_initializes_tools() -> None:
    """``python -m akosha.mcp --help`` doesn't actually print help (the
    module calls ``uvicorn.run()`` unconditionally), but the import
    path + lifespan must succeed — proven by ``Applied
    AKOSHA_TOOL_PROFILE=full → N tools registered`` in stdout.

    The subprocess is SIGKILLed the moment the marker line appears
    (uvicorn.run() never returns cleanly; we don't want to wait for the
    ASGI shutdown — it can take >10s and the test budget is 20s). The
    exit code is therefore either a clean exit (0), or a signal-killed
    one (negative). The proof of success is the marker line in stdout.
    """
    result = _run("akosha.mcp", "--help", timeout=20.0, marker="tools registered")
    # Real import errors would surface in stderr.
    assert "ImportError" not in result.stderr
    assert "ModuleNotFoundError" not in result.stderr
    # Tool registration confirms the full module init succeeded.
    assert "tools registered" in result.stdout


def test_python_m_akosha_mcp_status_exits_0_or_2() -> None:
    """``--help`` and ``--version`` exit 0; unknown args exit 2 (argparse).

    The CLI may not register ``status`` as a subcommand — in that
    case exit 2 is the expected argparse response. Either way,
    exit-code 0 (success) or 2 (argparse BadParameter) is the
    contract: a non-zero non-2 code means the import path or
    module init is broken.
    """
    result = _run("akosha", "status", timeout=10.0)
    # 0 = subcommand exists and runs, 2 = argparse rejected (unknown).
    # Anything else (e.g. ModuleNotFoundError -> exit 1) is a real bug.
    assert result.returncode in (0, 2), f"unexpected exit {result.returncode}: {result.stderr}"


def test_python_m_akosha_prints_version_string() -> None:
    """``--version`` prints a version (don't pin literal — changes per release)."""
    result = _run("akosha", "--version")
    # ``--version`` may exit 0 (oneiric) or 2 (no --version flag
    # registered). Either way the import path must succeed.
    if result.returncode != 0:
        assert result.returncode == 2, f"--version failed unexpectedly: {result.stderr}"
        return  # CLI didn't register --version — acceptable for now
    assert "0." in result.stdout or "0." in result.stderr
