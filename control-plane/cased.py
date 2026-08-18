#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""cased, Case control plane (API_SPEC.md §3).

REST on loopback by default (CASE_BIND/CASE_PORT). This module is the composition root: it owns the FastAPI
app, the (thin) route table, and startup wiring. All behaviour lives in the modules
it delegates to, lifecycle, handoffs, scheduler, deskclient, dockerd, store, events.
"""
import asyncio
from contextlib import asynccontextmanager
import hmac
import html
import json
import os
import re
import secrets
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs

import uvicorn
from fastapi import Body, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

import auth_attempts
import captcha
import dockerd
import events
import handoffs
import lifecycle
import links
import assist
import login_flow
import scheduler
import session_keeper
from config import (API_BASE, AUDIT_DIR, BIND_HOST, BIND_PORT, IMAGE, MAX_COMPUTER_RAM_MB,
                    MAX_CPUS, MAX_RAM_MB, MAX_RUNNING, MIN_CPUS, MIN_RAM_MB, RUNS_DIR,
                    VNC_PORT, log)
import browse
from deskclient import desk_bytes, desk_json, navigate
from errors import ApiError
from events import sse_gen
from notify import notifier
from store import store
from util import now

@asynccontextmanager
async def lifespan(_app):
    """Startup, then (after the yield) shutdown. `@app.on_event` is deprecated in
    FastAPI and warned about on every boot; a lifespan says the same thing once.
    Names below (sweeper, blocker_poller) are resolved when this runs, not when it
    is defined, so they may live further down the file."""
    events.set_loop(asyncio.get_running_loop())
    handoffs.rebuild_login_ctx()          # recover pending login handoffs across a restart (L3)
    auth_attempts.set_captcha_auto(
        lambda row, cid, name: login_flow._try_captcha_auto(
            row, cid, name, resume=True, record=False))
    await asyncio.to_thread(lifecycle.reconcile)
    threading.Thread(target=sweeper, daemon=True).start()
    threading.Thread(target=blocker_poller, daemon=True).start()
    notifier.listen(handoffs.on_ntfy_answer)
    log.info("cased up on %s (image=%s, max_running=%d, max_ram_mb=%d, captcha_auto=%s)",
             API_BASE, IMAGE, MAX_RUNNING, MAX_RAM_MB, "on" if captcha.enabled() else "off")
    yield
    # SIGTERM (docker compose down, systemctl stop): park the desktops. They are not
    # compose services, so nothing else would.
    await asyncio.to_thread(lifecycle.sleep_all)


app = FastAPI(title="cased", lifespan=lifespan)
BLOCKER_SEEN = {}           # computer_id -> fingerprint


# ---------- error handling ----------

@app.exception_handler(ApiError)
async def api_error(_, e: ApiError):
    return JSONResponse({"error": {"code": e.code, "message": e.message}}, status_code=e.status)


@app.exception_handler(RequestValidationError)
async def validation_error(_, e):
    # never echo the submitted body, it can carry a secret (A5). Report only where/why.
    details = [{"loc": er.get("loc"), "msg": er.get("msg"), "type": er.get("type")}
               for er in e.errors()]
    return JSONResponse({"error": {"code": "bad_request", "message": "request validation failed",
                                   "details": details}}, status_code=400)


@app.exception_handler(Exception)
async def internal_error(_, e):
    log.exception("internal error")
    return JSONResponse({"error": {"code": "internal", "message": f"{type(e).__name__}: {e}"}},
                        status_code=500)


# ---------- the console door ----------
# Caddy's `handle /console/*` stamps `X-Case-Door: console` with header_up, which
# REPLACES any client-supplied value, a browser cannot forge its way past this.
# A request wearing that header may reach exactly the routes below and nothing else:
# not exec, not eval, not files, not capture, not login, not computer-create, not the
# plaintext-credentials route that /fill exists specifically to avoid, and not the
# link routes (which would let a browser token revoke everyone else's access or renew
# its own indefinitely). A browser token is not an agent token.
#
# The policy lives here rather than in Caddy: one list, in Python, unit-testable,
# able to see method and path. A Caddy path-matcher would be a second copy of it in
# a language with no tests.
#
# Declared ABOVE audit_mw on purpose. Starlette makes the LAST-declared middleware the
# outermost, so audit_mw wraps this one and a request this guard rejects is still
# written to the audit log, which is exactly the request worth having a record of.
CONSOLE_ROUTES = [
    ("GET",    re.compile(r"^/v1/computers$")),
    ("GET",    re.compile(r"^/v1/runs$")),
    ("GET",    re.compile(r"^/v1/runs/[^/]+/screenshot$")),
    ("GET",    re.compile(r"^/v1/credentials$")),
    ("GET",    re.compile(r"^/v1/handoffs$")),
    ("POST",   re.compile(r"^/v1/handoffs/[^/]+/answer$")),
    ("POST",   re.compile(r"^/v1/computers/[^/]+/wake$")),
    ("POST",   re.compile(r"^/v1/computers/[^/]+/links$")),   # fill|vnc only, see mint_link
    ("DELETE", re.compile(r"^/v1/computers/[^/]+$")),
    ("GET",    re.compile(r"^/v1/connect$")),                 # one-shot MCP paste reveal
    ("POST",   re.compile(r"^/v1/mcp/rotate$")),             # mint new cs_, spare caller console
]
DOOR_BLOCKED = {"error": {"code": "not_found", "message": "not_found"}}

# Door-write helper (root via sudoers). Overridable in tests.
DOOR_WRITE_BIN = os.environ.get("CASE_DOOR_WRITE", "/usr/local/bin/case-door-write")
# Hosted boxes set this in /etc/caddy/case.env. Empty on a self-hosted box:
# there is no console to go back to, so the Back button is simply dropped.
DEFAULT_CONSOLE_ORIGIN = os.environ.get("CASE_CONSOLE_ORIGIN", "")
TOKEN_RE = re.compile(r"^cs_[0-9a-f]{32}$")
HOST_RE = links.HOSTNAME  # keep in sync: one hostname regex, owned by links.py


def _link_token(request: Request):
    """The console Link token from Authorization, if any. Used to spare the caller
    on rotate, never logged."""
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("link "):
        return auth[5:].strip() or None
    return None


def _paste_payload(host, token, seen_at=None):
    return {
        "host": host,
        "token": token,
        "seen_at": seen_at,
        # Three shapes of ONE url, and `url` leads. claude.ai and Claude Desktop's
        # connector UI take a URL and nothing else, no header field exists, so the
        # bearer shapes these used to emit were usable only by someone who already had
        # Claude Code. The token rides the path instead (deploy/Caddyfile @urltoken).
        "paste": {
            "url": f"https://{host}/mcp/{token}",
            "claude": f"claude mcp add --transport http case https://{host}/mcp/{token}",
            "json": ('{ "mcpServers": { "case": { "type": "http", '
                     f'"url": "https://{host}/mcp/{token}" }} }} }}'),
        } if token else None,
    }


def _door_write(host, token, origin):
    """Rewrite case.env + restart caddy. Returns None on success, error string on failure."""
    body = (f"CASE_MCP_HOST={host}\n"
            f"CASE_MCP_TOKEN={token}\n"
            f"CASE_CONSOLE_ORIGIN={origin}\n")
    try:
        r = subprocess.run(
            ["sudo", "-n", DOOR_WRITE_BIN],
            input=body, text=True, capture_output=True, timeout=60)
    except FileNotFoundError:
        return "door-write binary missing — install case-door-write + sudoers"
    except subprocess.TimeoutExpired:
        return "door-write timed out"
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "").strip() or f"exit {r.returncode}"
        # never echo token-bearing stderr into API if helper misbehaves, helpers
        # are written not to print secrets; still truncate hard.
        return err[:200]
    return None


def _new_mcp_token():
    return "cs_" + secrets.token_hex(16)


def case_token():
    """Optional share token. Empty = open on the bind address (laptop default)."""
    return (os.environ.get("CASE_TOKEN") or "").strip()


def bearer_ok(authorization):
    """True when CASE_TOKEN is unset, or Authorization: Bearer matches it."""
    want = case_token()
    if not want:
        return True
    auth = authorization or ""
    if not auth.lower().startswith("bearer "):
        return False
    got = auth[7:].strip()
    if len(got) != len(want):
        return False
    return hmac.compare_digest(got, want)


@app.middleware("http")
async def token_guard(request: Request, call_next):
    # /health stays open so compose can probe us without circulating the token.
    if request.url.path == "/health":
        return await call_next(request)
    if not bearer_ok(request.headers.get("authorization")):
        return JSONResponse(
            {"error": {"code": "unauthorized", "message": "unauthorized"}},
            status_code=401)
    return await call_next(request)


@app.middleware("http")
async def console_door_guard(request: Request, call_next):
    # No header = loopback (bin/case, the operator) or the agent's own path through
    # case-mcp. Both unchanged by this.
    if request.headers.get("x-case-door") == "console":
        # Starlette answers HEAD from every GET route; a reader that may GET may HEAD.
        method = "GET" if request.method == "HEAD" else request.method
        path = request.url.path
        if not any(m == method and rx.match(path) for m, rx in CONSOLE_ROUTES):
            return JSONResponse(status_code=404, content=DOOR_BLOCKED)
    return await call_next(request)


def from_console(request):
    """True when Caddy's /console door stamped this request. Routes use it to demand
    more of a browser than of the operator on loopback."""
    return request.headers.get("x-case-door") == "console"


# ---------- audit log ----------
# One JSONL line per API call, ~/.case/audit/<date>.jsonl. Answers "what did the
# agent do on this machine" without agents self-logging transcripts. Sessions are
# whatever the client sends as X-Case-Session (the MCP server sends one per process).
# Security invariant: response bodies are NEVER logged (screenshots, file contents),
# and request bodies that can carry secrets are redacted, secrets never hit disk.
# Redacted routes: /credentials (password/TOTP), /answer (OTP codes relayed by the
# human), /files (uploaded file contents may hold tokens), /fill (the human
# credential form posts the password itself). Matches API_SPEC §5.

def _redacted(path):
    return ("/credentials" in path or path.endswith("/answer")
            or path.endswith("/files") or path.startswith("/fill/")
            or path.endswith("/fill")   # agent form-fill bodies carry user data
            or path.startswith("/assist/")
            or path.endswith("/connect") or "/mcp/" in path)


@app.middleware("http")
async def audit_mw(request: Request, call_next):
    body = b"" if request.method in ("GET", "DELETE") else await request.body()
    t0 = time.time()
    resp = await call_next(request)
    path = request.url.path
    if path != "/health" and not path.endswith("/events"):   # skip noise + SSE streams
        req = "[redacted]" if _redacted(path) else body[:2000].decode("utf-8", "replace")
        # a fill/assist token is a live capability, the log records that the door
        # was used, never the key itself
        if path.startswith("/fill/"):
            logged_path = "/fill/[token]"
        elif path.startswith("/assist/"):
            logged_path = "/assist/[token]" + (
                "/submit" if path.endswith("/submit") else
                "/done" if path.endswith("/done") else "")
        else:
            logged_path = path
        line = {"ts": now(), "session": request.headers.get("x-case-session", "-"),
                "method": request.method, "path": logged_path, "status": resp.status_code,
                "ms": int((time.time() - t0) * 1000), "req": req}
        await asyncio.to_thread(_audit_append, line)
    return resp


def _audit_append(line):
    os.makedirs(AUDIT_DIR, exist_ok=True)
    with open(os.path.join(AUDIT_DIR, time.strftime("%Y-%m-%d") + ".jsonl"), "a") as f:
        f.write(json.dumps(line) + "\n")


def unlink_run_artifacts(paths):
    """Delete run PNGs only when they resolve inside RUNS_DIR. Missing files are fine."""
    root = os.path.realpath(RUNS_DIR)
    for p in paths or ():
        if not p:
            continue
        real = os.path.realpath(p)
        if real == root or not real.startswith(root + os.sep):
            continue
        try:
            os.unlink(real)
        except FileNotFoundError:
            pass


def prune_old_audit_files(audit_dir=None, keep_days=30):
    """Drop YYYY-MM-DD.jsonl older than keep_days. Never today's file; ignore junk names."""
    audit_dir = audit_dir or AUDIT_DIR
    if not os.path.isdir(audit_dir):
        return
    today = datetime.now(timezone.utc)
    today_s = today.strftime("%Y-%m-%d")
    cutoff = (today - timedelta(days=keep_days)).date()
    for name in os.listdir(audit_dir):
        if not name.endswith(".jsonl"):
            continue
        stamp = name[:-6]
        if stamp == today_s:
            continue
        try:
            day = datetime.strptime(stamp, "%Y-%m-%d").date()
        except ValueError:
            continue
        if day < cutoff:
            try:
                os.unlink(os.path.join(audit_dir, name))
            except FileNotFoundError:
                pass


# ---------- events (SSE) ----------

@app.get("/v1/events")
async def events_ep():
    return StreamingResponse(sse_gen(), media_type="text/event-stream")


@app.get("/v1/computers/{cid}/events")
async def computer_events(cid: str):
    lifecycle.get_computer(cid)
    return StreamingResponse(sse_gen(cid), media_type="text/event-stream")


# ---------- computers ----------

@app.post("/v1/computers", status_code=201)
def create_computer(body: dict = Body(default={})):
    body = body or {}
    # The create form is a trust boundary: these two numbers become `docker run
    # --memory/--cpus`, so garbage here is a container that will not start (or a
    # box that OOMs). Reject it with a message instead of a 500 from the daemon.
    cpus = _num(body.get("cpus"), 1, MIN_CPUS, MAX_CPUS, "cpus")
    ram_mb = int(_num(body.get("ram_mb"), 2048, MIN_RAM_MB, MAX_COMPUTER_RAM_MB, "ram_mb"))
    return lifecycle.provision(body.get("name"), cpus, ram_mb)


def _num(value, default, lo, hi, field):
    if value in (None, ""):
        return default
    try:
        n = float(value)
    except (TypeError, ValueError):
        raise ApiError(400, "bad_request", f"{field} must be a number")
    if not lo <= n <= hi:
        raise ApiError(400, "bad_request", f"{field} must be between {lo} and {hi}")
    return n


@app.get("/v1/computers")
def list_computers():
    summaries = store.schedule_summaries()
    creds = store.credential_names_by_computer()
    pending = store.pending_handoff_counts()
    return {"computers": [lifecycle.computer_json(r, summaries, creds, pending)
                          for r in store.list_computers()]}


@app.get("/v1/computers/{cid}")
def get_computer_ep(cid: str):
    return lifecycle.computer_json(lifecycle.get_computer(cid))


@app.delete("/v1/computers/{cid}", status_code=204)
def delete_computer(cid: str, request: Request, body: dict = Body(default=None)):
    row = lifecycle.get_computer(cid)
    # Through the console door this is the most destructive thing a browser can do: it
    # destroys the volume, and the volume is the identity. A client-side confirm() is
    # not a control, so the name has to come back over the wire, one mis-aimed click
    # on a link that was forwarded, screenshotted or left open cannot do this. Loopback
    # (bin/case rm, the operator) is unchanged.
    if from_console(request) and (body or {}).get("name") != row["name"]:
        raise ApiError(400, "confirm_name",
                       "deleting a computer destroys its volume — send "
                       "{\"name\": \"<the computer's name>\"} to confirm")
    lifecycle.destroy(cid)
    return Response(status_code=204)


@app.post("/v1/computers/{cid}/sleep")
def sleep_computer(cid: str):
    lifecycle.do_sleep(cid)
    return lifecycle.computer_json(lifecycle.get_computer(cid))


@app.post("/v1/computers/{cid}/wake")
def wake_computer(cid: str):
    lifecycle.do_wake(cid)
    return lifecycle.computer_json(lifecycle.get_computer(cid))


@app.get("/health")
def health():
    n = len(store.list_computers())
    try:
        dockerd.dc().ping()
        docker_ok = True
    except Exception:
        docker_ok = False
    return {
        "ok": True,
        "computers": n,
        "docker": docker_ok,
        "max_running": MAX_RUNNING,
        "running": store.active_count(),
        "max_ram_mb": MAX_RAM_MB,          # 0 = no budget (no /proc, or set to 0)
        "ram_mb": store.active_ram_mb(),
    }


# ---------- observation & action ----------

@app.get("/v1/computers/{cid}/screenshot")
def screenshot(cid: str, wake: bool = False):
    row = lifecycle.ensure_running(cid, wake)
    content = desk_bytes(row, "GET", "/screenshot")   # raises ApiError(423) during injection
    store.touch(cid)
    return Response(content, media_type="image/png")


@app.post("/v1/computers/{cid}/action")
def action(cid: str, body: dict = Body(...), wake: bool = False):
    row = lifecycle.ensure_running(cid, wake)
    out = desk_json(row, "POST", "/action", json=body, timeout=40)
    store.touch(cid)
    return out


@app.post("/v1/computers/{cid}/exec")
def exec_(cid: str, body: dict = Body(...), wake: bool = False):
    row = lifecycle.ensure_running(cid, wake)
    if "command" not in body:
        raise ApiError(400, "bad_request", "body needs 'command'")
    timeout = min(int(body.get("timeout_s") or 30), 600)
    out = desk_json(row, "POST", "/exec", json=body, timeout=timeout + 15)
    store.touch(cid)
    return out


@app.post("/v1/computers/{cid}/eval")
def eval_(cid: str, body: dict = Body(...), wake: bool = False):
    row = lifecycle.ensure_running(cid, wake)
    if "expression" not in body:
        raise ApiError(400, "bad_request", "body needs 'expression'")
    timeout = min(int(body.get("timeout_s") or 20), 120)
    out = desk_json(row, "POST", "/eval", json=body, timeout=timeout + 15)
    store.touch(cid)
    return out


@app.post("/v1/computers/{cid}/navigate")
def navigate_(cid: str, body: dict = Body(...), wake: bool = False):
    row = lifecycle.ensure_running(cid, wake)
    if "url" not in body:
        raise ApiError(400, "bad_request", "body needs 'url'")
    timeout = max(1, min(int(body.get("timeout_s") or 30), 120))   # never navigate then
    out = navigate(row, body["url"], timeout)                      # report failure at t=0
    store.touch(cid)
    return out


# ---------- element-level browsing (browse.py; control-plane composition) ----------

@app.get("/v1/computers/{cid}/page")
def page_(cid: str, wake: bool = False):
    row = lifecycle.ensure_running(cid, wake)
    out = browse.snapshot(row)
    store.touch(cid)
    return out


@app.post("/v1/computers/{cid}/click")
def click_(cid: str, body: dict = Body(...), wake: bool = False):
    row = lifecycle.ensure_running(cid, wake)
    if "ref" not in body:
        raise ApiError(400, "bad_request", "body needs 'ref' (from GET /page)")
    out = browse.click_element(row, int(body["ref"]), name=body.get("name"),
                               text=body.get("text"),
                               screenshot=bool(body.get("screenshot")))
    store.touch(cid)
    return out


@app.post("/v1/computers/{cid}/fill")
def fill_(cid: str, body: dict = Body(...), wake: bool = False):
    row = lifecycle.ensure_running(cid, wake)
    out = browse.fill(row, body.get("fields"), submit=bool(body.get("submit")))
    store.touch(cid)
    return out


@app.post("/v1/computers/{cid}/wait")
def wait_(cid: str, body: dict = Body(...), wake: bool = False):
    row = lifecycle.ensure_running(cid, wake)
    timeout = max(1, min(int(body.get("timeout_s") or 30), 120))
    out = browse.wait_for(row, selector=body.get("selector"), text=body.get("text"),
                          gone=bool(body.get("gone")),
                          network_idle=bool(body.get("network_idle")), timeout_s=timeout)
    store.touch(cid)
    return out


@app.post("/v1/computers/{cid}/teach-tick")
def teach_tick_(cid: str, wake: bool = False):
    row = lifecycle.ensure_running(cid, wake)
    out = browse.teach_tick(row)
    store.touch(cid)
    return out


@app.post("/v1/computers/{cid}/tabs")
def tabs_(cid: str, body: dict = Body(default={}), wake: bool = False):
    row = lifecycle.ensure_running(cid, wake)
    out = browse.tabs(row, action=body.get("action") or "list",
                      target_id=body.get("target_id"), url=body.get("url"))
    store.touch(cid)
    return out


@app.put("/v1/computers/{cid}/files", status_code=201)
async def file_put(cid: str, path: str, request: Request, wake: bool = False):
    data = await request.body()
    row = lifecycle.ensure_running(cid, wake)
    out = desk_json(row, "PUT", "/file", params={"path": path}, data=data, timeout=120)
    store.touch(cid)
    return out


@app.get("/v1/computers/{cid}/files")
def file_get(cid: str, path: str, wake: bool = False):
    row = lifecycle.ensure_running(cid, wake)
    content = desk_bytes(row, "GET", "/file", params={"path": path}, timeout=120)
    store.touch(cid)
    return Response(content, media_type="application/octet-stream")


# ---------- network capture ----------

@app.post("/v1/computers/{cid}/capture")
def capture_start(cid: str, body: dict = Body(...), wake: bool = False):
    row = lifecycle.ensure_running(cid, wake)
    if "pattern" not in body:
        raise ApiError(400, "bad_request", "body needs 'pattern'")
    pattern = body["pattern"]
    if len(pattern) > 200:
        raise ApiError(400, "bad_request", "pattern too long")
    out = desk_json(row, "POST", "/capture/start", json=body, timeout=20)
    store.touch(cid)
    return out


@app.get("/v1/computers/{cid}/capture")
def capture_get(cid: str, wake: bool = False):
    row = lifecycle.ensure_running(cid, wake)
    out = desk_json(row, "GET", "/capture", timeout=20)
    store.touch(cid)
    return out


@app.delete("/v1/computers/{cid}/capture")
def capture_delete(cid: str, wake: bool = False):
    row = lifecycle.ensure_running(cid, wake)
    out = desk_json(row, "DELETE", "/capture", timeout=20)
    store.touch(cid)
    return out


# ---------- credentials & login ----------

def credential_json(row):
    keys = row.keys() if hasattr(row, "keys") else row
    proof_raw = row["proof_spec"] if "proof_spec" in keys else None
    hosts_raw = row["verification_hosts"] if "verification_hosts" in keys else None
    probe = row["probe_url"] if "probe_url" in keys else None
    hosts = None
    if hosts_raw:
        try:
            hosts = json.loads(hosts_raw) if isinstance(hosts_raw, str) else hosts_raw
        except (TypeError, ValueError):
            hosts = None
        if not isinstance(hosts, list):
            hosts = None
    return {"name": row["name"], "username": row["username"],
            "domains": json.loads(row["domains"]), "has_totp": row["totp_seed"] is not None,
            "has_otp_phone": row["otp_phone"] is not None, "created_at": row["created_at"],
            "last_verified_at": row["last_verified_at"], "last_status": row["last_status"],
            "probe_url": probe or None,
            "has_proof_spec": bool(proof_raw and proof_raw not in ("", "{}")),
            "verification_hosts": hosts}


@app.post("/v1/computers/{cid}/credentials", status_code=201)
def add_credential(cid: str, body: dict = Body(...)):
    lifecycle.get_computer(cid)
    for field in ("name", "username", "secret", "domains"):
        if not body.get(field):
            raise ApiError(400, "bad_request", f"missing {field!r}")
    if not isinstance(body["domains"], list):
        raise ApiError(400, "bad_request", "domains must be a list")
    proof = body.get("proof_spec") if "proof_spec" in body else store._PROFILE_UNSET
    hosts = body.get("verification_hosts") if "verification_hosts" in body else store._PROFILE_UNSET
    probe = body.get("probe_url") if "probe_url" in body else store._PROFILE_UNSET
    if proof is not store._PROFILE_UNSET and proof is not None and not isinstance(proof, (dict, str)):
        raise ApiError(400, "bad_request", "proof_spec must be an object")
    if hosts is not store._PROFILE_UNSET and hosts is not None and not isinstance(hosts, list):
        raise ApiError(400, "bad_request", "verification_hosts must be a list")
    store.upsert_credential(
        cid, body["name"], body["username"], body["secret"],
        body.get("totp_seed"), body.get("otp_phone"), body["domains"],
        probe_url=probe, proof_spec=proof, verification_hosts=hosts)
    return credential_json(store.get_credential(cid, body["name"]))


@app.get("/v1/computers/{cid}/credentials")
def list_credentials(cid: str):
    lifecycle.get_computer(cid)
    return {"credentials": [credential_json(r) for r in store.list_credentials(cid)]}


@app.get("/v1/credentials")
def list_all_credentials():
    """Account-wide vault view for the console's CREDENTIALS tab. Still credential_json,
    so this widens the audience, never the shape."""
    names = store.computer_names()
    return {"credentials": [{**credential_json(r), "computer_id": r["computer_id"],
                             "computer_name": names.get(r["computer_id"],
                                                        r["computer_id"])}
                            for r in store.list_all_credentials()]}


@app.delete("/v1/computers/{cid}/credentials/{name}", status_code=204)
def delete_credential(cid: str, name: str):
    lifecycle.get_computer(cid)
    if store.delete_credential(cid, name) == 0:
        raise ApiError(404, "not_found", f"no credential {name!r}")
    return Response(status_code=204)


# ---------- human links (fill + desk) ----------
# Minted URLs are the only human auth on a box: no accounts, no sessions.
# Minting stays loopback-only (bin/case or the Drive UI), /v1 is deliberately
# NOT behind the public bearer door, because the
# agent's token must not be able to answer handoffs or mint its own links.

@app.post("/v1/computers/{cid}/links", status_code=201)
def mint_link(cid: str, body: dict = Body(...)):
    lifecycle.get_computer(cid)
    kind = body.get("kind")
    if kind not in ("fill", "vnc"):
        raise ApiError(400, "bad_request", "kind must be 'fill' or 'vnc'")
    ttl = body.get("ttl_s")
    if ttl is not None and not isinstance(ttl, int):
        raise ApiError(400, "bad_request", "ttl_s must be an integer number of seconds")
    return links.mint(cid, kind, ttl)


@app.post("/v1/links", status_code=201)
def mint_box_link(body: dict = Body(...)):
    """Box-scoped, console only. A console link belongs to the box, not to any one
    computer: POST /v1/computers/{cid}/links 404s on a box with no computers, which is
    exactly the state a brand-new box is in and exactly when the dashboard matters most.

    Console is deliberately NOT accepted by the computer-scoped route, so a console
    token, which is allowed to reach that route, to mint fill/vnc links, can never
    mint itself a fresh console token and extend its own access forever."""
    if body.get("kind") != "console":
        raise ApiError(400, "bad_kind", "box-scoped links are console only")
    # No ttl_s: console_check slides the expiry back out to the full TTL on every
    # successful use, so any value here would be silently discarded on first load.
    if "ttl_s" in body:
        raise ApiError(400, "bad_request",
                       "console links do not take ttl_s — every use slides the expiry")
    return links.mint(None, "console")


@app.delete("/v1/links")
def revoke_links():
    """Kill every outstanding human link on this box. MCP rotate calls it:
    a link token is a bearer capability too, so rotate must mean all of them."""
    return {"burned": store.burn_all_links()}


@app.get("/v1/connect")
def connect_reveal():
    """One-shot MCP paste-line for the console Connect modal. Consumes the pending
    Fernet copy; a second GET returns host + seen_at with token null."""
    st = store.mcp_token_status()
    host = st.get("host")
    token, seen_at = store.mcp_token_take()
    if not host:
        # never seeded, still a 200 so the modal can say "ask the operator"
        return _paste_payload(None, None, seen_at)
    if token:
        return _paste_payload(host, token, None)
    return _paste_payload(host, None, seen_at)


@app.post("/v1/mcp/seed")
def mcp_seed(request: Request, body: dict = Body(...)):
    """Loopback-only: writes the reveal copy after the MCP door is written. Never on the
    console door, a browser Link must not deposit an arbitrary bearer."""
    if from_console(request):
        raise ApiError(404, "not_found", "not_found")
    token = (body.get("token") or "").strip()
    host = (body.get("host") or "").strip()
    origin = (body.get("origin") or DEFAULT_CONSOLE_ORIGIN).strip()
    if not TOKEN_RE.match(token):
        raise ApiError(400, "bad_request", "token must be cs_ + 32 hex")
    if not HOST_RE.match(host):
        raise ApiError(400, "bad_request", "host must be a hostname")
    if not origin.startswith("https://"):
        raise ApiError(400, "bad_request", "origin must be an https URL")
    store.mcp_token_put(token, host=host, origin=origin)
    return {"ok": True}


@app.post("/v1/mcp/rotate")
def mcp_rotate(request: Request, body: dict = Body(...)):
    """Console door: mint a new cs_, rewrite Caddy env, burn fill/desk links, spare
    the caller's console Link, return the new paste once."""
    if (body or {}).get("confirm") != "rotate":
        raise ApiError(400, "confirm_rotate", "body must be {\"confirm\":\"rotate\"}")
    st = store.mcp_token_status()
    host = st.get("host")
    origin = st.get("origin") or DEFAULT_CONSOLE_ORIGIN
    if not host:
        # fall back to the public Host header (console door keeps the FQDN)
        host = (request.headers.get("host") or "").split(":")[0].strip().lower()
    if not HOST_RE.match(host or ""):
        raise ApiError(400, "bad_request", "box has no seeded host; POST /v1/mcp/seed with host first")
    token = _new_mcp_token()
    err = _door_write(host, token, origin)
    if err:
        raise ApiError(503, "door_write_failed", err)
    caller = _link_token(request)
    burned = store.burn_all_links(except_token=caller)
    # Put then immediately take: the take is what stamps seen_at, so a reload of
    # /v1/connect correctly says "already shown". We build the payload from the
    # plaintext we already hold, so a concurrent /v1/connect winning the take
    # cannot turn a completed rotation into a 500.
    store.mcp_token_put(token, host=host, origin=origin)
    store.mcp_token_take()
    out = _paste_payload(host, token, None)
    out["burned_links"] = burned
    out["warning"] = ("old MCP paste-line is dead; outstanding /fill and /desk links "
                      "were revoked. this console session stays up.")
    return out


def _fill_box_host(request: Request):
    """Host for the console ?box= param.

    Prefer box_meta (what mcp/seed recorded), then CASE_MCP_HOST,
    then the request host so a mis-set env still sends the human home.
    """
    st = store.mcp_token_status()
    return (st.get("host")
            or os.environ.get("CASE_MCP_HOST")
            or (request.url.hostname if request else None)
            or "")


def _fill_console_origin():
    """Origin the console bookmark was minted against.

    DEFAULT_CONSOLE_ORIGIN is import-time env only, cased does not load
    /etc/caddy/case.env. Preview / non-default boxes store the live origin in
    box_meta at seed time; that is the origin whose sessionStorage holds the
    console Link token, so the Back button must use it (same source mcp_rotate
    uses when rewriting case.env).
    """
    return store.mcp_token_status().get("origin") or DEFAULT_CONSOLE_ORIGIN


def _fill_page(page_html, request: Request, status_code=200):
    """DONE/GONE pages carry a Back-to-console button when we know the box host."""
    return HTMLResponse(
        links.with_console_back(page_html, _fill_box_host(request),
                                origin=_fill_console_origin()),
        status_code=status_code)


@app.get("/fill/{token}")
def fill_form(token: str, request: Request):
    row = links.valid(token, "fill")
    if not row:
        return _fill_page(links.GONE_HTML, request, status_code=410)
    comp = store.get_computer(row["computer_id"])
    # A bare id ("c_100c5af520") reads as machine noise, so a human skims past it and
    # cannot tell they are filling the wrong computer's vault. Say the state and the id
    # too: an agent that never named the computer leaves name == id, and then the state
    # is the only thing distinguishing this box's computers from each other.
    if comp:
        label = comp["name"] or comp["id"]
        if comp["name"] and comp["name"] != comp["id"]:
            label = f'{label} ({comp["id"]})'
        label = f'{label} — {comp["state"]}'
    else:
        label = "your computer"
    # escape: the computer name is set by the agent (MCP computer_create), and this is
    # the one page where a human types a password. Unescaped, the agent could script
    # its own credential-entry form, exactly what the vault exists to prevent.
    return HTMLResponse(links.FILL_HTML.replace("{computer}", html.escape(label)))


@app.post("/fill/{token}")
async def fill_submit(token: str, request: Request):
    row = links.valid(token, "fill")
    if not row:
        return _fill_page(links.GONE_HTML, request, status_code=410)
    # plain <form method=post>: urlencoded, parsed by hand, no multipart dep
    form = {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}
    raw = [d for d in (form.get("domains") or "").split(",") if d.strip()]
    domains = [d for d in (links.normalize_domain(d) for d in raw) if d]
    username = (form.get("username") or "").strip()
    secret = form.get("secret") or ""
    if not (domains and username and secret):
        raise ApiError(400, "bad_request", "website, username and password are required "
                                           "(website must be a domain, e.g. gmail.com)")
    # burn BEFORE the write: valid()+burn are two statements, so a double submit could
    # otherwise pass both checks and write twice. Compare-and-set makes the loser a 410.
    if store.burn_link(token) == 0:
        return _fill_page(links.GONE_HTML, request, status_code=410)
    store.upsert_credential(row["computer_id"], domains[0], username, secret,
                            (form.get("totp_seed") or "").strip() or None, None, domains)
    events.emit("credential_added", {"computer_id": row["computer_id"], "name": domains[0]})
    return _fill_page(links.DONE_HTML, request)


# ---------- Assist door (public /assist/*, token is the auth; no MCP bearer) ----------

def _cookie_value(cookie_header, name):
    return dict(p.strip().split("=", 1)
                for p in (cookie_header or "").split(";") if "=" in p).get(name, "")


def _assist_cookie(request):
    return _cookie_value(request.headers.get("cookie"), assist.COOKIE)


def _assist_set_cookie(view, set_sess):
    # Same-origin form submissions must retain provenance for check_same_origin;
    # this policy still strips the emailed Assist token on cross-origin requests.
    headers = {"Cache-Control": "no-store", "Referrer-Policy": "same-origin"}
    if set_sess:
        bound = view["bound"]
        headers["Set-Cookie"] = assist.session_cookie_header(
            set_sess, max_age=assist.session_max_age(bound["id"]) or assist.SESSION_TTL_S)
    return headers


@app.get("/assist/static/assist.js")
def assist_static_js():
    """Same-origin poll script, CSP script-src 'self' (no inline)."""
    return Response(assist.ASSIST_JS, media_type="application/javascript",
                    headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


@app.get("/assist/{token}")
def assist_get(token: str, request: Request):
    try:
        view, set_sess = assist.resolve_view(token, request.headers.get("cookie", ""))
    except ApiError:
        return HTMLResponse(assist.GONE_HTML, status_code=410,
                            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
    body = assist.render_page(view, token)
    return HTMLResponse(body, headers=_assist_set_cookie(view, set_sess))


@app.get("/assist/{token}/state")
def assist_state(token: str, request: Request):
    """JSON poll surface, status/revision/actions only; never secrets or URLs."""
    try:
        view, set_sess = assist.resolve_view(token, request.headers.get("cookie", ""))
    except ApiError as e:
        if e.status == 410:
            return JSONResponse({"error": {"code": "gone", "message": e.message}},
                                status_code=410,
                                headers={"Cache-Control": "no-store"})
        raise
    headers = _assist_set_cookie(view, set_sess)
    return JSONResponse(assist.state_payload(view), headers=headers)


@app.post("/assist/{token}/open")
async def assist_open(token: str, request: Request):
    """Navigate remote Chromium to a pastable HTTPS URL (allowlisted hosts only)."""
    if not assist.check_same_origin(request):
        raise ApiError(403, "csrf", "missing or mismatched Origin")
    sess = _assist_cookie(request)
    if not sess:
        return HTMLResponse(assist.GONE_HTML, status_code=410,
                            headers={"Cache-Control": "no-store"})
    try:
        assist.resolve_view(token, request.headers.get("cookie", ""))
    except ApiError:
        return HTMLResponse(assist.GONE_HTML, status_code=410,
                            headers={"Cache-Control": "no-store"})
    form = assist.parse_form(await request.body())
    url = (form.get("url") or "").strip()
    rev = form.get("expected_revision")
    expected = int(rev) if rev not in (None, "") else None
    try:
        assist.open_with_session(sess, url, expected_revision=expected)
    except ApiError as e:
        if e.status == 410:
            return HTMLResponse(assist.GONE_HTML, status_code=410,
                                headers={"Cache-Control": "no-store"})
        raise
    return HTMLResponse(
        assist.DONE_HTML.replace("{title}", "Opened").replace(
            "{body}", "The computer opened the link. Finish there, then return here."),
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


@app.post("/assist/{token}/submit")
async def assist_submit(token: str, request: Request):
    """OTP / submit_value, auth is the case_assist session cookie."""
    if not assist.check_same_origin(request):
        raise ApiError(403, "csrf", "missing or mismatched Origin")
    sess = _assist_cookie(request)
    if not sess:
        return HTMLResponse(assist.GONE_HTML, status_code=410)
    try:
        view, _ = assist.resolve_view(token, request.headers.get("cookie", ""))
    except ApiError:
        return HTMLResponse(assist.GONE_HTML, status_code=410)
    if "submit_value" not in view["allowed_actions"]:
        return HTMLResponse(
            assist.render_page(view, token),
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
    form = assist.parse_form(await request.body())
    value = (form.get("value") or "").strip()
    if not value:
        raise ApiError(400, "bad_request", "value is required")
    rev = form.get("expected_revision")
    expected = int(rev) if rev not in (None, "") else None
    try:
        row = assist.submit_with_session(sess, value, expected_revision=expected)
    except ApiError as e:
        if e.status == 410:
            return HTMLResponse(assist.GONE_HTML, status_code=410)
        raise
    st = row["status"]
    if st in ("completed", "answered"):
        title, body = "Submitted ✓", "The code was accepted. You can close this page."
    elif st == "failed":
        title, body = "Failed", "This challenge could not be completed. Ask for a new link."
    else:
        title, body = "Not yet", ("That code did not clear the challenge. "
                                  "Reopen the link and try again.")
    return HTMLResponse(assist.DONE_HTML.replace("{title}", title).replace("{body}", body))


@app.post("/assist/{token}/done")
async def assist_done(token: str, request: Request):
    """CAPTCHA / verify_page, human cleared the live desk challenge."""
    if not assist.check_same_origin(request):
        raise ApiError(403, "csrf", "missing or mismatched Origin")
    sess = _assist_cookie(request)
    if not sess:
        return HTMLResponse(assist.GONE_HTML, status_code=410)
    try:
        assist.resolve_view(token, request.headers.get("cookie", ""))
    except ApiError:
        return HTMLResponse(assist.GONE_HTML, status_code=410)
    form = assist.parse_form(await request.body())
    rev = form.get("expected_revision")
    expected = int(rev) if rev not in (None, "") else None
    try:
        row = assist.done_with_session(sess, expected_revision=expected)
    except ApiError as e:
        if e.status == 410:
            return HTMLResponse(assist.GONE_HTML, status_code=410)
        raise
    st = row["status"]
    if st == "completed" or st == "answered":
        title, body = "Done ✓", "Challenge cleared. You can close this page."
    elif st == "failed":
        title, body = "Failed", "This challenge could not be completed. Ask for a new link."
    else:
        title, body = "Still open", ("The challenge still looks present. "
                                     "Finish it on the desktop, then click I'm done again.")
    return HTMLResponse(assist.DONE_HTML.replace("{title}", title).replace("{body}", body))


@app.get("/v1/desk/check")
def desk_check_ep(request: Request):
    uri = request.headers.get("x-forwarded-uri", "")
    cookie = request.headers.get("cookie", "")
    link, set_tok = links.desk_check(uri, cookie)
    if not link:
        handoff = assist.valid_session(_cookie_value(cookie, assist.COOKIE))
        if handoff:
            link, set_tok = {"computer_id": handoff["computer_id"], "kind": "assist",
                             "expires_at": None, "token": None}, None
    if not link:
        return Response("unauthorized", status_code=401)
    # The door is one fixed host port, so a live token is not enough: the computer it
    # was minted for must be the one actually sitting behind that port. Otherwise the
    # partner meets whichever desktop happens to be awake, or a bare 502.
    comp = store.get_computer(link["computer_id"])
    if not comp or comp["state"] != "running":
        return HTMLResponse(links.NOTREADY_HTML.replace("{why}", links.ASLEEP), status_code=409)
    if VNC_PORT and comp["vnc_port"] != VNC_PORT:
        return HTMLResponse(links.NOTREADY_HTML.replace("{why}", links.STALE_PORT),
                            status_code=409)
    if not set_tok:
        return Response(status_code=200)      # cookie already good, let the request through
    # First hit, token in the URL. Caddy's forward_auth only forwards a NON-2xx auth
    # response to the browser (2xx just continues upstream), so a 302 is the only way to
    # hand a human a cookie here, and it strips the token out of history and Referer.
    return Response(status_code=302, headers={
        "Location": links.strip_token(uri),
        # never outlive the token itself: the 302 strips ?token= from the URL, so the
        # cookie is the browser's only copy of the credential.
        "Set-Cookie": (f"case_desk={set_tok}; Path=/desk; Max-Age={links.seconds_left(link)}; "
                       "Secure; HttpOnly; SameSite=Lax"),
        "Cache-Control": "no-store"})


@app.get("/v1/console/check")
def console_check_ep(request: Request):
    """Caddy forward_auth target for /console/*. 200 lets the request through to the
    allowlisted /v1 route; anything else is answered to the browser."""
    if links.console_check(request.headers.get("authorization")):
        return Response(status_code=200)
    raise ApiError(401, "unauthorized", "unauthorized")


@app.get("/v1/auth-attempts/{aid}")
def get_auth_attempt(aid: str):
    return auth_attempts.get_attempt(aid)


@app.get("/v1/auth-attempts/{aid}/wait")
async def wait_auth_attempt(
        aid: str,
        after_revision: int = Query(0),
        after_handoff_id: str | None = Query(None),
        timeout_s: int = Query(auth_attempts.WAIT_TIMEOUT_DEFAULT_S)):
    """Long-poll until the attempt cursor advances, becomes terminal, or times out."""
    return await auth_attempts.wait_attempt(
        aid,
        after_revision=after_revision,
        after_handoff_id=after_handoff_id,
        timeout_s=timeout_s)


@app.post("/v1/auth-attempts/{aid}/cancel")
def cancel_auth_attempt(aid: str, body: dict = Body(None)):
    body = body or {}
    return auth_attempts.cancel_attempt(aid, expected_revision=body.get("expected_revision"))


@app.post("/v1/computers/{cid}/login")
def login(cid: str, body: dict = Body(...), wake: bool = False):
    row = lifecycle.ensure_running(cid, wake)
    name = body.get("credential")
    material = store.credential_material(cid, name)
    if not material:
        raise ApiError(404, "not_found", f"no credential {name!r}")
    if not body.get("url"):
        raise ApiError(400, "bad_request", "missing 'url'")

    proof_spec = login_flow._credential_proof_spec(cid, name, body.get("proof_spec"))
    attempt = auth_attempts.start_attempt(
        cid, name, body["url"],
        proof_spec=proof_spec,
        idempotency_key=body.get("idempotency_key"))
    # Idempotent replay, never re-inject; agents poll GET /auth-attempts/{id}.
    if attempt["status"] == "awaiting_human":
        return auth_attempts.login_result(attempt)
    if attempt["status"] in auth_attempts.TERMINAL_STATUSES:
        return auth_attempts.login_result(attempt)
    if attempt["status"] in ("advancing", "proving"):
        advanced = auth_attempts.advance_attempt(attempt["id"])
        return auth_attempts.login_result(advanced)

    try:
        result = desk_json(row, "POST", "/login",
                           json={"credential": material, "url": body["url"]}, timeout=95)
    except ApiError:
        # domain_mismatch / desk errors must not leave the attempt stuck in
        # created, that blocks every later login with 409 auth_in_progress.
        auth_attempts.fail_attempt(attempt["id"], reason="desk_error")
        raise
    store.touch(cid)
    return login_flow._login_after_desk(row, cid, name, body["url"], attempt, result)


# ---------- handoffs ----------

@app.get("/v1/handoffs")
def list_handoffs(status: str = None):
    return {"handoffs": handoffs.list_handoffs(status)}


@app.get("/v1/handoffs/{hid}")
def get_handoff_ep(hid: str):
    """One handoff, screenshot included. The list routes drop it, see handoffs.py."""
    return handoffs.get_handoff(hid)


@app.get("/v1/computers/{cid}/handoffs")
def list_computer_handoffs(cid: str):
    return {"handoffs": handoffs.list_computer_handoffs(cid)}


@app.post("/v1/computers/{cid}/handoffs", status_code=201)
def request_handoff(cid: str, body: dict = Body(...)):
    return handoffs.request_handoff(cid, body.get("kind"), body.get("prompt"))


@app.post("/v1/handoffs/{hid}/answer")
def answer_handoff_ep(hid: str, body: dict = Body(...)):
    if "value" not in body:
        raise ApiError(400, "bad_request", "missing 'value'")
    return handoffs.handoff_json(handoffs.answer_handoff(hid, str(body["value"])))


# ---------- schedules ----------

@app.post("/v1/computers/{cid}/schedules", status_code=201)
def create_schedule(cid: str, body: dict = Body(...)):
    return scheduler.create_schedule(cid, body)


@app.get("/v1/computers/{cid}/schedules")
def list_schedules(cid: str):
    return scheduler.list_schedules(cid)


@app.delete("/v1/schedules/{sid}", status_code=204)
def delete_schedule(sid: str):
    scheduler.delete_schedule(sid)
    return Response(status_code=204)


@app.post("/v1/schedules/{sid}/run", status_code=202)
def run_schedule_now(sid: str):
    if not store.get_schedule(sid):
        raise ApiError(404, "not_found", f"no schedule {sid}")
    threading.Thread(target=scheduler.run_schedule, args=(sid,), daemon=True).start()
    return {"status": "started", "schedule": sid}


@app.get("/v1/schedules/{sid}/runs")
def list_runs(sid: str):
    return scheduler.list_runs(sid)


# ---------- runs (the console's ACTIVITY feed) ----------

def run_json(row, names=None):
    # artifact_path is a host path, the console gets a flag and fetches the bytes by id.
    # `names` is store.computer_names(); list routes pass it so a 50-run feed resolves
    # its owners in one query rather than fifty.
    cid = row["computer_id"]
    return {"id": row["id"], "schedule_id": row["schedule_id"], "computer_id": cid,
            "computer_name": names.get(cid, cid) if names else store.computer_name(cid),
            "started_at": row["started_at"], "ended_at": row["ended_at"],
            "exit_code": row["exit_code"], "summary": row["summary"],
            "status": row["status"], "has_screenshot": bool(row["artifact_path"])}


@app.get("/v1/runs")
def list_all_runs(limit: int = 50):
    names = store.computer_names()
    return {"runs": [run_json(r, names)
                     for r in store.list_all_runs(min(max(limit, 1), 200))]}


@app.get("/v1/runs/{rid}/screenshot")
def run_screenshot(rid: str):
    row = store.get_run(rid)
    if not row:
        raise ApiError(404, "not_found", f"no run {rid!r}")
    path = row["artifact_path"]
    # Serve only what we recorded, and only from RUNS_DIR, never a caller-supplied path.
    # realpath, not abspath: abspath is pure string arithmetic, so a symlink sitting in
    # RUNS_DIR and pointing anywhere on the disk would pass the containment check.
    if not path or os.path.dirname(os.path.realpath(path)) != os.path.realpath(RUNS_DIR) \
            or not os.path.exists(path):
        raise ApiError(404, "no_screenshot", f"run {rid!r} has no screenshot")
    with open(path, "rb") as f:
        return Response(content=f.read(), media_type="image/png")


# ---------- background loops ----------

def _spawn(fn, arg):
    threading.Thread(target=fn, args=(arg,), daemon=True).start()


def sweeper():
    tick = 0
    while True:
        time.sleep(20)
        tick += 1
        try:
            lifecycle.reconcile()      # re-align DB with Docker if the daemon restarted
            handoffs.expire_stale()
            store.prune_expired_links()
            store.prune_expired_assist_tokens()
            if tick % 180 == 0:
                cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).strftime(
                    "%Y-%m-%dT%H:%M:%SZ")
                unlink_run_artifacts(store.prune_old_runs(keep=1000))
                prune_old_audit_files()
                store.prune_terminal_handoffs(cutoff)
            scheduler.fire_due_schedules(_spawn)
            session_keeper.tick()      # preflight persistent session health
        except Exception:
            log.exception("sweeper")


def blocker_poller():
    while True:
        time.sleep(3)
        try:
            for row in store.running_rows():
                try:
                    b = desk_json(row, "GET", "/blocker", timeout=5).get("blocker")
                except ApiError:
                    continue
                cid = row["id"]
                if not b:
                    BLOCKER_SEEN.pop(cid, None)
                    continue
                if BLOCKER_SEEN.get(cid) == b["fingerprint"]:
                    continue
                if login_flow._route_blocker(row, b):
                    # Record only after routing succeeds, so transient errors retry.
                    BLOCKER_SEEN[cid] = b["fingerprint"]
        except Exception:
            log.exception("blocker poller")


if __name__ == "__main__":
    if BIND_HOST not in ("127.0.0.1", "::1", "localhost") and not case_token():
        log.warning("cased is bound to %s with no CASE_TOKEN, so anything on this "
                    "network (including the desktops) can drive the API. Set "
                    "CASE_TOKEN in .env to require a bearer token.", BIND_HOST)
    uvicorn.run(app, host=BIND_HOST, port=BIND_PORT, log_level="warning")
