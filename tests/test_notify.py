# SPDX-License-Identifier: MIT
"""RelayNotifier + channel selection. Run: .venv/bin/python tests/test_notify.py"""
import importlib
import os
import sys
import threading
import time
import unittest.mock as mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "control-plane"))
os.environ["CASE_HOME"] = "/tmp/case-notify-test"

# Force a clean import under known channel/env for module-level notifier wiring tests.
os.environ.pop("CASE_NOTIFY_CHANNEL", None)
os.environ.pop("CASE_NOTIFY_CREDENTIAL", None)
os.environ.pop("CASE_HANDOFF_RELAY_URL", None)
os.environ.pop("CASE_NTFY_TOPIC", None)

import notify  # noqa: E402
from events import emit  # noqa: E402 — used via mock path
import events  # noqa: E402


DEFAULT_RELAY = "https://vwttrlkoccrdijkymhiz.supabase.co/functions/v1/handoff-email"


def _handoff(**kw):
    h = {
        "id": "h_rel1",
        "kind": "otp",
        "prompt": "enter code",
        "screenshot": None,
        "assist_url": "https://acme.case.example/assist/tokABC",
        "expires_at": "2099-01-01T00:00:00Z",
        "domain": "example.com",
    }
    h.update(kw)
    return h


def test_relay_posts_expected_body_and_auth_never_recipient():
    r = notify.RelayNotifier(DEFAULT_RELAY, "cn_deadbeef" + "0" * 24)
    posted = {}

    def fake_post(url, **kw):
        posted["url"] = url
        posted["headers"] = kw.get("headers")
        posted["json"] = kw.get("json")
        resp = mock.Mock()
        resp.status_code = 200
        resp.raise_for_status = mock.Mock()
        return resp

    with mock.patch.object(notify.requests, "post", side_effect=fake_post), \
         mock.patch.object(notify.time, "sleep"):
        assert r._deliver(_handoff(), "acme") is True

    assert posted["url"] == DEFAULT_RELAY
    assert posted["headers"]["Authorization"] == "Bearer cn_deadbeef" + "0" * 24
    body = posted["json"]
    assert body["handoff_id"] == "h_rel1"
    assert body["assist_url"].endswith("/assist/tokABC")
    assert body["expires_at"] == "2099-01-01T00:00:00Z"
    assert body["kind"] == "otp"
    assert body["computer_name"] == "acme"
    assert body["domain"] == "example.com"
    for forbidden in ("to", "email", "recipient", "owner_email"):
        assert forbidden not in body


def test_relay_retries_then_marks_notify_failed_without_raising():
    r = notify.RelayNotifier(DEFAULT_RELAY, "cn_" + "ab" * 16)
    sleeps = []
    calls = {"n": 0}

    def boom(*a, **k):
        calls["n"] += 1
        raise notify.requests.ConnectionError("down")

    emitted = []

    def capture(type_, data):
        emitted.append((type_, data))

    with mock.patch.object(notify.requests, "post", side_effect=boom), \
         mock.patch.object(notify.time, "sleep", side_effect=lambda s: sleeps.append(s)), \
         mock.patch.object(notify, "emit", side_effect=capture):
        # synchronous path used by tests; notify() itself is fire-and-forget
        r._send(_handoff(), "acme")

    assert calls["n"] == 3
    assert sleeps == [0.5, 2.0, 5.0]
    assert emitted, "expected notify_failed event"
    typ, data = emitted[-1]
    assert typ == "handoff_notify_failed"
    assert data.get("notify_failed") is True
    assert data.get("handoff_id") == "h_rel1"
    # still pending — we never touch handoff status from the notifier
    assert "status" not in data or data.get("status") == "pending"


def test_relay_succeeds_on_second_attempt():
    r = notify.RelayNotifier(DEFAULT_RELAY, "cn_" + "cd" * 16)
    n = {"i": 0}

    def flaky(*a, **k):
        n["i"] += 1
        if n["i"] < 2:
            raise notify.requests.Timeout("slow")
        resp = mock.Mock()
        resp.status_code = 200
        resp.raise_for_status = mock.Mock()
        return resp

    with mock.patch.object(notify.requests, "post", side_effect=flaky), \
         mock.patch.object(notify.time, "sleep"), \
         mock.patch.object(notify, "emit") as em:
        r._send(_handoff(), "acme")
    assert n["i"] == 2
    em.assert_not_called()


def _clear_notify_env():
    for k in ("CASE_NOTIFY_CHANNEL", "CASE_NOTIFY_CREDENTIAL",
              "CASE_NTFY_TOPIC", "CASE_NTFY_ANSWER_TOPIC", "CASE_HANDOFF_RELAY_URL"):
        os.environ.pop(k, None)


def test_channel_ntfy_selects_ntfy_class():
    _clear_notify_env()
    os.environ["CASE_NOTIFY_CHANNEL"] = "ntfy"
    os.environ["CASE_NTFY_TOPIC"] = "case-test-topic"
    importlib.reload(notify)
    assert isinstance(notify.notifier, notify.Ntfy)
    assert notify.notifier.topic == "case-test-topic"
    _clear_notify_env()
    importlib.reload(notify)
    assert isinstance(notify.notifier, notify.RelayNotifier)


def test_channel_explicit_relay_selects_relay():
    _clear_notify_env()
    os.environ["CASE_NOTIFY_CHANNEL"] = "relay"
    # Even with a legacy topic present, explicit relay wins.
    os.environ["CASE_NTFY_TOPIC"] = "legacy-topic"
    importlib.reload(notify)
    assert isinstance(notify.notifier, notify.RelayNotifier)
    _clear_notify_env()
    importlib.reload(notify)


def test_unset_falls_back_to_ntfy_when_topic_set():
    """Legacy boxes: no channel, no cn_, but CASE_NTFY_TOPIC → keep ntfy."""
    _clear_notify_env()
    os.environ["CASE_NTFY_TOPIC"] = "partner-legacy-topic"
    importlib.reload(notify)
    assert isinstance(notify.notifier, notify.Ntfy)
    assert notify.notifier.topic == "partner-legacy-topic"
    _clear_notify_env()
    importlib.reload(notify)


def test_unset_prefers_relay_when_credential_enrolled():
    """Enrolled boxes: unset channel + CASE_NOTIFY_CREDENTIAL → Relay."""
    _clear_notify_env()
    os.environ["CASE_NOTIFY_CREDENTIAL"] = "cn_" + ("11" * 16)
    os.environ["CASE_NTFY_TOPIC"] = "also-present"  # credential wins
    importlib.reload(notify)
    assert isinstance(notify.notifier, notify.RelayNotifier)
    assert notify.notifier.credential.startswith("cn_")
    _clear_notify_env()
    importlib.reload(notify)


def test_unset_neither_cred_nor_topic_is_relay_fail_closed():
    _clear_notify_env()
    importlib.reload(notify)
    assert isinstance(notify.notifier, notify.RelayNotifier)
    assert not notify.notifier.credential


def test_ntfy_notify_still_posts_when_selected():
    ntfy = notify.Ntfy("https://ntfy.sh", "topic-x", None, "http://127.0.0.1:8787/v1")
    done = threading.Event()
    posted = {}

    def fake_post(url, **kw):
        posted["url"] = url
        posted["headers"] = kw.get("headers")
        done.set()
        return mock.Mock(status_code=200)

    with mock.patch.object(notify.requests, "post", side_effect=fake_post):
        ntfy.notify({"id": "h_1", "kind": "question", "prompt": "hi", "screenshot": None},
                    "box")
        assert done.wait(2), "ntfy thread did not run"
    assert posted["url"] == "https://ntfy.sh/topic-x"
    assert "h_1" in posted["headers"].get("X-Tags", "")


def test_relay_push_is_noop_schedule_reports_stay_off_relay():
    r = notify.RelayNotifier(DEFAULT_RELAY, "cn_" + "ef" * 16)
    with mock.patch.object(notify.requests, "post") as post:
        r.push("run finished ok")
        time.sleep(0.05)  # push would be async if it did anything
    post.assert_not_called()


def test_create_handoff_mints_assist_and_passes_url_to_notifier():
    """Integration: create_handoff → mint → notify payload carries assist_url."""
    os.environ["CASE_MCP_HOST"] = "acme.case.example"
    # handoffs imports notifier at load; stub after import
    import handoffs
    import assist
    from store import store

    captured = []

    class Cap:
        def notify(self, h, name):
            captured.append((dict(h), name))

    handoffs.notifier = Cap()
    hid_holder = {}

    # delete leftover
    store.q("DELETE FROM assist_tokens")
    try:
        row = {"id": "c_notify", "name": "acme", "state": "running"}
        h = handoffs.create_handoff(row, "otp", "enter the code", domain="login.example.com")
        hid_holder["id"] = h["id"]
        assert h["status"] == "pending"
        assert captured, "notify not called"
        payload, name = captured[0]
        assert name == "acme"
        assert payload["assist_url"].startswith(
            "https://acme.case.example/assist/")
        assert payload["expires_at"]
        assert payload["domain"] == "login.example.com"
        # plaintext token must not leak into public handoff json
        assert "assist_url" not in h
        token = payload["assist_url"].rsplit("/", 1)[-1]
        assert token
        th = __import__("hashlib").sha256(token.encode()).hexdigest()
        assert store.get_assist_by_token_hash(th) is not None
    finally:
        if hid_holder.get("id"):
            store.delete_handoff(hid_holder["id"])
        store.q("DELETE FROM assist_tokens")
        handoffs.LOGIN_CTX.clear()
        os.environ.pop("CASE_MCP_HOST", None)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("all passed")
