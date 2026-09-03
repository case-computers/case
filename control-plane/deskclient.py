# SPDX-License-Identifier: AGPL-3.0-only
"""The one seam to deskd (the in-container daemon).

Owns the whole contract so no caller re-implements it: bearer auth, timeout →
504, deskd's structured `{error:{code,message}}` → ApiError, and the 423
credential-injection status. JSON and binary endpoints both route through here;
screenshot helpers give the "best-effort, never raises" shape that handoffs and
run-reports want.
"""
import base64
import json
import time

import requests

from config import log
from dockerd import desk_base
from errors import ApiError


def desk(row, method, path, timeout=35, **kw):
    """Low-level: returns the raw Response (used when the caller needs the status)."""
    url = f"{desk_base(row['id'], row['desk_port'])}{path}"
    headers = {"Authorization": f"Bearer {row['desk_token']}"}
    try:
        return requests.request(method, url, headers=headers, timeout=timeout, **kw)
    except (requests.Timeout, requests.ConnectionError):
        raise ApiError(504, "daemon_timeout", "deskd did not respond")


def _raise(r):
    """Translate a deskd error response (>=400) into an ApiError. deskd sends
    {error:{code,message}}; 423 carries code 'credential_injection'."""
    try:
        e = r.json()["error"]
        raise ApiError(r.status_code, e["code"], e["message"])
    except (ValueError, KeyError):
        # deskd's raw body can echo a page or a credential, keep it in the log only
        log.warning("deskd %s: %s", r.status_code, r.text[:300])
        raise ApiError(r.status_code, "desk_error", f"deskd returned {r.status_code}")


def desk_json(row, method, path, timeout=35, **kw):
    r = desk(row, method, path, timeout=timeout, **kw)
    if r.status_code >= 400:
        _raise(r)
    return r.json()


def desk_bytes(row, method, path, timeout=35, **kw):
    r = desk(row, method, path, timeout=timeout, **kw)
    if r.status_code >= 400:
        _raise(r)
    return r.content


def eval_js(row, expression, timeout_s=20):
    return desk_json(row, "POST", "/eval",
                     json={"expression": expression, "timeout_s": timeout_s},
                     timeout=timeout_s + 15)


def eval_value(row, expression, timeout_s=15, default=None):
    """eval_js and unwrap .value; `default` on any non-dict/error shape."""
    try:
        r = eval_js(row, expression, timeout_s)
    except Exception:
        return default
    v = r.get("value") if isinstance(r, dict) else None
    return v if v is not None else default


# Built on /eval instead of a deskd /navigate route on purpose: wake starts the
# *existing* container (lifecycle.do_wake), so a new deskd route would reach only
# computers created after an image rebuild. This reaches every computer that
# exists today, on a cased restart.
_PAGE_TEXT = "document.body?document.body.innerText.slice(0,2000):''"


def page_text(row, _eval=None):
    """Best-effort first 2000 chars of the page. None on any failure so a
    navigation or click that already happened still returns what it did.
    `_eval` is the eval_js to use — browse patches its own copy."""
    fn = _eval or eval_js
    try:
        r = fn(row, _PAGE_TEXT, 3)
        t = r.get("value") if r.get("ok") else None
        return t if isinstance(t, str) and t else None
    except ApiError:
        return None


def _with_text(row, arrived):
    t = page_text(row)
    if t:
        arrived["text"] = t
    return arrived


_HYDRATE_N = ("document.readyState==='complete'"
              "?document.querySelectorAll('a,button,input,select,textarea').length:0")


def _hydrate(row, arrived, deadline):
    """SPA: readyState can complete before React mounts any controls."""
    time.sleep(min(2.0, max(0.0, deadline - time.time())))
    try:
        r = eval_js(row, _HYDRATE_N, 3)
        n = r.get("value") if r.get("ok") else 0
        if n:
            return _with_text(row, arrived)
    except ApiError as e:
        if e.status != 502:
            raise
    if time.time() >= deadline:
        return _with_text(row, arrived)
    try:
        eval_js(row, "location.reload()", 10)
    except ApiError as e:
        if e.status != 502:
            raise
        return _with_text(row, arrived)
    while time.time() < deadline:
        time.sleep(0.4)
        try:
            r = eval_js(row, _HYDRATE_N, 3)
        except ApiError as e:
            if e.status != 502:
                raise
            continue
        if r.get("ok") and r.get("value"):
            return _with_text(row, arrived)
    return _with_text(row, arrived)


def navigate(row, url, timeout_s=30):
    """Load `url` in the browser tab and block until the new document is ready.

    Stamps a sentinel on the current document, assigns location, then polls until a
    document *without* the sentinel reports readyState complete, so it can't be
    fooled by the old page still being complete, and a reload of the same URL still
    counts. Same-page (#hash) jumps keep the sentinel and will time out; use /eval.

    The polling lives here rather than in the caller's head: an agent doing this over
    MCP spends one LLM turn per poll, which is what this endpoint exists to delete.

    Only a 502 is treated as navigation churn; 504/423/400 are real failures and are
    raised, so a slept or wedged box says so instead of looking like a slow page.
    `timeout_s` is the whole budget, assign included, so the route stays inside the
    caller's own request timeout. A document that swaps for another reason (an
    in-flight click, a JS/meta-refresh interstitial) also clears the sentinel and is
    reported as arrival, check `url` if that matters.
    """
    deadline = time.time() + timeout_s
    poll = ("window.__case_nav===undefined&&document.readyState==='complete'"
            "?[location.href,document.title,"
            "document.querySelectorAll('a,button,input,select,textarea').length]:null")
    while True:
        try:
            r = eval_js(row, f"window.__case_nav=1;location.assign({json.dumps(url)})", 10)
            break
        except ApiError as e:
            # right after a wake deskd can have no Chromium page target yet (502))
            # the same error the poll loop tolerates, so don't make it fatal here.
            if e.status != 502 or time.time() >= deadline:
                raise
            time.sleep(0.4)
    if not r.get("ok"):   # malformed URL etc, say so now, don't burn the whole timeout
        return {"ok": False, "error": r.get("error") or "navigation rejected"}
    while time.time() < deadline:
        time.sleep(0.4)
        try:
            r = eval_js(row, poll, 3)
        except ApiError as e:
            if e.status != 502:
                raise          # asleep / injecting / wedged, not something to wait out
            continue           # context torn down mid-navigation: that IS progress
        v = r.get("value")
        if isinstance(v, list) and len(v) >= 2:   # a >64KB result is a truncated *string*
            arrived = {"ok": True, "url": v[0], "title": v[1]}
            if len(v) > 2 and v[2] == 0:
                return _hydrate(row, arrived, deadline)
            return _with_text(row, arrived)
    try:   # don't leave a uniquely-named global on a live page for site JS to find
        eval_js(row, "delete window.__case_nav", 3)
    except ApiError:
        pass
    return {"ok": False, "error": f"page did not finish loading within {timeout_s}s"}


def wait_desk(cid, port, token, deadline_s):
    url = desk_base(cid, port) + "/health"
    t0 = time.time()
    while time.time() - t0 < deadline_s:
        try:
            r = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=3)
            if r.ok and r.json().get("ok") and r.json().get("chrome"):
                return
        except Exception:
            pass
        time.sleep(1)
    raise ApiError(504, "daemon_timeout", f"deskd not healthy after {deadline_s}s")


def screenshot_bytes(row):
    """Best-effort PNG bytes, or None on any failure (asleep, 423 injection, hiccup).
    For run-report artifacts. Never raises."""
    try:
        return desk_bytes(row, "GET", "/screenshot", timeout=15)
    except Exception:
        return None


def screenshot_b64(row):
    """Best-effort screenshot as base64, or None. For handoff cards. Never raises."""
    b = screenshot_bytes(row)
    return base64.b64encode(b).decode() if b else None


# ---------- durable-auth deskd helpers (observation / action only) ----------

def observe_auth(row):
    """POST /auth/observe → {ok, observation} with generic challenge signals."""
    return desk_json(row, "POST", "/auth/observe", timeout=35)


def auth_submit_challenge(row, kind, value=None):
    """POST /auth/submit_challenge, otp/code fill+enter, or approval settle."""
    body = {"kind": kind}
    if value is not None:
        body["value"] = value
    return desk_json(row, "POST", "/auth/submit_challenge", json=body, timeout=90)


def auth_navigate_verification(row, url, domains=None):
    """POST /auth/navigate_verification, domain_ok against optional allowlist, then navigate."""
    body = {"url": url}
    if domains is not None:
        body["domains"] = domains
    return desk_json(row, "POST", "/auth/navigate_verification", json=body, timeout=40)
