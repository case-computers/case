# SPDX-License-Identifier: AGPL-3.0-only
"""Docker driver, the only module that knows docker-py.

All container knowledge lives here behind a few verbs (connect/create/ports/wait/
destroy/up). Containers are cattle: `container_up` is the ground truth the lifecycle
module reconciles the DB against after a daemon restart (colima reboot).
"""
import os
import time

import docker
import requests

from config import IMAGE, VNC_PORT, log
from errors import ApiError

NotFound = docker.errors.NotFound   # lifecycle catches this without importing docker-py

DESK_INTERNAL_PORT = 8000
VNC_INTERNAL_PORT = 6080

_dc = None


def _connect():
    # docker-py reads DOCKER_HOST, not CLI contexts, also try well-known local sockets
    candidates = [None,
                  "unix://" + os.path.expanduser("~/.colima/default/docker.sock"),
                  "unix://" + os.path.expanduser("~/.docker/run/docker.sock")]
    err = None
    for base in candidates:
        try:
            c = docker.from_env() if base is None else docker.DockerClient(base_url=base)
            c.ping()
            return c
        except Exception as e:
            err = e
    raise err


def dc():
    global _dc
    if _dc is None:
        _dc = _connect()
    return _dc


def _docker(fn):
    """fn(client), once more on a fresh client when the daemon went away under the
    cached one (host reboot / colima restart can move the socket)."""
    global _dc
    try:
        return fn(dc())
    except requests.ConnectionError:
        _dc = None
        return fn(dc())


def ping():
    _docker(lambda c: c.ping())


def docker_network():
    """Compose network name, or empty when cased runs on the host (loopback ports)."""
    return (os.environ.get("CASE_DOCKER_NETWORK") or "").strip()


def desk_host(cid):
    """Where cased should dial deskd/noVNC for this computer."""
    return f"case-{cid}" if docker_network() and cid else "127.0.0.1"


def desk_base(cid, port):
    return f"http://{desk_host(cid)}:{int(port)}"


def container_name(cid):
    return f"case-{cid}"


def container_run_kwargs(cid, cpus, ram_mb, volume, token):
    """Kwargs for docker.containers.run (image excluded). Unit-testable without a daemon."""
    kw = dict(
        detach=True, name=container_name(cid),
        environment={"DESK_TOKEN": token,
                     # DESK_DEBUG=1 on cased mirrors chromium/websockify logs
                     # to every desktop's docker logs
                     "DESK_DEBUG": (os.environ.get("DESK_DEBUG") or "").strip(),
                     "DESK_RESOLUTION": (os.environ.get("DESK_RESOLUTION") or "").strip(),
                     # start.sh puts websockify behind basic auth when it is set
                     "CASE_DOCKER_NETWORK": docker_network()},
        volumes={volume: {"bind": "/home/agent", "mode": "rw"}},
        # memswap_limit == mem_limit: without it the desktop swaps past its RAM cap
        mem_limit=f"{int(ram_mb)}m", memswap_limit=f"{int(ram_mb)}m", pids_limit=512,
        nano_cpus=int(cpus * 1e9), shm_size="1g",
        labels={"managed-by": "cased"})
    net = docker_network()
    if net:
        # Unpublished on the host: cased + UI reach these on the compose network.
        kw["network"] = net
    else:
        kw["ports"] = {"8000/tcp": ("127.0.0.1", None),
                       # a fixed CASE_VNC_PORT binding on every container only works
                       # when CASE_MAX_RUNNING=1: docker checks the bind at START, and
                       # the lifecycle guard never runs two desktops at once. Switch to
                       # per-container ports before raising MAX_RUNNING with a pinned port.
                       "6080/tcp": ("127.0.0.1", VNC_PORT)}
    return kw


def create_container(cid, cpus, ram_mb, volume, token):
    kw = container_run_kwargs(cid, cpus, ram_mb, volume, token)
    try:
        return _docker(lambda c: c.containers.run(IMAGE, **kw))
    except docker.errors.APIError as e:
        if e.status_code != 409:
            raise
        # Docker can 404 a name on lookup yet 409 it on create: a container stuck
        # mid-removal or dead after a daemon crash keeps the name reserved. The
        # corpse is cattle (identity lives in the volume) — clear it, retry once.
        try:
            dc().api.remove_container(container_name(cid), force=True)
        except Exception:
            pass
        return dc().containers.run(IMAGE, **kw)


def container_ports(container, deadline=10):
    if docker_network():
        return (DESK_INTERNAL_PORT, VNC_INTERNAL_PORT)
    t0 = time.time()
    while time.time() - t0 < deadline:
        container.reload()
        ports = container.attrs["NetworkSettings"]["Ports"] or {}
        try:
            return (int(ports["8000/tcp"][0]["HostPort"]), int(ports["6080/tcp"][0]["HostPort"]))
        except (KeyError, TypeError, IndexError):
            time.sleep(0.3)
    raise ApiError(500, "no_ports", "container published no host ports")


def get_container(cid):
    return _docker(lambda c: c.containers.get(container_name(cid)))


def managed_containers():
    """{name: container} for every desktop cased made, from one list call. Sparse:
    the listing carries the status, and container_ports reloads what it needs."""
    listed = _docker(lambda c: c.containers.list(all=True, sparse=True,
                                                 filters={"label": "managed-by=cased"}))
    return {n.lstrip("/"): c for c in listed for n in c.attrs.get("Names") or ()}


def container_up(cid):
    try:
        return get_container(cid).status == "running"
    except Exception:
        return False  # NotFound, or daemon died under us (e.g. colima restart)


def start_container(cid):
    get_container(cid).start()


def stop_container(cid, timeout=10):
    try:
        get_container(cid).stop(timeout=timeout)
    except docker.errors.NotFound:
        pass


def destroy_infra(cid, volume):
    """Best effort, never raises. Already gone is fine; anything else leaves a
    container or a volume behind, so say which."""
    try:
        get_container(cid).remove(force=True)
    except docker.errors.NotFound:
        pass
    except Exception as e:
        log.warning("destroy %s: container not removed (%s)", cid, e)
    try:
        _docker(lambda c: c.volumes.get(volume)).remove()
    except docker.errors.NotFound:
        pass
    except Exception as e:
        log.warning("destroy %s: volume %s not removed (%s)", cid, volume, e)


def create_volume(volume):
    _docker(lambda c: c.volumes.create(name=volume, labels={"managed-by": "cased"}))
