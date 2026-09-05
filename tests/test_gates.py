# SPDX-License-Identifier: MIT
"""The doors into cased: Host/Origin, the token-in-URL routes, middleware order.
Run: .venv/bin/python tests/test_gates.py"""
import glob
import os
import shutil
import sys
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "control-plane"))
# assignment, NOT setdefault: this suite writes audit files and wipes the audit
# directory, and an inherited CASE_HOME would aim that at the real vault.
os.environ["CASE_HOME"] = "/tmp/case-gates-test"
from fastapi.testclient import TestClient  # noqa: E402

import cased  # noqa: E402


def _client():
    # base_url, not the TestClient default: "testserver" is not a name a browser
    # could reach us by, so browser_ok rejects it exactly like a rebinding host.
    return TestClient(cased.app, base_url="http://127.0.0.1", raise_server_exceptions=False)


def _tokened(fn):
    os.environ["CASE_TOKEN"] = "share-me"
    try:
        fn()
    finally:
        os.environ.pop("CASE_TOKEN", None)


def test_untokened_box_still_checks_the_host():
    # DNS rebinding: evil.example resolves to 127.0.0.1, the browser sends its own
    # Host, and every same-origin rule the page relies on is satisfied.
    os.environ.pop("CASE_TOKEN", None)
    c = _client()
    assert c.get("/v1/computers", headers={"Host": "evil.example"}).status_code == 403
    assert c.get("/v1/computers").status_code == 200


def test_untokened_box_rejects_a_foreign_origin():
    os.environ.pop("CASE_TOKEN", None)
    c = _client()
    r = c.post("/v1/computers", json={}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403, r.text
    assert r.json()["error"]["code"] == "bad_host"


def test_token_guards_the_api_but_not_the_human_doors():
    def check():
        c = _client()
        assert c.get("/v1/computers").status_code == 401
        assert c.get("/fill/nope").status_code != 401       # token is in the URL
        assert c.get("/assist/nope").status_code == 410
    _tokened(check)


def test_health_says_only_ok_without_the_bearer():
    def check():
        c = _client()
        assert set(c.get("/health").json()) == {"ok"}
        assert "computers" in c.get("/health", headers={"Authorization": "Bearer share-me"}).json()
    _tokened(check)


def test_unauthorized_calls_leave_no_audit_line():
    # audit_mw is registered before token_guard so the guard runs outermost; the
    # other order logs (and keeps) whatever an unauthenticated caller sends.
    def check():
        shutil.rmtree(cased.AUDIT_DIR, ignore_errors=True)
        c = _client()
        assert c.get("/v1/computers").status_code == 401
        assert glob.glob(os.path.join(cased.AUDIT_DIR, "*.jsonl")) == []
        assert c.get("/v1/computers", headers={"Authorization": "Bearer share-me"}
                     ).status_code == 200
        assert glob.glob(os.path.join(cased.AUDIT_DIR, "*.jsonl"))   # still auditing
    _tokened(check)


def test_oversized_upload_is_refused_before_the_desktop_wakes():
    # A declared oversize body is rejected before the desktop is touched.
    os.environ.pop("CASE_TOKEN", None)
    r = _client().put("/v1/computers/c_x/files?path=/x", content=b"",
                      headers={"Content-Length": "9999999"})
    assert r.status_code == 413, r.text


def test_streamed_upload_counts_bytes_without_content_length():
    os.environ.pop("CASE_TOKEN", None)
    with mock.patch.object(cased, "_file_put", return_value={"bytes": cased.FILE_MAX}) as put:
        content = iter([b"x" * (1024 * 1024)] * 9)
        r = _client().put("/v1/computers/c_x/files?path=/home/agent/x", content=content)
        assert "content-length" not in r.request.headers
        assert r.status_code == 413, r.text
        put.assert_not_called()

        content = iter([b"x" * (1024 * 1024)] * 8)
        r = _client().put("/v1/computers/c_x/files?path=/home/agent/x", content=content)
        assert r.status_code == 201, r.text
        assert len(put.call_args.args[2]) == cased.FILE_MAX


def test_login_url_must_be_https():
    # login posts the vault's plaintext to the desktop; over http:// the target site
    # sees it on the wire. Loopback is the exception: it never leaves the desktop,
    # and the acceptance suite logs into a test site it runs there.
    from unittest import mock
    os.environ.pop("CASE_TOKEN", None)
    with mock.patch.object(cased.lifecycle, "ensure_running", return_value={"id": "c_x"}), \
         mock.patch.object(cased.store, "credential_material", return_value={"name": "a"}):
        c = _client()
        bad = c.post("/v1/computers/c_x/login", json={"credential": "a", "url": "http://x"})
        assert bad.status_code == 400, bad.text
        assert "https" in bad.json()["error"]["message"]
        for ok in ("http://localhost:8088/plain", "http://127.0.0.1:8088/plain"):
            r = c.post("/v1/computers/c_x/login", json={"credential": "a", "url": ok})
            assert r.status_code != 400, (ok, r.text)


def test_live_relay_needs_the_bearer_on_both_halves():
    # HTTP middleware never sees a websocket scope, so the socket has to check the
    # token itself or the desktop is one upgrade away from anyone.
    from starlette.websockets import WebSocketDisconnect

    def check():
        c = _client()
        assert c.get("/v1/computers/c_x/live/vnc.html").status_code == 401
        try:
            with c.websocket_connect("ws://127.0.0.1/v1/computers/c_x/live/websockify"):
                assert False, "socket accepted without a bearer"
        except WebSocketDisconnect as e:
            assert e.code == 1008, e.code
    _tokened(check)


def test_live_upstream_dials_the_desk_with_websockify_basic_auth():
    import base64
    os.environ.pop("CASE_DOCKER_NETWORK", None)
    base, headers = cased.live_upstream({"id": "c_x", "vnc_port": 32771, "desk_token": "t0k"})
    assert base == "http://127.0.0.1:32771"
    assert headers == {"Authorization": "Basic " + base64.b64encode(b"agent:t0k").decode()}
    # the relay forwards the tail of the URL verbatim, so traversal has to die here
    assert cased.live_path_ok("vnc.html")
    assert not cased.live_path_ok("../../etc/passwd")
    assert not cased.live_path_ok("%2e%2e/x")


def test_live_socket_rejects_foreign_browsers_before_dialing():
    from starlette.websockets import WebSocketDisconnect
    os.environ.pop("CASE_TOKEN", None)
    for headers in ({"Host": "evil.example"}, {"Origin": "https://evil.example"},
                    {"Origin": "null"}):
        with mock.patch.object(cased.lifecycle, "ensure_running") as ensure:
            try:
                with _client().websocket_connect("ws://127.0.0.1/v1/computers/c_x/live/websockify",
                                                 headers=headers):
                    assert False, "foreign browser accepted"
            except WebSocketDisconnect as e:
                assert e.code == 1008, e.code
            ensure.assert_not_called()


def test_live_socket_relays_for_allowed_browsers_and_bearers():
    import asyncio
    from contextlib import asynccontextmanager

    class Upstream:
        subprotocol = None

        def __init__(self):
            self.closed = asyncio.Event()

        async def close(self):
            self.closed.set()

        async def __aiter__(self):
            yield b"RFB 003.008\n"
            await self.closed.wait()

    @asynccontextmanager
    async def connect(*args, **kwargs):
        yield Upstream()

    row = {"id": "c_x", "vnc_port": 32771, "desk_token": "test"}
    cases = [("", {}), ("", {"Origin": "http://localhost:4174"}),
             ("share-me", {"Authorization": "Bearer share-me", "Origin": "https://client.example"})]
    for token, headers in cases:
        with mock.patch.dict(os.environ, {"CASE_TOKEN": token, "CASE_DOCKER_NETWORK": ""}), \
             mock.patch.object(cased.lifecycle, "ensure_running", return_value=row), \
             mock.patch.object(cased, "ws_connect", side_effect=connect):
            with _client().websocket_connect("ws://127.0.0.1/v1/computers/c_x/live/websockify",
                                             headers=headers) as ws:
                assert ws.receive_bytes() == b"RFB 003.008\n"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("PASS")
