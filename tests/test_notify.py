# SPDX-License-Identifier: MIT
"""ntfy notifier wiring. Run: .venv/bin/python tests/test_notify.py"""
import importlib
import os
import sys
import threading
import unittest.mock as mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "control-plane"))
os.environ["CASE_HOME"] = "/tmp/case-notify-test"

# Force a clean import under known env for module-level notifier wiring tests.
os.environ.pop("CASE_NTFY_TOPIC", None)

import notify  # noqa: E402


def test_notifier_is_ntfy_and_carries_the_topic():
    os.environ["CASE_NTFY_TOPIC"] = "case-test-topic"
    importlib.reload(notify)
    assert isinstance(notify.notifier, notify.Ntfy)
    assert notify.notifier.topic == "case-test-topic"
    os.environ.pop("CASE_NTFY_TOPIC", None)
    importlib.reload(notify)


def test_no_topic_means_warned_noop_not_a_crash():
    os.environ.pop("CASE_NTFY_TOPIC", None)
    importlib.reload(notify)
    assert isinstance(notify.notifier, notify.Ntfy)
    with mock.patch.object(notify.requests, "post") as post:
        notify.notifier.notify({"id": "h_1", "kind": "question", "prompt": "hi",
                                "screenshot": None}, "box")
        notify.notifier.push("run finished ok")
    post.assert_not_called()


def test_ntfy_notify_posts_to_the_topic():
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


def test_create_handoff_mints_assist_and_passes_url_to_notifier():
    """Integration: create_handoff → mint → notify payload carries assist_url."""
    os.environ["CASE_PUBLIC_HOST"] = "acme.case.example"
    # handoffs imports notifier at load; stub after import
    import handoffs
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
        os.environ.pop("CASE_PUBLIC_HOST", None)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("all passed")
