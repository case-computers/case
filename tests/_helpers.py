# SPDX-License-Identifier: MIT
"""Shared by the tests/test_*.py scripts, each of which runs as its own process."""
import atexit
import os
import shutil
import sys
import tempfile

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
