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
    ntfy = notify.Ntfy("https://ntfy.sh", "topic-x", None)
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
    assert "case-outbound" in posted["headers"].get("X-Tags", "")


def test_ntfy_notify_sends_bearer_token():
    os.environ["CASE_NTFY_TOKEN"] = "secret-tok"
    ntfy = notify.Ntfy("https://ntfy.sh", "topic-x", None)
    done = threading.Event()
    posted = {}

    def fake_post(url, **kw):
        posted["headers"] = kw.get("headers")
        done.set()
        return mock.Mock(status_code=200)

    try:
        with mock.patch.object(notify.requests, "post", side_effect=fake_post):
            ntfy.notify({"id": "h_1", "kind": "question", "prompt": "hi",
                         "screenshot": None}, "box")
            assert done.wait(2), "ntfy thread did not run"
        assert posted["headers"]["Authorization"] == "Bearer secret-tok"
        assert "case-outbound" in posted["headers"]["X-Tags"]
    finally:
        os.environ.pop("CASE_NTFY_TOKEN", None)


def test_same_topic_does_not_start_answer_listen():
    ntfy = notify.Ntfy("https://ntfy.sh", "same", "same")
    with mock.patch.object(notify.threading, "Thread") as th:
        ntfy.listen(lambda *a: None)
    th.assert_not_called()


def test_push_marks_outbound():
    ntfy = notify.Ntfy("https://ntfy.sh", "topic-x", None)
    done = threading.Event()
    posted = {}

    def fake_post(url, **kw):
        posted["headers"] = kw.get("headers")
        done.set()
        return mock.Mock(status_code=200)

    with mock.patch.object(notify.requests, "post", side_effect=fake_post):
        ntfy.push("run finished ok")
        assert done.wait(2), "ntfy thread did not run"
    assert posted["headers"].get("X-Tags") == "case-outbound"


def _post_once(payload, name="box"):
    ntfy = notify.Ntfy("https://ntfy.sh", "topic-x", None)
    done = threading.Event()
    posted = {}

    def fake_post(url, **kw):
        posted.update(kw.get("headers") or {})
        done.set()
        return mock.Mock(status_code=200)

    with mock.patch.object(notify.requests, "post", side_effect=fake_post):
        ntfy.notify(payload, name)
        assert done.wait(2), "ntfy thread did not run"
    return posted


def test_multiline_prompt_is_flattened_into_the_header():
    h = _post_once({"id": "h_1", "kind": "question", "screenshot": None,
                    "prompt": "line one\nline two\r\n  line three"})
    assert h["X-Message"] == "line one line two line three"


def test_assist_url_becomes_the_click_action():
    h = _post_once({"id": "h_1", "kind": "question", "prompt": "hi", "screenshot": None,
                    "assist_url": "https://acme.example/assist/tok"})
    assert h["X-Click"] == "https://acme.example/assist/tok"
    assert "X-Actions" not in h


def test_approval_buttons_use_the_signed_answer_url():
    h = _post_once({"id": "h_1", "kind": "approval", "prompt": "ok?", "screenshot": None,
                    "answer_url": "https://case.example.com/answer/h_1/sig"})
    assert h["X-Actions"].count("https://case.example.com/answer/h_1/sig") == 2
    assert h["X-Actions"].count("headers.Content-Type=application/json") == 2
    assert "approve" in h["X-Actions"] and "deny" in h["X-Actions"]


def test_no_answer_url_means_no_buttons():
    h = _post_once({"id": "h_1", "kind": "approval", "prompt": "ok?", "screenshot": None})
    assert "X-Actions" not in h


def test_answer_token_ok_only_for_the_matching_signature():
    import handoffs
    from store import store
    with mock.patch.object(store, "sign", lambda text: "sig:" + text):
        assert handoffs.answer_token_ok("h_1", "sig:answer:h_1")
        assert not handoffs.answer_token_ok("h_1", "sig:answer:h_2")
        assert not handoffs.answer_token_ok("h_1", "")
        assert not handoffs.answer_token_ok("h_1", None)
        try:
            handoffs.answer_by_token("h_1", "nope", "approve")
            assert False, "bad token must not reach the handoff"
        except handoffs.ApiError as e:
            assert e.status == 404, e


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
