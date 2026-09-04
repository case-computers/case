#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""cased, the Case control plane.

REST on loopback by default (CASE_BIND/CASE_PORT). This module is the composition
root: the FastAPI app, the (thin) route table, and startup wiring. Behaviour lives
in the modules it delegates to — lifecycle, handoffs, scheduler, deskclient,
dockerd, store, events.
"""
import asyncio
import base64
from contextlib import asynccontextmanager, contextmanager
import hmac
import html
import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, unquote, urlsplit

import requests
import uvicorn
from fastapi import Body, FastAPI, Query, Request, WebSocket
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from websockets.asyncio.client import connect as ws_connect

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
from util import now, row_get

@asynccontextmanager
async def lifespan(_app):
    """Startup, then (after the yield) shutdown. `@app.on_event` is deprecated in
    FastAPI and warned about on every boot; a lifespan says the same thing once.
    Names below (sweeper, blocker_poller) are resolved when this runs, not when it
    is defined, so they may live further down the file."""
    events.set_loop(asyncio.get_running_loop())
    handoffs.rebuild_login_ctx()          # recover pending login handoffs across a restart
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
    # never echo the submitted body, it can carry a secret. Report only where/why.
    details = [{"loc": er.get("loc"), "msg": er.get("msg"), "type": er.get("type")}
               for er in e.errors()]
    return JSONResponse({"error": {"code": "bad_request", "message": "request validation failed",
                                   "details": details}}, status_code=400)


@app.exception_handler(Exception)
async def internal_error(_, e):
    log.exception("internal error")
    return JSONResponse({"error": {"code": "internal", "message": "internal error"}},
                        status_code=500)


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
    return hmac.compare_digest(auth[7:].strip(), want)


PUBLIC_PREFIXES = ("/fill/", "/assist/", "/answer/")   # token-in-URL doors, no bearer


def allowed_hosts():
    """Names a browser may address us as; anything else is a rebinding page."""
    hosts = {"127.0.0.1", "localhost", "[::1]", "cased"}
    hosts.update(h.strip().lower()
                 for h in (os.environ.get("CASE_ALLOWED_HOSTS") or "").split(",") if h.strip())
    pub = (os.environ.get("CASE_PUBLIC_HOST") or "").strip().lower()
    if pub:
        hosts.add(pub)
    return hosts


def _host_of(value):
    v = (value or "").strip().lower()
    return v[:v.find("]") + 1] if v.startswith("[") else v.split(":")[0]


def browser_ok(request):
    """Host must be ours; a present Origin must be ours too (CSRF)."""
    if _host_of(request.headers.get("host")) not in allowed_hosts():
        return False
    origin = request.headers.get("origin")
    return not origin or _host_of(origin.split("//", 1)[-1]) in allowed_hosts()


# ---------- audit log ----------
# One JSONL line per API call, ~/.case/audit/<date>.jsonl. Answers "what did the
# agent do on this machine" without agents self-logging transcripts. Sessions are
# whatever the client sends as X-Case-Session (the MCP server sends one per process),
# alongside the caller's address and the query string.
# Security invariant: response bodies are NEVER logged (screenshots, file contents),
# and request bodies that can carry secrets are redacted, secrets never hit disk.
# Redacted routes: /credentials (password/TOTP), /answer (OTP codes relayed by the
# human), /files (uploaded file contents may hold tokens), /fill (the human
# credential form posts the password itself).
# Registered BEFORE token_guard: Starlette runs the last-registered middleware
# outermost, so an unauthorized call is rejected without reaching the log.

def _redacted(path):
    return ("/credentials" in path or path.endswith("/answer")
            or path.startswith("/answer/")
            or path.endswith("/files") or path.startswith("/fill/")
            or path.endswith("/fill")   # agent form-fill bodies carry user data
            or path.startswith("/assist/"))


@app.middleware("http")
async def audit_mw(request: Request, call_next):
    path = request.url.path
    big = int(request.headers.get("content-length") or 0) > 64 * 1024
    skip_body = request.method in ("GET", "DELETE") or _redacted(path) or big
    body = b"" if skip_body else await request.body()
    t0 = time.time()
    resp = await call_next(request)
    # skip noise + SSE streams; a desk open is ~25 asset GETs through the live relay
    if path != "/health" and not path.endswith("/events") and "/live/" not in path:
        req = "[redacted]" if _redacted(path) else body[:2000].decode("utf-8", "replace")
        # a fill/assist/answer token is a live capability, the log records that the
        # door was used, never the key itself
        if path.startswith("/fill/"):
            logged_path = "/fill/[token]"
        elif path.startswith("/assist/"):
            logged_path = "/assist/[token]" + (
                "/submit" if path.endswith("/submit") else
                "/done" if path.endswith("/done") else "")
        elif path.startswith("/answer/"):
            logged_path = "/answer/[token]"
        else:
            logged_path = path
        line = {"ts": now(), "session": request.headers.get("x-case-session", "-"),
                "client": request.client.host if request.client else "-",
                "method": request.method, "path": logged_path, "query": request.url.query,
                "status": resp.status_code, "ms": int((time.time() - t0) * 1000),
                "req": "[large]" if big else req}
        await asyncio.to_thread(_audit_append, line)
    return resp


def _audit_append(line):
    os.makedirs(AUDIT_DIR, mode=0o700, exist_ok=True)
    p = os.path.join(AUDIT_DIR, time.strftime("%Y-%m-%d") + ".jsonl")
    with os.fdopen(os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "a") as f:
        f.write(json.dumps(line) + "\n")


@app.middleware("http")
async def token_guard(request: Request, call_next):
    path = request.url.path
    # /health stays open so compose can probe us without circulating the token.
    if path == "/health":
        return await call_next(request)
    if path.startswith(PUBLIC_PREFIXES) or not case_token():
        if not browser_ok(request):
            return JSONResponse(
                {"error": {"code": "bad_host", "message": "unexpected Host or Origin"}},
                status_code=403)
        return await call_next(request)
    if not bearer_ok(request.headers.get("authorization")):
        return JSONResponse(
            {"error": {"code": "unauthorized", "message": "unauthorized"}},
            status_code=401)
    return await call_next(request)


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
def delete_computer(cid: str):
    lifecycle.get_computer(cid)
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
def health(request: Request):
    # Open door (compose probes it without the token), so an unauthenticated caller
    # learns liveness only — the inventory is for whoever holds the bearer.
    if not bearer_ok(request.headers.get("authorization")):
        return {"ok": True}
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

@contextmanager
def awake(cid, wake):
    """The shared desk-route preamble: ensure_running, then touch on success.

    Every route that drives the desk must touch last_active_at — session_keeper
    reads it to avoid navigating over a live session."""
    row = lifecycle.ensure_running(cid, wake)
    yield row
    store.touch(cid)


@app.get("/v1/computers/{cid}/screenshot")
def screenshot(cid: str, wake: bool = False, marks: bool = False):
    with awake(cid, wake) as row:
        # desk_bytes raises ApiError(423) during credential injection
        content = desk_bytes(row, "GET", "/screenshot")
        if marks:
            try:
                content = browse.overlay_marks(content, browse.element_rects(row))
            except Exception:
                pass
        return Response(content, media_type="image/png")


@app.post("/v1/computers/{cid}/action")
def action(cid: str, body: dict = Body(...), wake: bool = False):
    with awake(cid, wake) as row:
        return desk_json(row, "POST", "/action", json=body, timeout=40)


@app.post("/v1/computers/{cid}/exec")
def exec_(cid: str, body: dict = Body(...), wake: bool = False):
    with awake(cid, wake) as row:
        if "command" not in body:
            raise ApiError(400, "bad_request", "body needs 'command'")
        timeout = min(int(body.get("timeout_s") or 30), 600)
        return desk_json(row, "POST", "/exec", json=body, timeout=timeout + 15)


@app.post("/v1/computers/{cid}/eval")
def eval_(cid: str, body: dict = Body(...), wake: bool = False):
    with awake(cid, wake) as row:
        if "expression" not in body:
            raise ApiError(400, "bad_request", "body needs 'expression'")
        timeout = min(int(body.get("timeout_s") or 20), 120)
        return desk_json(row, "POST", "/eval", json=body, timeout=timeout + 15)


@app.post("/v1/computers/{cid}/navigate")
def navigate_(cid: str, body: dict = Body(...), wake: bool = False):
    with awake(cid, wake) as row:
        if "url" not in body:
            raise ApiError(400, "bad_request", "body needs 'url'")
        timeout = max(1, min(int(body.get("timeout_s") or 30), 120))   # never navigate then
        out = navigate(row, body["url"], timeout)                      # report failure at t=0
        if out.get("ok") and body.get("snapshot", True):
            fresh = browse.snapshot(row)      # navigate already waited for readyState
            if fresh.get("ok"):
                out["snapshot"] = fresh
        return out


# ---------- element-level browsing (browse.py; control-plane composition) ----------

@app.get("/v1/computers/{cid}/page")
def page_(cid: str, wake: bool = False):
    with awake(cid, wake) as row:
        return browse.snapshot(row)


@app.post("/v1/computers/{cid}/click")
def click_(cid: str, body: dict = Body(...), wake: bool = False):
    with awake(cid, wake) as row:
        if "ref" not in body:
            raise ApiError(400, "bad_request", "body needs 'ref' (from GET /page)")
        return browse.click_element(row, int(body["ref"]), name=body.get("name"),
                                    text=body.get("text"),
                                    screenshot=bool(body.get("screenshot")),
                                    snapshot_after=bool(body.get("snapshot", True)))


@app.post("/v1/computers/{cid}/hover")
def hover_(cid: str, body: dict = Body(...), wake: bool = False):
    with awake(cid, wake) as row:
        if "ref" not in body:
            raise ApiError(400, "bad_request", "body needs 'ref' (from GET /page)")
        return browse.hover(row, int(body["ref"]), name=body.get("name"))


@app.post("/v1/computers/{cid}/upload")
def upload_(cid: str, body: dict = Body(...), wake: bool = False):
    with awake(cid, wake) as row:
        if "ref" not in body or "path" not in body:
            raise ApiError(400, "bad_request", "body needs 'ref' and 'path'")
        return browse.upload(row, int(body["ref"]), body["path"], name=body.get("name"))


@app.post("/v1/computers/{cid}/fill")
def fill_(cid: str, body: dict = Body(...), wake: bool = False):
    with awake(cid, wake) as row:
        return browse.fill(row, body.get("fields"), submit=bool(body.get("submit")),
                           snapshot_after=bool(body.get("snapshot", True)))


@app.post("/v1/computers/{cid}/wait")
def wait_(cid: str, body: dict = Body(...), wake: bool = False):
    with awake(cid, wake) as row:
        timeout = max(1, min(int(body.get("timeout_s") or 30), 120))
        return browse.wait_for(row, selector=body.get("selector"), text=body.get("text"),
                               gone=bool(body.get("gone")),
                               network_idle=bool(body.get("network_idle")),
                               timeout_s=timeout)


@app.post("/v1/computers/{cid}/teach-tick")
def teach_tick_(cid: str, wake: bool = False):
    with awake(cid, wake) as row:
        return browse.teach_tick(row)


@app.post("/v1/computers/{cid}/tabs")
def tabs_(cid: str, body: dict = Body(default={}), wake: bool = False):
    with awake(cid, wake) as row:
        return browse.tabs(row, action=body.get("action") or "list",
                           target_id=body.get("target_id"), url=body.get("url"))


FILE_MAX = 8 * 1024 * 1024   # the body is buffered whole, so this is cased's RSS too


@app.put("/v1/computers/{cid}/files", status_code=201)
async def file_put(cid: str, path: str, request: Request, wake: bool = False):
    if int(request.headers.get("content-length") or 0) > FILE_MAX:
        raise ApiError(413, "too_large", "file over 8MB")
    data = await request.body()
    return await asyncio.to_thread(_file_put, cid, path, data, wake)


def _file_put(cid, path, data, wake):
    with awake(cid, wake) as row:
        return desk_json(row, "PUT", "/file", params={"path": path}, data=data, timeout=120)


@app.get("/v1/computers/{cid}/files")
def file_get(cid: str, path: str, wake: bool = False):
    with awake(cid, wake) as row:
        content = desk_bytes(row, "GET", "/file", params={"path": path}, timeout=120)
        return Response(content, media_type="application/octet-stream")


# ---------- network capture ----------

@app.post("/v1/computers/{cid}/capture")
def capture_start(cid: str, body: dict = Body(...), wake: bool = False):
    with awake(cid, wake) as row:
        if "pattern" not in body:
            raise ApiError(400, "bad_request", "body needs 'pattern'")
        if len(body["pattern"]) > 200:
            raise ApiError(400, "bad_request", "pattern too long")
        return desk_json(row, "POST", "/capture/start", json=body, timeout=20)


@app.get("/v1/computers/{cid}/capture")
def capture_get(cid: str, wake: bool = False):
    with awake(cid, wake) as row:
        return desk_json(row, "GET", "/capture", timeout=20)


@app.delete("/v1/computers/{cid}/capture")
def capture_delete(cid: str, wake: bool = False):
    with awake(cid, wake) as row:
        return desk_json(row, "DELETE", "/capture", timeout=20)


# ---------- credentials & login ----------

def credential_json(row):
    proof_raw = row_get(row, "proof_spec")
    hosts = store.json_list(row_get(row, "verification_hosts"))
    probe = row_get(row, "probe_url")
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
    """Vault view across every computer on the box. Still credential_json,
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
# Minting stays loopback-only (bin/case), because the agent's token must not
# be able to answer handoffs or mint its own links.

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


@app.get("/fill/{token}")
def fill_form(token: str):
    row = links.valid(token, "fill")
    if not row:
        return HTMLResponse(links.GONE_HTML, status_code=410)
    comp = store.get_computer(row["computer_id"])
    # Label with name, id and state so a human can tell which computer's vault
    # they are filling — a bare id reads as machine noise.
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
        return HTMLResponse(links.GONE_HTML, status_code=410)
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
        return HTMLResponse(links.GONE_HTML, status_code=410)
    store.upsert_credential(row["computer_id"], domains[0], username, secret,
                            (form.get("totp_seed") or "").strip() or None, None, domains)
    events.emit("credential_added", {"computer_id": row["computer_id"], "name": domains[0]})
    return HTMLResponse(links.DONE_HTML)


# ---------- Assist door (public /assist/*, token is the auth; no MCP bearer) ----------

def _cookie_value(cookie_header, name):
    return dict(p.strip().split("=", 1)
                for p in (cookie_header or "").split(";") if "=" in p).get(name, "")


def _assist_cookie(request):
    return _cookie_value(request.headers.get("cookie"), assist.COOKIE)


def _assist_set_cookie(set_sess):
    # Same-origin form submissions must retain provenance for check_same_origin;
    # this policy still strips the emailed Assist token on cross-origin requests.
    headers = {"Cache-Control": "no-store", "Referrer-Policy": "same-origin"}
    if set_sess:
        headers["Set-Cookie"] = assist.session_cookie_header(set_sess)
    return headers


@app.get("/assist/static/assist.js")
def assist_static_js():
    """Same-origin poll script, served as a file so the page carries no inline JS."""
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
    return HTMLResponse(body, headers=_assist_set_cookie(set_sess))


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
    headers = _assist_set_cookie(set_sess)
    return JSONResponse(assist.state_payload(view), headers=headers)


async def _assist_form(token, request):
    """The preamble every assist POST shares: CSRF, session cookie, a still-live
    view, and the urlencoded body. Returns (sess, view, form, expected_revision),
    or the HTMLResponse to send back instead."""
    if not assist.check_same_origin(request):
        raise ApiError(403, "csrf", "missing or mismatched Origin")
    gone = HTMLResponse(assist.GONE_HTML, status_code=410,
                        headers={"Cache-Control": "no-store"})
    sess = _assist_cookie(request)
    if not sess:
        return gone
    try:
        view, _ = assist.resolve_view(token, request.headers.get("cookie", ""))
    except ApiError:
        return gone
    form = assist.parse_form(await request.body())
    rev = form.get("expected_revision")
    return sess, view, form, int(rev) if rev not in (None, "") else None


@app.post("/assist/{token}/open")
async def assist_open(token: str, request: Request):
    """Navigate remote Chromium to a pastable HTTPS URL (allowlisted hosts only)."""
    got = await _assist_form(token, request)
    if isinstance(got, HTMLResponse):
        return got
    sess, _, form, expected = got
    try:
        await asyncio.to_thread(assist.open_with_session, sess,
                                (form.get("url") or "").strip(), expected_revision=expected)
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
    got = await _assist_form(token, request)
    if isinstance(got, HTMLResponse):
        return got
    sess, view, form, expected = got
    if "submit_value" not in view["allowed_actions"]:
        return HTMLResponse(
            assist.render_page(view, token),
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
    value = (form.get("value") or "").strip()
    if not value:
        raise ApiError(400, "bad_request", "value is required")
    try:
        row = await asyncio.to_thread(assist.submit_with_session, sess, value,
                                      expected_revision=expected)
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
    got = await _assist_form(token, request)
    if isinstance(got, HTMLResponse):
        return got
    sess, _, _, expected = got
    try:
        row = await asyncio.to_thread(assist.done_with_session, sess,
                                      expected_revision=expected)
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
    """Forward-auth target for a reverse proxy serving /desk/* (the noVNC view).
    200 lets the proxied request through; anything else is answered to the browser."""
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
    # human meets whichever desktop happens to be awake, or a bare 502.
    comp = store.get_computer(link["computer_id"])
    if not comp or comp["state"] != "running":
        return HTMLResponse(links.NOTREADY_HTML.replace("{why}", links.ASLEEP), status_code=409)
    if VNC_PORT and comp["vnc_port"] != VNC_PORT:
        return HTMLResponse(links.NOTREADY_HTML.replace("{why}", links.STALE_PORT),
                            status_code=409)
    if not set_tok:
        return Response(status_code=200)      # cookie already good, let the request through
    # First hit, token in the URL. forward-auth proxies only forward a NON-2xx auth
    # response to the browser (2xx just continues upstream), so a 302 is the only way to
    # hand a human a cookie here, and it strips the token out of history and Referer.
    return Response(status_code=302, headers={
        "Location": links.strip_token(uri),
        # never outlive the token itself: the 302 strips ?token= from the URL, so the
        # cookie is the browser's only copy of the credential.
        "Set-Cookie": (f"case_desk={set_tok}; Path=/desk; Max-Age={links.seconds_left(link)}; "
                       "Secure; HttpOnly; SameSite=Lax"),
        "Cache-Control": "no-store"})


# ---------- live view (noVNC, relayed) ----------

def live_upstream(row):
    """(base_url, headers) for a computer's noVNC: same dial deskclient uses for deskd."""
    auth = base64.b64encode(f"agent:{row['desk_token']}".encode()).decode()
    return dockerd.desk_base(row["id"], row["vnc_port"]), {"Authorization": f"Basic {auth}"}


def live_path_ok(path):
    return ".." not in path and ".." not in unquote(path)   # Starlette decoded once already


@app.get("/v1/computers/{cid}/live/{path:path}")
def live_http(cid: str, path: str, request: Request):
    """noVNC's static files, relayed so Drive never needs the desks network."""
    if not live_path_ok(path):
        raise ApiError(400, "bad_request", "bad path")
    base, headers = live_upstream(lifecycle.ensure_running(cid, False))
    q = request.url.query
    r = requests.get(f"{base}/{path}" + (f"?{q}" if q else ""), headers=headers, timeout=15)
    return Response(r.content, status_code=r.status_code, media_type=r.headers.get("content-type"))


@app.websocket("/v1/computers/{cid}/live/websockify")
async def live_ws(ws: WebSocket, cid: str):
    # token_guard is HTTP-only middleware; this is the only bearer check on the socket.
    if not bearer_ok(ws.headers.get("authorization")):
        await ws.close(code=1008)
        return
    try:
        base, headers = live_upstream(lifecycle.ensure_running(cid, False))
    except ApiError:
        await ws.close(code=1011)
        return
    subs = [p.strip() for p in ws.headers.get("sec-websocket-protocol", "").split(",") if p.strip()]
    # max_size=None: a full-screen framebuffer update is larger than the 1 MiB default.
    async with ws_connect("ws" + base[4:] + "/websockify", additional_headers=headers,
                          subprotocols=subs or None, max_size=None) as up:
        await ws.accept(subprotocol=up.subprotocol)

        async def to_desk():
            try:
                while True:
                    m = await ws.receive()
                    if m["type"] != "websocket.receive":
                        break
                    await up.send(m["bytes"] if m.get("bytes") is not None else m["text"])
            finally:
                await up.close()

        pump = asyncio.create_task(to_desk())
        try:
            async for msg in up:
                await ws.send_bytes(msg if isinstance(msg, bytes) else msg.encode())
        finally:
            pump.cancel()
            try:
                await ws.close()
            except RuntimeError:
                pass   # peer already closed


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
    url = str(body.get("url", ""))
    # Credentials must not cross a network in the clear. The desktop's own loopback
    # never leaves the box, so a local test site over http is still allowed.
    if not (url.startswith("https://") or (url.startswith("http://")
            and urlsplit(url).hostname in ("localhost", "127.0.0.1", "::1"))):
        raise ApiError(400, "bad_request", "url must be https, or http to loopback")

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


@app.post("/answer/{hid}/{token}")
def answer_public(hid: str, token: str, body: dict = Body(...)):
    """ntfy's Approve/Deny buttons. The signed token in the URL is the whole auth —
    a phone has no bearer, and the notification is the only place it leaks to."""
    return handoffs.answer_by_token(hid, token, body.get("value"))


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


# ---------- runs (scheduled-run activity) ----------

def run_json(row, names=None):
    # artifact_path is a host path, clients get a flag and fetch the bytes by id.
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
            # preflight persistent session health: it drives desks over the network,
            # and a hung one must not stall reconcile or the schedule fire loop
            threading.Thread(target=session_keeper.tick, daemon=True).start()
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
