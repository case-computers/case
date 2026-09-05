# SPDX-License-Identifier: AGPL-3.0-only
"""Notification channel: notify(handoff) out, on_answer(handoff_id, value) in.

ntfy (https://ntfy.sh or self-hosted) is the channel: handoffs post to
CASE_NTFY_TOPIC, answers come back over CASE_NTFY_ANSWER_TOPIC's SSE stream.
With no topic configured every call is a warned no-op — handoffs still show up
in the API and the Drive UI, they just don't reach a phone. Schedule reports
use push()."""
import base64
import json
import logging
import os
import re
import threading
import time

import requests

log = logging.getLogger("cased.notify")
OUTBOUND_TAG = "case-outbound"


def _ntfy_token():
    return (os.environ.get("CASE_NTFY_TOKEN") or "").strip()


def _auth_headers():
    token = _ntfy_token()
    return {"Authorization": f"Bearer {token}"} if token else {}


def _tags(ev):
    raw = ev.get("tags") or []
    if isinstance(raw, str):
        return [x.strip() for x in raw.split(",") if x.strip()]
    return [str(x) for x in raw]


class Ntfy:
    def __init__(self, url, topic, answer_topic):
        self.url = url.rstrip("/")
        self.topic = topic
        self.answer_topic = answer_topic
        if not topic:
            log.warning("CASE_NTFY_TOPIC unset — handoff notifications disabled")
        if topic and answer_topic and topic == answer_topic:
            log.warning("CASE_NTFY_ANSWER_TOPIC equals CASE_NTFY_TOPIC — answer listen disabled")

    def _same_topic(self):
        return bool(self.topic and self.answer_topic and self.topic == self.answer_topic)

    def notify(self, handoff, computer_name):
        if not self.topic:
            return
        threading.Thread(target=self._send, args=(handoff, computer_name), daemon=True).start()

    def _send(self, h, computer_name):
        try:
            ascii_ = lambda s: (s or "").encode("ascii", "replace").decode()
            tags = [OUTBOUND_TAG]
            if h.get("id"):
                tags.append(h["id"])
            prompt = " ".join((h.get("prompt") or "").split())    # header values can't hold newlines
            headers = {
                **_auth_headers(),
                "X-Title": ascii_(f"[Case] {h['kind']} — {computer_name}"),
                "X-Tags": ",".join(tags),
                "X-Message": ascii_(prompt)[:800],
            }
            if h.get("assist_url"):
                headers["X-Click"] = h["assist_url"]
            if h.get("answer_url"):
                a = h["answer_url"]
                headers["X-Actions"] = (
                    f"http, Approve, {a}, method=POST, headers.Content-Type=application/json, body={{\"value\":\"approve\"}}; "
                    f"http, Deny, {a}, method=POST, headers.Content-Type=application/json, body={{\"value\":\"deny\"}}")
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
                              headers={**_auth_headers(), "X-Title": "Case run",
                                       "X-Tags": OUTBOUND_TAG}, timeout=15)
            except Exception as e:
                log.warning("ntfy push failed: %s", e)
        threading.Thread(target=_p, daemon=True).start()

    def listen(self, on_answer):
        """Subscribe to the answer topic (SSE); messages are '{handoff_id} {value}' or a bare value."""
        if not self.answer_topic or self._same_topic():
            return
        threading.Thread(target=self._listen, args=(on_answer,), daemon=True).start()

    def _listen(self, on_answer):
        headers = _auth_headers()
        while True:
            try:
                r = requests.get(f"{self.url}/{self.answer_topic}/sse", stream=True,
                                 headers=headers, timeout=(10, 90))
                for line in r.iter_lines():
                    if not line or not line.startswith(b"data: "):
                        continue
                    try:
                        ev = json.loads(line[6:])
                    except ValueError:
                        continue
                    if ev.get("event") != "message" or OUTBOUND_TAG in _tags(ev):
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
            except Exception as e:
                log.warning("ntfy listen: %s", e)
            time.sleep(5)


def build_notifier():
    return Ntfy(os.environ.get("CASE_NTFY_URL", "https://ntfy.sh"),
                os.environ.get("CASE_NTFY_TOPIC"),
                os.environ.get("CASE_NTFY_ANSWER_TOPIC"))


notifier = build_notifier()
