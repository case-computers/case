# SPDX-License-Identifier: MIT
"""The state machine (API_SPEC §2) reified in lifecycle.TRANSITIONS. Pure — no Docker.
Run: .venv/bin/python tests/test_lifecycle.py"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "control-plane"))
os.environ.setdefault("CASE_HOME", "/tmp/case-lifecycle-test")
from errors import ApiError  # noqa: E402
from lifecycle import can_transition, do_sleep, ensure_running  # noqa: E402
from store import store  # noqa: E402


def test_spec_edges_allowed():
    for a, b in [("creating", "running"), ("running", "asleep"), ("asleep", "waking"),
                 ("waking", "running"), ("running", "waking"), ("running", "deleted"),
                 ("asleep", "deleted"),
                 ("waking", "asleep")]:                       # recovery edge (wake failed)
        assert can_transition(a, b), (a, b)


def test_illegal_edges_rejected():
    for a, b in [("asleep", "running"),      # must go through waking
                 ("deleted", "running"),     # terminal
                 ("running", "creating"),    # can't un-create
                 ("creating", "asleep")]:     # never provisioned-to-asleep directly
        assert not can_transition(a, b), (a, b)


def _raises(fn, code):
    try:
        fn()
    except ApiError as e:
        assert e.code == code, e.code
        return
    assert False, f"expected ApiError {code}"


def test_creating_computer_is_not_actionable():
    # a computer mid-provision: ensure_running must give coherent guidance (not "wake it"),
    # and do_sleep must reject BEFORE stopping the half-built container (M2).
    cid = "c_unittest_creating"
    store.delete_computer(cid)
    store.insert_computer(cid, "t", "img", 1, 512, "vol", "tok")   # state=creating
    try:
        _raises(lambda: ensure_running(cid, wake=True), "provisioning")
        _raises(lambda: do_sleep(cid), "illegal_transition")       # raises before any docker call
    finally:
        store.delete_computer(cid)


def test_wake_respects_max_running():
    # wake used to bypass the create-time cap; hosted CAX11 needs the same ceiling.
    import lifecycle
    old = lifecycle.MAX_RUNNING
    cid = "c_unittest_wake_cap"
    other = "c_unittest_wake_cap_busy"
    store.delete_computer(cid)
    store.delete_computer(other)
    try:
        lifecycle.MAX_RUNNING = 1
        store.insert_computer(other, "busy", "img", 1, 512, "vol-b", "tok-b")
        store.set_state(other, "running")          # fills the only slot
        store.insert_computer(cid, "asleep", "img", 1, 512, "vol-a", "tok-a")
        store.set_state(cid, "asleep")
        from lifecycle import do_wake
        _raises(lambda: do_wake(cid), "too_many_running")  # before any docker call
    finally:
        lifecycle.MAX_RUNNING = old
        store.delete_computer(cid)
        store.delete_computer(other)


def test_sleep_blocked_when_auth_attempt_active():
    # API_SPEC: POST …/sleep → 409 auth_in_progress while a non-terminal AuthAttempt exists.
    cid = "c_unittest_sleep_pin"
    store.delete_computer(cid)
    store.q("DELETE FROM auth_attempts WHERE computer_id=?", (cid,))
    store.insert_computer(cid, "pin", "img", 1, 512, "vol-pin", "tok-pin")
    store.set_state(cid, "running")
    store.insert_auth_attempt(
        "a_unittest_pin", cid, "github", "https://example.com/login",
        status="awaiting_human")
    try:
        _raises(lambda: do_sleep(cid), "auth_in_progress")  # before any docker call
        assert store.get_computer(cid)["state"] == "running"
    finally:
        store.q("DELETE FROM auth_attempts WHERE computer_id=?", (cid,))
        store.delete_computer(cid)


def test_wake_respects_ram_budget():
    # A count cap cannot see size: one 8 GB desktop fits "max 8 running" and still
    # OOMs a 4 GB box. The budget is what actually refuses it.
    import lifecycle
    old_ram, old_run = lifecycle.MAX_RAM_MB, lifecycle.MAX_RUNNING
    cid, other = "c_unittest_ram", "c_unittest_ram_busy"
    store.delete_computer(cid)
    store.delete_computer(other)
    try:
        lifecycle.MAX_RUNNING, lifecycle.MAX_RAM_MB = 8, 4096      # room to count, not to fit
        store.insert_computer(other, "busy", "img", 1, 3072, "vol-b", "tok-b")
        store.set_state(other, "running")
        store.insert_computer(cid, "big", "img", 1, 2048, "vol-a", "tok-a")
        store.set_state(cid, "asleep")
        assert store.active_ram_mb() == 3072
        from lifecycle import do_wake
        _raises(lambda: do_wake(cid), "not_enough_ram")            # 3072 + 2048 > 4096
        lifecycle.MAX_RAM_MB = 8192                                # budget raised: admitted
        lifecycle.admit(2048)
    finally:
        lifecycle.MAX_RAM_MB, lifecycle.MAX_RUNNING = old_ram, old_run
        store.delete_computer(cid)
        store.delete_computer(other)


def test_ram_budget_off_by_default_is_not_a_cap():
    # No /proc (macOS) or CASE_MAX_RAM_MB=0 must behave exactly as before.
    import lifecycle
    old = lifecycle.MAX_RAM_MB
    try:
        lifecycle.MAX_RAM_MB = 0
        lifecycle.admit(1024 * 1024)          # a terabyte: nothing to check it against
    finally:
        lifecycle.MAX_RAM_MB = old


def test_sleep_all_parks_awake_computers_and_survives_a_failure():
    """cased's shutdown hook. One computer sleeps, one refuses (auth in flight);
    the refusal must not stop the rest, and must not raise out of the hook."""
    import lifecycle
    ok, stuck = "c_unittest_sleepall_ok", "c_unittest_sleepall_stuck"
    for cid in (ok, stuck):
        store.delete_computer(cid)
        store.q("DELETE FROM auth_attempts WHERE computer_id=?", (cid,))
    stopped = []
    real_stop = lifecycle.dockerd.stop_container
    lifecycle.dockerd.stop_container = lambda cid, **kw: stopped.append(cid)
    try:
        store.insert_computer(ok, "ok", "img", 1, 512, "vol-ok", "tok-ok")
        store.set_state(ok, "running")
        store.insert_computer(stuck, "stuck", "img", 1, 512, "vol-st", "tok-st")
        store.set_state(stuck, "running")
        store.insert_auth_attempt("a_unittest_sleepall", stuck, "github",
                                  "https://example.com/login", status="awaiting_human")
        slept = lifecycle.sleep_all()
        assert ok in slept and stuck not in slept, slept
        assert stopped == [ok], stopped
        assert store.get_computer(ok)["state"] == "asleep"
        assert store.get_computer(stuck)["state"] == "running"   # pinned by the login
    finally:
        lifecycle.dockerd.stop_container = real_stop
        store.q("DELETE FROM auth_attempts WHERE computer_id=?", (stuck,))
        store.delete_computer(ok)
        store.delete_computer(stuck)


def test_health_exposes_awake_cap():
    import cased
    from config import MAX_RUNNING
    h = cased.health()
    assert h["ok"] is True
    assert h["max_running"] == MAX_RUNNING
    assert "running" in h
    assert "computers" in h
    # the deployer draws the budget from here, so it has to be in the payload
    assert "max_ram_mb" in h and "ram_mb" in h


def test_create_rejects_nonsense_sizing():
    """cpus/ram_mb come from a browser form and land in `docker run --memory/--cpus`.
    Reject them here, or the daemon does it with a 500."""
    import cased
    for body in ({"ram_mb": 0}, {"ram_mb": "banana"}, {"cpus": 0},
                 {"cpus": 999}, {"ram_mb": 10 ** 9}):
        _raises(lambda b=body: cased.create_computer(b), "bad_request")
    # blank/absent means "the default", not "invalid"
    assert cased._num(None, 2048, 512, 65536, "ram_mb") == 2048
    assert cased._num("", 1, 0.25, 32, "cpus") == 1
    assert cased._num("4096", 2048, 512, 65536, "ram_mb") == 4096


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("PASS")
