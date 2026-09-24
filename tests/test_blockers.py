# SPDX-License-Identifier: MIT
"""Blocker routing: one live challenge owns one durable handoff."""
import unittest.mock as mock

import _helpers

_helpers.isolated_home()

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


def test_late_check_your_email_wall_is_a_device_challenge():
    # deskd tags this page email_verify, which advance_attempt raises as device.
    for probe_out, kind in (({"otp": False, "email": True, "text": "Check your email"}, "device"),
                            ({"otp": True, "email": True, "text": "Enter the code"}, "otp")):
        with mock.patch.object(login_flow, "eval_value", return_value=probe_out), \
             mock.patch.object(login_flow, "screenshot_b64", return_value=None), \
             mock.patch.object(cased.auth_attempts, "raise_challenge",
                               return_value={"id": "a_1", "revision": 2,
                                             "status": "awaiting_human",
                                             "current_handoff_id": "h_1"}) as raise_:
            login_flow._post_login_challenge(
                ROW, "c_1", "instagram.com", "https://instagram.com/", attempt_id="a_1")
        assert raise_.call_args.args[1] == kind, (probe_out, raise_.call_args)


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


class _Stop(Exception):
    pass


def _one_pass(rows, desk):
    """Run blocker_poller for exactly one sweep over `rows`."""
    with mock.patch.object(cased.time, "sleep", side_effect=[None, _Stop()]), \
         mock.patch.object(store, "running_rows", return_value=rows), \
         mock.patch.object(cased, "desk_json", side_effect=desk):
        try:
            cased.blocker_poller()
        except _Stop:
            pass


def test_expired_handoff_is_raised_again_while_the_page_still_blocks():
    store.delete_handoff("h_seen")
    cased.BLOCKER_SEEN.clear()
    try:
        store.insert_handoff("h_seen", "c_1", "otp", "code", None, None,
                             challenge_fingerprint=BLOCKER["fingerprint"])
        cased.BLOCKER_SEEN["c_1"] = (BLOCKER["fingerprint"], "h_seen")
        desk = lambda row, *a, **k: {"blocker": BLOCKER}
        with mock.patch.object(login_flow, "_route_blocker", return_value="h_new") as route:
            _one_pass([ROW], desk)
            route.assert_not_called()               # its handoff is still open
            store.set_handoff_status("h_seen", "expired")
            _one_pass([ROW], desk)
            route.assert_called_once()
        assert cased.BLOCKER_SEEN["c_1"] == (BLOCKER["fingerprint"], "h_new")
    finally:
        store.delete_handoff("h_seen")
        cased.BLOCKER_SEEN.clear()


def test_poller_forgets_computers_that_left_running_and_survives_a_bad_desk():
    cased.BLOCKER_SEEN.clear()
    cased.BLOCKER_SEEN["c_slept"] = ("fp", "h_x")
    other = {"id": "c_2", "name": "bo", "state": "running"}

    def desk(row, *a, **k):
        if row["id"] == "c_1":
            raise RuntimeError("desk went sideways")
        return {"blocker": BLOCKER}

    try:
        with mock.patch.object(login_flow, "_route_blocker", return_value="h_2") as route:
            _one_pass([ROW, other], desk)
        assert route.call_args.args[0]["id"] == "c_2"   # c_1's error did not skip c_2
        assert "c_slept" not in cased.BLOCKER_SEEN
        assert cased.BLOCKER_SEEN["c_2"] == (BLOCKER["fingerprint"], "h_2")
    finally:
        cased.BLOCKER_SEEN.clear()


if __name__ == "__main__":
    _helpers.run_tests(globals())
