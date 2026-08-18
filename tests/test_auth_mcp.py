# SPDX-License-Identifier: MIT
"""MCP durable-auth handles: computer_login + auth_attempt_get/wait.
Call-shape tests with mocked HTTP — no Docker, no network.
Run: .venv/bin/python tests/test_auth_mcp.py"""
import importlib
import os
import sys
from unittest import mock

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "mcp"))


def _load(schedules=False):
    for k in ("CASE_MCP_HTTP", "CASE_MCP_PORT", "CASE_MCP_SCHEDULES"):
        os.environ.pop(k, None)
    if schedules:
        os.environ["CASE_MCP_SCHEDULES"] = "1"
    mod = importlib.import_module("case_mcp")
    return importlib.reload(mod)


def _ok(payload):
    r = mock.Mock()
    r.status_code = 200
    r.json.return_value = payload
    r.text = ""
    r.content = b""
    return r


def test_auth_tool_surface():
    m = _load()
    names = list(m.mcp._tool_manager._tools)
    assert "auth_attempt_wait" in names, names
    assert "computer_login" in names
    # archived: wait(max_wait_s=1) is the snapshot now — one read tool, not two
    assert "auth_attempt_get" not in names, names


def test_schedule_tools_archived_by_default():
    m = _load()
    names = list(m.mcp._tool_manager._tools)
    assert not [n for n in names if n.startswith("schedule_")], names
    m = _load(schedules=True)
    names = list(m.mcp._tool_manager._tools)
    assert "schedule_create" in names and "schedule_runs" in names, names


def test_computer_login_passes_idempotency_and_proof_spec():
    m = _load()
    with mock.patch.object(m, "call", return_value=_ok({
        "status": "success", "attempt_id": "a_1", "revision": 1,
    })) as call:
        out = m.computer_login(
            "c_1", "github", "https://example.com/login",
            idempotency_key="idem-42",
            proof_spec={"url_contains": "/home"})
    assert out["attempt_id"] == "a_1"
    call.assert_called_once()
    args, kwargs = call.call_args
    assert args[0] == "POST"
    assert args[1] == "/computers/c_1/login"
    assert kwargs["params"] == {"wake": "true"}
    assert kwargs["json"] == {
        "credential": "github",
        "url": "https://example.com/login",
        "idempotency_key": "idem-42",
        "proof_spec": {"url_contains": "/home"},
    }
    assert kwargs["timeout"] == 280


def test_computer_login_omits_optional_when_unset():
    m = _load()
    with mock.patch.object(m, "call", return_value=_ok({
        "status": "handoff_pending", "handoff_id": "h_1", "attempt_id": "a_2",
    })) as call:
        m.computer_login("c_1", "github", "https://example.com/login")
    assert call.call_args.kwargs["json"] == {
        "credential": "github", "url": "https://example.com/login",
    }


def test_file_get_returns_readable_text_and_flags_binary():
    m = _load()
    txt = mock.Mock(status_code=200, content="hello ☃".encode("utf-8"), text="")
    with mock.patch.object(m, "call", return_value=txt):
        out = m.computer_file_get("c_1", "/home/agent/a.txt")
    assert out == {"encoding": "utf8", "content": "hello ☃",
                   "bytes": len("hello ☃".encode("utf-8"))}, out
    binary = mock.Mock(status_code=200, content=b"\x89PNG\r\n\x1a\n\x00", text="")
    with mock.patch.object(m, "call", return_value=binary):
        out = m.computer_file_get("c_1", "/home/agent/p.png")
    assert out["encoding"] == "base64" and out["bytes"] == 9, out


def test_handoff_request_kind_in_body():
    m = _load()
    with mock.patch.object(m, "call", return_value=_ok({"id": "h_d"})) as call:
        m.handoff_request("c_1", "Scan the QR", kind="device")
    assert call.call_args.kwargs["json"] == {
        "kind": "device", "prompt": "Scan the QR",
    }


def test_auth_attempt_wait_terminal_return():
    m = _load()
    terminal = {
        "changed": True,
        "wait_status": "terminal",
        "attempt": {
            "id": "a_t", "status": "authenticated", "revision": 4,
            "current_handoff_id": None, "proof_level": "configured",
        },
        "login_result": {
            "status": "success", "attempt_id": "a_t", "revision": 4,
            "proof_level": "configured",
        },
    }
    with mock.patch.object(m, "call", return_value=_ok(terminal)) as call:
        out = m.auth_attempt_wait("a_t", since_revision=2, max_wait_s=30)
    assert out["wait_status"] == "terminal"
    assert out["status"] == "success"
    assert out["attempt_id"] == "a_t"
    call.assert_called_once()
    args, kwargs = call.call_args
    assert args == ("GET", "/auth-attempts/a_t/wait")
    assert kwargs["params"]["after_revision"] == 2
    assert kwargs["params"]["timeout_s"] <= 30
    # Never restarts login.
    assert all("/login" not in str(c.args) for c in call.call_args_list)


def test_auth_attempt_wait_chains_intermediate_challenges():
    m = _load()
    mid = {
        "changed": True,
        "wait_status": "changed",
        "attempt": {
            "id": "a_c", "status": "awaiting_human", "revision": 3,
            "current_handoff_id": "h_otp",
        },
        "login_result": {
            "status": "handoff_pending", "attempt_id": "a_c",
            "handoff_id": "h_otp", "revision": 3,
        },
    }
    done = {
        "changed": True,
        "wait_status": "terminal",
        "attempt": {
            "id": "a_c", "status": "authenticated", "revision": 5,
            "current_handoff_id": "h_otp", "proof_level": "heuristic",
        },
        "login_result": {
            "status": "success", "attempt_id": "a_c", "revision": 5,
            "proof_level": "heuristic",
        },
    }
    with mock.patch.object(m, "call", side_effect=[_ok(mid), _ok(done)]) as call, \
         mock.patch.object(m.time, "time", side_effect=[1000.0, 1000.1, 1000.2, 1000.3]):
        out = m.auth_attempt_wait("a_c", since_revision=1, max_wait_s=60)
    assert out["wait_status"] == "terminal"
    assert out["status"] == "success"
    assert call.call_count == 2
    # Second leg advances the cursor past the intermediate handoff.
    second = call.call_args_list[1]
    assert second.args[1] == "/auth-attempts/a_c/wait"
    assert second.kwargs["params"]["after_revision"] == 3
    assert second.kwargs["params"]["after_handoff_id"] == "h_otp"
    assert all(c.args[0] == "GET" and "/login" not in c.args[1]
               for c in call.call_args_list)


def test_auth_attempt_wait_bounded_timeout_no_login_retry():
    m = _load()
    timed = {
        "changed": False,
        "wait_status": "timeout",
        "attempt": {
            "id": "a_x", "status": "awaiting_human", "revision": 2,
            "current_handoff_id": "h_1",
        },
    }
    snap = {
        "id": "a_x", "status": "awaiting_human", "revision": 2,
        "current_handoff_id": "h_1",
    }
    # First call: wait times out at REST; budget already expired → snapshot GET.
    with mock.patch.object(m, "call", side_effect=[_ok(timed), _ok(snap)]) as call, \
         mock.patch.object(m.time, "time", side_effect=[0.0, 0.0, 1000.0]):
        out = m.auth_attempt_wait("a_x", since_revision=2, max_wait_s=1)
    assert out["wait_status"] == "timeout"
    assert out["status"] == "handoff_pending"
    assert out["handoff_id"] == "h_1"
    assert out["revision"] == 2
    paths = [c.args[1] for c in call.call_args_list]
    assert any(p.endswith("/wait") for p in paths)
    assert any(p == "/auth-attempts/a_x" for p in paths)
    assert not any("/login" in p for p in paths)


def test_auth_attempt_wait_caps_max_wait():
    m = _load()
    timed = {
        "changed": False,
        "wait_status": "timeout",
        "attempt": {
            "id": "a_cap", "status": "awaiting_human", "revision": 1,
            "current_handoff_id": "h_1",
        },
    }
    snap = {
        "id": "a_cap", "status": "awaiting_human", "revision": 1,
        "current_handoff_id": "h_1",
    }
    with mock.patch.object(m, "call", side_effect=[_ok(timed), _ok(snap)]) as call, \
         mock.patch.object(m.time, "time", side_effect=[0.0, 0.0, 10_000.0]):
        m.auth_attempt_wait("a_cap", max_wait_s=9999)
    # Cap is 240 — first leg cannot request more than that.
    assert call.call_args_list[0].kwargs["params"]["timeout_s"] <= 240


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
