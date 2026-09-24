# SPDX-License-Identifier: MIT
"""Usage stats: opt-out is real, the payload is anonymous, the heartbeat is daily.
Run: .venv/bin/python tests/test_telemetry.py"""
import importlib
import json
import os
import shutil
import sys
import unittest.mock as mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "control-plane"))
HOME = "/tmp/case-telemetry-test"
shutil.rmtree(HOME, ignore_errors=True)
os.environ["CASE_HOME"] = HOME
os.environ.pop("CASE_TELEMETRY", None)
os.environ.pop("DO_NOT_TRACK", None)

import telemetry  # noqa: E402
from store import store  # noqa: E402

# Everything a payload is allowed to carry. A new key here is a deliberate act:
# usage stats leave the box, so anything not on this list is a leak.
ALLOWED = {
    "self_host", "install_age_days",
    "computers", "credentials", "schedules", "schedules_enabled",
    "runs_7d", "runs_ok_7d",                            # install_ping / heartbeat
    "status", "kind", "duration_s", "had_artifact",     # run_completed
}
# The only string-valued properties, and the only values they may take.
ENUMS = {"status": {"ok", "fail", "skipped"}, "kind": {"interval", "daily"}}


def _on():
    """Stats are off inside a test run by design; these tests opt back in."""
    os.environ["CASE_TELEMETRY"] = "1"
    t = importlib.reload(telemetry)
    assert t.ENABLED
    return t


def test_off_by_default_inside_a_test_run():
    os.environ.pop("CASE_TELEMETRY", None)
    t = importlib.reload(telemetry)
    assert not t.ENABLED, "the suite would ingest into the live PostHog project"


def test_opt_out_sends_nothing():
    for var in ("CASE_TELEMETRY", "DO_NOT_TRACK"):
        os.environ["CASE_TELEMETRY"] = "1"          # the override the suite uses
        os.environ[var] = "0" if var == "CASE_TELEMETRY" else "1"
        t = importlib.reload(telemetry)
        try:
            assert not t.ENABLED, f"{var} did not disable usage stats"
            with mock.patch.object(t.requests, "post") as post:
                t.capture("run_completed", {"status": "ok"})
                t.install_ping()
                t.heartbeat_if_due()
            assert not post.called, f"{var} set but a request went out"
        finally:
            os.environ.pop(var, None)
            os.environ.pop("CASE_TELEMETRY", None)


def test_payload_is_anonymous_and_the_id_is_stable():
    t = _on()
    with mock.patch.object(t.requests, "post") as post:
        t._send("run_completed", {"status": "ok", "duration_s": 12,
                                  "had_artifact": True, "kind": "daily"})
        t._send("install_ping", t.counts())
        bodies = [c.kwargs["json"] for c in post.call_args_list]
    assert len(bodies) == 2
    ids = {b["distinct_id"] for b in bodies}
    assert len(ids) == 1 and len(ids.pop()) == 32, "install id must be one stable hex id"
    for b in bodies:
        assert set(b["properties"]) <= ALLOWED, \
            f"unlisted property leaving the box: {set(b['properties']) - ALLOWED}"
        for k, v in b["properties"].items():
            if isinstance(v, str):
                assert v in ENUMS.get(k, ()), f"free-form string in payload: {k}={v!r}"
            else:
                assert isinstance(v, (bool, int)), f"{k}={v!r} is not a number"
        assert b["api_key"] == t.KEY and b["event"]
    # the id lives in a plain file the operator can read or delete
    with open(t.PATH) as f:
        assert len(json.load(f)["install_id"]) == 32


def test_heartbeat_is_once_a_day():
    t = _on()
    with mock.patch.object(t, "capture") as cap:
        t.heartbeat_if_due()
        t.heartbeat_if_due()
        assert cap.call_count == 1, "heartbeat fired twice in one day"
        s = t._load()
        s["last_heartbeat_day"] = "2000-01-01"
        t._save(s)
        t.heartbeat_if_due()
        assert cap.call_count == 2, "heartbeat did not fire on a new day"


def test_counts_are_counts():
    c = store.telemetry_counts("2000-01-01T00:00:00Z")
    assert set(c) == {"computers", "credentials", "schedules", "schedules_enabled",
                      "runs_7d", "runs_ok_7d"}
    assert all(type(v) is int for v in c.values()), c


def test_deleted_computers_are_not_counted():
    # destroy() keeps the row as a 'deleted' tombstone
    for cid, state in (("c_tele_live", "asleep"), ("c_tele_gone", "deleted")):
        store.delete_computer(cid)
        store.insert_computer(cid, cid, "img", 1, 512, "vol", "tok")
        store.set_state(cid, state)
    try:
        assert store.telemetry_counts("2000-01-01T00:00:00Z")["computers"] == 1
    finally:
        store.delete_computer("c_tele_live")
        store.delete_computer("c_tele_gone")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("PASS")
