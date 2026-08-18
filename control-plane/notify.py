# SPDX-License-Identifier: AGPL-3.0-only
"""Notification channel: notify(handoff) out, on_answer(handoff_id, value) in.

Production default: RelayNotifier → central handoff-email Edge Function (owner
derived server-side from hashed cn_ credential; box never sends a recipient).
Dev/operator override: CASE_NOTIFY_CHANNEL=ntfy keeps the phone-topic loop.
Schedule reports use push(), ntfy when that channel is selected, else no-op.
"""
import base64
import json
import logging
import os
import re
import threading
import time

import requests

from config import API_BASE
from events import emit

log = logging.getLogger("cased.notify")

# Keep in sync with bin/case-give RELAY_URL_DEFAULT (separate process; cannot import).
DEFAULT_RELAY_URL = (
    "https://vwttrlkoccrdijkymhiz.supabase.co/functions/v1/handoff-email")
RELAY_BACKOFF_S = (0.5, 2.0, 5.0)


class Ntfy:
    def __init__(self, url, topic, answer_topic, api_base):
        self.url = url.rstrip("/")
        self.topic = topic
        self.answer_topic = answer_topic
        self.api_base = api_base
        if not topic:
            log.warning("CASE_NTFY_TOPIC unset — handoff notifications disabled")

    def notify(self, handoff, computer_name):
        if not self.topic:
            return
        threading.Thread(target=self._send, args=(handoff, computer_name), daemon=True).start()

    def _send(self, h, computer_name):
        try:
            ascii_ = lambda s: (s or "").encode("ascii", "replace").decode()
            headers = {
                "X-Title": ascii_(f"[Case] {h['kind']} — {computer_name}"),
                "X-Tags": h["id"],
                "X-Message": ascii_(h["prompt"])[:800],
            }
            if h["kind"] == "approval":
                a = f"{self.api_base}/handoffs/{h['id']}/answer"
                headers["X-Actions"] = (
                    f"http, Approve, {a}, method=POST, body={{\"value\":\"approve\"}}; "
                    f"http, Deny, {a}, method=POST, body={{\"value\":\"deny\"}}")
            body = b""
            if h.get("screenshot"):
                headers["X-Filename"] = "screen.png"
                body = base64.b64decode(h["screenshot"])
            requests.post(f"{self.url}/{self.topic}", data=body, headers=headers, timeout=15)
        except Exception as e:
            log.warning("ntfy publish failed: %s", e)

    def push(self, text):
        """Fire-and-forget one-line notification (scheduled-run reports); no screenshot, no actions."""
        if not self.topic:
            return
        def _p():
            try:
                requests.post(f"{self.url}/{self.topic}",
                              data=(text or "").encode("ascii", "replace")[:1000],
                              headers={"X-Title": "Case run"}, timeout=15)
            except Exception as e:
                log.warning("ntfy push failed: %s", e)
        threading.Thread(target=_p, daemon=True).start()

    def listen(self, on_answer):
        """Subscribe to the answer topic (SSE); messages are '{handoff_id} {value}' or a bare value."""
        if not self.answer_topic:
            return
        threading.Thread(target=self._listen, args=(on_answer,), daemon=True).start()

    def _listen(self, on_answer):
        while True:
            try:
                r = requests.get(f"{self.url}/{self.answer_topic}/sse", stream=True, timeout=(10, None))
                for line in r.iter_lines():
                    if not line or not line.startswith(b"data: "):
                        continue
                    try:
                        ev = json.loads(line[6:])
                    except ValueError:
                        continue
                    if ev.get("event") != "message":
                        continue
                    msg = (ev.get("message") or "").strip()
                    m = re.match(r"^(h_\w+)\s+(.+)$", msg, re.S)
                    try:
                        if m:
                            on_answer(m.group(1), m.group(2).strip())
                        elif msg:
                            on_answer(None, msg)
                    except Exception as e:
                        log.warning("answer via ntfy rejected: %s", e)
            except Exception:
                pass
            time.sleep(5)


class RelayNotifier:
    """POST handoff assist links to the central Edge Function. Never sends a recipient."""

    def __init__(self, url, credential):
        self.url = (url or DEFAULT_RELAY_URL).rstrip("/")
        self.credential = credential or ""
        if not self.credential:
            log.warning("CASE_NOTIFY_CREDENTIAL unset — handoff relay disabled")

    def notify(self, handoff, computer_name):
        threading.Thread(target=self._send, args=(handoff, computer_name), daemon=True).start()

    def _mark_failed(self, handoff):
        # Leave the handoff pending, humans can still use console /desk / re-notify later.
        emit("handoff_notify_failed", {
            "handoff_id": handoff.get("id"),
            "computer_id": handoff.get("computer_id"),
            "notify_failed": True,
            "status": "pending",
        })

    def _deliver(self, h, computer_name):
        """One POST. Returns True on HTTP success. Raises on transport/HTTP error."""
        if not self.credential:
            raise RuntimeError("CASE_NOTIFY_CREDENTIAL unset")
        assist_url = h.get("assist_url") or ""
        if not assist_url:
            raise RuntimeError("assist_url missing")
        body = {
            "handoff_id": h["id"],
            "assist_url": assist_url,
            "expires_at": h.get("expires_at") or "",
            "kind": h.get("kind") or "",
            "computer_name": computer_name or "",
            "domain": h.get("domain") or "",
        }
        # Invariant: never attach recipient fields, relay derives owner from credential hash.
        r = requests.post(
            self.url,
            json=body,
            headers={
                "Authorization": f"Bearer {self.credential}",
                "Content-Type": "application/json",
            },
            timeout=15,
        )
        if r.status_code >= 400:
            # Do not log body/Authorization/assist_url, status + handoff id only.
            raise RuntimeError(f"relay HTTP {r.status_code} handoff_id={h.get('id')}")
        return True

    def _send(self, h, computer_name):
        last_err = None
        for i, delay in enumerate(RELAY_BACKOFF_S):
            try:
                self._deliver(h, computer_name)
                return
            except Exception as e:
                last_err = e
                log.warning("relay deliver attempt %d/3 failed handoff_id=%s: %s",
                            i + 1, h.get("id"), e)
                time.sleep(delay)
        log.warning("relay deliver exhausted handoff_id=%s last=%s", h.get("id"), last_err)
        self._mark_failed(h)

    def push(self, text):
        """Schedule reports stay off the handoff-email relay (handoff-only)."""
        return

    def listen(self, on_answer):
        """No inbound answer channel on the relay, humans use /assist or console."""
        return


def build_notifier():
    """Select notify backend.

    Explicit CASE_NOTIFY_CHANNEL=ntfy|relay wins. When unset/empty (legacy
    boxes): prefer Relay if CASE_NOTIFY_CREDENTIAL is enrolled, else fall
    back to Ntfy when CASE_NTFY_TOPIC is set, else Relay fail-closed.
    """
    raw = os.environ.get("CASE_NOTIFY_CHANNEL")
    channel = (raw or "").strip().lower()
    cred = (os.environ.get("CASE_NOTIFY_CREDENTIAL") or "").strip()
    topic = (os.environ.get("CASE_NTFY_TOPIC") or "").strip()

    def _ntfy():
        return Ntfy(os.environ.get("CASE_NTFY_URL", "https://ntfy.sh"),
                    os.environ.get("CASE_NTFY_TOPIC"),
                    os.environ.get("CASE_NTFY_ANSWER_TOPIC"), API_BASE)

    def _relay():
        return RelayNotifier(
            os.environ.get("CASE_HANDOFF_RELAY_URL", DEFAULT_RELAY_URL),
            os.environ.get("CASE_NOTIFY_CREDENTIAL"),
        )

    if channel == "ntfy":
        return _ntfy()
    if channel == "relay":
        return _relay()
    # Unset / empty / unknown: enrolled relay first, then legacy ntfy topic.
    if cred:
        return _relay()
    if topic:
        return _ntfy()
    return _relay()


notifier = build_notifier()
