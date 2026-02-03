#!/usr/bin/env python3
"""Patch crackerjack to increase refurb stall timeout.

This script patches the crackerjack hook_executor.py to increase
the ProcessMonitor stall_timeout from 180s to 600s for refurb.

Usage:
    python scripts/fix_crackerjack_refurb_timeout.py
    crackerjack run -v
"""

import sys
from pathlib import Path

# Find the hook_executor.py file
venv_path = Path(sys.prefix) / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages" / "crackerjack" / "executors"
hook_executor_path = venv_path / "hook_executor.py"

if not hook_executor_path.exists():
    print(f"Error: Could not find hook_executor.py at {hook_executor_path}")
    sys.exit(1)

# Read the file
content = hook_executor_path.read_text()

# Check if already patched
if "CRACKERJACK_PATCHED" in content:
    print("✅ Crackerjack already patched for refurb timeout")
    sys.exit(0)

# Patch the ProcessMonitor instantiation for refurb
old_code = """        monitor = ProcessMonitor(
            check_interval=30.0,
            cpu_threshold=0.1,
            stall_timeout=180.0,
        )"""

new_code = """        # CRACKERJACK_PATCHED: Increased stall_timeout for slow tools
        monitor = ProcessMonitor(
            check_interval=30.0,
            cpu_threshold=0.1,
            stall_timeout=600.0,  # Increased from 180s to 600s
        )"""

if old_code in content:
    content = content.replace(old_code, new_code)
    hook_executor_path.write_text(content)
    print(f"✅ Patched {hook_executor_path}")
    print("   Increased ProcessMonitor stall_timeout from 180s to 600s")
else:
    print("⚠️ Could not find the code to patch. Crackerjack may have been updated.")
    sys.exit(1)
