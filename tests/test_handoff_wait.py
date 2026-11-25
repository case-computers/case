# SPDX-License-Identifier: MIT
"""Freehand handoffs have the same wait contract as auth attempts.
Run: .venv/bin/python tests/test_handoff_wait.py"""
import asyncio
import unittest.mock as mock

import _helpers

_helpers.isolated_home()
import events  # noqa: E402
import handoffs  # noqa: E402


class FakeRow(dict):
    def keys(self):
        return super().keys()


def row(hid="h_1", status="pending"):
    return FakeRow({"id": hid, "computer_id": "c_1", "kind": "device",
                    "prompt": "sign in on the live desk", "status": status,
                    "created_at": "2026-09-11T00:00:00Z", "answer": None,
                    "domain": None, "continuation": "verify_page",
                    "challenge_fingerprint": None, "screenshot": None})


def run(coro):
    try:
        return asyncio.run(coro)
    finally:
        events.SUBS.clear()
        events.LOOP = None


def test_already_answered_returns_immediately():
    with mock.patch.object(handoffs, "get_handoff", lambda h: row(status="completed")):
        out = run(handoffs.wait_handoff("h_1", after_status="pending", timeout_s=5))
    assert out["wait_status"] == "changed" and out["status"] == "completed"


def test_timeout_keeps_the_handoff_open():
    with mock.patch.object(handoffs, "get_handoff", lambda h: row()):
        out = run(handoffs.wait_handoff("h_1", after_status="pending", timeout_s=1))
    assert out["wait_status"] == "timeout"
    assert out["status"] == "pending"


def test_answer_during_the_wait_wakes_it():
    state = {"status": "pending"}
    with mock.patch.object(handoffs, "get_handoff", lambda h: row(status=state["status"])):
        async def scenario():
            events.set_loop(asyncio.get_running_loop())

            async def answer_later():
                await asyncio.sleep(0.05)
                state["status"] = "completed"
                events.emit("handoff_answered", {"handoff_id": "h_1", "value_present": True})
            task = asyncio.ensure_future(answer_later())
            out = await handoffs.wait_handoff("h_1", after_status="pending", timeout_s=10)
            await task
            return out

        out = run(scenario())
    assert out["wait_status"] == "changed" and out["status"] == "completed"


def test_event_for_another_handoff_is_ignored():
    with mock.patch.object(handoffs, "get_handoff", lambda h: row()):
        async def scenario():
            events.set_loop(asyncio.get_running_loop())

            async def noise():
                await asyncio.sleep(0.05)
                events.emit("handoff_answered", {"handoff_id": "h_OTHER"})
            task = asyncio.ensure_future(noise())
            out = await handoffs.wait_handoff("h_1", after_status="pending", timeout_s=1)
            await task
            return out

        assert run(scenario())["wait_status"] == "timeout"


def test_wait_never_returns_the_screenshot():
    out = handoffs.handoff_json(row(), with_screenshot=False)
    assert "screenshot_png_b64" not in out


def test_timeout_is_clamped():
    with mock.patch.object(handoffs, "get_handoff", lambda h: row(status="completed")):
        for bad in (0, -5, 10 ** 9, "nonsense", None):
            out = run(handoffs.wait_handoff("h_1", after_status="pending", timeout_s=bad))
            assert out["wait_status"] == "changed"


def test_validating_is_not_an_answer():
    with mock.patch.object(handoffs, "get_handoff", lambda h: row(status="validating")):
        out = run(handoffs.wait_handoff("h_1", after_status="pending", timeout_s=1))
    assert out["wait_status"] == "timeout", out


def test_a_validating_bounce_does_not_wake_the_wait():
    seq = iter(["validating", "pending"])
    with mock.patch.object(handoffs, "get_handoff",
                           lambda h: row(status=next(seq, "pending"))):
        async def scenario():
            events.set_loop(asyncio.get_running_loop())

            async def bounce():
                await asyncio.sleep(0.05)
                events.emit("handoff_answered", {"handoff_id": "h_1"})
            task = asyncio.ensure_future(bounce())
            out = await handoffs.wait_handoff("h_1", after_status="pending", timeout_s=1)
            await task
            return out

        assert run(scenario())["wait_status"] == "timeout"


def test_a_real_answer_still_wakes_it():
    seq = iter(["pending", "completed"])
    with mock.patch.object(handoffs, "get_handoff",
                           lambda h: row(status=next(seq, "completed"))):
        async def scenario():
            events.set_loop(asyncio.get_running_loop())

            async def answer():
                await asyncio.sleep(0.05)
                events.emit("handoff_answered", {"handoff_id": "h_1"})
            task = asyncio.ensure_future(answer())
            out = await handoffs.wait_handoff("h_1", after_status="pending", timeout_s=5)
            await task
            return out

        out = run(scenario())
    assert out["wait_status"] == "changed" and out["status"] == "completed", out


if __name__ == "__main__":
    _helpers.run_tests(globals())
