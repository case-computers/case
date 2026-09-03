# SPDX-License-Identifier: AGPL-3.0-only
"""Scheduler + runner: recurring tasks.

A schedule = "at time T, wake computer X, run this task in headless Claude Code,
report, sleep." The sweeper ticks every 20s and calls fire_due_schedules(). One run:
reschedule-first (so a hung run never wedges the slot) → wake → brain → capture
artifacts (screenshot to host + logbook entry on the machine) → sleep → record + ntfy.
Recurrence is deliberately minimal — interval seconds, or daily 'HH:MM' local;
add croniter only if someone actually needs '*/15 * * * 1-5'.
"""
import base64
import os
import random
import shlex
import shutil
import subprocess
import threading
from datetime import datetime, timedelta, timezone

from config import (BRAIN_BIN, BRAIN_CMD, BRAIN_TIMEOUT, MAX_RUNNING, MCP_CONFIG,
                    RUNS_DIR, log)
from deskclient import desk_json, screenshot_bytes
from errors import ApiError
from events import emit
from lifecycle import do_sleep, do_wake, get_computer
from notify import notifier
from store import store
from util import new_id, now

SCHED_RUNNING = set()   # in-memory guard, fine while one cased process runs
_LOCK = threading.Lock()   # guards the check-then-add on SCHED_RUNNING (sweeper vs run-now)


def compute_next(kind, spec, jitter_s):
    """Next fire time as UTC ISO. Lexicographic order == chronological (zero-padded, Z)."""
    j = random.randint(0, int(jitter_s or 0))
    if kind == "interval":
        if int(spec) < 60:
            raise ApiError(400, "bad_request", "interval must be at least 60 seconds")
        nxt = datetime.now(timezone.utc) + timedelta(seconds=int(spec) + j)
    elif kind == "daily":
        local = datetime.now()
        hh, mm = (int(x) for x in str(spec).split(":"))
        t = local.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if t <= local:
            t += timedelta(days=1)
        nxt = (t + timedelta(seconds=j)).astimezone(timezone.utc)   # naive→aware picks that date's offset
    else:
        raise ApiError(400, "bad_kind", "kind must be 'interval' or 'daily'")
    return nxt.strftime("%Y-%m-%dT%H:%M:%SZ")


def brain_argv(full_prompt):
    """argv for the schedule brain. CASE_BRAIN_CMD template wins; else stock claude.
    Per-token substitution, {mcp} before {prompt}, so the inserted prompt is never
    re-scanned for placeholders. Raises ValueError on a malformed/incomplete template
    (unbalanced quotes, or no {prompt}) so run_brain can return a clean 127.
    NOTE: the template path carries NO --allowedTools clamp, an arbitrary harness runs
    with the box's full host privileges. Only give schedules a harness you trust."""
    if BRAIN_CMD:
        if "{prompt}" not in BRAIN_CMD:
            raise ValueError("CASE_BRAIN_CMD must contain {prompt}")
        return [t.replace("{mcp}", MCP_CONFIG).replace("{prompt}", full_prompt)
                for t in shlex.split(BRAIN_CMD)]      # shlex.split may raise ValueError
    binp = BRAIN_BIN or shutil.which("claude") or "claude"
    return [binp, "-p", full_prompt, "--mcp-config", MCP_CONFIG,
            "--allowedTools", "mcp__case__*"]


def run_brain(cid, prompt):
    """Invoke the headless brain against this computer via Case MCP. Returns (code, summary)."""
    try:
        argv = brain_argv(f"On Case computer {cid}: {prompt}")
    except ValueError as e:
        return 127, f"bad CASE_BRAIN_CMD: {e}"
    binp = argv[0] if os.path.sep in argv[0] else shutil.which(argv[0])
    if not binp or not os.path.exists(binp):
        return 127, f"brain binary not found ({argv[0]}) — set CASE_BRAIN_BIN or CASE_BRAIN_CMD"
    argv[0] = binp
    # only when argv actually uses it, a `--mcp-config={mcp}` template embeds it in a token
    if any(MCP_CONFIG in t for t in argv) and not os.path.exists(MCP_CONFIG):
        return 127, f"mcp config not found at {MCP_CONFIG} — set CASE_MCP_CONFIG"
    # BYOK: stock path forces the caller's logged-in/subscription auth by blanking the
    # key, UNLESS the operator explicitly set one (their key = their cost, still BYOK).
    # Template path owns its env untouched.
    env = os.environ
    if not BRAIN_CMD and not os.environ.get("ANTHROPIC_API_KEY"):
        env = {**os.environ, "ANTHROPIC_API_KEY": ""}
    try:
        p = subprocess.run(argv, capture_output=True, text=True,
                           timeout=BRAIN_TIMEOUT, env=env)
        return p.returncode, (p.stdout or p.stderr or "").strip()[-800:]
    except subprocess.TimeoutExpired:
        return -1, f"brain run timed out ({BRAIN_TIMEOUT}s)"
    except OSError as e:                              # non-exec file, dir, ENOENT race
        return 127, f"brain exec failed ({argv[0]}): {e}"


def capture_run_artifacts(cid, rid, name, status, summary, started):
    """Evidence per run: final screenshot → host, markdown entry → the machine's own
    daily logbook (lives in the volume, so the report accumulates with tenure). Best-effort:
    a capture failure must never fail the run."""
    row = get_computer(cid)
    artifact = None
    shot = screenshot_bytes(row)
    if shot:
        try:
            os.makedirs(RUNS_DIR, exist_ok=True)
            artifact = os.path.join(RUNS_DIR, f"{rid}.png")
            with open(artifact, "wb") as f:
                f.write(shot)
        except Exception:
            log.warning("run %s screenshot write failed", rid)
            artifact = None
    try:
        entry = (f"\n## {started} — {name} [{status}]\n\n{(summary or '').strip() or '(no output)'}\n"
                 + (f"\n_screenshot: {rid}.png (host {RUNS_DIR})_\n" if artifact else ""))
        # base64 so a summary with quotes/newlines/backticks can't break or inject the shell
        b64 = base64.b64encode(entry.encode()).decode()
        cmd = (f"mkdir -p /home/agent/reports && echo {b64} | base64 -d "
               f">> /home/agent/reports/{started[:10]}.md")
        desk_json(row, "POST", "/exec", json={"command": cmd}, timeout=20)
    except Exception:
        log.warning("run %s report append failed", rid)
    return artifact


def run_schedule(sid):
    with _LOCK:                                  # atomic check-then-claim (sweeper vs run-now)
        if sid in SCHED_RUNNING:
            return                               # previous run still going; skip this tick
        SCHED_RUNNING.add(sid)
    try:
        s = store.get_schedule(sid, enabled_only=True)
        if not s:
            return
        # Reschedule FIRST so a hung/crashed run never wedges the slot.
        store.set_schedule_next(sid, compute_next(s["kind"], s["spec"], s["jitter_s"]))
        cid, rid, started = s["computer_id"], new_id("run"), now()
        code, summary, status, artifact = -1, "", "fail", None
        # Only the run that woke an asleep box may put it back, never borrow a live session
        # (and never sleep under an active AuthAttempt; do_sleep also 409s as a belt).
        woke_for_run = False
        try:
            was_asleep = get_computer(cid)["state"] == "asleep"
            do_wake(cid)
            woke_for_run = was_asleep
            code, summary = run_brain(cid, s["prompt"])
            status = "ok" if code == 0 else "fail"
            artifact = capture_run_artifacts(cid, rid, s["name"], status, summary, started)  # awake
        except ApiError as e:
            # a box at CASE_MAX_RUNNING is "not now", not a failure, and a schedule
            # must never sleep someone else's live session to make room.
            if e.code == "too_many_running":
                status = "skipped"
                summary = f"another computer is running (max {MAX_RUNNING} on this box)"
            else:
                summary = f"{e.code}: {e.message}"
                log.exception("schedule %s run failed", sid)
        except Exception as e:
            summary = f"{type(e).__name__}: {e}"
            log.exception("schedule %s run failed", sid)
        finally:
            try:
                if woke_for_run and not store.active_attempt_exists(cid):
                    do_sleep(cid)
            except Exception:
                log.exception("sleep after schedule %s", sid)
            store.insert_run(rid, sid, cid, started, now(), code, summary, artifact, status)
            store.set_schedule_result(sid, started, status)
            shot = " 📸" if artifact else ""
            notifier.push(f"[{s['name']}] {status}: {summary[:200]}{shot}")
            emit("schedule.run", {"schedule": sid, "run": rid, "computer_id": cid, "status": status})
    finally:
        SCHED_RUNNING.discard(sid)


def fire_due_schedules(spawn):
    """Called by the sweeper. `spawn(fn, arg)` runs run_schedule off-thread."""
    for s in store.due_schedules(now()):
        spawn(run_schedule, s["id"])


def schedule_json(row):
    return {k: row[k] for k in ("id", "computer_id", "name", "prompt", "kind", "spec",
                                "jitter_s", "enabled", "next_run_at", "last_run_at",
                                "last_status", "created_at")}


def create_schedule(cid, body):
    get_computer(cid)                            # 404 if unknown
    if "prompt" not in body or "spec" not in body:
        raise ApiError(400, "bad_request", "prompt and spec are required")
    kind = body.get("kind", "daily")
    try:
        jitter = int(body.get("jitter_s", 300))
        nxt = compute_next(kind, body["spec"], jitter)   # also validates kind/spec
    except (TypeError, ValueError):
        raise ApiError(400, "bad_request",
                       "spec must be seconds (interval) or HH:MM (daily); "
                       "jitter_s must be an integer")
    sid = new_id("sch")
    store.insert_schedule(sid, cid, str(body.get("name") or sid), body["prompt"],
                          kind, str(body["spec"]), jitter, nxt)
    return schedule_json(store.get_schedule(sid))


def list_schedules(cid):
    get_computer(cid)
    return [schedule_json(r) for r in store.list_schedules(cid)]


def delete_schedule(sid):
    if store.delete_schedule(sid) == 0:
        raise ApiError(404, "not_found", f"no schedule {sid}")


def list_runs(sid):
    return [dict(r) for r in store.list_runs(sid)]
