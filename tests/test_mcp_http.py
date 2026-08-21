# SPDX-License-Identifier: MIT
"""The remote door: case_mcp's HTTP mode must stay loopback-only and stateless,
and stdio must stay the default. No Docker, no network.
Run: .venv/bin/python tests/test_mcp_http.py"""
import importlib
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "mcp"))


def _load(**env):
    for k in ("CASE_MCP_HTTP", "CASE_MCP_PORT", "CASE_MCP_BIND"):
        os.environ.pop(k, None)
    os.environ.update(env)
    mod = importlib.import_module("case_mcp")
    return importlib.reload(mod)


def test_stdio_is_the_default():
    assert _load().HTTP is False          # unset env → every existing flow untouched


def test_http_mode_binds_loopback_only():
    m = _load(CASE_MCP_HTTP="1", CASE_MCP_PORT="8899")
    assert m.HTTP is True
    assert m.mcp.settings.host == "127.0.0.1"      # default; compose overrides CASE_MCP_BIND
    assert m.mcp.settings.port == 8899
    assert m.mcp.settings.stateless_http is True


def test_http_mode_honours_bind_env():
    m = _load(CASE_MCP_HTTP="1", CASE_MCP_BIND="0.0.0.0")
    assert m.mcp.settings.host == "0.0.0.0"


def test_http_app_serves_mcp_path():
    m = _load(CASE_MCP_HTTP="1")
    assert m.mcp.settings.port == 8788             # the port compose publishes
    paths = [getattr(r, "path", None) for r in m.mcp.streamable_http_app().routes]
    assert "/mcp" in paths, paths


def test_no_credential_write_tool():
    # security invariant: secrets enter via `case cred add` only, never a tool call
    m = _load()
    names = list(m.mcp._tool_manager._tools)
    assert not [n for n in names if "cred" in n], names


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
