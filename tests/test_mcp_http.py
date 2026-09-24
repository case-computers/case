# SPDX-License-Identifier: MIT
"""The remote door: case_mcp's HTTP mode defaults to loopback (compose overrides the
bind and publishes 127.0.0.1) and stays stateless, and stdio must stay the default.
No Docker, no network.
Run: .venv/bin/python tests/test_mcp_http.py"""
import importlib
import os
import sys
import types

import _helpers

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "mcp"))


def _load(**env):
    for k in ("CASE_MCP_HTTP", "CASE_MCP_PORT", "CASE_MCP_BIND",
              "CASE_ALLOWED_HOSTS", "CASE_PUBLIC_HOST"):
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


LIST_CALL = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "computer_list", "arguments": {}}}
ACCEPT = {"Accept": "application/json, text/event-stream"}


def _mcp_statuses(requests_, **env):
    """POST a tools/call per (Host, Origin) to one HTTP app; returns the statuses."""
    from starlette.testclient import TestClient
    m = _load(CASE_MCP_HTTP="1", CASE_MCP_BIND="0.0.0.0", **env)
    m.requests = types.SimpleNamespace(request=lambda *a, **kw: _Resp(200, {"computers": []}))
    out = []
    with TestClient(m.mcp.streamable_http_app()) as c:
        for host, origin in requests_:
            h = {"Host": host, **ACCEPT}
            if origin:
                h["Origin"] = origin
            out.append(c.post("/mcp", headers=h, json=LIST_CALL).status_code)
    return out


def test_http_refuses_a_rebound_host():
    # compose binds 0.0.0.0, where the SDK checks nothing by itself: a rebinding page
    # reaches 127.0.0.1:8788 under its own name and must not get computer_exec
    codes = _mcp_statuses([
        ("rebind.evil.example:8788", None),
        ("127.0.0.1:8788", "http://rebind.evil.example:8788"),
        ("127.0.0.1:8788", None),
        ("localhost:8788", "http://localhost:8788"),
        ("mcp:8788", None),
        ("case.example.com", "https://case.example.com"),
        ("box.example.com:8788", None),
    ], CASE_ALLOWED_HOSTS="case.example.com", CASE_PUBLIC_HOST="box.example.com")
    assert codes == [421, 403, 200, 200, 200, 200, 200], codes


def test_slow_tool_leaves_health_answering():
    # FastMCP runs sync tools on the event loop: a 280s computer_login froze /health
    # and every other client until it returned
    import threading
    import time
    from starlette.testclient import TestClient
    m = _load(CASE_MCP_HTTP="1")
    started, release = threading.Event(), threading.Event()

    def slow(*a, **kw):
        started.set()
        release.wait(10)
        return _Resp(200, {"computers": []})
    m.requests = types.SimpleNamespace(request=slow)
    with TestClient(m.mcp.streamable_http_app(), base_url="http://127.0.0.1:8788") as c:
        call = threading.Thread(target=c.post, args=("/mcp",),
                                kwargs={"headers": ACCEPT, "json": LIST_CALL})
        call.start()
        assert started.wait(5)
        t0 = time.time()
        assert c.get("/health").text == "ok"
        took = time.time() - t0
        release.set()
        call.join()
    assert took < 2, took


def test_optional_params_accept_explicit_null():
    # clients send "x": null for an unset optional; `x: int = None` rejected that
    import asyncio
    m = _load()
    for name, tool in m.mcp._tool_manager._tools.items():
        for arg, prop in tool.parameters["properties"].items():
            if "default" in prop and prop["default"] is None:
                assert {"type": "null"} in prop.get("anyOf", []), (name, arg, prop)
    sent = []
    m.requests = types.SimpleNamespace(
        request=lambda *a, **kw: sent.append(kw["json"]) or _Resp(200, {"ok": True}))
    asyncio.run(m.mcp.call_tool("computer_action", {
        "computer_id": "c", "type": "key", "keys": "Return", "x": None, "button": None}))
    assert sent == [{"type": "key", "screenshot": False, "keys": "Return"}], sent


def test_no_credential_write_tool():
    # security invariant: secrets enter via `case cred add` only, never a tool call
    m = _load()
    names = list(m.mcp._tool_manager._tools)
    writes = ("add", "create", "set", "put", "update", "save", "store", "write",
              "delete", "remove")
    assert not [n for n in names if "cred" in n and any(w in n for w in writes)], names


if __name__ == "__main__":
    _helpers.run_tests(globals())
