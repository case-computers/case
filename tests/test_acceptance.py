# SPDX-License-Identifier: MIT
"""Acceptance tests A1–A10. Run order matters — pytest runs top-down.

Requires: cased running on 127.0.0.1:8787 (logs at ~/.case/cased.log), image built.
A7 is manual (phone). A8 is gated behind CASE_A8=1 (restarts the Docker VM).
A9 runs via Claude Code separately. Set CASE_KEEP=1 to keep the test computer around
(A1 reaps any previous accept-1 first, so at most one ever survives).
"""
import base64
import contextlib
import glob
import hashlib
import hmac
import json
import os
import secrets
import struct
import subprocess
import threading
import time

import pytest
import requests

BASE = "http://127.0.0.1:8787/v1"
SITE_USER = "agent@example.com"
SITE_PASS = "s3cr3t-" + secrets.token_hex(8)          # unique per run so log-grep is meaningful
# Durable auth requires a positive proof_spec for status=success (else unverified).
SITE_PROOF = {
    "expression": "!!document.body && /You are signed in/.test(document.body.innerText)",
}
TOTP_SEED = base64.b32encode(secrets.token_bytes(10)).decode()
CASE_HOME = os.environ.get("CASE_HOME", os.path.expanduser("~/.case"))

CAPTURED = []          # every JSON/text API response body, for the A5 vault grep
_computer = {}


def api(method, path, timeout=180, **kw):
    r = requests.request(method, BASE + path, timeout=timeout, **kw)
    if "json" in r.headers.get("content-type", "") or "text" in r.headers.get("content-type", ""):
        CAPTURED.append(r.text)
    return r


def cid():
    if not _computer:  # standalone run (e.g. CASE_A8=1): reuse the kept accept-1
        for c in api("GET", "/computers").json()["computers"]:
            if c["name"] == "accept-1":
                _computer.update(c)
    assert _computer, "A1 must run first (or a kept accept-1 must exist)"
    return _computer["id"]


@contextlib.contextmanager
def spare_slot():
    """Free the one desktop slot so a test can create a second computer.

    A box behind a reverse proxy runs CASE_MAX_RUNNING=1 *and* pins the noVNC host
    port (CASE_VNC_PORT=6080, so the /desk door has a fixed upstream). Together those
    mean a second *running* computer cannot exist there at all: create fails with
    "Bind for 127.0.0.1:6080 failed: port is already allocated". A Mac has the headroom
    and never notices, which is why these tests passed there and only there.

    Waking accept-1 again is not optional — every later test calls cid() and expects a
    live desktop behind it.
    """
    api("POST", f"/computers/{cid()}/sleep")
    try:
        yield
    finally:
        api("POST", f"/computers/{cid()}/wake")


def exec_(cmd, timeout_s=30):
    r = api("POST", f"/computers/{cid()}/exec", json={"command": cmd, "timeout_s": timeout_s})
    assert r.status_code == 200, r.text
    return r.json()


def act(a):
    r = api("POST", f"/computers/{cid()}/action", json=a)
    assert r.status_code == 200, r.text
    return r.json()


def png_dims(data):
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    w, h = struct.unpack(">II", data[16:24])
    return w, h


def totp(seed, at):
    key = base64.b32decode(seed.replace(" ", "").upper(), casefold=True)
    h = hmac.new(key, struct.pack(">Q", int(at // 30)), hashlib.sha1).digest()
    o = h[-1] & 15
    return str((int.from_bytes(h[o:o + 4], "big") & 0x7FFFFFFF) % 10 ** 6).zfill(6)


def save_shot(name):
    r = api("GET", f"/computers/{cid()}/screenshot")
    if r.status_code == 200:
        out = os.path.join(os.path.dirname(__file__), "shots")
        os.makedirs(out, exist_ok=True)
        with open(os.path.join(out, name), "wb") as f:
            f.write(r.content)
    return r


# ---------- A1 boot ----------

def reap_stale():
    """Delete leftover accept-1 boxes before minting a fresh one.

    CASE_KEEP=1 (the documented way to run this suite) skips test_zz_cleanup, so
    without this every run left another accept-1 behind — they pile up in the DB
    and in Drive, and each one holds five test credentials. Reaping here rather
    than at teardown keeps CASE_KEEP's whole point (one box survives to poke at)
    while capping the count at one. Only ever touches the name this file creates.
    """
    for c in api("GET", "/computers").json()["computers"]:
        if c["name"] == "accept-1":
            api("DELETE", f"/computers/{c['id']}", json={"name": c["name"]})


def test_a1_boot():
    reap_stale()
    t0 = time.time()
    r = api("POST", "/computers", json={"name": "accept-1"})
    assert r.status_code == 201, r.text
    c = r.json()
    assert c["state"] == "running"
    assert time.time() - t0 <= 60
    _computer.update(c)
    time.sleep(3)  # let xfdesktop paint
    shot = save_shot("a1_desktop.png")
    assert shot.status_code == 200
    assert png_dims(shot.content) == (1280, 800)
    assert len(shot.content) > 30_000, "screenshot suspiciously small — likely a black screen"


# ---------- A2 exec ----------

def test_a2_exec():
    out = exec_("echo hi")
    assert out["exit_code"] == 0
    assert out["stdout"].strip() == "hi"
    assert out["truncated"] is False


# ---------- A3 act ----------

def test_a3_act():
    act({"type": "click", "x": 640, "y": 400})
    act({"type": "key", "keys": "ctrl+l"})
    act({"type": "type", "text": "https://news.ycombinator.com"})
    act({"type": "key", "keys": "Return"})
    title = ""
    for _ in range(30):
        time.sleep(1)
        out = exec_('xdotool search --onlyvisible --class "[Cc]hrom" getwindowname 2>/dev/null | head -3')
        title = out["stdout"]
        if "Hacker News" in title:
            break
    time.sleep(2)  # let first paint land before the keepsake shot
    save_shot("a3_hn.png")
    assert "Hacker News" in title, f"chromium window title: {title!r}"


# ---------- A4 files ----------

def test_a4_files():
    blob = secrets.token_bytes(1024 * 1024)
    r = api("PUT", f"/computers/{cid()}/files", params={"path": "/home/agent/x.bin"}, data=blob)
    assert r.status_code == 201 and r.json()["bytes"] == len(blob)
    r = api("GET", f"/computers/{cid()}/files", params={"path": "/home/agent/x.bin"})
    assert r.status_code == 200 and r.content == blob
    out = exec_("sha256sum /home/agent/x.bin")
    assert hashlib.sha256(blob).hexdigest() in out["stdout"]
    r = api("GET", f"/computers/{cid()}/files", params={"path": "/home/agent/nope.bin"})
    assert r.status_code == 404


# ---------- test site helper ----------

def start_site():
    src = open(os.path.join(os.path.dirname(__file__), "site_server.py"), "rb").read()
    r = api("PUT", f"/computers/{cid()}/files", params={"path": "/home/agent/site_server.py"}, data=src)
    assert r.status_code == 201
    # separate exec: pkill -f must not share a command line with the plain string it hunts.
    # Wait until the old listener is gone — a 1s sleep alone races on cx23 under load.
    exec_("pkill -f '[s]ite_server' || true; "
          "for i in 1 2 3 4 5 6 7 8; do pgrep -f '[s]ite_server' >/dev/null || break; sleep 1; done; "
          "true")
    # Bind is 127.0.0.1 (not localhost) — curl the same. Retry ready-check; unbuffered
    # so a bind failure lands in /tmp/site.log instead of a silent empty file.
    out = exec_(
        f"SITE_USER='{SITE_USER}' SITE_PASS='{SITE_PASS}' PYTHONUNBUFFERED=1 "
        "nohup python3 /home/agent/site_server.py >/tmp/site.log 2>&1 & "
        "for i in 1 2 3 4 5 6 7 8 9 10 11 12; do "
        "  code=$(curl -s -m 2 -o /dev/null -w '%{http_code}' http://127.0.0.1:8088/plain || true); "
        "  if [ \"$code\" = 200 ]; then echo \"$code\"; exit 0; fi; "
        "  sleep 1; "
        "done; "
        "echo FAIL; cat /tmp/site.log; exit 1",
        timeout_s=90,
    )
    assert out["stdout"].strip().endswith("200"), out


def add_cred(name, secret=SITE_PASS, domains=("localhost",), **extra):
    body = {"name": name, "username": SITE_USER, "secret": secret, "domains": list(domains), **extra}
    r = api("POST", f"/computers/{cid()}/credentials", json=body)
    assert r.status_code == 201, r.text
    return r.json()


# ---------- A5 vault hygiene ----------

def test_a5_vault_hygiene():
    start_site()
    pub = add_cred("local-plain")
    assert pub["has_totp"] is False and SITE_PASS not in json.dumps(pub)

    hits = {"n423": 0}
    stop = threading.Event()

    def hammer():
        while not stop.is_set():
            r = requests.get(f"{BASE}/computers/{cid()}/screenshot", timeout=10)
            if r.status_code == 423:
                hits["n423"] += 1
            time.sleep(0.1)

    t = threading.Thread(target=hammer)
    t.start()
    try:
        r = api("POST", f"/computers/{cid()}/login",
                json={"credential": "local-plain", "url": "http://localhost:8088/plain",
                      "proof_spec": SITE_PROOF})
    finally:
        stop.set()
        t.join()
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "success", r.text
    assert hits["n423"] >= 1, "screenshot was never blocked during injection"

    # the secret must appear nowhere: cased log, container logs, any API response
    cased_log = open(os.path.join(CASE_HOME, "cased.log"), errors="replace").read() \
        if os.path.exists(os.path.join(CASE_HOME, "cased.log")) else ""
    desk_log = subprocess.run(["docker", "logs", f"case-{cid()}"],
                              capture_output=True, text=True)
    for blob, where in [(cased_log, "cased.log"),
                        (desk_log.stdout + desk_log.stderr, "deskd docker logs"),
                        ("\n".join(CAPTURED), "API responses")]:
        assert SITE_PASS not in blob, f"SECRET LEAKED into {where}"

    # domain guard
    add_cred("wrong-domain", domains=["github.com"])
    r = api("POST", f"/computers/{cid()}/login",
            json={"credential": "wrong-domain", "url": "http://localhost:8088/plain"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "domain_mismatch", r.text

    # wrong password -> failed
    add_cred("local-bad", secret="not-the-password")
    r = api("POST", f"/computers/{cid()}/login",
            json={"credential": "local-bad", "url": "http://localhost:8088/plain"})
    assert r.status_code == 200 and r.json()["status"] == "failed", r.text


# ---------- human doors (fill + desk) ----------

ROOT = BASE.rsplit("/v1", 1)[0]          # /fill lives outside /v1 — straight at cased


def test_fill_link():
    """A minted fill link writes a credential through the browser-form door —
    single-use, never through MCP, never plaintext in the audit log (A5 family)."""
    r = api("POST", f"/computers/{cid()}/links", json={"kind": "fill"})
    assert r.status_code == 201, r.text
    tok, path = r.json()["token"], r.json()["path"]
    assert path == f"/fill/{tok}", r.text

    assert "name=secret type=password" in requests.get(f"{ROOT}/fill/{tok}", timeout=10).text

    # a pasted URL is what humans actually type — it must land as a bare host, or the
    # login never matches and the credential name (with a /) cannot even be deleted
    r = requests.post(f"{ROOT}/fill/{tok}", timeout=10,
                      data={"domains": "https://Fill-Test.example.com/inbox",
                            "username": "u@example.com",
                            "secret": "s3cret-fill-A5",
                            "totp_seed": ""})
    assert r.status_code == 200 and "Saved" in r.text, r.text

    r = api("GET", f"/computers/{cid()}/credentials")
    cred = next(c for c in r.json()["credentials"] if c["name"] == "fill-test.example.com")
    assert cred["domains"] == ["fill-test.example.com"], cred
    assert "s3cret-fill-A5" not in r.text

    # burned: the same link never accepts a second write
    r = requests.post(f"{ROOT}/fill/{tok}", timeout=10,
                      data={"domains": "x.com", "username": "u", "secret": "p"})
    assert r.status_code == 410, r.text

    # neither the password (body) nor the token (path — it is a live capability)
    # may reach the audit log
    blob = "".join(open(p, errors="replace").read()
                   for p in glob.glob(os.path.join(CASE_HOME, "audit", "*.jsonl")))
    assert "s3cret-fill-A5" not in blob, "fill password leaked into the audit log"
    assert tok not in blob, "fill token leaked into the audit log"

    # the name is addressable, so the human can undo what they added
    assert api("DELETE", f"/computers/{cid()}/credentials/fill-test.example.com"
               ).status_code == 204


def test_fill_form_escapes_an_agent_chosen_name():
    """The computer name comes from the agent (computer_create). The credential page
    must never let it become script — that would let the agent read the password the
    human types, on the one page whose whole promise is that it cannot."""
    # This only needs the name to reach the DB and come back out through the form —
    # never two live desktops.
    with spare_slot():
        r = api("POST", "/computers", json={"name": "</h1><script>steal()</script>"})
        assert r.status_code == 201, r.text
        evil = r.json()["id"]
        try:
            tok = api("POST", f"/computers/{evil}/links", json={"kind": "fill"}).json()["token"]
            page = requests.get(f"{ROOT}/fill/{tok}", timeout=10).text
            assert "<script>steal()" not in page, "agent-set name injected raw into the form"
            assert "&lt;script&gt;steal()" in page, page[:400]
        finally:
            api("DELETE", f"/computers/{evil}")


def test_desk_check():
    """The forward-auth contract a reverse proxy relies on for /desk/*: query token redirects
    with the cookie, the cookie alone keeps the session, garbage stays out."""
    tok = api("POST", f"/computers/{cid()}/links", json={"kind": "vnc"}).json()["token"]

    # 302, not 200: forward_auth hands a non-2xx auth response back to the browser,
    # which is the only way the Set-Cookie reaches a human. Token leaves the URL.
    r = requests.get(f"{BASE}/desk/check", timeout=10, allow_redirects=False,
                     headers={"X-Forwarded-Uri": f"/desk/vnc.html?token={tok}&autoconnect=1"})
    assert r.status_code == 302 and f"case_desk={tok}" in r.headers.get("set-cookie", ""), r.headers
    assert r.headers["Location"] == "/desk/vnc.html?autoconnect=1", r.headers

    r = requests.get(f"{BASE}/desk/check", timeout=10, headers={"Cookie": f"case_desk={tok}"})
    assert r.status_code == 200 and "set-cookie" not in {k.lower() for k in r.headers}

    r = requests.get(f"{BASE}/desk/check", timeout=10,
                     headers={"X-Forwarded-Uri": "/desk/vnc.html?token=nope"})
    assert r.status_code == 401

    # a token is not enough: it must name the computer that is actually behind the
    # door, or the human meets whichever desktop happens to be awake
    # desk-bind only ever has to exist and be asleep, so it never needs the slot at the
    # same time as accept-1 — but creating it does, because create starts the container.
    with spare_slot():
        r = api("POST", "/computers", json={"name": "desk-bind"})
        assert r.status_code == 201, r.text
        other = r.json()["id"]
        try:
            api("POST", f"/computers/{other}/sleep")
            t2 = api("POST", f"/computers/{other}/links", json={"kind": "vnc"}).json()["token"]
            r = requests.get(f"{BASE}/desk/check", timeout=10, allow_redirects=False,
                             headers={"X-Forwarded-Uri": f"/desk/vnc.html?token={t2}"})
            assert r.status_code == 409 and "asleep" in r.text, (r.status_code, r.text[:200])
        finally:
            api("DELETE", f"/computers/{other}")


# ---------- eval ----------

def test_eval():
    r = api("POST", f"/computers/{cid()}/eval", json={"expression": "1+1"})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "value": 2, "truncated": False}

    r = api("POST", f"/computers/{cid()}/eval", json={"expression": "nope.nope"})
    out = r.json()
    assert r.status_code == 200 and out["ok"] is False and "ReferenceError" in out["error"]

    # navigate + DOM read, the intended replacement for screenshot-scraping
    start_site()
    r = api("POST", f"/computers/{cid()}/eval",
            json={"expression": "location.assign('http://localhost:8088/plain'); 1"})
    assert r.json()["ok"] is True, r.text
    val = None
    for _ in range(20):
        time.sleep(0.5)
        r = api("POST", f"/computers/{cid()}/eval",
                json={"expression": "document.readyState==='complete' "
                                    "&& !!document.querySelector('input[type=password]')"})
        val = r.json().get("value")
        if val is True:
            break
    assert val is True, r.text

    # eval must 423 during credential injection, same contract as screenshots
    add_cred("eval-inj")
    hits = {"n423": 0}
    stop = threading.Event()

    def hammer():
        while not stop.is_set():
            rr = requests.post(f"{BASE}/computers/{cid()}/eval",
                               json={"expression": "1"}, timeout=10)
            if rr.status_code == 423:
                hits["n423"] += 1
            time.sleep(0.05)

    t = threading.Thread(target=hammer)
    t.start()
    try:
        r = api("POST", f"/computers/{cid()}/login",
                json={"credential": "eval-inj", "url": "http://localhost:8088/plain",
                      "proof_spec": SITE_PROOF})
    finally:
        stop.set()
        t.join()
    assert r.status_code == 200 and r.json()["status"] == "success", r.text
    assert hits["n423"] >= 1, "eval was never blocked during injection"


def test_navigate():
    """The one-call replacement for the assign+poll loop above. Unit tests fake
    eval_js; this is the only place the sentinel JS meets a real browser."""
    start_site()
    api("POST", f"/computers/{cid()}/navigate", json={"url": "http://localhost:8088/totp"})

    r = api("POST", f"/computers/{cid()}/navigate", json={"url": "http://localhost:8088/plain"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["ok"] is True and out["url"].endswith("/plain"), out

    # no polling here on purpose: if navigate returned early this read finds nothing
    r = api("POST", f"/computers/{cid()}/eval",
            json={"expression": "[location.pathname,"
                                "!!document.querySelector('input[type=password]'),"
                                "window.__case_nav===undefined]"})
    assert r.json()["value"] == ["/plain", True, True], \
        f"navigate returned before the new document was ready, or left its sentinel: {r.text}"

    # same URL again: a reload swaps the document, so it must still count as arrival
    r = api("POST", f"/computers/{cid()}/navigate", json={"url": "http://localhost:8088/plain"})
    assert r.json()["ok"] is True, r.text

    r = api("POST", f"/computers/{cid()}/navigate", json={"url": "http://", "timeout_s": 30})
    out = r.json()
    assert out["ok"] is False and "SyntaxError" in out["error"], out


# ---------- A6 TOTP ----------

def test_a6_totp():
    add_cred("local-totp", totp_seed=TOTP_SEED)
    before = api("GET", f"/computers/{cid()}/handoffs").json()["handoffs"]
    t0 = time.time()
    r = api("POST", f"/computers/{cid()}/login",
            json={"credential": "local-totp", "url": "http://localhost:8088/totp",
                  "proof_spec": SITE_PROOF})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "success", r.text
    after = api("GET", f"/computers/{cid()}/handoffs").json()["handoffs"]
    assert len(after) == len(before), "TOTP login must not create a handoff"

    rec = json.loads(exec_("cat /tmp/received.json")["stdout"])
    codes = [e["data"].get("code") for e in rec if e["path"] == "/totp/code"]
    assert codes, "site never received a TOTP code"
    valid = {totp(TOTP_SEED, t) for t in (t0 - 30, t0, t0 + 30, time.time())}
    assert codes[-1] in valid, f"entered code {codes[-1]} not a valid TOTP for the seed"


# ---------- A7 handoff loop — API half (phone half is manual) ----------

def test_a7_handoff_api_loop():
    events = []
    stop = threading.Event()

    def listen():
        with requests.get(f"{BASE}/events", stream=True, timeout=(5, 60)) as r:
            for line in r.iter_lines():
                if stop.is_set():
                    return
                if line:
                    events.append(line.decode())

    t = threading.Thread(target=listen, daemon=True)
    t.start()
    time.sleep(1)

    r = api("POST", f"/computers/{cid()}/handoffs",
            json={"kind": "approval", "prompt": "Ship it?"})
    assert r.status_code == 201, r.text
    h = r.json()
    assert h["status"] == "pending" and h["screenshot_png_b64"]

    r = api("GET", "/handoffs", params={"status": "pending"})
    assert h["id"] in [x["id"] for x in r.json()["handoffs"]]

    r = api("POST", f"/handoffs/{h['id']}/answer", json={"value": "approve"})
    assert r.status_code == 200 and r.json()["status"] == "completed"
    assert r.json()["answer"] == "approve"

    r = api("POST", f"/handoffs/{h['id']}/answer", json={"value": "approve"})
    assert r.status_code == 409

    time.sleep(2)
    stop.set()
    blob = "\n".join(events)
    assert "event: handoff_created" in blob and h["id"] in blob
    assert "event: handoff_answered" in blob


@pytest.mark.skip(reason="manual: needs ntfy topics configured + a phone (spec A7)")
def test_a7_phone():
    pass


# ---------- asleep semantics (spec §2 rules) ----------

def test_sleep_wake_semantics():
    r = api("POST", f"/computers/{cid()}/sleep")
    assert r.status_code == 200 and r.json()["state"] == "asleep"
    r = api("POST", f"/computers/{cid()}/sleep")            # idempotent
    assert r.status_code == 200 and r.json()["state"] == "asleep"
    r = api("POST", f"/computers/{cid()}/exec", json={"command": "echo hi"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "asleep"
    r = api("POST", f"/computers/{cid()}/exec", params={"wake": "true"},
            json={"command": "echo hi"})                    # ?wake=true wakes first
    assert r.status_code == 200 and r.json()["stdout"].strip() == "hi"
    r = api("POST", f"/computers/{cid()}/wake")             # idempotent
    assert r.status_code == 200 and r.json()["state"] == "running"
    r = api("GET", "/computers/c_doesnotexist")
    assert r.status_code == 404


# ---------- A10 fleet ----------

def test_a10_fleet():
    ids = [cid()]
    try:
        for i in range(2, 7):
            r = api("POST", "/computers", json={"name": f"fleet-{i}"})
            assert r.status_code == 201, f"fleet-{i}: {r.text}"
            ids.append(r.json()["id"])
        for i in ids:
            out = api("POST", f"/computers/{i}/exec", json={"command": "echo hi"}).json()
            assert out["stdout"].strip() == "hi"
        for i in ids:
            r = api("POST", f"/computers/{i}/sleep")
            assert r.json()["state"] == "asleep"
        ps = subprocess.run(["docker", "ps", "-q", "--filter", "label=managed-by=cased"],
                            capture_output=True, text=True)
        assert ps.stdout.strip() == "", "containers still running after fleet sleep"
    finally:
        for i in ids[1:]:
            api("DELETE", f"/computers/{i}")
    api("POST", f"/computers/{cid()}/wake")


# ---------- A8 THE WEDGE (gated: restarts the Docker VM) ----------

@pytest.mark.skipif(os.environ.get("CASE_A8") != "1", reason="set CASE_A8=1 (restarts colima)")
def test_a8_wedge():
    start_site()
    add_cred("wedge")   # this run's SITE_PASS; stored creds are from an earlier run
    r = api("POST", f"/computers/{cid()}/login",
            json={"credential": "wedge", "url": "http://localhost:8088/plain",
                  "proof_spec": SITE_PROOF})
    assert r.json()["status"] == "success"

    r = api("POST", f"/computers/{cid()}/sleep")
    assert r.json()["state"] == "asleep"

    subprocess.run(["colima", "restart"], check=True, capture_output=True, timeout=300)
    time.sleep(5)

    t0 = time.time()
    r = api("POST", f"/computers/{cid()}/wake", timeout=120)
    assert r.status_code == 200 and r.json()["state"] == "running", r.text
    wake_s = time.time() - t0

    start_site()                                   # site process died with the VM; cookies didn't
    act({"type": "key", "keys": "ctrl+l"})
    act({"type": "type", "text": "http://localhost:8088/whoami"})
    act({"type": "key", "keys": "Return"})
    title = ""
    for _ in range(20):
        time.sleep(1)
        title = exec_('xdotool search --onlyvisible --class "[Cc]hrom" getwindowname | head -3')["stdout"]
        if "signed-in" in title:
            break
    save_shot("a8_after_reboot.png")
    assert "signed-in" in title, f"session lost across sleep -> VM restart -> wake: {title!r}"
    print(f"\nA8 wedge: wake took {wake_s:.1f}s, session survived")


# ---------- cleanup ----------

def test_zz_cleanup():
    if os.environ.get("CASE_KEEP") == "1":
        pytest.skip("CASE_KEEP=1")
    r = api("DELETE", f"/computers/{cid()}")
    assert r.status_code == 204
    assert api("GET", f"/computers/{cid()}").status_code == 404
