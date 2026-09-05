# SPDX-License-Identifier: AGPL-3.0-only
"""Optional captcha auto-solve for login-path challenges.

Leaf-ish module: depends only on config + requests (never handoffs, no cycle).
Local detection + verification stay authoritative (DETECT_JS, still_challenge,
gate_open). Auto-solve is quarantined behind a capability adapter; Death By
Captcha is the only vendor this release, and only for declared capabilities.

Off unless CASE_DBC_* credentials are set. Only sitekey/publickey + pageurl
(+ configured proxy) leave the box, and the caller strips the pageurl's query
and fragment first, so session tokens in the URL stay here; tokens, DBC
passwords, screenshots, and page text are never logged.

Capabilities: recaptcha_v2 (DBC type 4), recaptcha_enterprise (type 25,
requires proxy), arkose (type 6). Unsupported / terminal DBC answers
(is_correct=false, overload, missing enterprise proxy) return immediately so
the caller creates a typed captcha (verify_page) handoff, never a long poll
and never a false login success.
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Protocol, TypedDict
import requests

from config import log

# Declared DBC capabilities only, no CapSolver/2Captcha this release.
DBC_CAPABILITIES = frozenset({"recaptcha_v2", "recaptcha_enterprise", "arkose"})

DBC_UPLOAD = "https://api.dbcapi.me/api/captcha"
# DBC's own client-side default is 60s (FAQ tech #5); their internal solving timeout is
# 3 min and they may bill a captcha solved after we stopped polling, so cutting off
# earlier both misses usable tokens and pays for them. Terminal failures short-circuit
# well before this, so the cap only bounds genuinely slow solves.
DBC_POLL_CAP_S = 60
POLL_INTERVAL_S = 2.5

# Post-solve verify, stronger than deskd.RE_CAPTCHA alone. Mirrors RE_BLOCK plus
# OTP/verification-code phrasing and RE_FAIL hard-fail signals. Detection itself
# stays deskd-authoritative; this only gates auto-resume.
RE_VERIFY = re.compile(
    r"captcha|verify you.?re human|not a robot|"
    r"two.?factor|\b2fa\b|one.?time (code|password)|verification code|"
    r"authentication code|enter the code|unusual activity|suspicious login",
    re.I)
RE_FAIL_VERIFY = re.compile(
    r"incorrect|invalid|wrong password|try again|couldn.?t find|doesn.?t match", re.I)

# Returns {family, key, pageurl, callback, enterprise}. callback=true means a
# usable completion path exists (grecaptcha client callback, Arkose callback, or
# #captcha-challenge form, including inside iframe[src*=captchaInternal]).
# enterprise=true when iframe src / grecaptcha.enterprise says so (DBC type 25).
# Prefer the family that actually has a completion path over "any Arkose key".
DETECT_JS = r"""
(() => {
  const out = {family: null, key: null, pageurl: location.href, callback: false,
               enterprise: false};

  function captchaInternalDoc() {
    try {
      const iframe = document.querySelector('iframe[src*="captchaInternal"]');
      return iframe && (iframe.contentDocument || iframe.contentWindow.document);
    } catch (e) { return null; }
  }

  // {key, enterprise} — enterprise from iframe path or grecaptcha.enterprise.
  function firstRecaptchaMeta() {
    let fallback = null, fallbackEnt = false;
    function scan(doc) {
      if (!doc) return null;
      for (const f of doc.querySelectorAll('iframe')) {
        const s = f.src || '';
        if (!/recaptcha|google\.com\/recaptcha/i.test(s)) continue;
        const m = s.match(/[?&]k=([^&]+)/);
        if (!m) continue;
        const k = decodeURIComponent(m[1]);
        const ent = /recaptcha\/enterprise/i.test(s);
        if (/bframe/i.test(s)) return {key: k, enterprise: ent};
        if (!fallback) { fallback = k; fallbackEnt = ent; }
      }
      return null;
    }
    const hit = scan(document) || scan(captchaInternalDoc());
    if (hit) return hit;
    const el = document.querySelector('[data-sitekey]');
    let key = el ? el.getAttribute('data-sitekey') : fallback;
    let ent = fallbackEnt;
    try {
      if (window.grecaptcha && window.grecaptcha.enterprise) ent = true;
    } catch (e) {}
    return key ? {key: key, enterprise: !!ent} : null;
  }

  function firstArkoseKey() {
    const el = document.querySelector('[data-pkey]');
    if (el) return el.getAttribute('data-pkey');
    for (const f of document.querySelectorAll('iframe')) {
      const s = f.src || '';
      if (!/arkoselabs|funcaptcha|client-api\.arkoselabs/i.test(s)) continue;
      const m = s.match(/[?&](?:pk|publickey)=([^&]+)/i);
      if (m) return decodeURIComponent(m[1]);
    }
    return null;
  }

  // Predicate MUST match callRecaptchaCallback in INJECT_JS_TEMPLATE (no 'promise').
  function hasRecaptchaCallback() {
    try {
      const cfg = window.___grecaptcha_cfg;
      if (!cfg || !cfg.clients) return false;
      const seen = typeof WeakSet !== 'undefined' ? new WeakSet() : null;
      function walk(o, depth) {
        if (!o || typeof o !== 'object' || depth > 8) return false;
        try { if (seen) { if (seen.has(o)) return false; seen.add(o); } } catch (e) { return false; }
        let keys;
        try { keys = Object.getOwnPropertyNames(o); } catch (e) { return false; }
        for (const k of keys) {
          let v;
          try { v = o[k]; } catch (e) { continue; }
          if (typeof v === 'function' && /callback/i.test(k)) return true;
          if (v && typeof v === 'object' && walk(v, depth + 1)) return true;
        }
        return false;
      }
      for (const c of Object.values(cfg.clients)) {
        if (walk(c, 0)) return true;
      }
    } catch (e) {}
    return false;
  }

  function hasCaptchaForm() {
    function check(doc) {
      if (!doc) return false;
      const form = doc.querySelector('form#captcha-challenge, form[action*="challenge/verify"]');
      if (!form) return false;
      return !!(form.querySelector('#g-recaptcha-response, textarea[name="g-recaptcha-response"]')
                || doc.querySelector('#g-recaptcha-response, textarea[name="g-recaptcha-response"]'));
    }
    return check(document) || check(captchaInternalDoc());
  }

  function hasArkoseCallback() {
    try {
      if (typeof window.arkoseCallback === 'function') return true;
      if (window.ArkoseEnforcement || window.remoteSetup) return true;
      if (document.querySelector('input[name="fc-token"], #verification-token, input[name="verification-token"]'))
        return true;
      const doc = captchaInternalDoc();
      if (doc && doc.querySelector('input[name="fc-token"], #verification-token, input[name="verification-token"]'))
        return true;
    } catch (e) {}
    return false;
  }

  const rm = firstRecaptchaMeta();
  const rk = rm && rm.key;
  const ak = firstArkoseKey();
  const arkoseReady = !!(ak && hasArkoseCallback());
  const recaptchaReady = !!(rk && (hasRecaptchaCallback() || hasCaptchaForm()));
  // Prefer whichever family has a usable completion path.
  if (arkoseReady) {
    out.family = 'arkose';
    out.key = ak;
    out.callback = true;
  } else if (recaptchaReady) {
    out.family = 'recaptcha';
    out.key = rk;
    out.enterprise = !!(rm && rm.enterprise);
    out.callback = true;
  } else if (ak) {
    out.family = 'arkose';
    out.key = ak;
    out.callback = false;
  } else if (rk) {
    out.family = 'recaptcha';
    out.key = rk;
    out.enterprise = !!(rm && rm.enterprise);
    out.callback = false;
  }
  return out;
})()
"""

# Post-inject settle probe, Python polls this for ~8–10s (no /login/resume yet).
SETTLE_JS = "({href: location.href, ready: document.readyState})"

# Strict verify: challenge-phrase text + visible password field (deskd vis heuristic).
VERIFY_JS = r"""
(() => {
  const text = ((document.body && document.body.innerText) || '').slice(0, 5000);
  const href = location.href || '';
  const vis = e => e && !e.disabled && e.offsetParent !== null;
  let hasPassword = false;
  try {
    hasPassword = !![...document.querySelectorAll('input[type="password"]')].find(vis);
  } catch (e) {
    hasPassword = !!document.querySelector('input[type="password"]');
  }
  return {text: text + ' ' + href, hasPassword: !!hasPassword, href: href};
})()
"""

# Post-login gate probe. deskd's classify() reads only the top document's innerText,
# so LinkedIn's /checkpoint/challenge page, whose reCAPTCHA lives in an iframe and
# whose visible heading is "Let's do a quick security check", matches none of its
# phrases and, with the password field gone, is reported as a successful login. The
# session is not real: the next navigation bounces back to /login. This catches that
# state from the control plane, so the DBC path can run without an image rebuild.
GATE_JS = r"""
(() => {
  const href = location.href || '';
  const text = ((document.body && document.body.innerText) || '').slice(0, 3000);
  let framed = false;
  try {
    framed = [...document.querySelectorAll('iframe')].some(
      f => /recaptcha|arkoselabs|funcaptcha|captchaInternal/i.test(f.src || ''));
  } catch (e) {}
  const gated = /\/checkpoint\/challenge/i.test(href)
    || /quick security check|security verification|verify you.?re human|not a robot|captcha/i.test(text)
    || framed;
  return {gated: !!gated, href: href, framed: !!framed};
})()
"""


def gate_open(info) -> bool:
    """True when a post-'success' login is really still sitting on a captcha gate."""
    return bool(isinstance(info, dict) and info.get("gated"))


# TOKEN_PLACEHOLDER is replaced with json.dumps(token) by inject().
INJECT_JS_TEMPLATE = r"""
(() => {
  const token = TOKEN_PLACEHOLDER;
  const family = FAMILY_PLACEHOLDER;

  function captchaInternalDoc() {
    try {
      const iframe = document.querySelector('iframe[src*="captchaInternal"]');
      return iframe && (iframe.contentDocument || iframe.contentWindow.document);
    } catch (e) { return null; }
  }

  function setRecaptchaFields(tok) {
    let wrote = 0;
    function write(doc) {
      if (!doc) return;
      for (const el of doc.querySelectorAll('#g-recaptcha-response, textarea[name="g-recaptcha-response"]')) {
        el.value = tok;
        el.innerHTML = tok;
        try { el.dispatchEvent(new Event('input', {bubbles: true})); } catch (e) {}
        wrote++;
      }
    }
    write(document);
    write(captchaInternalDoc());
    return wrote > 0;
  }

  // Predicate MUST match hasRecaptchaCallback in DETECT_JS (callback name only).
  function callRecaptchaCallback(tok) {
    const cfg = window.___grecaptcha_cfg;
    if (!cfg || !cfg.clients) return false;
    const seen = typeof WeakSet !== 'undefined' ? new WeakSet() : null;
    function walk(o, depth) {
      if (!o || typeof o !== 'object' || depth > 8) return false;
      try { if (seen) { if (seen.has(o)) return false; seen.add(o); } } catch (e) { return false; }
      let keys;
      try { keys = Object.getOwnPropertyNames(o); } catch (e) { return false; }
      for (const k of keys) {
        let v;
        try { v = o[k]; } catch (e) { continue; }
        if (typeof v === 'function' && /callback/i.test(k)) {
          try { v(tok); return true; } catch (e) {}
        }
        if (v && typeof v === 'object' && walk(v, depth + 1)) return true;
      }
      return false;
    }
    for (const c of Object.values(cfg.clients)) {
      if (walk(c, 0)) return true;
    }
    return false;
  }

  function submitCaptchaForm() {
    function trySubmit(doc) {
      if (!doc) return false;
      const form = doc.querySelector('form#captcha-challenge, form[action*="challenge/verify"]');
      if (!form) return false;
      try {
        if (typeof form.requestSubmit === 'function') form.requestSubmit();
        else form.submit();
        return true;
      } catch (e) { return false; }
    }
    return trySubmit(document) || trySubmit(captchaInternalDoc());
  }

  function injectArkose(tok) {
    let wrote = false;
    function write(doc) {
      if (!doc) return;
      for (const sel of ['input[name="fc-token"]', '#verification-token',
                         'input[name="verification-token"]', '#fc-token']) {
        const el = doc.querySelector(sel);
        if (el) { el.value = tok; wrote = true; }
      }
    }
    write(document);
    write(captchaInternalDoc());
    if (typeof window.arkoseCallback === 'function') {
      try { window.arkoseCallback(tok); return {ok: true, method: 'arkose_callback'}; }
      catch (e) { return {ok: false, error: 'arkose_callback_threw'}; }
    }
    try {
      const enforcer = window.ArkoseEnforcement || window.remoteSetup;
      if (enforcer && typeof enforcer.setConfig === 'function') { /* no-op probe */ }
    } catch (e) {}
    if (wrote) {
      if (submitCaptchaForm()) return {ok: true, method: 'arkose_form'};
      return {ok: false, error: 'arkose_no_callback'};
    }
    return {ok: false, error: 'arkose_no_field'};
  }

  if (family === 'arkose') return injectArkose(token);

  const wrote = setRecaptchaFields(token);
  if (callRecaptchaCallback(token)) return {ok: true, method: 'recaptcha_callback'};
  if (!wrote) return {ok: false, error: 'field_gone'};
  if (submitCaptchaForm()) return {ok: true, method: 'recaptcha_form'};
  return {ok: false, error: 'no_callback'};
})()
"""


def enabled() -> bool:
    """True only when DBC credentials are present (env-gated per box)."""
    if (os.environ.get("CASE_DBC_AUTHTOKEN") or "").strip():
        return True
    user = (os.environ.get("CASE_DBC_USERNAME") or "").strip()
    pw = (os.environ.get("CASE_DBC_PASSWORD") or "").strip()
    return bool(user and pw)


def _auth_fields() -> dict:
    tok = (os.environ.get("CASE_DBC_AUTHTOKEN") or "").strip()
    if tok:
        return {"authtoken": tok}
    return {
        "username": (os.environ.get("CASE_DBC_USERNAME") or "").strip(),
        "password": (os.environ.get("CASE_DBC_PASSWORD") or "").strip(),
    }


def _proxy_fields() -> dict:
    """Omit proxy/proxytype entirely when proxy is empty, DBC rejects blank proxy."""
    proxy = (os.environ.get("CASE_DBC_PROXY") or "").strip()
    if not proxy:
        return {}
    proxytype = (os.environ.get("CASE_DBC_PROXYTYPE") or "HTTP").strip() or "HTTP"
    return {"proxy": proxy, "proxytype": proxytype}


def inject_js(family: str, token: str) -> str:
    """Build inject expression with token/family embedded as JSON literals."""
    return (INJECT_JS_TEMPLATE
            .replace("TOKEN_PLACEHOLDER", json.dumps(token))
            .replace("FAMILY_PLACEHOLDER", json.dumps(family)))


def still_challenge(page_text: str, has_password: bool = False) -> bool:
    """True unless verify is clean: no challenge/fail phrases AND no password field.

    Anything short of that is a failure → caller reports + falls through to handoff.
    """
    if has_password:
        return True
    blob = page_text or ""
    return bool(RE_VERIFY.search(blob) or RE_FAIL_VERIFY.search(blob))


class SolveResult(TypedDict):
    id: str
    token: str


class Solver(Protocol):
    """Optional auto-solver. Only declared capabilities may leave the box."""

    capabilities: frozenset[str]

    def solve(self, capability: str, pageurl: str, key: str, *,
              timeout_s: float = DBC_POLL_CAP_S) -> SolveResult | None: ...


def capability_for(family: str, *, enterprise: bool = False) -> str | None:
    """Map detect-family (+ enterprise flag) → declared capability name, or None."""
    if family == "recaptcha":
        return "recaptcha_enterprise" if enterprise else "recaptcha_v2"
    if family == "arkose":
        return "arkose"
    return None


def _dbc_overload(status_code: int, parsed) -> bool:
    if status_code == 503:
        return True
    return isinstance(parsed, dict) and parsed.get("error") == "service-overload"


def _dbc_solve(capability: str, pageurl: str, key: str, *,
               timeout_s: float = DBC_POLL_CAP_S) -> SolveResult | None:
    """Upload to DBC and poll until solved, refused, or the wall-clock budget is spent.

    Terminal answers (is_correct=false, service-overload) return immediately, no long
    poll that delays the human handoff. Never logs token/password.
    """
    if capability not in DBC_CAPABILITIES or not pageurl or not key:
        return None
    if not enabled():
        return None

    deadline = time.time() + max(1.0, float(timeout_s))
    proxy = _proxy_fields()
    if capability == "recaptcha_enterprise":
        if not proxy:
            log.warning("captcha_auto=fail dbc_enterprise_needs_proxy")
            return None
        # Try type 25 first; FAQ #18 allows type 4 for some Enterprise keys on
        # non-terminal upload failure. Overload itself is terminal (no fallback poll).
        payload = {"googlekey": key, "pageurl": pageurl, **proxy}
        attempts = [("25", "token_enterprise_params"), ("4", "token_params")]
    elif capability == "recaptcha_v2":
        payload = {"googlekey": key, "pageurl": pageurl, **proxy}
        attempts = [("4", "token_params")]
    else:  # arkose
        payload = {"publickey": key, "pageurl": pageurl, **proxy}
        attempts = [("6", "funcaptcha_params")]

    body, captcha_id, used_type = None, None, None
    for dbc_type, param in attempts:
        remaining = deadline - time.time()
        if remaining <= 0.5:
            break
        data = dict(_auth_fields())
        data["type"] = dbc_type
        data[param] = json.dumps(payload)
        try:
            r = requests.post(DBC_UPLOAD, data=data, timeout=min(30.0, max(0.5, remaining)),
                              headers={"Accept": "application/json"})
        except requests.RequestException as e:
            log.warning("captcha_auto=fail dbc_upload_error=%s", type(e).__name__)
            continue
        try:
            parsed = r.json()
        except ValueError:
            parsed = None
        if _dbc_overload(r.status_code, parsed):
            # Terminal: do not fall through to another type or burn the poll budget.
            log.warning("captcha_auto=fail dbc_overload type=%s", dbc_type)
            return None
        if isinstance(parsed, dict) and parsed.get("captcha"):
            body, captcha_id, used_type = parsed, parsed["captcha"], dbc_type
            break
        log.warning("captcha_auto=fail dbc_upload_status=%s type=%s", r.status_code, dbc_type)

    if not captcha_id:
        return None
    if used_type != attempts[0][0]:
        log.info("captcha_auto=fallback dbc_type=%s", used_type)

    # Immediate solve (rare), text present on upload response.
    text = (body.get("text") or "").strip()
    if body.get("is_correct") and text:
        return {"id": str(captcha_id), "token": text}

    polls = 0
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        time.sleep(min(POLL_INTERVAL_S, remaining))
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        try:
            pr = requests.get(f"{DBC_UPLOAD}/{captcha_id}",
                              timeout=min(15.0, max(0.5, remaining)),
                              headers={"Accept": "application/json"})
            pb = pr.json()
        except (requests.RequestException, ValueError):
            continue
        polls += 1
        text = (pb.get("text") or "").strip()
        # DBC's terminal "cannot solve" answer: is_correct flips to false (observed with
        # text="?" at ~17s on LinkedIn's Enterprise key). Polling on burns the rest of
        # the budget for nothing and delays the human handoff, stop here.
        if pb.get("is_correct") is False:
            log.warning("captcha_auto=fail dbc_unsolvable type=%s", used_type)
            return None
        if pb.get("is_correct") and text:
            return {"id": str(captcha_id), "token": text}

    log.warning("captcha_auto=fail dbc_timeout id_present=1")
    return None


class DbcSolver:
    """Death By Captcha, the only Solver implementation this release."""

    capabilities = DBC_CAPABILITIES

    def solve(self, capability: str, pageurl: str, key: str, *,
              timeout_s: float = DBC_POLL_CAP_S) -> SolveResult | None:
        if capability not in self.capabilities:
            return None
        return _dbc_solve(capability, pageurl, key, timeout_s=timeout_s)


_DEFAULT_SOLVER = DbcSolver()


def solve_if_capable(family: str, pageurl: str, key: str, *, enterprise: bool = False,
                     timeout_s: float = DBC_POLL_CAP_S,
                     solver: Solver | None = None) -> SolveResult | None:
    """Capability boundary: solve only when the adapter declares the family.

    Returns None immediately (no DBC HTTP, no long poll) when disabled, the family
    is unsupported, or enterprise lacks a configured proxy, caller creates email
    handoff. Min egress: site key, page URL, configured proxy only.
    """
    if not enabled():
        return None
    if not pageurl or not key:
        return None
    cap = capability_for(family, enterprise=enterprise)
    impl = solver if solver is not None else _DEFAULT_SOLVER
    if cap is None or cap not in impl.capabilities:
        log.info("captcha_auto=skip reason=unsupported_capability family=%s", family)
        return None
    if cap == "recaptcha_enterprise" and not _proxy_fields():
        log.warning("captcha_auto=fail dbc_enterprise_needs_proxy")
        return None
    return impl.solve(cap, pageurl, key, timeout_s=timeout_s)


def report(captcha_id) -> None:
    """Ask DBC to refund a bad solve. Best-effort; never raises."""
    if not captcha_id or not enabled():
        return
    try:
        requests.post(f"{DBC_UPLOAD}/{captcha_id}/report", data=_auth_fields(),
                      timeout=15, headers={"Accept": "application/json"})
    except requests.RequestException as e:
        log.warning("captcha report failed: %s", type(e).__name__)
