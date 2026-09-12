# SPDX-License-Identifier: AGPL-3.0-only
"""Usage stats that help improve Case.

Three events: install_ping (boot), install_heartbeat (once per UTC day), and
run_completed (did a scheduled run work). Each carries a random install id and a
handful of counts. Never a name, prompt, domain, URL, username, or anything from
the vault.

Opt out with CASE_TELEMETRY=0 or DO_NOT_TRACK=1: nothing is sent and no id is
minted. The id lives in plain JSON at $CASE_HOME/telemetry.json; delete the file
to reset it.

Every send is a daemon thread with a short timeout, so a slow or missing network
never delays a request or a boot. Failures are dropped at debug level.
"""
import json
import os
import sys
import threading
import uuid
from datetime import datetime, timedelta, timezone

import requests

from config import CASE_HOME, log
from store import store
from util import now

URL = "https://bios.case.computer/i/v0/e/"   # our PostHog ingest proxy
KEY = "phc_kQYe9hUva7ABjyDmvXxfiAuvJHqYNfB8QonRw9sBY4JH"   # public write-only token
PATH = os.path.join(CASE_HOME, "telemetry.json")

_CHOICE = (os.environ.get("CASE_TELEMETRY") or "").strip().lower()
# The unit tests would otherwise ping on every run. CASE_TELEMETRY=1 is the
# override tests/test_telemetry.py uses.
_TEST_RUN = ("pytest" in sys.modules
             or os.path.basename(sys.argv[0] or "").startswith("test_"))
ENABLED = ((os.environ.get("DO_NOT_TRACK") or "").strip().lower() not in ("1", "true", "yes", "on")
           and _CHOICE not in ("0", "false", "no", "off")
           and (not _TEST_RUN or _CHOICE in ("1", "true", "yes", "on")))
_LOCK = threading.RLock()


def _load():
    # Locked so two sends racing on a missing file mint one id, not two.
    with _LOCK:
        try:
            with open(PATH) as f:
                s = json.load(f)
            if isinstance(s, dict) and s.get("install_id"):
                return s
        except Exception:
            pass
        s = {"install_id": uuid.uuid4().hex, "first_seen": now()}
        _save(s)
        return s


def _save(s):
    os.makedirs(CASE_HOME, exist_ok=True)
    tmp = PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f, indent=2)
    os.replace(tmp, PATH)


def _age_days(s):
    try:
        seen = datetime.strptime(s["first_seen"], "%Y-%m-%dT%H:%M:%SZ")
        return (datetime.now(timezone.utc).replace(tzinfo=None) - seen).days
    except Exception:
        return 0


def counts():
    since = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        return store.telemetry_counts(since)
    except Exception:
        return {}


def capture(event, props=None):
    """Fire-and-forget: returns immediately, never raises."""
    if not ENABLED:
        return
    threading.Thread(target=_send, args=(event, props or {}), daemon=True).start()


def _send(event, props):
    try:
        s = _load()
        properties = {**props, "self_host": True, "install_age_days": _age_days(s)}
        requests.post(URL, timeout=5, json={
            "api_key": KEY, "event": event, "distinct_id": s["install_id"],
            "timestamp": now(), "properties": properties})
    except Exception:
        log.debug("usage stats %s dropped", event, exc_info=True)


def install_ping():
    capture("install_ping", counts())


def heartbeat_if_due():
    """Sweeper: at most one event per UTC day per install."""
    if not ENABLED:
        return
    today = now()[:10]
    with _LOCK:
        s = _load()
        if s.get("last_heartbeat_day") == today:
            return
        s["last_heartbeat_day"] = today
        _save(s)
    capture("install_heartbeat", counts())
