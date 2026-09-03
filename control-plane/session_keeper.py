# SPDX-License-Identifier: AGPL-3.0-only
"""Session keeper, preflight persistent session health.

Cadenced probe (default 6h, env `CASE_SESSION_KEEPER_INTERVAL_S`): for each credential
with a `probe_url` and/or `proof_spec`, wake if needed, load the probe URL, observe/prove,
update `last_status`. Sleeps only when *this* probe woke the box and no AuthAttempt is
pinning it awake.

Skips busy computers: active AuthAttempt, in-flight schedule brain, or a recently
touched live session (last_active_at within CASE_SESSION_KEEPER_BUSY_S, default 15m).

Heuristic-only "looks fine" is never recorded as durable `ok`.
Unhealthy → record `failed`; optionally open an AuthAttempt for human recovery later.
"""
import os
import threading
import time
from datetime import datetime, timezone

from auth_attempts import (
    check_proof,
    parse_proof_spec,
    observation_looks_logged_out as _looks_logged_out,
)
from config import log
from lifecycle import do_sleep, do_wake, get_computer
from store import store
from util import row_get

# Default: every 6 hours per computer, not every 20s sweeper tick.
INTERVAL_S = max(60, int(os.environ.get("CASE_SESSION_KEEPER_INTERVAL_S", "21600")))
# Skip probing a running computer the agent/human touched this recently.
BUSY_S = max(0, int(os.environ.get("CASE_SESSION_KEEPER_BUSY_S", "900")))

# computer_id → monotonic timestamp of last completed probe pass for that box
_last_probe_at = {}
_TICK = threading.Lock()


def _has_probe_profile(row):
    return bool(row_get(row, "probe_url") or parse_proof_spec(row_get(row, "proof_spec")))


def _eval_proof(computer, proof_spec, observation=None):
    """Positive proof_spec against live tab / last observation. Never logs secrets."""
    return bool(check_proof(computer, proof_spec, observation=observation))


def _decide_status_for(computer, proof_spec, observation):
    """Return last_status to record, or None if inconclusive (don't claim durable ok)."""
    if proof_spec:
        return "ok" if _eval_proof(computer, proof_spec, observation) else "failed"
    if _looks_logged_out(observation):
        return "failed"
    # Heuristic "looks fine" is not durable health, leave last_status alone.
    return None


def _maybe_start_recovery(computer_id, name, probe_url, proof_spec):
    """Optional stub: open an AuthAttempt so a human can recover later. Never raises."""
    if not probe_url:
        log.info("session_keeper: %s/%s unhealthy; no probe_url to start recovery",
                 computer_id, name)
        return
    try:
        import auth_attempts
        auth_attempts.start_attempt(
            computer_id, name, probe_url, proof_spec=proof_spec,
            idempotency_key=f"keeper:{computer_id}:{name}")
        log.info("session_keeper: started recovery attempt for %s/%s", computer_id, name)
    except Exception as e:
        log.info("session_keeper: recovery stub for %s/%s: %s", computer_id, name, e)


def _observe(computer):
    from deskclient import observe_auth
    try:
        resp = observe_auth(computer)
        return (resp or {}).get("observation") if isinstance(resp, dict) else None
    except Exception:
        log.exception("session_keeper: observe_auth failed for %s", computer["id"])
        return None


def _parse_iso(ts):
    if not ts or not isinstance(ts, str):
        return None
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _due(computer_id):
    last = _last_probe_at.get(computer_id)
    if last is None:
        return True
    return (time.monotonic() - last) >= INTERVAL_S


def _schedule_owns(computer_id):
    """True when a schedule brain is mid-run on this computer."""
    try:
        import scheduler
        running = list(getattr(scheduler, "SCHED_RUNNING", ()) or ())
    except Exception:
        return False
    for sid in running:
        try:
            s = store.get_schedule(sid)
        except Exception:
            continue
        if s and s["computer_id"] == computer_id:
            return True
    return False


def _recently_active(row):
    """Live session the agent/human is using, do not navigate over them."""
    if not row or row["state"] != "running" or BUSY_S <= 0:
        return False
    ts = _parse_iso(row_get(row, "last_active_at"))
    if not ts:
        return False
    age = (datetime.now(timezone.utc) - ts).total_seconds()
    return age < BUSY_S


def _busy(computer_id):
    if store.active_attempt_exists(computer_id):
        return True
    if _schedule_owns(computer_id):
        return True
    try:
        row = get_computer(computer_id)
    except Exception:
        return True
    return _recently_active(row)


def _probe_one_awake(computer_id, name):
    """Probe a credential on an already-running computer. Returns status or None."""
    crow = store.get_credential(computer_id, name)
    if not crow or not _has_probe_profile(crow):
        return None
    if store.active_attempt_exists(computer_id):
        return None

    probe_url = row_get(crow, "probe_url")
    proof_spec = parse_proof_spec(row_get(crow, "proof_spec"))
    computer = get_computer(computer_id)
    observation = None

    if probe_url:
        from deskclient import navigate
        try:
            navigate(computer, probe_url)
        except Exception:
            log.exception("session_keeper: navigate probe_url failed for %s/%s",
                          computer_id, name)
            store.record_credential_result(computer_id, name, "failed")
            _maybe_start_recovery(computer_id, name, probe_url, proof_spec)
            return "failed"
        observation = _observe(computer)

    computer = get_computer(computer_id)
    if proof_spec and observation is None and not probe_url:
        observation = _observe(computer)

    status = _decide_status_for(computer, proof_spec, observation)
    if status:
        store.record_credential_result(computer_id, name, status)
        if status == "failed":
            _maybe_start_recovery(computer_id, name, probe_url, proof_spec)
    return status


def tick():
    """Sweeper entry: probe due credentials, batched per computer, with busy/cadence guards."""
    if not _TICK.acquire(blocking=False):   # a pass can outlast the sweep interval
        return
    try:
        _tick()
    finally:
        _TICK.release()


def _tick():
    try:
        creds = [c for c in store.list_all_credentials() if _has_probe_profile(c)]
    except Exception:
        log.exception("session_keeper: list credentials failed")
        return

    by_cid = {}
    for c in creds:
        by_cid.setdefault(c["computer_id"], []).append(c)
    for cid in list(_last_probe_at):        # destroyed computers must not leak the cadence map
        if cid not in by_cid:
            _last_probe_at.pop(cid)

    for cid, group in by_cid.items():
        if not _due(cid):
            continue
        if _busy(cid):
            continue
        try:
            row = get_computer(cid)
        except Exception:
            continue
        woke = row["state"] == "asleep"
        try:
            if woke:
                do_wake(cid)
            for c in group:
                if _busy(cid):
                    break
                try:
                    _probe_one_awake(cid, c["name"])
                except Exception:
                    log.exception("session_keeper: probe %s/%s failed", cid, c["name"])
        except Exception:
            log.exception("session_keeper: tick for %s failed", cid)
        finally:
            _last_probe_at[cid] = time.monotonic()
            if woke and not store.active_attempt_exists(cid):
                try:
                    do_sleep(cid)
                except Exception:
                    log.exception("session_keeper: sleep after tick %s", cid)
