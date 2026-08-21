# SPDX-License-Identifier: MIT
"""Blocker routing: one live challenge owns one durable handoff."""
import os
import sys
import unittest.mock as mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "control-plane"))
os.environ["CASE_HOME"] = "/tmp/case-blockers-test"

import login_flow  # noqa: E402
import cased  # noqa: E402
from store import store  # noqa: E402


ROW = {"id": "c_1", "name": "ava", "state": "running"}
BLOCKER = {
    "kind": "otp",
    "prompt": "instagram.com: enter the code",
    "fingerprint": "instagram.com|/codeentry|enter the code",
}


def test_late_challenge_probe_matches_path_not_full_url():
    with mock.patch.object(login_flow, "eval_value", return_value={"otp": True, "text": "Enter the code"}) as evaluate, \
         mock.patch.object(login_flow, "screenshot_b64", return_value=None), \
         mock.patch.object(cased.auth_attempts, "raise_challenge",
                           return_value={"id": "a_1", "revision": 2,
                                         "status": "awaiting_human",
                                         "current_handoff_id": "h_otp"}):
        result = login_flow._post_login_challenge(
            ROW, "c_1", "instagram.com", "https://instagram.com/", attempt_id="a_1")
    probe = evaluate.call_args.args[1]
    assert "location.pathname" in probe, probe
    assert ".test(href)" not in probe, probe
    assert result["status"] == "handoff_pending", result


def test_active_attempt_reuses_its_pending_handoff():
    active = {"id": "a_1", "current_handoff_id": "h_live"}
    live = {"id": "h_live", "status": "pending"}
    with mock.patch.object(store, "get_active_auth_attempt", return_value=active), \
         mock.patch.object(store, "get_handoff", return_value=live), \
         mock.patch.object(cased.auth_attempts, "raise_challenge") as raise_, \
         mock.patch.object(cased.handoffs, "create_handoff") as create:
        hid = login_flow._route_blocker(ROW, BLOCKER)
    assert hid == "h_live", hid
    raise_.assert_not_called()
    create.assert_not_called()


def test_active_attempt_without_live_child_gets_bound_handoff():
    active = {"id": "a_1", "current_handoff_id": None}
    with mock.patch.object(store, "get_active_auth_attempt", return_value=active), \
         mock.patch.object(cased.auth_attempts, "raise_challenge",
                           return_value={"current_handoff_id": "h_new"}) as raise_, \
         mock.patch.object(cased.handoffs, "create_handoff") as create:
        hid = login_flow._route_blocker(ROW, BLOCKER)
    assert hid == "h_new", hid
    assert raise_.call_args.args[:3] == ("a_1", "otp", BLOCKER["prompt"])
    assert raise_.call_args.kwargs["challenge_fingerprint"] == BLOCKER["fingerprint"]
    create.assert_not_called()


def test_stale_attempt_pointer_is_replaced_on_same_attempt():
    active = {"id": "a_1", "current_handoff_id": "h_old"}
    old = {"id": "h_old", "status": "expired"}
    with mock.patch.object(store, "get_active_auth_attempt", return_value=active), \
         mock.patch.object(store, "get_handoff", return_value=old), \
         mock.patch.object(cased.auth_attempts, "raise_challenge",
                           return_value={"current_handoff_id": "h_new"}) as raise_, \
         mock.patch.object(cased.handoffs, "create_handoff"):
        hid = login_flow._route_blocker(ROW, BLOCKER)
    assert hid == "h_new", hid
    raise_.assert_called_once()


def test_proving_attempt_never_gets_parallel_standalone_handoff():
    active = {"id": "a_1", "status": "proving", "current_handoff_id": None}
    with mock.patch.object(store, "get_active_auth_attempt", return_value=active), \
         mock.patch.object(cased.auth_attempts, "raise_challenge") as raise_, \
         mock.patch.object(cased.handoffs, "create_handoff") as create:
        hid = login_flow._route_blocker(ROW, BLOCKER)
    assert hid is None
    raise_.assert_not_called()
    create.assert_not_called()


def test_standalone_blocker_reuses_pending_fingerprint_across_restart():
    existing = {"id": "h_existing", "status": "pending"}
    with mock.patch.object(store, "get_active_auth_attempt", return_value=None), \
         mock.patch.object(store, "get_open_handoff_by_fingerprint",
                           return_value=existing), \
         mock.patch.object(cased.handoffs, "create_handoff") as create:
        hid = login_flow._route_blocker(ROW, BLOCKER)
    assert hid == "h_existing", hid
    create.assert_not_called()


def test_standalone_blocker_persists_fingerprint():
    with mock.patch.object(store, "get_active_auth_attempt", return_value=None), \
         mock.patch.object(store, "get_open_handoff_by_fingerprint", return_value=None), \
         mock.patch.object(login_flow, "screenshot_b64", return_value=None), \
         mock.patch.object(cased.handoffs, "create_handoff",
                           return_value={"id": "h_new"}) as create:
        hid = login_flow._route_blocker(ROW, BLOCKER)
    assert hid == "h_new", hid
    assert create.call_args.kwargs["challenge_fingerprint"] == BLOCKER["fingerprint"]


def test_store_fingerprint_lookup_ignores_terminal_handoffs():
    store.delete_handoff("h_fp")
    try:
        store.insert_handoff(
            "h_fp", "c_1", "otp", "code", None, None,
            challenge_fingerprint=BLOCKER["fingerprint"])
        assert store.get_open_handoff_by_fingerprint(
            "c_1", BLOCKER["fingerprint"])["id"] == "h_fp"
        store.set_handoff_status("h_fp", "expired")
        assert store.get_open_handoff_by_fingerprint(
            "c_1", BLOCKER["fingerprint"]) is None
    finally:
        store.delete_handoff("h_fp")


if __name__ == "__main__":
    for name, fn in sorted(globals().copy().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("PASS")
