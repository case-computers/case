# SPDX-License-Identifier: AGPL-3.0-only
"""Computer lifecycle + the state machine.

This module is the only place that enforces the state transitions.
`set_state` reads the current state, rejects illegal edges, and writes with a
compare-and-set so a concurrent transition (the sweeper thread vs an HTTP handler)
can't be silently clobbered. Docker is the ground truth: `reconcile` *forces* the DB
to match reality after a daemon restart, it corrects the machine rather than
transitioning it, so it deliberately bypasses the transition guard.
"""
import secrets
import time

from config import DESK_H, DESK_W, IMAGE, MAX_RAM_MB, MAX_RUNNING, log
from errors import ApiError
from events import emit
from store import store
import deskclient
import dockerd

# creating → running ⇄ asleep, transient waking, terminal deleted.
# 'running → waking' and 'waking → asleep' are recovery edges (a wake that finds the
# container down / fails), kept explicit here.
TRANSITIONS = {
    "creating": {"running", "deleted"},
    "running":  {"asleep", "waking", "deleted"},
    "waking":   {"running", "asleep", "deleted"},
    "asleep":   {"waking", "deleted"},
    "deleted":  set(),
}


def get_computer(cid):
    row = store.get_computer(cid)
    if not row or row["state"] == "deleted":
        raise ApiError(404, "not_found", f"no computer {cid!r}")
    return row


def can_transition(from_, to):
    return to in TRANSITIONS.get(from_, set())


def set_state(cid, to):
    """Guarded transition. No-op if already in `to`; raises on an illegal edge;
    compare-and-set so a concurrent change is detected, not clobbered."""
    row = store.get_computer(cid)
    if not row:
        raise ApiError(404, "not_found", f"no computer {cid!r}")
    current = row["state"]
    if current == to:
        return True
    if not can_transition(current, to):
        raise ApiError(409, "illegal_transition", f"{current} → {to} not allowed")
    if store.set_state(cid, to, expect=current) == 0:
        # someone transitioned it between our read and write, don't emit a stale edge.
        # The CAS makes the write safe; orchestration races (wake vs sleep) remain —
        # add per-cid locks only if they actually bite.
        return False
    emit("state_changed", {"computer_id": cid, "from": current, "to": to})
    return True


def _force_state(cid, current, to):
    """Reconcile-only: align the DB to Docker truth, bypassing the transition guard."""
    if current == to:
        return
    store.set_state(cid, to)
    emit("state_changed", {"computer_id": cid, "from": current, "to": to})


def _vnc_url(row):
    if row["state"] != "running" or not row["vnc_port"]:
        return None
    # Compose does not publish noVNC on the host; the Drive UI proxies it.
    if dockerd.docker_network():
        return None
    return f"http://127.0.0.1:{row['vnc_port']}/vnc.html"


def computer_json(row, summaries=None, creds=None, pending=None):
    # tasks + next_run_at ride along so the console's COMPUTERS tab needs no second
    # call per computer (and no GET /v1/schedules route at all).
    cid = row["id"]
    if summaries is not None:
        tasks, next_run_at = summaries.get(cid, (0, None))
    else:
        tasks, next_run_at = store.schedule_summary(cid)
    if creds is not None:
        credentials = creds.get(cid, [])
    else:
        credentials = store.credential_names(cid)
    if pending is not None:
        pending_handoffs = pending.get(cid, 0)
    else:
        pending_handoffs = store.pending_handoff_count(cid)
    return {
        "id": row["id"], "name": row["name"], "state": row["state"], "image": row["image"],
        "created_at": row["created_at"], "last_active_at": row["last_active_at"],
        "resources": {"cpus": row["cpus"], "ram_mb": row["ram_mb"], "disk_volume": row["volume"]},
        "display": {"width": DESK_W, "height": DESK_H},
        "vnc_url": _vnc_url(row),
        "vnc_port": row["vnc_port"] if row["state"] == "running" else None,
        "credentials": credentials,
        "pending_handoffs": pending_handoffs,
        "tasks": tasks,
        "next_run_at": next_run_at,
    }


def admit(ram_mb):
    """Refuse a wake/create that the host cannot carry. Two guards: MAX_RUNNING is
    how many desktops the operator wants live at once, MAX_RAM_MB is whether the
    next one fits in memory."""
    if store.active_count() >= MAX_RUNNING:
        raise ApiError(409, "too_many_running", f"max {MAX_RUNNING} running computers")
    if MAX_RAM_MB:
        want = store.active_ram_mb() + int(ram_mb)
        if want > MAX_RAM_MB:
            raise ApiError(409, "not_enough_ram",
                           f"{want} MB of desktops on a {MAX_RAM_MB} MB budget "
                           f"(sleep one, or raise CASE_MAX_RAM_MB)")


def provision(name=None, cpus=1, ram_mb=2048):
    cpus, ram_mb = float(cpus), int(ram_mb)
    admit(ram_mb)
    cid = "c_" + secrets.token_hex(5)
    name = str(name or cid)
    token = secrets.token_hex(16)
    volume = f"case-{cid}"
    store.insert_computer(cid, name, IMAGE, cpus, ram_mb, volume, token)
    try:
        dockerd.create_volume(volume)
        container = dockerd.create_container(cid, cpus, ram_mb, volume, token)
        desk_port, vnc_port = dockerd.container_ports(container)
        store.set_ports(cid, desk_port, vnc_port)
        deskclient.wait_desk(cid, desk_port, token, 60)
    except ApiError:
        dockerd.destroy_infra(cid, volume)
        store.delete_computer(cid)
        raise
    except Exception as e:
        dockerd.destroy_infra(cid, volume)
        store.delete_computer(cid)
        raise ApiError(500, "create_failed", f"create failed: {type(e).__name__}")
    if not _try_set(cid, "running"):
        # raced a DELETE while provisioning, tear down the infra we just built
        dockerd.destroy_infra(cid, volume)
        store.delete_computer(cid)
        raise ApiError(409, "create_lost_race", "computer deleted during provisioning")
    return computer_json(get_computer(cid))


def _try_set(cid, to):
    """set_state that returns False instead of raising on an illegal edge, for the
    final commit of a build, where 'the row was deleted under us' is a race to clean
    up after, not a caller error."""
    try:
        return set_state(cid, to)
    except ApiError:
        return False


def destroy(cid):
    row = get_computer(cid)
    dockerd.destroy_infra(cid, row["volume"])
    store.delete_credentials(cid)
    # deletion is terminal and the infra is already gone, force it, so a concurrent
    # wake that moved the row to 'waking' can't leave it un-deleted.
    _force_state(cid, row["state"], "deleted")


def do_sleep(cid):
    row = get_computer(cid)
    if row["state"] == "asleep":
        return
    # Active AuthAttempt pins the desktop awake so Assist/VNC handoff can't lose the tab.
    if store.active_attempt_exists(cid):
        raise ApiError(409, "auth_in_progress",
                       "cannot sleep while authentication is in progress")
    if not can_transition(row["state"], "asleep"):
        # validate BEFORE the side effect, don't stop a mid-provision container then 409
        raise ApiError(409, "illegal_transition", f"{row['state']} → asleep not allowed")
    dockerd.stop_container(cid)
    set_state(cid, "asleep")


def sleep_all():
    """Sleep every awake desktop on shutdown. Best effort, never raises.

    The desktops are not compose services, so `docker compose down` would
    otherwise leave them running with a DB that still says "running". Data is
    safe either way (start.sh traps SIGTERM and quiesces Chromium); this just
    makes the graceful path the default one."""
    slept = []
    for row in store.list_computers():
        if row["state"] not in ("running", "waking"):
            continue
        try:
            do_sleep(row["id"])
            slept.append(row["id"])
        except Exception as e:      # a login mid-flight, a dead container, a race
            log.warning("shutdown: %s did not sleep cleanly (%s)", row["id"], e)
    if slept:
        log.info("shutdown: slept %d computer(s): %s", len(slept), ", ".join(slept))
    return slept


def do_wake(cid):
    row = get_computer(cid)
    if row["state"] == "running" and dockerd.container_up(cid):
        return  # DB can lie after a daemon restart, only skip if the container really is up
    t0 = time.monotonic()
    # Asleep computers don't count against the budget; waking one must, same as create.
    # (create already checks; wake used to bypass the cap and OOM a small box.)
    if row["state"] == "asleep":
        admit(row["ram_mb"])
    if not set_state(cid, "waking"):
        return  # lost a race (concurrent sleep/delete), don't build infra on a stale row
    try:
        try:
            dockerd.start_container(cid)
        except dockerd.NotFound:
            # container is cattle, the volume is the identity, recreate around it
            dockerd.create_container(cid, row["cpus"], row["ram_mb"], row["volume"],
                                     row["desk_token"])
        desk_port, vnc_port = dockerd.container_ports(dockerd.get_container(cid))
        store.set_ports(cid, desk_port, vnc_port)
        t_container = time.monotonic() - t0
        deskclient.wait_desk(cid, desk_port, row["desk_token"], 30)
        # The two halves of a wake bill very differently — container start is docker,
        # the rest is Chromium coming up. Split them or you tune the wrong one.
        log.info("wake %s: container %.2fs, deskd healthy %.2fs",
                 cid, t_container, time.monotonic() - t0)
    except Exception:
        _try_set(cid, "asleep")   # tolerate a concurrent delete here too
        raise
    if not _try_set(cid, "running"):
        # a DELETE landed while we were waking → we rebuilt a container the delete didn't
        # know about. Tear it down instead of leaking a zombie on a 'deleted' row.
        dockerd.destroy_infra(cid, row["volume"])
        raise ApiError(409, "wake_lost_race", "computer was deleted/changed during wake")


def ensure_running(cid, wake):
    row = get_computer(cid)
    if row["state"] == "running":
        return row
    if row["state"] == "creating":
        raise ApiError(409, "provisioning", "computer is still being created; retry shortly")
    if wake:
        do_wake(cid)
        return get_computer(cid)
    raise ApiError(409, "asleep", f"computer is {row['state']}; retry with ?wake=true")


def reconcile():
    """After cased/daemon restart: force the DB to match what Docker actually has."""
    for row in store.all_non_deleted():
        cid, current = row["id"], row["state"]
        try:
            container = dockerd.get_container(cid)
        except dockerd.NotFound:
            log.warning("container for %s missing; will recreate from volume on wake", cid)
            _force_state(cid, current, "asleep")
            continue
        except Exception as e:
            log.warning("reconcile skipped (docker unavailable): %s", e)
            return
        if container.status == "running":
            try:
                desk_port, vnc_port = dockerd.container_ports(container)
                if (desk_port, vnc_port) != (row["desk_port"], row["vnc_port"]):
                    store.set_ports(cid, desk_port, vnc_port)
                _force_state(cid, current, "running")
            except ApiError:
                _force_state(cid, current, "asleep")
        else:
            _force_state(cid, current, "asleep")
