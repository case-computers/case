# SPDX-License-Identifier: MIT
"""The remote door: case_mcp's HTTP mode defaults to loopback (compose overrides the
bind and publishes 127.0.0.1) and stays stateless, and stdio must stay the default.
No Docker, no network.
Run: .venv/bin/python tests/test_mcp_http.py"""
import importlib
import os
import sys
import types

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


def test_http_mode_defaults_to_loopback():
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


class _Resp:
    def __init__(self, status_code, body):
        self.status_code, self._body = status_code, body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def _failed_call(body, status_code=500):
    """call() against a >=400 response; returns (message, chained exception)."""
    m = _load()
    m.requests = types.SimpleNamespace(request=lambda *a, **kw: _Resp(status_code, body))
    try:
        m.call("GET", "/computers")
    except RuntimeError as e:
        return str(e), e.__context__
    assert False, "call() must raise on a >=400 response"


def test_call_reports_the_cased_error():
    msg, _ = _failed_call({"error": {"code": "not_found", "message": "no such computer"}})
    assert msg == "not_found: no such computer", msg


def test_call_falls_back_to_the_status_unchained():
    msg, chained = _failed_call(ValueError("not json"), 502)
    assert msg == "cased returned 502", msg
    assert chained is None, chained          # a chained decode error buries the status


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
