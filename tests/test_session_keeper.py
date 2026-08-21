# SPDX-License-Identifier: MIT
"""Session keeper preflight (probe + skip-when-auth-pin).
Run: .venv/bin/python tests/test_session_keeper.py"""
import json
import os
import sys
import tempfile
import unittest.mock as mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "control-plane"))
# assignment, NOT setdefault: these tests write credential probe rows.
_HOME = tempfile.mkdtemp(prefix="case-session-keeper-")
os.environ["CASE_HOME"] = _HOME

import session_keeper  # noqa: E402
from store import store  # noqa: E402


def _cleanup():
    store.q("DELETE FROM auth_attempts")
    store.q("DELETE FROM credentials")
    store.q("DELETE FROM computers")


def _computer(cid="c_sk1", state="running"):
    store.delete_computer(cid)
    store.insert_computer(cid, "sk", "img", 1, 512, f"vol-{cid}", f"tok-{cid}")
    store.set_state(cid, state)
    # Fresh create stamps last_active_at=now; clear so keeper busy-guard does not
    # treat brand-new test rows as a live agent session.
    store.q("UPDATE computers SET last_active_at=? WHERE id=?",
            ("2020-01-01T00:00:00Z", cid))
    return cid


def _cred(cid, name="github", probe_url="https://example.com/",
          proof_spec=None, last_status=None):
    store.upsert_credential(cid, name, "u@x", "secret", None, None, ["example.com"])
    store.q(
        "UPDATE credentials SET probe_url=?, proof_spec=?, last_status=? "
        "WHERE computer_id=? AND name=?",
        (probe_url,
         json.dumps(proof_spec) if isinstance(proof_spec, dict) else proof_spec,
         last_status, cid, name))


def _reset_keeper_clock():
    session_keeper._last_probe_at.clear()
    session_keeper.INTERVAL_S = 21600
    session_keeper.BUSY_S = 900


def test_tick_skips_computer_with_active_attempt():
    _cleanup()
    _reset_keeper_clock()
    cid = _computer(state="asleep")
    _cred(cid, proof_spec={"url_contains": "/home"})
    store.insert_auth_attempt(
        "a_sk_active", cid, "github", "https://example.com/login",
        status="awaiting_human")
    woke, slept, probed = [], [], []

    with mock.patch.object(session_keeper, "do_wake", side_effect=lambda c: woke.append(c)), \
         mock.patch.object(session_keeper, "do_sleep", side_effect=lambda c: slept.append(c)), \
         mock.patch.object(session_keeper, "_probe_one_awake",
                           side_effect=lambda c, n: probed.append((c, n)) or "ok"):
        session_keeper.tick()

    assert woke == [], woke
    assert slept == [], slept
    assert probed == [], probed


def test_probe_one_skips_when_active_attempt():
    _cleanup()
    _reset_keeper_clock()
    cid = _computer(state="running")
    _cred(cid, proof_spec={"url_contains": "/home"})
    store.insert_auth_attempt(
        "a_sk_probe", cid, "github", "https://example.com/login", status="proving")
    with mock.patch.object(session_keeper, "do_wake") as wake, \
         mock.patch.object(session_keeper, "_observe") as observe:
        out = session_keeper._probe_one_awake(cid, "github")
    assert out is None
    wake.assert_not_called()
    observe.assert_not_called()


def test_probe_records_ok_when_proof_passes():
    _cleanup()
    _reset_keeper_clock()
    cid = _computer(state="running")
    _cred(cid, probe_url="https://example.com/app",
          proof_spec={"url_contains": "/app"})
    obs = {"href": "https://example.com/app/home", "visible_fields": {},
           "challenge_signals": []}

    with mock.patch("deskclient.navigate", return_value={"ok": True}), \
         mock.patch.object(session_keeper, "_observe", return_value=obs), \
         mock.patch.object(session_keeper, "_eval_proof", return_value=True):
        status = session_keeper._probe_one_awake(cid, "github")

    assert status == "ok"
    row = store.get_credential(cid, "github")
    assert row["last_status"] == "ok"
    assert row["last_verified_at"]


def test_probe_records_failed_when_logged_out():
    _cleanup()
    _reset_keeper_clock()
    cid = _computer(state="running")
    _cred(cid, probe_url="https://example.com/login", proof_spec=None)
    obs = {"href": "https://example.com/login",
           "visible_fields": {"pass": True}, "challenge_signals": []}

    with mock.patch("deskclient.navigate", return_value={"ok": True}), \
         mock.patch.object(session_keeper, "_observe", return_value=obs), \
         mock.patch.object(session_keeper, "_maybe_start_recovery") as recover:
        status = session_keeper._probe_one_awake(cid, "github")

    assert status == "failed"
    assert store.get_credential(cid, "github")["last_status"] == "failed"
    recover.assert_called_once()


def test_tick_sleeps_only_if_it_woke():
    _cleanup()
    _reset_keeper_clock()
    cid = _computer(state="asleep")
    _cred(cid, proof_spec={"url_contains": "/x"})
    slept = []

    with mock.patch.object(session_keeper, "do_wake"), \
         mock.patch.object(session_keeper, "do_sleep",
                           side_effect=lambda c: slept.append(c)), \
         mock.patch.object(session_keeper, "_probe_one_awake", return_value="ok"):
        session_keeper.tick()
    assert slept == [cid], slept

    slept.clear()
    store.set_state(cid, "running")
    with mock.patch.object(session_keeper, "do_wake") as wake, \
         mock.patch.object(session_keeper, "do_sleep",
                           side_effect=lambda c: slept.append(c)), \
         mock.patch.object(session_keeper, "_probe_one_awake", return_value="ok"):
        session_keeper.tick()
    wake.assert_not_called()
    assert slept == [], slept


def test_tick_batches_per_computer_one_wake():
    _cleanup()
    _reset_keeper_clock()
    cid = _computer(state="asleep")
    _cred(cid, name="a", proof_spec={"url_contains": "/a"})
    _cred(cid, name="b", proof_spec={"url_contains": "/b"})
    woke, slept, names = [], [], []

    with mock.patch.object(session_keeper, "do_wake", side_effect=lambda c: woke.append(c)), \
         mock.patch.object(session_keeper, "do_sleep", side_effect=lambda c: slept.append(c)), \
         mock.patch.object(session_keeper, "_probe_one_awake",
                           side_effect=lambda c, n: names.append(n) or "ok"):
        session_keeper.tick()

    assert woke == [cid], woke
    assert slept == [cid], slept
    assert sorted(names) == ["a", "b"], names


def test_typo_proof_spec_is_not_a_probe_profile():
    _cleanup()
    cid = _computer()
    _cred(cid, probe_url=None, proof_spec={"not_a_key": "/x"})
    row = store.get_credential(cid, "github")
    assert session_keeper._has_probe_profile(row) is False


def test_eval_proof_rejects_unrecognized_predicates():
    _cleanup()
    cid = _computer()
    from lifecycle import get_computer
    computer = get_computer(cid)
    assert session_keeper._eval_proof(
        computer, {"not_a_key": "/x"},
        observation={"href": "https://example.com/x"}) is False


def test_observe_failure_returns_none_on_sqlite_row():
    """observe_auth errors must not crash on sqlite3.Row (no .get)."""
    _cleanup()
    cid = _computer(state="running")
    from lifecycle import get_computer
    computer = get_computer(cid)
    assert not hasattr(computer, "get")
    with mock.patch("deskclient.observe_auth", side_effect=RuntimeError("boom")):
        out = session_keeper._observe(computer)
    assert out is None


def test_tick_respects_cadence_and_skips_recent_live_session():
    _cleanup()
    _reset_keeper_clock()
    cid = _computer(state="asleep")
    _cred(cid, proof_spec={"url_contains": "/a"})
    wakes = []

    with mock.patch.object(session_keeper, "do_wake", side_effect=lambda c: wakes.append(c)), \
         mock.patch.object(session_keeper, "do_sleep"), \
         mock.patch.object(session_keeper, "_probe_one_awake", return_value="ok"):
        session_keeper.tick()
        session_keeper.tick()  # within INTERVAL — must no-op
    assert wakes == [cid], wakes

    # Live + recently touched → busy even when due
    _reset_keeper_clock()
    store.set_state(cid, "running")
    store.q("UPDATE computers SET last_active_at=? WHERE id=?",
            ("2099-01-01T00:00:00Z", cid))  # "now" far future vs wall clock? use real now
    from util import now as _now
    store.q("UPDATE computers SET last_active_at=? WHERE id=?", (_now(), cid))
    wakes.clear()
    with mock.patch.object(session_keeper, "do_wake", side_effect=lambda c: wakes.append(c)), \
         mock.patch.object(session_keeper, "_probe_one_awake",
                           side_effect=lambda *a, **k: wakes.append("probed")):
        session_keeper.tick()
    assert wakes == [], wakes


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("PASS")
