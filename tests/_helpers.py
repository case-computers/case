# SPDX-License-Identifier: MIT
"""Shared by the tests/test_*.py scripts, each of which runs as its own process."""
import atexit
import importlib
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


def raises(fn, code):
    """Call fn and return the ApiError it must raise, asserting its code."""
    from errors import ApiError
    try:
        fn()
    except ApiError as e:
        assert e.code == code, (e.code, e.message)
        return e
    assert False, f"expected ApiError {code}"


def running_computer(cid="c_1"):
    from store import store
    store.q("DELETE FROM computers WHERE id=?", (cid,))
    store.insert_computer(cid, "ava", "case-desk:0.1", 1, 2048, "vol", "tok")
    store.set_state(cid, "running")


def desk_check_ep(uri, cookie):
    """Call the /desk forward-auth check as the reverse proxy would."""
    from starlette.requests import Request
    import cased
    scope = {"type": "http", "headers": [
        (b"x-forwarded-uri", uri.encode()),
        (b"cookie", cookie.encode()),
    ], "method": "GET", "path": "/v1/desk/check", "query_string": b""}
    return cased.desk_check_ep(Request(scope))


def load_case_mcp(**env):
    """Re-import case_mcp with `env` as the only settings it reads at import."""
    mcp = os.path.join(ROOT, "mcp")
    if mcp not in sys.path:
        sys.path.insert(0, mcp)
    for k in ("CASE_MCP_HTTP", "CASE_MCP_PORT", "CASE_MCP_BIND", "CASE_MCP_SCHEDULES",
              "CASE_ALLOWED_HOSTS", "CASE_PUBLIC_HOST"):
        os.environ.pop(k, None)
    os.environ.update(env)
    return importlib.reload(importlib.import_module("case_mcp"))


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
