#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""deskd — in-container desk daemon (API_SPEC.md §4).

Owns the display: screenshot, input, exec, file I/O, credential injection,
blocker detection. cased-only, bearer DESK_TOKEN. Secrets are never logged.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import struct
import subprocess
import threading
import time
import zlib
from collections import deque
from urllib.parse import urlparse

import requests
import uvicorn
import websocket
from fastapi import Body, FastAPI, Request
from fastapi.responses import JSONResponse, Response

TOKEN = os.environ["DESK_TOKEN"]
CDP = "http://127.0.0.1:9222"
CAP = 64 * 1024
CAPTURE_CAP = 256 * 1024   # per captured response body; GraphQL timelines run 100–500 KB

app = FastAPI()

state = {
    "injecting": False,   # screenshots 423 while true
    "login": None,        # pending challenge ctx for /login/resume
    "in_login": False,    # suppress blocker watchdog during login flows
    "blocker": None,      # {"kind","prompt","fingerprint"} or None
    "capture": None,      # {"thread","stop","buf","pattern","error"} or None
}


def err(status, code, message):
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


@app.middleware("http")
async def auth(request: Request, call_next):
    got = request.headers.get("authorization") or ""
    if not hmac.compare_digest(got, f"Bearer {TOKEN}"):
        return err(401, "unauthorized", "bad or missing DESK_TOKEN")
    return await call_next(request)


def denv():
    e = dict(os.environ)
    e["DISPLAY"] = os.environ.get("DISPLAY", ":0")
    return e


FB = "/dev/shm/Xvfb_screen0"   # Xvfb -fbdir maps the framebuffer here, XWD format


def _png_chunk(tag, body):
    c = tag + body
    return struct.pack(">I", len(body)) + c + struct.pack(">I", zlib.crc32(c))


def grab():
    # Read the live framebuffer straight from Xvfb's mmap — no X round trip, no
    # subprocess, no image library. A frame caught mid-draw can tear; acceptable
    # for agent screenshots and it is what a human eye sees on a real screen too.
    with open(FB, "rb") as f:
        data = f.read()
    (hdr_size, version, pix_fmt, _d, width, height, _xo, byte_order, _u, _bo,
     _p, bpp, bpl, _v, rmask, gmask, bmask, _b, _c, ncolors) = struct.unpack(">20I", data[:80])
    if not (version == 7 and pix_fmt == 2 and bpp == 32):
        raise RuntimeError(f"unexpected XWD format v{version} fmt{pix_fmt} bpp{bpp}")
    off = hdr_size + ncolors * 12
    mv = memoryview(data)
    std = byte_order == 0 and (rmask, gmask, bmask) == (0xFF0000, 0xFF00, 0xFF)
    raw = bytearray()
    for y in range(height):
        row = mv[off + y * bpl: off + y * bpl + width * 4]
        raw.append(0)  # PNG row filter: none
        rgb = bytearray(width * 3)
        if std:  # BGRX in memory: C-speed slice shuffle
            rgb[0::3] = row[2::4]
            rgb[1::3] = row[1::4]
            rgb[2::3] = row[0::4]
        else:    # generic masks/endianness, per-pixel
            rs = (rmask & -rmask).bit_length() - 1
            gs = (gmask & -gmask).bit_length() - 1
            bs = (bmask & -bmask).bit_length() - 1
            for i, px in enumerate(struct.unpack((">" if byte_order else "<") + f"{width}I", row)):
                rgb[i * 3] = (px >> rs) & 0xFF
                rgb[i * 3 + 1] = (px >> gs) & 0xFF
                rgb[i * 3 + 2] = (px >> bs) & 0xFF
        raw += rgb
    return (b"\x89PNG\r\n\x1a\n"
            + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + _png_chunk(b"IDAT", zlib.compress(bytes(raw), 6))
            + _png_chunk(b"IEND", b""))


@app.get("/health")
def health():
    try:
        chrome = requests.get(f"{CDP}/json/version", timeout=2).ok
    except Exception:
        chrome = False
    return {"ok": True, "chrome": chrome}


@app.get("/screenshot")
def screenshot():
    if state["injecting"]:
        return err(423, "credential_injection", "screenshots blocked during credential injection")
    return Response(grab(), media_type="image/png")


# ---------- actions ----------

def xdo(*args, timeout=15):
    subprocess.run(["xdotool", *args], env=denv(), check=True, timeout=timeout)


def do_action(a):
    t = a["type"]
    if t == "click":
        btn = {"left": "1", "middle": "2", "right": "3"}[a.get("button", "left")]
        xdo("mousemove", str(int(a["x"])), str(int(a["y"])))
        xdo("click", btn)
    elif t == "double_click":
        xdo("mousemove", str(int(a["x"])), str(int(a["y"])))
        xdo("click", "--repeat", "2", "--delay", "80", "1")
    elif t == "move":
        xdo("mousemove", str(int(a["x"])), str(int(a["y"])))
    elif t == "drag":
        f, to = a["from"], a["to"]
        xdo("mousemove", str(int(f["x"])), str(int(f["y"])))
        xdo("mousedown", "1")
        xdo("mousemove", "--sync", str(int((f["x"] + to["x"]) // 2)), str(int((f["y"] + to["y"]) // 2)))
        xdo("mousemove", "--sync", str(int(to["x"])), str(int(to["y"])))
        time.sleep(0.05)
        xdo("mouseup", "1")
    elif t == "scroll":
        xdo("mousemove", str(int(a["x"])), str(int(a["y"])))
        dy = int(a["dy"])
        if dy:
            xdo("click", "--repeat", str(min(abs(dy), 50)), "--delay", "40", "5" if dy > 0 else "4")
    elif t == "type":
        text = str(a["text"])
        # a killed xdotool leaves text half-typed and the caller retrying the
        # whole thing — scale the timeout so long texts can't hit it
        xdo("type", "--delay", "15", "--", text, timeout=15 + len(text) // 10)
    elif t == "key":
        xdo("key", "--", str(a["keys"]))
    elif t == "wait":
        time.sleep(min(int(a["ms"]), 5000) / 1000)
    else:
        raise ValueError(f"unknown action type {t!r}")


@app.post("/action")
def action(a: dict = Body(...)):
    try:
        do_action(a)
    except KeyError as e:
        return err(400, "bad_action", f"missing field {e}")
    except Exception as e:
        return err(400, "bad_action", str(e))
    out = {"ok": True}
    if a.get("screenshot"):
        time.sleep(min(int(a.get("delay_ms", 300)), 5000) / 1000)
        if state["injecting"]:
            return err(423, "credential_injection", "screenshots blocked during credential injection")
        out["screenshot_png_b64"] = base64.b64encode(grab()).decode()
    return out


# ---------- exec & files ----------

@app.post("/exec")
def exec_(b: dict = Body(...)):
    if "command" not in b:
        return err(400, "bad_request", "command required")
    timeout = min(int(b.get("timeout_s", 30)), 600)
    cwd = b.get("cwd", "/home/agent")
    try:
        p = subprocess.run(["bash", "-c", b["command"]], cwd=cwd, env=denv(),
                           capture_output=True, timeout=timeout)
        code, out, errb = p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired as e:
        code, out = 124, e.stdout or b""
        errb = (e.stderr or b"") + b"\n[deskd] command timed out"
    except NotADirectoryError:
        return err(400, "bad_cwd", f"no such directory: {cwd}")
    truncated = len(out) > CAP or len(errb) > CAP
    return {"exit_code": code, "stdout": out[:CAP].decode(errors="replace"),
            "stderr": errb[:CAP].decode(errors="replace"), "truncated": truncated}


@app.put("/file")
async def file_put(request: Request, path: str):
    data = await request.body()
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    return JSONResponse({"path": path, "bytes": len(data)}, status_code=201)


@app.get("/file")
def file_get(path: str):
    if not os.path.isfile(path):
        return err(404, "not_found", path)
    with open(path, "rb") as f:
        return Response(f.read(), media_type="application/octet-stream")


# ---------- CDP ----------

class Tab:
    """One websocket to the most-recently-active Chromium page."""

    def __init__(self):
        pages = [t for t in requests.get(f"{CDP}/json/list", timeout=5).json() if t["type"] == "page"]
        if not pages:
            raise RuntimeError("no chromium page target")
        # suppress_origin: chromium 136+ rejects CDP websockets with an Origin header
        self.ws = websocket.create_connection(pages[0]["webSocketDebuggerUrl"], timeout=30,
                                              suppress_origin=True)
        self._id = 0

    def cmd(self, method, **params):
        self._id += 1
        self.ws.send(json.dumps({"id": self._id, "method": method, "params": params}))
        while True:
            m = json.loads(self.ws.recv())
            if m.get("id") == self._id:
                if "error" in m:
                    raise RuntimeError(f"CDP {method}: {m['error'].get('message')}")
                return m.get("result", {})

    def js(self, expr):
        r = self.cmd("Runtime.evaluate", expression=expr, returnByValue=True)
        return r.get("result", {}).get("value")

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def navigate(tab, url, timeout=25):
    tab.cmd("Page.navigate", url=url)
    t0 = time.time()
    while time.time() - t0 < timeout:
        if tab.js("document.readyState") == "complete":
            return
        time.sleep(0.3)


def settle(tab, extra=2.0, timeout=15):
    t0 = time.time()
    time.sleep(0.5)
    while time.time() - t0 < timeout:
        if tab.js("document.readyState") == "complete":
            break
        time.sleep(0.3)
    time.sleep(extra)


def wait_login_fields(tab, timeout=15.0):
    """Poll for visible login inputs. SPAs often hit readyState=complete before React mounts the form."""
    t0 = time.time()
    fields = {}
    while time.time() - t0 < timeout:
        fields = tab.js(HAS_FIELDS) or {}
        if fields.get("user") or fields.get("pass"):
            return fields
        time.sleep(0.3)
    return fields


def wait_post_submit(tab, timeout=20.0):
    """After password submit, wait for a challenge page or a clear leave from login chrome.

    SPAs (Instagram codeentry especially) can unmount login inputs while leaving
    login-form copy in the DOM, then paint an email-code wall a few seconds later.
    Returning success in that gap makes cased prove→unverified with no handoff.
    """
    t0 = time.time()
    login_chrome = re.compile(r"(log ?in|sign ?in|password|username|mobile number)", re.I)
    while time.time() - t0 < timeout:
        text = tab.js(PAGE_TEXT) or ""
        fields = tab.js(HAS_FIELDS) or {}
        href = tab.js("location.href") or ""
        if RE_CAPTCHA.search(text) or RE_OTP.search(text) or RE_APPROVAL.search(text):
            return "challenge"
        if "/codeentry" in href or "/challenge" in href or "/checkpoint" in href:
            return "challenge"
        if fields.get("pass") or fields.get("user"):
            time.sleep(0.4)
            continue
        if login_chrome.search(text):
            time.sleep(0.4)
            continue
        return "settled"
    return "timeout"


def press_enter(tab):
    for typ in ("rawKeyDown", "char", "keyUp"):
        kw = {"type": typ, "key": "Enter", "code": "Enter",
              "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13}
        if typ == "char":
            kw["text"] = "\r"
        tab.cmd("Input.dispatchKeyEvent", **kw)


@app.post("/eval")
def eval_(b: dict = Body(...)):
    if state["injecting"]:
        return err(423, "credential_injection", "eval blocked during credential injection")
    if "expression" not in b:
        return err(400, "bad_request", "body needs 'expression'")
    timeout = min(int(b.get("timeout_s", 20)), 120)
    try:
        tab = Tab()
        try:
            tab.ws.settimeout(timeout + 5)
            r = tab.cmd("Runtime.evaluate", expression=b["expression"],
                        returnByValue=True, awaitPromise=True, timeout=timeout * 1000)
        finally:
            tab.close()
    except Exception as e:
        return err(502, "eval_error", f"{type(e).__name__}: {e}")
    exc = r.get("exceptionDetails")
    if exc:
        desc = exc.get("exception", {}).get("description") or exc.get("text", "js exception")
        return {"ok": False, "error": str(desc)[:2000]}
    val = r.get("result", {}).get("value")
    enc = json.dumps(val, default=str)
    if len(enc) > CAP:
        return {"ok": True, "value": enc[:CAP], "truncated": True}
    return {"ok": True, "value": val, "truncated": False}


# offsetParent alone is not enough: X/Twitter keeps an opacity:0 password input on the
# email step (display:block, non-null offsetParent). Treating that as visible made login
# one-step and typed the password into a hidden field while the human still saw email-only
# — or concatenated into the focused text box when focus did not stick.
VIS = ("const vis=e=>{if(!e||e.disabled||e.offsetParent===null)return false;"
       "const st=getComputedStyle(e);if(st.display==='none'||st.visibility==='hidden'"
       "||Number(st.opacity)===0)return false;"
       "const r=e.getBoundingClientRect();return r.width>0&&r.height>0;};")
USER_SEL = ('input[autocomplete="username"],input[type="email"],input[name*="user" i],'
            'input[name*="email" i],input[name*="login" i],input[id*="user" i],'
            'input[id*="email" i],input[id*="login" i],input[type="text"]')
CODE_SEL = ('input[autocomplete="one-time-code"],input[name*="otp" i],input[name*="code" i],'
            'input[id*="otp" i],input[id*="code" i],input[type="tel"],input[type="number"],'
            'input[type="text"]')
HAS_FIELDS = (f"(()=>{{{VIS}"
              f"const p=[...document.querySelectorAll('input[type=\"password\"]')].find(vis);"
              f"const u=[...document.querySelectorAll('{USER_SEL}')].find(vis);"
              "return {user:!!u, pass:!!p};})()")
# select() so Input.insertText replaces rather than concatenating on a reused focus.
FOCUS_USER = (f"(()=>{{{VIS}const u=[...document.querySelectorAll('{USER_SEL}')].find(vis);"
              "if(!u)return false; u.focus(); if(u.select)u.select(); return true;})()")
FOCUS_PASS = (f"(()=>{{{VIS}const p=[...document.querySelectorAll('input[type=\"password\"]')].find(vis);"
              "if(!p)return false; p.focus(); if(p.select)p.select(); return true;})()")
FOCUS_CODE = (f"(()=>{{{VIS}const c=[...document.querySelectorAll('{CODE_SEL}')].find(vis);"
              "if(!c)return false; c.focus(); if(c.select)c.select(); return true;})()")
PAGE_TEXT = "(document.body ? document.body.innerText.slice(0, 5000) : '')"

# Generic auth observation — no website names. challenge_signals are re-derived in
# Python (challenge_signals_from_text) so unit tests own the phrase map; JS still
# emits the full dict shape for a self-contained CDP eval.
OBSERVE_AUTH_JS = r"""
(() => {
  const vis = e => {
    if (!e || e.disabled || e.offsetParent === null) return false;
    const st = getComputedStyle(e);
    if (st.display === 'none' || st.visibility === 'hidden' || Number(st.opacity) === 0) return false;
    const r = e.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const userSel = 'input[autocomplete="username"],input[type="email"],input[name*="user" i],input[name*="email" i],input[name*="login" i],input[id*="user" i],input[id*="email" i],input[id*="login" i],input[type="text"]';
  const codeSel = 'input[autocomplete="one-time-code"],input[name*="otp" i],input[name*="code" i],input[id*="otp" i],input[id*="code" i],input[type="tel"],input[type="number"],input[type="text"]';
  const u = [...document.querySelectorAll(userSel)].find(vis);
  const p = [...document.querySelectorAll('input[type="password"]')].find(vis);
  const c = [...document.querySelectorAll(codeSel)].find(vis);
  const srcs = [...document.querySelectorAll('iframe')].map(f => f.src || '');
  const html = document.documentElement ? document.documentElement.innerHTML.slice(0, 200000) : '';
  const recaptcha = srcs.some(s => /recaptcha|google\.com\/recaptcha/i.test(s))
    || !!document.querySelector('.g-recaptcha, [data-sitekey]')
    || /grecaptcha/i.test(html);
  const hcaptcha = srcs.some(s => /hcaptcha/i.test(s))
    || !!document.querySelector('.h-captcha, [data-hcaptcha-sitekey]');
  const arkose = srcs.some(s => /arkoselabs|funcaptcha/i.test(s))
    || !!document.querySelector('[data-pkey]');
  const turnstile = srcs.some(s => /challenges\.cloudflare\.com|turnstile/i.test(s))
    || !!document.querySelector('.cf-turnstile, [data-turnstile-sitekey]');
  const generic_captcha = (!recaptcha && !hcaptcha && !arkose && !turnstile) && (
    srcs.some(s => /captcha/i.test(s))
    || !!document.querySelector('[class*="captcha" i], [id*="captcha" i]'));
  const text = (document.body ? document.body.innerText : '').slice(0, 5000);
  const signals = [];
  if (/captcha|verify you.?re human|not a robot/i.test(text)) signals.push('captcha');
  if (/two.?factor|\b2fa\b|one.?time|verification code|authentication code|enter the code|\b\d\s?-?\s?digit code/i.test(text))
    signals.push('otp');
  if (/approve this|check your (phone|device)|confirm (it.?s you|this sign)|waiting for approval/i.test(text))
    signals.push('approval');
  if (/verify your email|confirm your email|check your email|email verification|we sent .{0,40}email|click the link .{0,40}email/i.test(text))
    signals.push('email_verify');
  if (/\bpasskey\b|security key|webauthn|use (your )?(fingerprint|face)|biometric/i.test(text))
    signals.push('passkey');
  if ((recaptcha || hcaptcha || arkose || turnstile || generic_captcha) && !signals.includes('captcha'))
    signals.push('captcha');
  return {
    href: location.href,
    ready: document.readyState === 'complete',
    title: document.title || '',
    visible_fields: {user: !!u, pass: !!p, code: !!c},
    frame_markers: {recaptcha, hcaptcha, arkose, turnstile, generic_captcha},
    challenge_signals: signals,
    page_state: text.slice(0, 500),
  };
})()
"""

# Watchdog RE_BLOCK scans arbitrary page text (DMs, feeds, drafts). Bare "2fa" / "captcha"
# false-positive on outreach copy ("asking about 2fa and captcha flows") and spam handoff
# emails. Keep challenge phrasing only. Login classify still uses RE_OTP / RE_CAPTCHA.
RE_BLOCK = re.compile(r"verify you.?re human|two.?factor|one.?time (code|password)|"
                      r"enter the code|unusual activity|suspicious login|"
                      r"(solve|complete|pass).{0,24}captcha|not a robot", re.I)
RE_CAPTCHA = re.compile(r"captcha|verify you.?re human|not a robot", re.I)
# \b2fa\b, never bare "2fa" in URL blobs: "%2Fa" spells 2fa case-insensitively.
RE_OTP = re.compile(r"two.?factor|\b2fa\b|one.?time|verification code|authentication code|"
                    r"enter the code|\b\d\s?-?\s?digit code", re.I)
RE_APPROVAL = re.compile(r"approve this|check your (phone|device)|confirm (it.?s you|this sign)|"
                         r"waiting for approval", re.I)
RE_EMAIL_VERIFY = re.compile(r"verify your email|confirm your email|check your email|"
                             r"email verification|we sent .{0,40}email|"
                             r"click the link .{0,40}email", re.I)
RE_PASSKEY = re.compile(r"\bpasskey\b|security key|webauthn|"
                        r"use (your )?(fingerprint|face)|biometric", re.I)
RE_FAIL = re.compile(r"incorrect|invalid|wrong password|try again|couldn.?t find|doesn.?t match", re.I)

# Stable tag order for generic challenge_signals (phrase-based; never site names).
_SIGNAL_RULES = (
    ("captcha", RE_CAPTCHA),
    ("otp", RE_OTP),
    ("approval", RE_APPROVAL),
    ("email_verify", RE_EMAIL_VERIFY),
    ("passkey", RE_PASSKEY),
)


def challenge_signals_from_text(text, frame_markers=None):
    """Map page text (+ optional frame markers) → generic challenge_signals tags."""
    blob = text or ""
    out = []
    for tag, rx in _SIGNAL_RULES:
        if rx.search(blob):
            out.append(tag)
    fm = frame_markers or {}
    if any(fm.get(k) for k in ("recaptcha", "hcaptcha", "arkose", "turnstile", "generic_captcha")):
        if "captcha" not in out:
            out.insert(0, "captcha")
    return out


def finalize_auth_observation(raw):
    """Normalize a JS observation dict; Python owns challenge_signals + page_state cap."""
    obs = dict(raw or {})
    page = obs.get("page_state") or ""
    if not isinstance(page, str):
        page = str(page)
    page = page[:500]
    fm = obs.get("frame_markers") or {}
    if not isinstance(fm, dict):
        fm = {}
    vf = obs.get("visible_fields") or {}
    if not isinstance(vf, dict):
        vf = {}
    return {
        "href": obs.get("href") or "",
        "ready": bool(obs.get("ready")),
        "title": obs.get("title") or "",
        "visible_fields": {
            "user": bool(vf.get("user")),
            "pass": bool(vf.get("pass")),
            "code": bool(vf.get("code")),
        },
        "frame_markers": {
            "recaptcha": bool(fm.get("recaptcha")),
            "hcaptcha": bool(fm.get("hcaptcha")),
            "arkose": bool(fm.get("arkose")),
            "turnstile": bool(fm.get("turnstile")),
            "generic_captcha": bool(fm.get("generic_captcha")),
        },
        "challenge_signals": challenge_signals_from_text(page, fm),
        "page_state": page,
    }


def snippet(text, rx):
    m = rx.search(text)
    if not m:
        return text[:120].strip().replace("\n", " ")
    lo = max(0, m.start() - 60)
    return text[lo:m.end() + 60].strip().replace("\n", " ")


def totp(seed, at=None):
    # RFC-6238; mirrored in control-plane/auth_attempts._totp (image cannot import it).
    key = base64.b32decode(seed.replace(" ", "").upper(), casefold=True)
    ctr = int((at or time.time()) // 30)
    h = hmac.new(key, struct.pack(">Q", ctr), hashlib.sha1).digest()
    o = h[-1] & 15
    return str((int.from_bytes(h[o:o + 4], "big") & 0x7FFFFFFF) % 10 ** 6).zfill(6)


def fill(tab, focus_js, text):
    if not tab.js(focus_js):
        raise RuntimeError("field not found")
    tab.cmd("Input.insertText", text=text)


def domain_ok(host, domains):
    host = (host or "").lower()
    return any(host == d.lower() or host.endswith("." + d.lower()) for d in domains)


def challenge(tab, cred, kind, prompt):
    state["login"] = {"kind": kind, "cred_name": cred["name"], "at": time.time()}
    try:
        shot = base64.b64encode(grab()).decode()
    except Exception:
        shot = None
    return {"status": "challenge", "kind": kind, "prompt": prompt, "screenshot_png_b64": shot}


def classify(tab, cred):
    text = tab.js(PAGE_TEXT) or ""
    href = tab.js("location.href") or ""
    # Match the page a human would read, never the URL. These patterns are English
    # phrases; a URL is an opaque blob of percent-encoding, and `%2F` + `a` reads as
    # "2fa" case-insensitively — so `…redirect_uri=https%3A%2F%2Fapi.x.com…` used to
    # classify an ordinary OAuth hop as an OTP challenge and type a TOTP code into a
    # page that never asked for one. href is kept for `host` only.
    blob = text
    fields = tab.js(HAS_FIELDS) or {}
    host = urlparse(href).hostname or "site"
    # Path-only OTP walls (Instagram /auth_platform/codeentry) can paint the URL
    # before body text; do not treat that as success. Avoid bare "%2Fa"/2fa URL traps
    # by matching concrete path segments only — never the full opaque query blob.
    path = (urlparse(href).path or "").lower()
    if any(seg in path for seg in ("/codeentry", "/checkpoint", "/two_factor", "/two-factor")):
        return challenge(tab, cred, "otp", f"{host}: verification code entry")

    if RE_CAPTCHA.search(blob):
        return challenge(tab, cred, "captcha", f"{host}: {snippet(blob, RE_CAPTCHA)}")
    if RE_OTP.search(blob):
        if cred.get("totp_seed"):
            fill(tab, FOCUS_CODE, totp(cred["totp_seed"]))
            press_enter(tab)
            settle(tab)
            after = (tab.js(PAGE_TEXT) or "") + " " + (tab.js("location.href") or "")
            if RE_FAIL.search(after) or RE_OTP.search(after):
                return {"status": "failed", "reason": "totp code rejected"}
            return {"status": "success", "totp_used": True}
        # SMS OTP / other code challenge -> human (or Twilio, decided by cased)
        kind = "otp"
        return challenge(tab, cred, kind, f"{host}: {snippet(blob, RE_OTP)}")
    if RE_APPROVAL.search(blob):
        return challenge(tab, cred, "approval", f"{host}: {snippet(blob, RE_APPROVAL)}")
    if fields.get("pass") and RE_FAIL.search(text):
        return {"status": "failed", "reason": snippet(text, RE_FAIL)}
    if fields.get("pass"):
        return {"status": "failed", "reason": "still on login form after submit"}
    return {"status": "success"}


def advanced_past_identifier(tab):
    """True only with positive evidence the identifier submit reached a challenge.

    A missing username field alone is not enough: SPAs often unmount the identifier
    input a beat before the password/OTP wall mounts, and classify() would treat that
    empty interim page as success.
    """
    path = (urlparse(tab.js("location.href") or "").path or "").lower()
    if any(seg in path for seg in ("/codeentry", "/checkpoint", "/two_factor", "/two-factor")):
        return True
    return bool(challenge_signals_from_text(tab.js(PAGE_TEXT) or ""))


def fill_login_form(tab, cred):
    """Shared user/pass inject path (one-step or two-step). Returns failure reason or None."""
    # readyState alone is insufficient for SPA login shells (Discord, etc.)
    fields = wait_login_fields(tab)
    if not fields.get("user") and not fields.get("pass"):
        return "no login fields found"
    if fields.get("user"):
        fill(tab, FOCUS_USER, cred["username"])
    if fields.get("pass"):
        fill(tab, FOCUS_PASS, cred["secret"])
        press_enter(tab)
    else:  # two-step (username first)
        press_enter(tab)
        settle(tab)
        if (tab.js(HAS_FIELDS) or {}).get("pass"):
            fill(tab, FOCUS_PASS, cred["secret"])
            press_enter(tab)
        elif not advanced_past_identifier(tab):
            # No password and no challenge evidence yet (including SPA gaps where the
            # username unmounted first). Fail loudly — classify() would read that as success.
            return "password field never appeared"
        # else: password skipped for a real challenge wall —
        # fall through so login() can wait_post_submit + classify.
    settle(tab)
    return None


def apply_challenge_action(tab, kind, value=None):
    """Generic challenge action: otp/code fill+enter, or approval settle. Returns err str or None."""
    k = (kind or "").lower()
    if k in ("approval", "approve"):
        if value is not None and str(value).lower() == "deny":
            return "denied by human"
        time.sleep(8)  # human approved out-of-band; let the site catch up
        settle(tab)
        return None
    if k in ("otp", "code"):
        if value is None or str(value) == "":
            return "missing challenge value"
        fill(tab, FOCUS_CODE, str(value))
        press_enter(tab)
        settle(tab)
        return None
    return f"unknown challenge kind {kind!r}"


@app.post("/login")
def login(b: dict = Body(...)):
    cred, url = b["credential"], b["url"]
    state["login"] = None
    state["in_login"] = True
    state["injecting"] = True
    try:
        tab = Tab()
        try:
            navigate(tab, url)
            host = urlparse(tab.js("location.href") or url).hostname
            if not domain_ok(host, cred.get("domains") or []):
                return err(400, "domain_mismatch",
                           f"page origin {host!r} not in credential domains")
            reason = fill_login_form(tab, cred)
            if reason:
                return {"status": "failed", "reason": reason}
            wait_post_submit(tab)
            return classify(tab, cred)
        finally:
            tab.close()
    except Exception as e:
        return {"status": "failed", "reason": f"login error: {type(e).__name__}: {e}"}
    finally:
        state["injecting"] = False
        if not state["login"]:
            state["in_login"] = False


@app.post("/login/resume")
def login_resume(b: dict = Body(...)):
    value = str(b["value"])
    ctx = state["login"]
    if not ctx:
        return err(409, "no_pending_login", "no login is waiting on a handoff")
    state["login"] = None
    state["injecting"] = True
    try:
        tab = Tab()
        try:
            if ctx["kind"] == "approval" or value.lower() in ("approve", "deny"):
                reason = apply_challenge_action(tab, "approval", value)
                if reason:
                    return {"status": "failed", "reason": reason}
            else:
                reason = apply_challenge_action(tab, "otp", value)
                if reason:
                    return {"status": "failed", "reason": reason}
            blob = (tab.js(PAGE_TEXT) or "") + " " + (tab.js("location.href") or "")
            if RE_FAIL.search(blob):
                return {"status": "failed", "reason": snippet(blob, RE_FAIL)}
            page_text = tab.js(PAGE_TEXT) or ""
            fields_ok = tab.js(HAS_FIELDS) is not None      # eval reachable
            if fields_ok and RE_OTP.search(page_text):      # text only: href %2Fa trap, see classify()
                return {"status": "failed", "reason": "challenge still present"}
            return {"status": "success"}
        finally:
            tab.close()
    except Exception as e:
        return {"status": "failed", "reason": f"resume error: {type(e).__name__}: {e}"}
    finally:
        state["injecting"] = False
        state["in_login"] = False


# ---------- durable-auth observations / actions (cased owns workflow state) ----------

@app.post("/auth/observe")
def auth_observe():
    if state["injecting"]:
        return err(423, "credential_injection", "observe blocked during credential injection")
    try:
        tab = Tab()
        try:
            raw = tab.js(OBSERVE_AUTH_JS)
        finally:
            tab.close()
    except Exception as e:
        return err(502, "observe_error", f"{type(e).__name__}: {e}")
    if not isinstance(raw, dict):
        return err(502, "observe_error", "observation did not return an object")
    return {"ok": True, "observation": finalize_auth_observation(raw)}


@app.post("/auth/submit_challenge")
def auth_submit_challenge(b: dict = Body(...)):
    if "kind" not in b:
        return err(400, "bad_request", "body needs 'kind'")
    kind = b["kind"]
    value = b.get("value")
    state["in_login"] = True
    state["injecting"] = True
    try:
        tab = Tab()
        try:
            reason = apply_challenge_action(tab, kind, value)
            if reason == "missing challenge value":
                return err(400, "bad_request", "body needs 'value' for otp/code")
            if reason and reason.startswith("unknown challenge kind"):
                return err(400, "bad_request", reason)
            if reason:
                return {"ok": False, "reason": reason}
            return {"ok": True}
        finally:
            tab.close()
    except Exception as e:
        return {"ok": False, "reason": f"submit_challenge error: {type(e).__name__}: {e}"}
    finally:
        state["injecting"] = False
        state["in_login"] = False


@app.post("/auth/navigate_verification")
def auth_navigate_verification(b: dict = Body(...)):
    if "url" not in b:
        return err(400, "bad_request", "body needs 'url'")
    if state["injecting"]:
        return err(423, "credential_injection",
                   "navigate_verification blocked during credential injection")
    url = b["url"]
    domains = b.get("domains")
    host = urlparse(url).hostname
    if domains is not None and not domain_ok(host, domains):
        return err(400, "domain_mismatch",
                   f"url origin {host!r} not in allowed domains")
    try:
        tab = Tab()
        try:
            navigate(tab, url)
            settle(tab, extra=0.5)
            href = tab.js("location.href") or url
            final_host = urlparse(href).hostname
            if domains is not None and not domain_ok(final_host, domains):
                return err(400, "domain_mismatch",
                           f"page origin {final_host!r} not in allowed domains")
            return {"ok": True, "href": href, "title": tab.js("document.title") or ""}
        finally:
            tab.close()
    except Exception as e:
        return err(502, "navigate_error", f"{type(e).__name__}: {e}")


# ---------- network capture (CDP Network domain) ----------
# Browser-level wiretap: buffers response bodies whose URL matches a regex.
# Survives SPA navigations and catches fetch + XHR alike (page-world JS hooks
# do neither reliably — the reason this exists). Response bodies only; request
# bodies and headers (Set-Cookie, Authorization) are never captured.


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def capture_step(msg, pattern_re, want, pending, buf):
    """Fold one CDP message into (want, pending, buf). Return CDP commands to send.

    Two-phase, per the CDP contract: `Network.responseReceived` only means the
    *headers* arrived — the body may still be streaming, and `getResponseBody`
    fails ("No data found") if read then. So we remember the match in `want` and
    fetch the body only on `Network.loadingFinished`. `loadingFailed` and error
    replies are surfaced as buf items with an `error` key, never dropped silently.

    Pure: no I/O, no globals — the whole match/fetch/truncate policy lives here so
    tests exercise it without a websocket. `want` maps requestId -> {url,status}
    (awaiting finish); `pending` maps our getResponseBody command-id -> {url,status}
    (awaiting body reply); the worker assigns those command ids.
    """
    method = msg.get("method")
    if method == "Network.responseReceived":
        r = msg["params"]["response"]
        url = (r.get("url") or "")[:4096]
        if pattern_re.search(url):
            want[msg["params"]["requestId"]] = {"url": url, "status": r.get("status")}
        return []
    if method == "Network.loadingFinished":
        meta = want.pop(msg["params"]["requestId"], None)
        if meta:
            return [("Network.getResponseBody", {"requestId": msg["params"]["requestId"]}, meta)]
        return []
    if method == "Network.loadingFailed":
        meta = want.pop(msg["params"]["requestId"], None)
        if meta:
            buf.append({"ts": now_iso(), "url": meta["url"], "status": meta["status"],
                        "error": msg["params"].get("errorText", "loadingFailed")})
        return []
    mid = msg.get("id")                     # a reply to one of our getResponseBody commands
    if mid in pending:
        meta = pending.pop(mid)
        if "result" in msg:
            body = msg["result"].get("body", "")
            if msg["result"].get("base64Encoded"):
                try:
                    body = base64.b64decode(body).decode("utf-8", "replace")
                except Exception:
                    pass
            buf.append({"ts": now_iso(), "url": meta["url"], "status": meta["status"],
                        "body": body[:CAPTURE_CAP], "truncated": len(body) > CAPTURE_CAP})
        else:
            buf.append({"ts": now_iso(), "url": meta["url"], "status": meta["status"],
                        "error": (msg.get("error") or {}).get("message", "getResponseBody failed")})
    return []


def capture_worker(cap):
    """Own a persistent ws to the page target and pump Network events into cap['buf'].

    Cannot reuse Tab.cmd — its recv loop discards every event (no matching id).
    Dies on ws close (chromium shutdown at sleep) — restart after wake.
    """
    pattern_re = re.compile(cap["pattern"])
    want = {}             # requestId -> {url,status}, awaiting loadingFinished
    pending = {}          # our command id -> {url,status}, awaiting body reply
    next_id = [10000]     # keep clear of any low ids; events have no id anyway
    try:
        pages = [t for t in requests.get(f"{CDP}/json/list", timeout=5).json()
                 if t["type"] == "page"]
        if not pages:
            raise RuntimeError("no chromium page target")
        ws = websocket.create_connection(pages[0]["webSocketDebuggerUrl"], timeout=30,
                                         suppress_origin=True)
        ws.settimeout(2)
        ws.send(json.dumps({"id": 1, "method": "Network.enable"}))
        while not cap["stop"].is_set():
            try:
                msg = json.loads(ws.recv())
            except websocket.WebSocketTimeoutException:
                continue
            if state["injecting"]:          # never capture during credential injection
                want.clear()                # drop anything mid-flight when injection starts
                continue
            for method, params, meta in capture_step(msg, pattern_re, want, pending, cap["buf"]):
                next_id[0] += 1
                pending[next_id[0]] = meta
                ws.send(json.dumps({"id": next_id[0], "method": method, "params": params}))
        ws.close()
    except Exception as e:
        cap["error"] = f"{type(e).__name__}: {e}"


def _drain(buf):
    """Atomic drain — popleft to empty so a concurrent worker append is never
    cleared unseen (list()+clear() has a lost-update window between the two)."""
    items = []
    while True:
        try:
            items.append(buf.popleft())
        except IndexError:
            return items


def _stop_capture():
    cap = state["capture"]
    if cap:
        cap["stop"].set()
        cap["thread"].join(timeout=5)
    state["capture"] = None
    return cap


@app.post("/capture/start")
def capture_start(b: dict = Body(...)):
    pattern = b.get("pattern")
    if not pattern:
        return err(400, "bad_request", "body needs 'pattern'")
    if len(pattern) > 200:
        return err(400, "bad_request", "pattern too long")
    try:
        re.compile(pattern)
    except re.error as e:
        return err(400, "bad_request", f"invalid regex: {e}")
    _stop_capture()
    # maxlen 100 x CAPTURE_CAP (256 KB) = ~25 MB worst-case buffer; oldest drop on overflow
    cap = {"stop": threading.Event(), "buf": deque(maxlen=100),
           "pattern": pattern, "error": None}
    cap["thread"] = threading.Thread(target=capture_worker, args=(cap,), daemon=True)
    state["capture"] = cap
    cap["thread"].start()
    return {"ok": True, "pattern": pattern}


@app.get("/capture")
def capture_get():
    if state["injecting"]:
        return err(423, "credential_injection", "capture blocked during credential injection")
    cap = state["capture"]
    if not cap:
        return {"items": [], "running": False, "error": None}
    return {"items": _drain(cap["buf"]),
            "running": cap["thread"].is_alive() and not cap["stop"].is_set(),
            "error": cap["error"]}


@app.delete("/capture")
def capture_delete():
    if state["injecting"]:
        return err(423, "credential_injection", "capture blocked during credential injection")
    cap = _stop_capture()
    return {"items": _drain(cap["buf"]) if cap else [], "running": False,
            "error": cap["error"] if cap else None}


@app.post("/quiesce")
def quiesce():
    """Ask chromium to close itself (CDP Browser.close = full graceful shutdown).

    SIGTERM is chromium's *fast* shutdown and drops cookies newer than the last
    ~30s commit batch — the exact login-then-sleep pattern. start.sh calls this
    from its TERM trap before falling back to pkill.
    """
    try:
        url = requests.get(f"{CDP}/json/version", timeout=3).json()["webSocketDebuggerUrl"]
        ws = websocket.create_connection(url, timeout=5, suppress_origin=True)
        ws.send(json.dumps({"id": 1, "method": "Browser.close"}))
        ws.close()
    except Exception:
        pass
    return {"ok": True}


# ---------- blocker watchdog (§7) ----------

@app.get("/blocker")
def blocker():
    return {"blocker": state["blocker"]}


def blocker_signal(text, href):
    """Typed, stable blocker signal. Query tokens never define challenge identity."""
    m = RE_BLOCK.search(text) if (text or "").strip() else None
    if not m:
        return None
    host = urlparse(href or "").hostname or "page"
    path = urlparse(href or "").path or "/"
    if RE_CAPTCHA.search(text):
        kind = "captcha"
    elif RE_OTP.search(text):
        kind = "otp"
    elif RE_APPROVAL.search(text):
        kind = "approval"
    else:
        # Page-generated free-text prompts are untrusted. Require the human to
        # inspect and clear them in the live desk instead of injecting an answer.
        kind = "device"
    raw = f"{host}|{path}|{kind}|{m.group(0).lower()}"
    return {
        "kind": kind,
        "fingerprint": hashlib.sha1(raw.encode()).hexdigest()[:12],
        "prompt": f"{host}: {snippet(text, RE_BLOCK)}",
        "domain": host,
    }


def watchdog():
    while True:
        time.sleep(2)
        lg = state["login"]
        if lg and time.time() - lg["at"] > 900:      # handoff expired upstream (15 min TTL)
            state["login"] = None
            state["in_login"] = False
        if state["in_login"]:
            continue
        try:
            tab = Tab()
            try:
                text = (tab.js(PAGE_TEXT) or "")[:3000]
                href = tab.js("location.href") or ""
            finally:
                tab.close()
            # text only, and only once the page has painted. Matching the href raised a
            # handoff on every OAuth redirect carrying `%2Fa` (see classify); an empty
            # innerText meant `snippet` had nothing to quote, so the human got a
            # promptless "ava needs you" for a page that was merely still loading.
            signal = blocker_signal(text, href)
            if not state["blocker"] or not signal \
                    or state["blocker"]["fingerprint"] != signal["fingerprint"]:
                state["blocker"] = signal
        except Exception:
            pass


if __name__ == "__main__":
    threading.Thread(target=watchdog, daemon=True).start()
    # access_log off: log policy — nothing request-shaped ever hits the logs
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning", access_log=False)
