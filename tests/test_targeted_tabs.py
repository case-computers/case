# SPDX-License-Identifier: MIT
"""Targeted-tab routing regressions. No Docker, browser, or network required.
Run: .venv/bin/python tests/test_targeted_tabs.py
"""
import importlib.util
import json
import os
from pathlib import Path
import sys
import threading
import types
import unittest
from collections import deque
from unittest import mock

import _helpers

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "control-plane"))
sys.path.insert(0, str(ROOT / "mcp"))
_helpers.isolated_home()


def load_source(name, relative_path):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ROW = {"id": "c_target", "desk_port": 1, "desk_token": "test"}
TARGET = "BBBB-2222"
PAGES = [
    {"type": "page", "id": "AAAA-1111", "webSocketDebuggerUrl": "ws://active"},
    {"type": "service_worker", "id": TARGET, "webSocketDebuggerUrl": "ws://worker"},
    {"type": "page", "id": TARGET, "webSocketDebuggerUrl": "ws://target"},
]


class TargetedTabsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with mock.patch.dict(os.environ, {"DESK_TOKEN": "test"}):
            sys.modules.setdefault("websocket", types.ModuleType("websocket"))
            cls.desk = load_source("_targeted_desk", "image/deskd.py")
        cls.client = load_source("_targeted_client", "control-plane/deskclient.py")
        cls.browse = load_source("_targeted_browse", "control-plane/browse.py")
        cls.browse.eval_js = cls.client.eval_js
        cls.browse.targeted_tabs = cls.client.targeted_tabs
        cls.api = load_source("_targeted_api", "control-plane/cased.py")
        cls.api.browse = cls.browse
        cls.api.eval_js = cls.client.eval_js
        cls.mcp = load_source("_targeted_mcp", "mcp/case_mcp.py")

    def test_explicit_websocket_target_ignores_active_order_and_workers(self):
        for pages in (PAGES, list(reversed(PAGES))):
            self.assertEqual(self.desk.page_ws_url(pages, TARGET), "ws://target")
        self.assertEqual(self.desk.page_ws_url(PAGES), "ws://active")
        with mock.patch.object(self.desk.requests, "get") as get, \
                mock.patch.object(self.desk.websocket, "create_connection", create=True) as connect:
            get.return_value.json.return_value = PAGES
            self.desk.Tab(TARGET)
            connect.assert_called_once_with("ws://target", timeout=30, suppress_origin=True)

    def test_unknown_target_never_connects_to_active_tab(self):
        with mock.patch.object(self.desk.requests, "get") as get, \
                mock.patch.object(self.desk.websocket, "create_connection", create=True) as connect:
            get.return_value.json.return_value = PAGES
            result = self.desk.eval_({"expression": "1", "target_id": "MISSING-9999"})
            self.assertEqual(result.status_code, 502)
            connect.assert_not_called()

    def test_malformed_targets_rejected_before_browser_access(self):
        for target in ("abcd\n", "../abcd", "abcd;true", "a" * 65, [TARGET]):
            with self.subTest(target=target), \
                    mock.patch.object(self.desk, "Tab") as tab, \
                    mock.patch.object(self.browse, "targeted_tabs") as probe:
                result = self.desk.eval_({"expression": "1", "target_id": target})
                self.assertEqual(result.status_code, 400)
                with self.assertRaises(self.api.ApiError) as raised:
                    self.browse.prepare_target(ROW, target)
                self.assertEqual(raised.exception.status, 400)
                tab.assert_not_called()
                probe.assert_not_called()

    def test_capable_desktop_keeps_background_target_unfocused(self):
        with mock.patch.object(self.client, "desk_json", return_value={"targeted_tabs": True}) as request, \
                mock.patch.object(self.browse, "tabs") as tabs:
            self.assertEqual(self.browse.prepare_target(ROW, TARGET), TARGET)
            request.assert_called_once_with(ROW, "GET", "/health", timeout=5)
            tabs.assert_not_called()

    def test_legacy_desktop_activates_before_falling_back(self):
        with mock.patch.object(self.client, "desk_json", return_value={}), \
                mock.patch.object(self.browse, "tabs", return_value={"ok": True}) as tabs:
            self.assertIsNone(self.browse.prepare_target(ROW, TARGET))
            tabs.assert_called_once_with(ROW, action="activate", target_id=TARGET)

    def test_front_actions_activate_even_on_capable_desktop(self):
        with mock.patch.object(self.browse, "targeted_tabs", return_value=True), \
                mock.patch.object(self.browse, "tabs", return_value={"ok": True}) as tabs:
            self.assertEqual(self.browse.prepare_target(ROW, TARGET, front=True), TARGET)
            tabs.assert_called_once_with(ROW, action="activate", target_id=TARGET)

    def test_legacy_activation_error_prevents_eval(self):
        with mock.patch.object(self.api.lifecycle, "ensure_running", return_value=ROW), \
                mock.patch.object(self.browse, "targeted_tabs", return_value=False), \
                mock.patch.object(self.browse, "_cdp_curl", side_effect=self.api.ApiError(502, "cdp_unreachable", "missing tab")), \
                mock.patch.object(self.api, "eval_js") as evaluate:
            with self.assertRaises(self.api.ApiError):
                self.api.eval_(ROW["id"], {"expression": "1", "target_id": TARGET})
            evaluate.assert_not_called()

    def test_legacy_missing_target_response_prevents_eval(self):
        with mock.patch.object(self.api.lifecycle, "ensure_running", return_value=ROW), \
                mock.patch.object(self.browse, "targeted_tabs", return_value=False), \
                mock.patch.object(self.browse, "_cdp_curl", return_value="No such target id"), \
                mock.patch.object(self.api, "eval_js") as evaluate:
            with self.assertRaises(self.api.ApiError) as raised:
                self.api.eval_(ROW["id"], {"expression": "1", "target_id": TARGET})
            self.assertEqual(raised.exception.status, 502)
            evaluate.assert_not_called()

    def test_failed_cdp_http_request_prevents_legacy_eval(self):
        with mock.patch.object(self.api.lifecycle, "ensure_running", return_value=ROW), \
                mock.patch.object(self.browse, "targeted_tabs", return_value=False), \
                mock.patch.object(self.browse, "desk_json", return_value={"exit_code": 22, "stdout": ""}) as request, \
                mock.patch.object(self.api, "eval_js") as evaluate:
            with self.assertRaises(self.api.ApiError) as raised:
                self.api.eval_(ROW["id"], {"expression": "1", "target_id": TARGET})
            self.assertEqual(raised.exception.status, 502)
            self.assertIn("curl -fsS", request.call_args.kwargs["json"]["command"])
            evaluate.assert_not_called()

    def test_backend_eval_forwards_target_and_clamps_timeout(self):
        with mock.patch.object(self.api.lifecycle, "ensure_running", return_value=ROW), \
                mock.patch.object(self.api.store, "touch"), \
                mock.patch.object(self.browse, "targeted_tabs", return_value=True), \
                mock.patch.object(self.client, "desk_json", return_value={"ok": True, "value": 1}) as request:
            self.api.eval_(ROW["id"], {"expression": "1", "target_id": TARGET, "timeout_s": 999})
            request.assert_called_once_with(ROW, "POST", "/eval", json={
                "expression": "1", "target_id": TARGET, "timeout_s": 120}, timeout=135)

    def test_backend_snapshot_query_reaches_targeted_eval(self):
        with mock.patch.object(self.api.lifecycle, "ensure_running", return_value=ROW), \
                mock.patch.object(self.api.store, "touch"), \
                mock.patch.object(self.browse, "targeted_tabs", return_value=True), \
                mock.patch.object(self.client, "desk_json", return_value={"ok": True, "value": {
                    "url": "https://target.test", "title": "Target", "count": 0, "els": []}}) as request:
            result = self.api.page_(ROW["id"], target_id=TARGET)
            self.assertEqual(result["title"], "Target")
            self.assertEqual(request.call_args.kwargs["json"]["target_id"], TARGET)
            self.assertEqual(request.call_args.args[2], "/eval")

    def test_backend_capture_forwards_target_only_on_capable_desktop(self):
        for capable in (True, False):
            with self.subTest(capable=capable), \
                    mock.patch.object(self.api.lifecycle, "ensure_running", return_value=ROW), \
                    mock.patch.object(self.api.store, "touch"), \
                    mock.patch.object(self.browse, "targeted_tabs", return_value=capable), \
                    mock.patch.object(self.browse, "tabs", return_value={"ok": True}), \
                    mock.patch.object(self.api, "desk_json", return_value={"ok": True}) as request:
                self.api.capture_start(ROW["id"], {"pattern": "graphql", "target_id": TARGET})
                body = {"pattern": "graphql"}
                if capable:
                    body["target_id"] = TARGET
                request.assert_called_once_with(ROW, "POST", "/capture/start", json=body, timeout=20)

    def test_open_capture_ws_uses_explicit_websocket(self):
        with mock.patch.object(self.desk.requests, "get") as get, \
                mock.patch.object(self.desk.websocket, "create_connection", create=True) as connect:
            get.return_value.json.return_value = PAGES
            ws = self.desk.open_capture_ws(TARGET)
            connect.assert_called_once_with("ws://target", timeout=30, suppress_origin=True)
            ws.send.assert_called_once()

    def test_capture_worker_uses_preflight_websocket(self):
        stop = threading.Event()
        stop.set()
        mock_ws = mock.Mock()
        cap = {"pattern": "graphql", "target_id": TARGET, "stop": stop, "buf": deque(),
               "error": None, "ws": mock_ws}
        self.desk.capture_worker(cap)
        mock_ws.close.assert_called_once_with()
        self.assertIsNone(cap["error"])

    def test_capture_start_binds_worker_to_target(self):
        mock_ws = mock.Mock()
        with mock.patch.dict(self.desk.state, {"capture": None}), \
                mock.patch.object(self.desk, "open_capture_ws", return_value=mock_ws) as open_ws, \
                mock.patch.object(self.desk.threading, "Thread") as thread:
            result = self.desk.capture_start({"pattern": "graphql", "target_id": TARGET})
            self.assertTrue(result["ok"])
            open_ws.assert_called_once_with(TARGET)
            cap = thread.call_args.kwargs["args"][0]
            self.assertEqual(cap["target_id"], TARGET)
            self.assertIs(cap["ws"], mock_ws)
            self.assertIs(thread.call_args.kwargs["target"], self.desk.capture_worker)
            thread.return_value.start.assert_called_once_with()

    def test_capture_start_rejects_missing_target_before_worker(self):
        with mock.patch.dict(self.desk.state, {"capture": None}), \
                mock.patch.object(self.desk.requests, "get") as get, \
                mock.patch.object(self.desk.threading, "Thread") as thread:
            get.return_value.json.return_value = PAGES
            result = self.desk.capture_start({"pattern": "graphql", "target_id": "MISSING-9999"})
            self.assertEqual(result.status_code, 502)
            thread.assert_not_called()

    def test_navigation_keeps_target_for_assign_poll_and_page_text(self):
        results = [{"ok": True}, {"ok": True, "value": ["https://target.test", "Target", 20]},
                   {"ok": True, "value": "target page text"}]
        with mock.patch.object(self.client, "eval_js", side_effect=results) as evaluate, \
                mock.patch.object(self.client.time, "sleep"):
            result = self.client.navigate(ROW, "https://target.test", target_id=TARGET)
            self.assertEqual(result["text"], "target page text")
            self.assertEqual(evaluate.call_count, 3)
            for call in evaluate.call_args_list:
                self.assertEqual(call.kwargs["target_id"], TARGET)

    def test_mcp_eval_snapshot_and_capture_preserve_target(self):
        cases = [
            (self.mcp.computer_eval, {"expression": "1"}, "POST", "/eval", "json"),
            (self.mcp.computer_navigate, {"url": "https://target.test"}, "POST", "/navigate", "json"),
            (self.mcp.computer_snapshot, {}, "GET", "/page", "params"),
            (self.mcp.computer_capture_start, {"url_pattern": "graphql"}, "POST", "/capture", "json"),
        ]
        for fn, kwargs, method, path, field in cases:
            with self.subTest(tool=fn.__name__), mock.patch.object(self.mcp, "call") as call:
                call.return_value.json.return_value = {"ok": True}
                fn(ROW["id"], target_id=TARGET, **kwargs)
                self.assertEqual(call.call_args.args, (method, f"/computers/{ROW['id']}{path}"))
                self.assertEqual(call.call_args.kwargs[field]["target_id"], TARGET)
                call.reset_mock()
                fn(ROW["id"], **kwargs)
                self.assertNotIn("target_id", call.call_args.kwargs[field])

    def test_targeting_does_not_bypass_credential_injection_lock(self):
        with mock.patch.dict(self.desk.state, {"injecting": True}), \
                mock.patch.object(self.desk, "Tab") as tab:
            results = [
                self.desk.eval_({"expression": "1", "target_id": TARGET}),
                self.desk.capture_get(),
                self.desk.capture_delete(),
            ]
            for result in results:
                self.assertEqual(result.status_code, 423)
                self.assertEqual(json.loads(result.body)["error"]["code"], "credential_injection")
            tab.assert_not_called()

    def test_targeted_eval_keeps_bounded_timeout_validation(self):
        with mock.patch.object(self.desk, "Tab") as tab:
            result = self.desk.eval_({"expression": "1", "target_id": TARGET, "timeout_s": "invalid"})
            self.assertEqual(result.status_code, 400)
            tab.assert_not_called()
            tab.return_value.cmd.return_value = {"result": {"value": 1}}
            self.desk.eval_({"expression": "1", "target_id": TARGET, "timeout_s": 999})
            tab.assert_called_once_with(TARGET)
            self.assertEqual(tab.return_value.cmd.call_args.kwargs["timeout"], 120000)
            tab.return_value.ws.settimeout.assert_called_once_with(125)


if __name__ == "__main__":
    unittest.main()