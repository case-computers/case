# SPDX-License-Identifier: MIT
"""Shared by the tests/test_*.py scripts, each of which runs as its own process."""
import atexit
import os
import shutil
import sys
import tempfile
import traceback

ROOT = os.path.join(os.path.dirname(__file__), "..")


def isolated_home():
    """Put control-plane on sys.path and CASE_HOME in a fresh directory, removed at exit.

    Call it before anything imports config or store. Assignment, not setdefault: an
    inherited CASE_HOME (a dev shell, ~/.case/env) would point the tests at a live
    vault, and a fixed path is shared by parallel runs and keeps stale rows.
    """
    sys.path.insert(0, os.path.join(ROOT, "control-plane"))
    home = tempfile.mkdtemp(prefix="case-test-")
    atexit.register(shutil.rmtree, home, ignore_errors=True)
    os.environ["CASE_HOME"] = home
    return home


def run_tests(namespace):
    """Run every test_* function in `namespace` (a script's globals()) in name order.

    A failure is printed and the rest still run; any failure exits non-zero.
    """
    failed = []
    for name, fn in sorted(namespace.items()):
        if not (name.startswith("test_") and callable(fn)):
            continue
        try:
            fn()
        except Exception:
            traceback.print_exc()
            print("FAIL", name)
            failed.append(name)
        else:
            print("ok", name)
    if failed:
        print(f"FAILED {len(failed)}: {' '.join(failed)}")
        sys.exit(1)
    print("PASS")
