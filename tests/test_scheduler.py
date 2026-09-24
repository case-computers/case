# SPDX-License-Identifier: MIT
"""compute_next is the only non-trivial branch in the scheduler — cover it.
Run: .venv/bin/python tests/test_scheduler.py   (no pytest/deps needed)"""
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "control-plane"))
# assignment, NOT setdefault: the run-status tests below truncate the runs table, and an
# inherited CASE_HOME (exported in a dev shell, or ~/.case/env) would point that at a
# live box's real history. Same reasoning as tests/test_links.py.
os.environ["CASE_HOME"] = "/tmp/case-sched-test"
from scheduler import compute_next  # noqa: E402
from store import store  # noqa: E402


def _dt(iso):
    return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def test_interval_adds_seconds():
    before = datetime.now(timezone.utc)
    nxt = _dt(compute_next("interval", "3600", 0))
    delta = (nxt - before).total_seconds()
    assert 3590 <= delta <= 3660, delta          # ~1h ahead


def test_daily_is_in_the_future():
    # whatever HH:MM, next fire must be later than now (today-if-ahead, else tomorrow)
    for spec in ("00:01", "12:00", "23:59"):
        nxt = _dt(compute_next("daily", spec, 0))
        assert nxt > datetime.now(timezone.utc), spec


def test_daily_within_24h_plus_jitter():
    nxt = _dt(compute_next("daily", "12:00", 0))
    assert (nxt - datetime.now(timezone.utc)).total_seconds() <= 24 * 3600 + 5


def test_daily_fires_at_requested_local_time():
    # the tz conversion (local HH:MM -> stored UTC) must round-trip to the right wall clock
    for spec in ("00:00", "06:30", "12:00", "18:45", "23:59"):
        local = _dt(compute_next("daily", spec, 0)).astimezone()   # back to system local tz
        assert f"{local.hour:02d}:{local.minute:02d}" == spec, (spec, local.isoformat())


def test_daily_kolkata_is_0330_utc():
    nxt = _dt(compute_next("daily", "09:00", 0, "Asia/Kolkata"))
    assert nxt.hour == 3 and nxt.minute == 30, nxt.isoformat()


def test_daily_box_local_survives_dst_switch():
    # Set the night before clocks go back (London, 2026-10-25 01:00 UTC). 09:00 local
    # tomorrow is 09:00Z, not the 08:00Z a frozen BST offset would give.
    import time
    old = os.environ.get("TZ")
    os.environ["TZ"] = "Europe/London"
    time.tzset()
    try:
        nxt = compute_next("daily", "09:00", 0, now=datetime(2026, 10, 24, 23, 0))
        assert nxt == "2026-10-25T09:00:00Z", nxt
    finally:
        if old is None:
            del os.environ["TZ"]
        else:
            os.environ["TZ"] = old
        time.tzset()


def test_daily_tz_survives_dst_switch():
    from zoneinfo import ZoneInfo
    now = datetime(2026, 10, 24, 23, 0, tzinfo=ZoneInfo("Europe/London"))
    nxt = compute_next("daily", "09:00", 0, "Europe/London", now=now)
    assert nxt == "2026-10-25T09:00:00Z", nxt


def test_daily_skips_spring_forward_gap():
    # America/New_York 2026-03-08: 02:00 → 03:00, so 02:30 never happens.
    # Fire the next day at 02:30 EDT (06:30Z), not 03:30 that morning.
    from zoneinfo import ZoneInfo
    now = datetime(2026, 3, 8, 0, 30, tzinfo=ZoneInfo("America/New_York"))
    nxt = compute_next("daily", "02:30", 0, "America/New_York", now=now)
    assert nxt == "2026-03-09T06:30:00Z", nxt


def test_daily_jitter_does_not_land_in_spring_forward_gap():
    # 01:45 + 45m = 02:30, which does not exist on 2026-03-08 in New York.
    # Skip to the next day, keep the same jitter: 2026-03-09 02:30 EDT = 06:30Z.
    from zoneinfo import ZoneInfo
    import unittest.mock as mock
    now = datetime(2026, 3, 8, 0, 30, tzinfo=ZoneInfo("America/New_York"))
    with mock.patch("scheduler.random.randint", return_value=2700):
        nxt = compute_next("daily", "01:45", 2700, "America/New_York", now=now)
    assert nxt == "2026-03-09T06:30:00Z", nxt


def test_daily_box_local_skips_spring_forward_gap():
    import time
    old = os.environ.get("TZ")
    os.environ["TZ"] = "America/New_York"
    time.tzset()
    try:
        nxt = compute_next("daily", "02:30", 0, now=datetime(2026, 3, 8, 0, 30))
        assert nxt == "2026-03-09T06:30:00Z", nxt
    finally:
        if old is None:
            del os.environ["TZ"]
        else:
            os.environ["TZ"] = old
        time.tzset()


def test_bad_tz_raises():
    from errors import ApiError
    try:
        compute_next("daily", "09:00", 0, "Not/AZone")
        assert False, "expected bad_tz"
    except ApiError as e:
        assert e.code == "bad_tz", e


def test_sqlite_row_has_no_get_but_tz_index_works():
    store.q("DELETE FROM schedules")
    store.insert_schedule("sch_tz", "c_1", "n", "p", "daily", "09:00", 0,
                          "2026-08-30T03:30:00Z", "Asia/Kolkata")
    s = store.get_schedule("sch_tz")
    assert not hasattr(s, "get"), type(s)
    tz = s["tz"] if "tz" in s.keys() else None
    assert tz == "Asia/Kolkata", tz
    nxt = compute_next(s["kind"], s["spec"], s["jitter_s"], tz)
    assert nxt[11:16] == "03:30", nxt


def test_schedules_tz_column_exists():
    cols = [r["name"] for r in store.db.execute("PRAGMA table_info(schedules)")]
    assert "tz" in cols


def test_jitter_stays_bounded():
    base = datetime.now(timezone.utc)
    for _ in range(20):
        nxt = _dt(compute_next("interval", "60", 600))
        delta = (nxt - base).total_seconds()
        # -1: compute_next stores whole seconds, so truncation can land up to
        # 0.999s before `base` when the jitter draw is 0.
        assert 60 - 1 <= delta <= 60 + 600 + 5, delta


def test_sub_minute_interval_is_refused():
    from errors import ApiError
    for spec in ("0", "1", "59"):
        try:
            compute_next("interval", spec, 600)
            assert False, spec
        except ApiError as e:
            assert e.status == 400 and "60 seconds" in e.message, (spec, e.message)


def test_bad_schedule_spec_raises_bad_request():
    from errors import ApiError
    from scheduler import create_schedule
    import unittest.mock as mock

    with mock.patch("scheduler.get_computer", return_value={"id": "c_1"}):
        for kind, spec in (("daily", "9am"), ("interval", "soon")):
            try:
                create_schedule("c_1", {"prompt": "x", "kind": kind, "spec": spec})
                assert False, (kind, spec)
            except ApiError as e:
                assert e.code == "bad_request", (kind, spec, e.code)
        try:
            create_schedule("c_1", {"prompt": "x", "spec": "3600", "jitter_s": "x"})
            assert False, "jitter"
        except ApiError as e:
            assert e.code == "bad_request", e.code


def test_report_entry_base64_is_shell_safe():
    # a summary full of shell metachars must not break or inject the append command
    import base64, re
    entry = "## run `rm -rf /`; echo \"$HOME\" 'x'\n& | ; > <\n"
    b64 = base64.b64encode(entry.encode()).decode()
    assert re.fullmatch(r"[A-Za-z0-9+/=]+", b64)          # nothing the shell can interpret
    assert base64.b64decode(b64).decode() == entry         # round-trips exactly


def test_brain_argv_default_is_claude_shaped():
    import scheduler
    old = scheduler.BRAIN_CMD
    try:
        scheduler.BRAIN_CMD = ""
        argv = scheduler.brain_argv("do the thing")
        assert argv[1:3] == ["-p", "do the thing"], argv
        assert "--mcp-config" in argv and "mcp__case__*" in argv, argv
    finally:
        scheduler.BRAIN_CMD = old


def test_brain_argv_template_keeps_prompt_one_token():
    import scheduler
    old = scheduler.BRAIN_CMD
    try:
        scheduler.BRAIN_CMD = "codex exec --mcp-config {mcp} {prompt}"
        argv = scheduler.brain_argv("two words")
        assert argv[:2] == ["codex", "exec"], argv
        assert argv[3] == scheduler.MCP_CONFIG, argv
        assert argv[-1] == "two words", argv       # spaces never split the prompt
    finally:
        scheduler.BRAIN_CMD = old


def test_brain_argv_prompt_mentioning_mcp_is_not_reexpanded():
    import scheduler
    old = scheduler.BRAIN_CMD
    try:
        scheduler.BRAIN_CMD = "codex exec --mcp-config {mcp} {prompt}"
        argv = scheduler.brain_argv("does {mcp} load")   # literal {mcp} in the prompt
        assert argv[-1] == "does {mcp} load", argv       # stays inert, not the config path
    finally:
        scheduler.BRAIN_CMD = old


def test_brain_argv_template_without_prompt_raises():
    import scheduler
    old = scheduler.BRAIN_CMD
    try:
        scheduler.BRAIN_CMD = "codex exec --mcp-config {mcp}"
        try:
            scheduler.brain_argv("x")
            assert False, "expected ValueError for missing {prompt}"
        except ValueError:
            pass
    finally:
        scheduler.BRAIN_CMD = old


def test_run_brain_malformed_template_is_clean_127():
    import scheduler
    old = scheduler.BRAIN_CMD
    try:
        for bad in ("   ", 'codex exec "oops', "codex exec {mcp}"):
            scheduler.BRAIN_CMD = bad
            code, msg = scheduler.run_brain("c_x", "do it")
            assert code == 127, (bad, code, msg)     # never an uncaught IndexError/ValueError
    finally:
        scheduler.BRAIN_CMD = old


def test_stock_brain_resolves_case_mcp_json_from_anywhere():
    # case-mcp.json runs `python3 mcp/case_mcp.py`, relative, with whatever python3 is
    # on PATH: from any other cwd, or with a system python lacking the deps, the
    # brain came up without its tools.
    import tempfile
    import scheduler
    fake = os.path.join(tempfile.mkdtemp(), "claude")
    with open(fake, "w") as f:
        f.write("#!/bin/sh\npwd\ncommand -v python3\n")
    os.chmod(fake, 0o755)
    old = (scheduler.BRAIN_BIN, scheduler.BRAIN_CMD, scheduler.BRAIN_URL)
    here = os.getcwd()
    try:
        scheduler.BRAIN_BIN, scheduler.BRAIN_CMD, scheduler.BRAIN_URL = fake, "", ""
        os.chdir("/")
        code, out = scheduler.run_brain("c_1", "hi")
    finally:
        os.chdir(here)
        scheduler.BRAIN_BIN, scheduler.BRAIN_CMD, scheduler.BRAIN_URL = old
    cwd, py = out.splitlines()
    assert code == 0, out
    assert os.path.exists(os.path.join(cwd, "mcp", "case_mcp.py")), cwd
    assert os.path.dirname(py) == os.path.dirname(sys.executable), py


def test_busy_box_is_a_skip_not_a_raw_apierror():
    # a box at CASE_MAX_RUNNING=1: a busy box must report a plain-English skip
    # (never "ApiError: …") and must never sleep someone else's live session.
    import scheduler
    from errors import ApiError
    rec = {}

    class _Store:
        def get_schedule(self, sid, enabled_only=False):
            return {"id": sid, "computer_id": "c_1", "name": "nightly", "prompt": "go",
                    "kind": "interval", "spec": "3600", "jitter_s": 0}
        def set_schedule_next(self, *a): pass
        def insert_run(self, rid, sid, cid, started, ended, code, summary, artifact, status):
            rec["summary"] = summary
        def set_schedule_result(self, sid, at, status):
            rec["status"] = status

    def _busy(cid):
        raise ApiError(409, "too_many_running", "max 1 running computers")

    old = (scheduler.store, scheduler.do_wake, scheduler.do_sleep, scheduler.get_computer,
           scheduler.notifier, scheduler.emit)
    try:
        scheduler.store = _Store()
        scheduler.get_computer = lambda cid: {"id": cid, "state": "asleep"}
        scheduler.do_wake = _busy
        scheduler.do_sleep = lambda cid: None
        scheduler.notifier = type("N", (), {"push": lambda self, m: None})()
        scheduler.emit = lambda *a, **k: None
        scheduler.run_schedule("sch_x")
    finally:
        (scheduler.store, scheduler.do_wake, scheduler.do_sleep, scheduler.get_computer,
         scheduler.notifier, scheduler.emit) = old
    assert rec["status"] == "skipped", rec
    assert "another computer is running" in rec["summary"], rec
    assert "ApiError" not in rec["summary"], rec


def test_run_row_persists_its_status():
    store.q("DELETE FROM runs")
    store.insert_run("run_a", "sch_1", "c_1", "2026-07-27T09:00:00Z",
                     "2026-07-27T09:05:00Z", 0, "did the thing", None, "ok")
    store.insert_run("run_b", "sch_1", "c_1", "2026-07-27T10:00:00Z",
                     "2026-07-27T10:00:01Z", -1, "another computer is running", None, "skipped")
    rows = {r["id"]: r["status"] for r in store.list_runs("sch_1")}
    # exit_code alone cannot tell these apart: a timeout is also -1
    assert rows == {"run_a": "ok", "run_b": "skipped"}, rows


def test_list_all_runs_spans_schedules_newest_first():
    store.q("DELETE FROM runs")
    store.insert_run("run_old", "sch_1", "c_1", "2026-07-27T08:00:00Z",
                     "2026-07-27T08:01:00Z", 0, "", None, "ok")
    store.insert_run("run_new", "sch_2", "c_2", "2026-07-27T11:00:00Z",
                     "2026-07-27T11:01:00Z", 0, "", None, "ok")
    assert [r["id"] for r in store.list_all_runs()] == ["run_new", "run_old"]


def test_run_schedule_does_not_sleep_borrowed_running_computer():
    # A schedule that finds the box already running must not put it to sleep afterwards —
    # that session may be a human Assist / agent desk session.
    import scheduler
    slept, rec = [], {}

    class _Store:
        def get_schedule(self, sid, enabled_only=False):
            return {"id": sid, "computer_id": "c_live", "name": "nightly", "prompt": "go",
                    "kind": "interval", "spec": "3600", "jitter_s": 0}
        def set_schedule_next(self, *a): pass
        def insert_run(self, rid, sid, cid, started, ended, code, summary, artifact, status):
            rec["status"] = status
        def set_schedule_result(self, sid, at, status): pass
        def active_attempt_exists(self, cid):
            return False

    old = (scheduler.store, scheduler.do_wake, scheduler.do_sleep, scheduler.get_computer,
           scheduler.run_brain, scheduler.capture_run_artifacts, scheduler.notifier,
           scheduler.emit)
    try:
        scheduler.store = _Store()
        scheduler.get_computer = lambda cid: {"id": cid, "state": "running"}
        scheduler.do_wake = lambda cid: None
        scheduler.do_sleep = lambda cid: slept.append(cid)
        scheduler.run_brain = lambda cid, p, name="": (0, "done")
        scheduler.capture_run_artifacts = lambda *a, **k: None
        scheduler.notifier = type("N", (), {"push": lambda self, m: None})()
        scheduler.emit = lambda *a, **k: None
        scheduler.run_schedule("sch_borrow")
    finally:
        (scheduler.store, scheduler.do_wake, scheduler.do_sleep, scheduler.get_computer,
         scheduler.run_brain, scheduler.capture_run_artifacts, scheduler.notifier,
         scheduler.emit) = old
    assert rec.get("status") == "ok", rec
    assert slept == [], slept


def test_run_schedule_sleeps_only_when_it_woke_and_no_auth():
    import scheduler
    slept, rec = [], {}

    class _Store:
        def __init__(self):
            self.auth = False
        def get_schedule(self, sid, enabled_only=False):
            return {"id": sid, "computer_id": "c_asleep", "name": "nightly", "prompt": "go",
                    "kind": "interval", "spec": "3600", "jitter_s": 0}
        def set_schedule_next(self, *a): pass
        def insert_run(self, rid, sid, cid, started, ended, code, summary, artifact, status):
            rec["status"] = status
        def set_schedule_result(self, sid, at, status): pass
        def active_attempt_exists(self, cid):
            return self.auth

    st = _Store()
    old = (scheduler.store, scheduler.do_wake, scheduler.do_sleep, scheduler.get_computer,
           scheduler.run_brain, scheduler.capture_run_artifacts, scheduler.notifier,
           scheduler.emit)
    try:
        scheduler.store = st
        scheduler.get_computer = lambda cid: {"id": cid, "state": "asleep"}
        scheduler.do_wake = lambda cid: None
        scheduler.do_sleep = lambda cid: slept.append(cid)
        scheduler.run_brain = lambda cid, p, name="": (0, "done")
        scheduler.capture_run_artifacts = lambda *a, **k: None
        scheduler.notifier = type("N", (), {"push": lambda self, m: None})()
        scheduler.emit = lambda *a, **k: None
        scheduler.run_schedule("sch_own")
        assert slept == ["c_asleep"], slept
        slept.clear()
        st.auth = True
        scheduler.run_schedule("sch_own_auth")
        assert slept == [], slept   # active AuthAttempt → leave awake
    finally:
        (scheduler.store, scheduler.do_wake, scheduler.do_sleep, scheduler.get_computer,
         scheduler.run_brain, scheduler.capture_run_artifacts, scheduler.notifier,
         scheduler.emit) = old
    assert rec.get("status") == "ok", rec


def test_a_run_does_not_sleep_the_box_under_another_schedule():
    # A woke the box, B started on it while A ran; A's cleanup used to sleep it
    # under B. The last run out sleeps it instead.
    import threading
    import time
    import unittest.mock as mock
    import scheduler
    store.q("DELETE FROM schedules")
    store.q("DELETE FROM runs")
    state, seen = {"c_two": "asleep"}, []
    for sid in ("sch_A", "sch_B"):
        store.insert_schedule(sid, "c_two", sid, "p", "interval", "3600", 0,
                              "2020-01-01T00:00:00Z")
    b_started, a_done = threading.Event(), threading.Event()

    def brain(cid, prompt, name=""):
        if name == "sch_A":
            b_started.wait(2)
        else:
            b_started.set()
            a_done.wait(2)
            seen.append(state[cid])
        return 0, "ok"

    def run_a():
        scheduler.run_schedule("sch_A")
        a_done.set()

    with mock.patch.object(scheduler, "get_computer", lambda cid: {"id": cid, "state": state[cid]}), \
         mock.patch.object(scheduler, "do_wake", lambda cid: state.update({cid: "running"})), \
         mock.patch.object(scheduler, "do_sleep", lambda cid: state.update({cid: "asleep"})), \
         mock.patch.object(scheduler, "run_brain", brain), \
         mock.patch.object(scheduler, "capture_run_artifacts", lambda *a, **k: None), \
         mock.patch.object(scheduler.notifier, "push"), \
         mock.patch.object(scheduler, "emit"):
        ta = threading.Thread(target=run_a)
        ta.start()
        while state["c_two"] != "running":     # B borrows a box A already woke
            time.sleep(0.01)
        tb = threading.Thread(target=scheduler.run_schedule, args=("sch_B",))
        tb.start()
        ta.join(5)
        tb.join(5)
    assert seen == ["running"], seen             # B still had its box after A finished
    assert state["c_two"] == "asleep"            # and the last one out put it back
    assert scheduler._HOLDERS == {}


def test_ram_tight_box_is_a_skip_too():
    import scheduler
    from errors import ApiError
    rec = {}

    class _Store:
        def get_schedule(self, sid, enabled_only=False):
            return {"id": sid, "computer_id": "c_1", "name": "nightly", "prompt": "go",
                    "kind": "interval", "spec": "3600", "jitter_s": 0}
        def set_schedule_next(self, *a): pass
        def insert_run(self, rid, sid, cid, started, ended, code, summary, artifact, status):
            rec["summary"] = summary
        def set_schedule_result(self, sid, at, status):
            rec["status"] = status

    def _tight(cid):
        raise ApiError(409, "not_enough_ram", "3072 MB in use of 4096")

    old = (scheduler.store, scheduler.do_wake, scheduler.do_sleep, scheduler.get_computer,
           scheduler.notifier, scheduler.emit)
    try:
        scheduler.store = _Store()
        scheduler.get_computer = lambda cid: {"id": cid, "state": "asleep"}
        scheduler.do_wake = _tight
        scheduler.do_sleep = lambda cid: None
        scheduler.notifier = type("N", (), {"push": lambda self, m: None})()
        scheduler.emit = lambda *a, **k: None
        scheduler.run_schedule("sch_x")
    finally:
        (scheduler.store, scheduler.do_wake, scheduler.do_sleep, scheduler.get_computer,
         scheduler.notifier, scheduler.emit) = old
    assert rec["status"] == "skipped", rec
    assert "not enough free RAM" in rec["summary"], rec
    assert "ApiError" not in rec["summary"], rec


def _run_real_row(sid):
    """run_schedule against the real store with the computer side faked out."""
    import unittest.mock as mock
    import scheduler
    with mock.patch.object(scheduler, "get_computer", lambda cid: {"id": cid, "state": "running"}), \
         mock.patch.object(scheduler, "do_wake") as wake, \
         mock.patch.object(scheduler, "do_sleep"), \
         mock.patch.object(scheduler, "run_brain", lambda cid, p, name="": (0, "done")), \
         mock.patch.object(scheduler, "capture_run_artifacts", lambda *a, **k: None), \
         mock.patch.object(scheduler.notifier, "push"), \
         mock.patch.object(scheduler, "emit"):
        scheduler.run_schedule(sid)
    return wake


def test_legacy_sub_minute_interval_runs_at_the_floor():
    # Rows stored before the 60s floor used to raise out of compute_next on every
    # sweep, before the reschedule, so they re-fired forever and never recorded a run.
    store.q("DELETE FROM schedules")
    store.q("DELETE FROM runs")
    store.insert_schedule("sch_legacy", "c_1", "legacy", "p", "interval", "30", 0,
                          "2020-01-01T00:00:00Z")
    _run_real_row("sch_legacy")
    nxt = _dt(store.get_schedule("sch_legacy")["next_run_at"])
    assert 55 <= (nxt - datetime.now(timezone.utc)).total_seconds() <= 65, nxt
    assert [r["status"] for r in store.list_runs("sch_legacy")] == ["ok"]


def test_unresolvable_tz_fails_the_run_and_backs_off_a_day():
    store.q("DELETE FROM schedules")
    store.q("DELETE FROM runs")
    store.insert_schedule("sch_badtz", "c_1", "tz", "p", "daily", "07:00", 0,
                          "2020-01-01T00:00:00Z", tz="Mars/Olympus")
    wake = _run_real_row("sch_badtz")
    wake.assert_not_called()
    s = store.get_schedule("sch_badtz")
    assert s["enabled"] == 1
    left = (_dt(s["next_run_at"]) - datetime.now(timezone.utc)).total_seconds()
    assert 86000 <= left <= 86400, left                # not due again next sweep
    runs = store.list_runs("sch_badtz")
    assert [r["status"] for r in runs] == ["fail"], runs
    assert "Mars/Olympus" in runs[0]["summary"], runs[0]["summary"]
    assert s["last_status"] == "fail"


def test_run_brain_url_finished():
    import unittest.mock as mock
    import scheduler
    old_cmd, old_url = scheduler.BRAIN_CMD, scheduler.BRAIN_URL
    try:
        scheduler.BRAIN_CMD = ""
        scheduler.BRAIN_URL = "http://ui:4174/api/brain"
        resp = mock.Mock(status_code=200, content=b'{"ok":true}', text="ok")
        resp.json.return_value = {"ok": True, "finished": True, "text": "done"}
        with mock.patch("scheduler.requests.post", return_value=resp) as post:
            code, text = scheduler.run_brain("c_1", "hello", name="nightly")
        assert (code, text) == (0, "done")
        assert post.call_args.args[0] == "http://ui:4174/api/brain"
        assert post.call_args.kwargs["json"] == {"computer_id": "c_1", "prompt": "hello",
                                                 "name": "nightly"}
        # same clip as the argv path, so runs.summary and the logbook stay bounded
        resp.json.return_value = {"ok": True, "finished": True, "text": "x" * 5000 + "END"}
        with mock.patch("scheduler.requests.post", return_value=resp):
            _, text = scheduler.run_brain("c_1", "hello")
        assert len(text) == 800 and text.endswith("END"), len(text)
    finally:
        scheduler.BRAIN_CMD, scheduler.BRAIN_URL = old_cmd, old_url


def test_run_brain_url_unfinished():
    import unittest.mock as mock
    import scheduler
    old_cmd, old_url = scheduler.BRAIN_CMD, scheduler.BRAIN_URL
    try:
        scheduler.BRAIN_CMD = ""
        scheduler.BRAIN_URL = "http://ui:4174/api/brain"
        resp = mock.Mock(status_code=200, content=b'{"ok":true}', text="")
        resp.json.return_value = {"ok": True, "finished": False, "text": "stopped mid-task"}
        with mock.patch("scheduler.requests.post", return_value=resp):
            code, text = scheduler.run_brain("c_1", "hello")
        assert code == 3 and text == "stopped mid-task"
    finally:
        scheduler.BRAIN_CMD, scheduler.BRAIN_URL = old_cmd, old_url


def test_run_brain_url_503():
    import unittest.mock as mock
    import scheduler
    old_cmd, old_url = scheduler.BRAIN_CMD, scheduler.BRAIN_URL
    try:
        scheduler.BRAIN_CMD = ""
        scheduler.BRAIN_URL = "http://ui:4174/api/brain"
        resp = mock.Mock(status_code=503, content=b'{"error":"no key"}', text="")
        resp.json.return_value = {"error": "set CASE_DRIVE_API_KEY in .env"}
        with mock.patch("scheduler.requests.post", return_value=resp):
            code, text = scheduler.run_brain("c_1", "hello")
        assert code == 2
        assert "CASE_DRIVE_API_KEY" in text
    finally:
        scheduler.BRAIN_CMD, scheduler.BRAIN_URL = old_cmd, old_url


def test_run_brain_url_connection_error():
    import unittest.mock as mock
    import scheduler
    old_cmd, old_url = scheduler.BRAIN_CMD, scheduler.BRAIN_URL
    try:
        scheduler.BRAIN_CMD = ""
        scheduler.BRAIN_URL = "http://ui:4174/api/brain"
        # ConnectTimeout subclasses both ConnectionError and Timeout: it is "unreachable"
        for exc in (scheduler.requests.ConnectionError(), scheduler.requests.ConnectTimeout()):
            with mock.patch("scheduler.requests.post", side_effect=exc):
                code, text = scheduler.run_brain("c_1", "hello")
            assert code == 127, (exc, code)
            assert "http://ui:4174/api/brain" in text and "CASE_BRAIN_CMD" in text, text
    finally:
        scheduler.BRAIN_CMD, scheduler.BRAIN_URL = old_cmd, old_url


def test_run_brain_url_sends_bearer_only_when_token_set():
    import unittest.mock as mock
    import scheduler
    old_cmd, old_url = scheduler.BRAIN_CMD, scheduler.BRAIN_URL
    old_tok = os.environ.get("CASE_TOKEN")
    try:
        scheduler.BRAIN_CMD = ""
        scheduler.BRAIN_URL = "http://ui:4174/api/brain"
        resp = mock.Mock(status_code=200, content=b"{}", text="")
        resp.json.return_value = {"ok": True, "finished": True, "text": ""}
        os.environ["CASE_TOKEN"] = "tok"
        with mock.patch("scheduler.requests.post", return_value=resp) as post:
            scheduler.run_brain("c_1", "hello")
        assert post.call_args.kwargs["headers"] == {"Authorization": "Bearer tok"}
        os.environ["CASE_TOKEN"] = ""
        with mock.patch("scheduler.requests.post", return_value=resp) as post:
            scheduler.run_brain("c_1", "hello")
        assert post.call_args.kwargs["headers"] == {}
        resp401 = mock.Mock(status_code=401, content=b'{"error":"unauthorized"}', text="")
        resp401.json.return_value = {"error": "unauthorized"}
        with mock.patch("scheduler.requests.post", return_value=resp401):
            code, text = scheduler.run_brain("c_1", "hello")
        assert code == 1 and "CASE_TOKEN" in text, (code, text)
    finally:
        scheduler.BRAIN_CMD, scheduler.BRAIN_URL = old_cmd, old_url
        if old_tok is None:
            os.environ.pop("CASE_TOKEN", None)
        else:
            os.environ["CASE_TOKEN"] = old_tok


def test_run_brain_url_timeout():
    import unittest.mock as mock
    import scheduler
    old_cmd, old_url = scheduler.BRAIN_CMD, scheduler.BRAIN_URL
    try:
        scheduler.BRAIN_CMD = ""
        scheduler.BRAIN_URL = "http://ui:4174/api/brain"
        with mock.patch("scheduler.requests.post", side_effect=scheduler.requests.Timeout()):
            code, text = scheduler.run_brain("c_1", "hello")
        assert code == -1
        assert "timed out" in text
    finally:
        scheduler.BRAIN_CMD, scheduler.BRAIN_URL = old_cmd, old_url


def test_run_brain_cmd_wins_over_url():
    import unittest.mock as mock
    import scheduler
    old_cmd, old_url = scheduler.BRAIN_CMD, scheduler.BRAIN_URL
    try:
        scheduler.BRAIN_CMD = "definitely-not-a-brain-bin {prompt}"
        scheduler.BRAIN_URL = "http://ui:4174/api/brain"
        with mock.patch("scheduler.requests.post") as post:
            code, _ = scheduler.run_brain("c_1", "hello")
        assert post.call_count == 0
        assert code == 127
    finally:
        scheduler.BRAIN_CMD, scheduler.BRAIN_URL = old_cmd, old_url


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("PASS")
