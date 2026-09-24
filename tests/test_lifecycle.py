# SPDX-License-Identifier: MIT
"""The state machine reified in lifecycle.TRANSITIONS. Pure — no Docker.
Run: .venv/bin/python tests/test_lifecycle.py"""
import os

import _helpers

HOME = _helpers.isolated_home()
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


def test_creating_computer_is_not_actionable():
    # a computer mid-provision: ensure_running must give coherent guidance (not "wake it"),
    # and do_sleep must reject BEFORE stopping the half-built container (M2).
    cid = "c_unittest_creating"
    store.delete_computer(cid)
    store.insert_computer(cid, "t", "img", 1, 512, "vol", "tok")   # state=creating
    try:
        _helpers.raises(lambda: ensure_running(cid, wake=True), "provisioning")
        _helpers.raises(lambda: do_sleep(cid), "illegal_transition")  # raises before any docker call
    finally:
        store.delete_computer(cid)


def test_wake_respects_max_running():
    # wake used to bypass the create-time cap; a small host needs the same ceiling.
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
        _helpers.raises(lambda: do_wake(cid), "too_many_running")  # before any docker call
    finally:
        lifecycle.MAX_RUNNING = old
        store.delete_computer(cid)
        store.delete_computer(other)


def test_concurrent_creates_cannot_both_fit_under_the_cap():
    # admit() then insert was two steps: with MAX_RUNNING=1 two creates in the same
    # moment both passed the check and both came up.
    import threading
    import time as _time
    from unittest import mock
    import deskclient
    import dockerd
    import lifecycle
    for r in store.all_non_deleted():
        store.delete_computer(r["id"])
    real_admit, out = lifecycle.admit, []

    def slow_admit(ram_mb):
        real_admit(ram_mb)
        _time.sleep(0.2)                           # widen the check-then-insert window

    def create():
        try:
            out.append(lifecycle.provision("x")["state"])
        except ApiError as e:
            out.append(e.code)

    with mock.patch.object(lifecycle, "MAX_RUNNING", 1), \
         mock.patch.object(lifecycle, "admit", slow_admit), \
         mock.patch.object(dockerd, "create_volume"), \
         mock.patch.object(dockerd, "create_container"), \
         mock.patch.object(dockerd, "container_ports", lambda c, deadline=10: (1, 2)), \
         mock.patch.object(deskclient, "wait_desk"):
        ts = [threading.Thread(target=create) for _ in range(2)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
    try:
        assert sorted(out) == ["running", "too_many_running"], out
    finally:
        for r in store.all_non_deleted():
            store.delete_computer(r["id"])


def test_a_second_wake_does_not_take_over_a_waking_row():
    from unittest import mock
    import dockerd
    import lifecycle
    cid = "c_unittest_waking_claim"
    _asleep_row(cid)
    store.set_state(cid, "waking")
    try:
        with mock.patch.object(dockerd, "container_up", return_value=False), \
             mock.patch.object(dockerd, "start_container") as start:
            _helpers.raises(lambda: lifecycle.do_wake(cid), "waking")
        start.assert_not_called()
    finally:
        store.delete_computer(cid)


def test_sleep_blocked_when_auth_attempt_active():
    # POST …/sleep → 409 auth_in_progress while a non-terminal AuthAttempt exists.
    cid = "c_unittest_sleep_pin"
    store.delete_computer(cid)
    store.q("DELETE FROM auth_attempts WHERE computer_id=?", (cid,))
    store.insert_computer(cid, "pin", "img", 1, 512, "vol-pin", "tok-pin")
    store.set_state(cid, "running")
    store.insert_auth_attempt(
        "a_unittest_pin", cid, "github", "https://example.com/login",
        status="awaiting_human")
    try:
        _helpers.raises(lambda: do_sleep(cid), "auth_in_progress")  # before any docker call
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
        _helpers.raises(lambda: do_wake(cid), "not_enough_ram")  # 3072 + 2048 > 4096
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


def test_wake_rebuilds_a_container_docker_no_longer_has():
    # `docker rm` on a sleeping desktop: start_container 404s and the wake has to
    # recreate around the volume. dockerd.NotFound did not exist, so this path
    # raised AttributeError instead.
    from unittest import mock
    import deskclient
    import dockerd
    import lifecycle
    cid = "c_unittest_wake_gone"
    store.delete_computer(cid)
    store.insert_computer(cid, "gone", "img", 1, 512, "vol-g", "tok-g")
    store.set_state(cid, "asleep")
    try:
        with mock.patch.object(dockerd, "start_container", side_effect=dockerd.NotFound("x")), \
             mock.patch.object(dockerd, "create_container") as create, \
             mock.patch.object(dockerd, "get_container"), \
             mock.patch.object(dockerd, "container_ports", return_value=(1, 2)), \
             mock.patch.object(dockerd, "container_up", return_value=True), \
             mock.patch.object(deskclient, "wait_desk"):
            lifecycle.do_wake(cid)
        create.assert_called_once_with(cid, 1.0, 512, "vol-g", "tok-g")
        assert store.get_computer(cid)["state"] == "running"
    finally:
        store.delete_computer(cid)


class _Box:
    """Just enough of a container for do_wake/reconcile."""
    def __init__(self, st):
        self.st = st

    @property
    def status(self):
        return self.st["container"]


def _wake_harness(cid, during_wait):
    """do_wake with Docker faked; `during_wait` runs while deskd is coming up."""
    from unittest import mock
    import deskclient
    import dockerd
    import lifecycle
    st = {"container": "exited", "destroyed": [], "stopped": []}

    def stop(c, timeout=10):
        st["container"] = "exited"
        st["stopped"].append(c)

    with mock.patch.object(dockerd, "start_container",
                           lambda c: st.update(container="running")), \
         mock.patch.object(dockerd, "get_container", lambda c: _Box(st)), \
         mock.patch.object(dockerd, "managed_containers",
                           lambda: {dockerd.container_name(cid): _Box(st)}), \
         mock.patch.object(dockerd, "container_ports", lambda c, deadline=10: (1, 2)), \
         mock.patch.object(dockerd, "container_up", lambda c: st["container"] == "running"), \
         mock.patch.object(dockerd, "stop_container", stop), \
         mock.patch.object(dockerd, "destroy_infra",
                           lambda c, v: st["destroyed"].append(v)), \
         mock.patch.object(deskclient, "wait_desk", lambda *a: during_wait(cid, st)):
        try:
            lifecycle.do_wake(cid)
            st["error"] = None
        except ApiError as e:
            st["error"] = e.code
    return st


def _asleep_row(cid):
    store.delete_computer(cid)
    store.insert_computer(cid, "t", "img", 1, 512, "case-" + cid, "tok")
    store.set_state(cid, "asleep")


def test_sleep_landing_mid_wake_keeps_the_volume():
    # The row went asleep under a wake: that is not a delete, so the user's home
    # volume must survive; the container is just stopped to match the row.
    import lifecycle
    cid = "c_unittest_wake_sleep"
    _asleep_row(cid)
    try:
        st = _wake_harness(cid, lambda c, st: lifecycle.do_sleep(c))
        assert st["error"] == "wake_lost_race", st
        assert st["destroyed"] == [], st
        assert st["container"] == "exited", st
        assert store.get_computer(cid)["state"] == "asleep"
    finally:
        store.delete_computer(cid)


def test_sleep_from_another_thread_waits_for_the_wake():
    import threading
    from unittest import mock
    import time as _time
    import lifecycle
    cid = "c_unittest_wake_serial"
    _asleep_row(cid)
    other = {}

    def during(c, st):
        other["t"] = threading.Thread(target=lifecycle.do_sleep, args=(c,))
        other["t"].start()
        _time.sleep(0.2)
        assert store.get_computer(c)["state"] == "waking"   # the sleep is queued behind us

    try:
        with mock.patch.object(lifecycle.dockerd, "stop_container"):   # outlives the harness
            st = _wake_harness(cid, during)
            other["t"].join(5)
        assert st["error"] is None, st
        assert st["destroyed"] == [], st
        assert store.get_computer(cid)["state"] == "asleep"
    finally:
        store.delete_computer(cid)


def test_failed_wake_stops_the_container_it_started():
    # deskd never got healthy: the row goes back to asleep, and so must the
    # container, or the next reconcile flips the row to running behind everyone.
    cid = "c_unittest_wake_fail"
    _asleep_row(cid)

    def unhealthy(c, st):
        raise ApiError(504, "daemon_timeout", "deskd not healthy after 30s")

    try:
        st = _wake_harness(cid, unhealthy)
        assert st["error"] == "daemon_timeout", st
        assert st["stopped"] == [cid] and st["container"] == "exited", st
        assert st["destroyed"] == [], st
        assert store.get_computer(cid)["state"] == "asleep"
    finally:
        store.delete_computer(cid)


def test_delete_landing_mid_wake_still_tears_down():
    import lifecycle
    cid = "c_unittest_wake_delete"
    _asleep_row(cid)
    try:
        st = _wake_harness(cid, lambda c, st: lifecycle._force_state(c, "waking", "deleted"))
        assert st["error"] == "wake_lost_race", st
        assert st["destroyed"] == ["case-" + cid], st
    finally:
        store.delete_computer(cid)


def test_reconcile_leaves_a_wake_in_flight_alone():
    # The sweeper ticks every 20s; mid-wake the container is not up yet, and forcing
    # waking → asleep used to make the wake destroy the computer's volume.
    import lifecycle

    def sweep(cid, st):
        st["container"] = "created"
        lifecycle.reconcile()
        assert store.get_computer(cid)["state"] == "waking"
        st["container"] = "running"

    cid = "c_unittest_wake_recon"
    _asleep_row(cid)
    try:
        st = _wake_harness(cid, sweep)
        assert st["error"] is None, st
        assert st["destroyed"] == [], st
        assert store.get_computer(cid)["state"] == "running"
    finally:
        store.delete_computer(cid)


def test_reconcile_still_repairs_a_stale_waking_row():
    # cased restarted mid-wake: nothing is in flight, so the row is fixed as before.
    from unittest import mock
    import dockerd
    import lifecycle
    cid = "c_unittest_stale_waking"
    _asleep_row(cid)
    store.set_state(cid, "waking")
    try:
        with mock.patch.object(dockerd, "managed_containers", return_value={}):
            lifecycle.reconcile()
        assert store.get_computer(cid)["state"] == "asleep"
    finally:
        store.delete_computer(cid)


def test_reconcile_during_provision_does_not_lose_the_create():
    from unittest import mock
    import deskclient
    import dockerd
    import lifecycle

    def volume(v):
        # the sweeper ticks before the container exists
        with mock.patch.object(dockerd, "managed_containers", return_value={}):
            lifecycle.reconcile()

    with mock.patch.object(dockerd, "create_volume", volume), \
         mock.patch.object(dockerd, "create_container", lambda *a: None), \
         mock.patch.object(dockerd, "container_ports", lambda c, deadline=10: (1, 2)), \
         mock.patch.object(dockerd, "destroy_infra") as destroy, \
         mock.patch.object(deskclient, "wait_desk"), \
         mock.patch.object(lifecycle, "admit"):
        out = lifecycle.provision("race")
    try:
        assert out["state"] == "running", out
        destroy.assert_not_called()
    finally:
        store.delete_computer(out["id"])


def test_vault_directory_and_database_are_private():
    # ~/.case holds the Fernet key and every encrypted secret. An install that
    # predates this (or a loose umask) leaves them world-readable.
    import shutil
    from store import Store
    home = os.path.join(HOME, "perms")
    shutil.rmtree(home, ignore_errors=True)
    os.makedirs(home, mode=0o755)                  # the permissive dir an upgrade inherits
    try:
        Store(home)
        assert os.stat(home).st_mode & 0o777 == 0o700
        assert os.stat(os.path.join(home, "case.db")).st_mode & 0o777 == 0o600
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_destroy_takes_the_schedules_with_it():
    # An orphaned schedule keeps firing against a deleted computer, and every run
    # fails on a row that is no longer there.
    import lifecycle
    cid, sid = "c_unittest_destroy_sched", "s_unittest_destroy_sched"
    store.delete_computer(cid)
    store.delete_schedule(sid)
    real = lifecycle.dockerd.destroy_infra
    lifecycle.dockerd.destroy_infra = lambda cid, volume: None
    try:
        store.insert_computer(cid, "doomed", "img", 1, 512, "vol-d", "tok-d")
        store.set_state(cid, "running")
        store.insert_schedule(sid, cid, "nightly", "do a thing", "cron", "0 3 * * *", 0, None)
        lifecycle.destroy(cid)
        assert store.get_schedule(sid) is None
    finally:
        lifecycle.dockerd.destroy_infra = real
        store.delete_schedule(sid)
        store.delete_computer(cid)


def test_health_exposes_awake_cap():
    import cased
    from types import SimpleNamespace
    from config import MAX_RUNNING
    h = cased.health(SimpleNamespace(headers={}))   # untokened box: every caller is trusted
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
        _helpers.raises(lambda b=body: cased.create_computer(b), "bad_request")
    # blank/absent means "the default", not "invalid"
    assert cased._num(None, 2048, 512, 65536, "ram_mb") == 2048
    assert cased._num("", 1, 0.25, 32, "cpus") == 1
    assert cased._num("4096", 2048, 512, 65536, "ram_mb") == 4096


if __name__ == "__main__":
    _helpers.run_tests(globals())
