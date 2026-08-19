# SPDX-License-Identifier: MIT
"""Auth attempt long-poll wait (cursor + event wake).
Run: .venv/bin/python tests/test_auth_wait.py"""
import asyncio
import os
import shutil
import sys
import tempfile
import unittest.mock as mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "control-plane"))
_HOME = tempfile.mkdtemp(prefix="case-auth-wait-")
os.environ["CASE_HOME"] = _HOME

import auth_attempts  # noqa: E402
import events  # noqa: E402
import handoffs  # noqa: E402
from errors import ApiError  # noqa: E402
from store import store  # noqa: E402

handoffs.notifier = type("N", (), {"notify": lambda self, h, name: None})()
COMP = {"id": "c_1", "name": "ava", "state": "running"}


def _cleanup():
    store.q("DELETE FROM auth_attempts")
    store.q("DELETE FROM handoffs")
    store.q("DELETE FROM credentials")
    handoffs.LOGIN_CTX.clear()
    events.SUBS.clear()
    events.LOOP = None


def _run(coro):
    return asyncio.run(coro)


def test_wait_returns_immediately_when_cursor_stale():
    _cleanup()
    a = auth_attempts.start_attempt("c_1", "github", "https://example.com/login")
    # Force a revision bump so after_revision=0 is stale.
    auth_attempts.cancel_attempt(a["id"], expected_revision=0)

    async def go():
        events.set_loop(asyncio.get_running_loop())
        return await auth_attempts.wait_attempt(
            a["id"], after_revision=0, timeout_s=5)

    out = _run(go())
    assert out["changed"] is True
    assert out["wait_status"] == "terminal"
    assert out["attempt"]["status"] == "cancelled"
    assert out["login_result"]["status"] == "failed"
    assert "secret" not in str(out).lower()
    assert "password" not in str(out).lower()


def test_wait_timeout_returns_snapshot():
    _cleanup()
    a = auth_attempts.start_attempt("c_1", "github", "https://example.com/login")

    async def go():
        events.set_loop(asyncio.get_running_loop())
        return await auth_attempts.wait_attempt(
            a["id"], after_revision=0, after_handoff_id=None, timeout_s=1)

    out = _run(go())
    assert out["changed"] is False
    assert out["wait_status"] == "timeout"
    assert out["attempt"]["id"] == a["id"]
    assert out["attempt"]["status"] == "created"
    assert out["attempt"]["revision"] == 0


def test_wait_wakes_on_cas_publish():
    _cleanup()
    a = auth_attempts.start_attempt("c_1", "github", "https://example.com/login")

    async def go():
        events.set_loop(asyncio.get_running_loop())

        async def cancel_soon():
            await asyncio.sleep(0.15)
            auth_attempts.cancel_attempt(a["id"], expected_revision=0)

        task = asyncio.create_task(cancel_soon())
        out = await auth_attempts.wait_attempt(
            a["id"], after_revision=0, timeout_s=5)
        await task
        return out

    out = _run(go())
    assert out["changed"] is True
    assert out["attempt"]["status"] == "cancelled"
    assert out["wait_status"] == "terminal"


def test_wait_wakes_on_handoff_pointer_change():
    _cleanup()
    a = auth_attempts.start_attempt("c_1", "github", "https://example.com/login")
    # Move into advancing with revision 1, no handoff yet.
    auth_attempts._cas_or_conflict(a["id"], "created", "advancing", 0)
    before = store.get_auth_attempt(a["id"])
    assert before["revision"] == 1
    assert before["current_handoff_id"] is None

    async def go():
        events.set_loop(asyncio.get_running_loop())

        async def raise_soon():
            await asyncio.sleep(0.15)
            with mock.patch("lifecycle.get_computer", return_value=COMP), \
                 mock.patch("deskclient.screenshot_b64", return_value=None), \
                 mock.patch("handoffs.create_handoff",
                            return_value={"id": "h_wait1"}):
                auth_attempts.raise_challenge(
                    a["id"], "otp", "enter code", domain="example.com")

        task = asyncio.create_task(raise_soon())
        out = await auth_attempts.wait_attempt(
            a["id"], after_revision=1, after_handoff_id=None, timeout_s=5)
        await task
        return out

    out = _run(go())
    assert out["changed"] is True
    assert out["attempt"]["current_handoff_id"] == "h_wait1"
    assert out["attempt"]["status"] == "awaiting_human"
    assert out["login_result"]["status"] == "handoff_pending"


def test_cancel_fails_open_child_handoff():
    _cleanup()
    a = auth_attempts.start_attempt("c_1", "github", "https://example.com/login")
    auth_attempts._cas_or_conflict(a["id"], "created", "advancing", 0)
    store.insert_handoff(
        "h_open", "c_1", "otp", "enter code", None, "github",
        domain="example.com", attempt_id=a["id"], sequence=1, revision=0)
    store.set_attempt_handoff(a["id"], "h_open")
    handoffs.LOGIN_CTX["h_open"] = {"computer_id": "c_1", "credential": "github"}
    # revision is still 1 (advancing); cancel from advancing
    out = auth_attempts.cancel_attempt(a["id"], expected_revision=1)
    assert out["status"] == "cancelled"
    assert store.get_handoff("h_open")["status"] == "failed"
    assert "h_open" not in handoffs.LOGIN_CTX


def test_wait_404_unknown_attempt():
    _cleanup()

    async def go():
        events.set_loop(asyncio.get_running_loop())
        return await auth_attempts.wait_attempt("a_missing", timeout_s=1)

    try:
        _run(go())
        assert False, "expected ApiError"
    except ApiError as e:
        assert e.code == "not_found"


def test_wait_caps_timeout():
    _cleanup()
    a = auth_attempts.start_attempt("c_1", "github", "https://example.com/login")
    assert auth_attempts.WAIT_TIMEOUT_MAX_S == 270

    async def short():
        events.set_loop(asyncio.get_running_loop())
        return await auth_attempts.wait_attempt(
            a["id"], after_revision=0, timeout_s=1)

    out = _run(short())
    assert out["wait_status"] == "timeout"


def test_concurrent_waiters_all_wake_and_unsubscribe():
    _cleanup()
    a = auth_attempts.start_attempt("c_1", "github", "https://example.com/login")

    async def go():
        events.set_loop(asyncio.get_running_loop())

        async def one():
            return await auth_attempts.wait_attempt(
                a["id"], after_revision=0, timeout_s=5)

        t1 = asyncio.create_task(one())
        t2 = asyncio.create_task(one())
        await asyncio.sleep(0.1)
        assert len(events.SUBS) >= 2
        auth_attempts.cancel_attempt(a["id"], expected_revision=0)
        r1, r2 = await asyncio.gather(t1, t2)
        assert r1["changed"] and r2["changed"]
        assert r1["attempt"]["status"] == "cancelled"
        assert r2["attempt"]["status"] == "cancelled"
        # Waiters cleaned up.
        assert events.SUBS == []

    _run(go())


def test_wait_payload_never_includes_secrets():
    _cleanup()
    store.upsert_credential(
        "c_1", "github", "u", "super-secret-password", None, None, ["github.com"])
    a = auth_attempts.start_attempt("c_1", "github", "https://example.com/login")
    out = auth_attempts._wait_payload(
        auth_attempts.attempt_public(store.get_auth_attempt(a["id"])),
        changed=False, wait_status="timeout")
    blob = json_dumps(out)
    assert "super-secret-password" not in blob
    assert "proof_spec" not in blob  # raw spec never on public attempt


def test_sequential_child_replacement_wakes_waiter():
    """OTP → next challenge on the same attempt advances the handoff cursor."""
    _cleanup()
    a = auth_attempts.start_attempt("c_1", "github", "https://example.com/login")
    auth_attempts._cas_or_conflict(a["id"], "created", "advancing", 0)
    store.insert_handoff(
        "h_otp", "c_1", "otp", "enter code", None, "github",
        domain="example.com", attempt_id=a["id"], sequence=1, revision=0)
    store.set_attempt_handoff(a["id"], "h_otp")
    auth_attempts._cas_or_conflict(a["id"], "advancing", "awaiting_human", 1)
    mid = store.get_auth_attempt(a["id"])
    assert mid["current_handoff_id"] == "h_otp"
    assert mid["revision"] == 2

    async def go():
        events.set_loop(asyncio.get_running_loop())

        async def next_challenge():
            await asyncio.sleep(0.15)
            store.set_handoff_status("h_otp", "resolved", answer=None)
            with mock.patch("lifecycle.get_computer", return_value=COMP), \
                 mock.patch("deskclient.screenshot_b64", return_value=None), \
                 mock.patch("handoffs.create_handoff",
                            return_value={"id": "h_passkey"}):
                # raise_challenge CAS from awaiting_human → advancing → awaiting
                auth_attempts.raise_challenge(
                    a["id"], "passkey", "touch key", domain="example.com")

        task = asyncio.create_task(next_challenge())
        out = await auth_attempts.wait_attempt(
            a["id"], after_revision=2, after_handoff_id="h_otp", timeout_s=5)
        await task
        return out

    out = _run(go())
    assert out["changed"] is True
    assert out["attempt"]["current_handoff_id"] == "h_passkey"
    assert out["attempt"]["status"] == "awaiting_human"
    assert out["login_result"]["handoff_id"] == "h_passkey"


def test_wait_safe_after_restart_style_reread():
    """Missed publish still surfaces via periodic SQLite re-read (no LOOP)."""
    _cleanup()
    a = auth_attempts.start_attempt("c_1", "github", "https://example.com/login")
    # Intentionally leave events.LOOP unset so emit is a no-op; cancel still
    # writes SQLite and the waiter's re-read must notice.
    events.LOOP = None

    async def go():
        async def cancel_soon():
            await asyncio.sleep(0.2)
            auth_attempts.cancel_attempt(a["id"], expected_revision=0)

        task = asyncio.create_task(cancel_soon())
        out = await auth_attempts.wait_attempt(
            a["id"], after_revision=0, timeout_s=5)
        await task
        return out

    out = _run(go())
    assert out["changed"] is True
    assert out["attempt"]["status"] == "cancelled"


def _awaiting_with_child(hid):
    a = auth_attempts.start_attempt("c_1", "github", "https://example.com/login")
    auth_attempts._cas_or_conflict(a["id"], "created", "advancing", 0)
    store.insert_handoff(
        hid, "c_1", "otp", "enter code", None, "github",
        domain="example.com", attempt_id=a["id"], sequence=1, revision=1)
    store.set_attempt_handoff(a["id"], hid)
    auth_attempts._cas_or_conflict(a["id"], "advancing", "awaiting_human", 1)
    return a["id"], int(store.get_auth_attempt(a["id"])["revision"])


def test_wait_timeout_reobserves_desk_solved_challenge():
    # Human typed the OTP straight into the desk: challenge gone, nothing bumped
    # the attempt. The timeout path must peek and advance instead of waiting forever.
    _cleanup()
    aid, rev = _awaiting_with_child("h_desk_solved")
    solved = {"ok": True, "observation": {"challenge_signals": [], "visible_fields": {}}}

    async def go():
        events.set_loop(asyncio.get_running_loop())
        with mock.patch("lifecycle.get_computer", return_value=COMP), \
             mock.patch("deskclient.observe_auth", return_value=solved):
            return await auth_attempts.wait_attempt(
                aid, after_revision=rev, timeout_s=1)

    out = _run(go())
    assert out["changed"] is True, out
    # heuristic attempt (no proof_spec) ends unverified, never authenticated
    assert out["attempt"]["status"] == "unverified", out
    assert out["login_result"]["status"] == "unverified"
    assert store.get_handoff("h_desk_solved")["status"] == "completed"


def test_wait_timeout_challenge_still_up_keeps_waiting():
    _cleanup()
    aid, rev = _awaiting_with_child("h_still_up")
    pending = {"ok": True, "observation": {"challenge_signals": ["otp"],
                                           "visible_fields": {"code": True}}}

    async def go():
        events.set_loop(asyncio.get_running_loop())
        with mock.patch("lifecycle.get_computer", return_value=COMP), \
             mock.patch("deskclient.observe_auth", return_value=pending):
            return await auth_attempts.wait_attempt(
                aid, after_revision=rev, timeout_s=1)

    out = _run(go())
    assert out["changed"] is False
    assert out["wait_status"] == "timeout"
    assert out["attempt"]["status"] == "awaiting_human"
    assert int(out["attempt"]["revision"]) == rev
    assert store.get_handoff("h_still_up")["status"] == "pending"


def json_dumps(obj):
    import json
    return json.dumps(obj)


if __name__ == "__main__":
    try:
        for name, fn in sorted(globals().items()):
            if name.startswith("test_"):
                fn()
                print("ok", name)
        print("PASS")
    finally:
        try:
            store.db.close()
        except Exception:
            pass
        shutil.rmtree(_HOME, ignore_errors=True)
