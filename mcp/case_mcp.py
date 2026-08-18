#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""case-mcp — MCP server, thin 1:1 wrapper over cased (API_SPEC.md §9).

Credentials add/delete is deliberately NOT exposed: secrets enter via
human-driven CLI/HTTP only, never through a model's tool call.

Transport: stdio by default (scheduler brain, local dogfood, partner tunnels).
CASE_MCP_HTTP=1 → streamable-http, stateless. Binds 127.0.0.1 by default
(CASE_MCP_BIND overrides; compose sets 0.0.0.0 and publishes 127.0.0.1:8788).
Hosted boxes leave the default and put Caddy in front (TLS + bearer).
"""
import base64
import os
import re
import secrets
import time

import requests
from mcp.server.fastmcp import FastMCP, Image

BASE = os.environ.get("CASE_URL", "http://127.0.0.1:8787/v1")
HTTP = os.environ.get("CASE_MCP_HTTP") == "1"
BIND = (os.environ.get("CASE_MCP_BIND") or "127.0.0.1").strip() or "127.0.0.1"
# In HTTP mode this is one id for the whole box (stateless: no per-client session) —
# the box has one partner, so the audit log stays as useful as it is over stdio.
SESSION = "mcp_" + secrets.token_hex(4)   # one per MCP process; keys cased's audit log
# stateless_http: no server-side session state, so a Caddy/systemd restart never
# strands a client mid-session. Harmless over stdio.
mcp = FastMCP("case", stateless_http=True,
              host=BIND, port=int(os.environ.get("CASE_MCP_PORT", "8788")))


def _headers(**extra):
    h = {"X-Case-Session": SESSION}
    tok = (os.environ.get("CASE_TOKEN") or "").strip()
    if tok:
        h["Authorization"] = "Bearer " + tok
    h.update(extra)
    return h


def call(method, path, **kw):
    r = requests.request(method, BASE + path, timeout=kw.pop("timeout", 150),
                         headers=_headers(), **kw)
    if r.status_code >= 400:
        raise RuntimeError(r.text[:500])
    return r


@mcp.tool()
def computer_create(name: str = "") -> dict:
    """Create a persistent computer (Linux desktop + Chromium). Blocks until running.
    Returns the computer incl. its `id` — pass that id to every other tool.
    Computers are durable, not scratch: logins, cookies and files survive sleep, wake
    and host reboots. Check computer_list first and reuse an existing computer —
    only create a new one for an identity that should stay separate."""
    return call("POST", "/computers", json={"name": name} if name else {}).json()


@mcp.tool()
def computer_list() -> dict:
    """List all computers with state, resources and VNC URL.
    `vnc_url` is bound to the *host's* loopback and its port changes on every wake —
    it is not reachable from wherever you are running, so never hand it to a user as
    a link. Use computer_screenshot to see the screen."""
    return call("GET", "/computers").json()


@mcp.tool()
def computer_screenshot(computer_id: str) -> Image:
    """Screenshot of the computer's display (1280x800). Wakes the computer if asleep."""
    r = call("GET", f"/computers/{computer_id}/screenshot", params={"wake": "true"})
    return Image(data=r.content, format="png")


@mcp.tool()
def computer_action(computer_id: str, type: str, x: int = None, y: int = None,
                    button: str = None, text: str = None, keys: str = None,
                    dy: int = None, ms: int = None,
                    from_x: int = None, from_y: int = None,
                    to_x: int = None, to_y: int = None,
                    screenshot: bool = False) -> object:
    """Perform a UI action: click|double_click|move|drag|scroll|type|key|wait.
    Coordinates are pixels, origin top-left of 1280x800. keys uses xdotool syntax
    (e.g. 'ctrl+l', 'Return'). Set screenshot=true to get the post-action screen.
    For elements INSIDE a web page prefer computer_snapshot + computer_click_element
    (no coordinate guessing); use this for the desktop itself, canvas/custom-drawn
    UI, keyboard shortcuts, and scrolling. Prefer keys over mouse for menus and
    dropdowns when a shortcut exists."""
    a = {"type": type, "screenshot": screenshot}
    for k, v in (("x", x), ("y", y), ("button", button), ("text", text),
                 ("keys", keys), ("dy", dy), ("ms", ms)):
        if v is not None:
            a[k] = v
    if type == "drag":
        a["from"] = {"x": from_x, "y": from_y}
        a["to"] = {"x": to_x, "y": to_y}
    out = call("POST", f"/computers/{computer_id}/action", params={"wake": "true"}, json=a).json()
    shot = out.pop("screenshot_png_b64", None)
    if shot:
        return Image(data=base64.b64decode(shot), format="png")
    return out


@mcp.tool()
def computer_exec(computer_id: str, command: str, timeout_s: int = 30) -> dict:
    """Run a shell command on the computer (bash, as user 'agent')."""
    return call("POST", f"/computers/{computer_id}/exec", params={"wake": "true"},
                json={"command": command, "timeout_s": timeout_s},
                timeout=timeout_s + 30).json()


@mcp.tool()
def computer_eval(computer_id: str, expression: str, timeout_s: int = 20) -> dict:
    """Evaluate a JavaScript expression in the computer's active browser tab
    (CDP Runtime.evaluate, JSON result, promises awaited). Prefer this over
    screenshots for reading page content — exact DOM data, tiny token cost.
    Return plain values (strings, numbers, arrays, plain objects) directly — they are
    serialised for you and promises are awaited, so JSON.stringify and manual await
    wrappers are never needed. DOM nodes are NOT serialisable: map them to plain values
    first, e.g. expression="[...document.querySelectorAll('a')].map(a=>a.href)"
    — returning the elements themselves fails.
    To read a page as prose, "document.body.innerText" beats hand-written selectors.
    Use computer_navigate to change page — do not drive location.assign from here.
    Returns {ok, value, truncated} or {ok:false, error}."""
    return call("POST", f"/computers/{computer_id}/eval", params={"wake": "true"},
                json={"expression": expression, "timeout_s": timeout_s},
                timeout=timeout_s + 20).json()


@mcp.tool()
def computer_navigate(computer_id: str, url: str, timeout_s: int = 30) -> dict:
    """Point the computer's browser at url and block until the page has loaded.
    One call — do not follow it with readyState polling. Returns
    {ok, url, title} (url is the final one, after redirects) or {ok:false, error}.
    The body is ready when this returns; `title` is best-effort and can be "" on
    pages that set it a beat late — that is not a signal to wait or retry.
    Then read the page with computer_eval("document.body.innerText").
    Same-page '#anchor' jumps are not navigations; use computer_eval for those."""
    return call("POST", f"/computers/{computer_id}/navigate", params={"wake": "true"},
                json={"url": url, "timeout_s": timeout_s},
                timeout=timeout_s + 20).json()


@mcp.tool()
def computer_snapshot(computer_id: str) -> dict:
    """Numbered list of the visible interactive elements on the computer's active
    browser tab — the cheap way to see what's clickable. PREFER THIS OVER
    computer_screenshot for anything inside a web page: ~10x fewer tokens and no
    coordinate guessing. Returns {ok, url, title, count, elements} where each
    element is a line like '[12] button "Save changes"' or
    '[13] input(email) "" =\\'\\' — pass that number to computer_click_element or
    computer_fill. Refs are re-derived per call (document order), so a ref is valid
    until the page changes; after a click or navigation, snapshot again.
    Screenshots are still right for canvas/custom-drawn UI, visual layout questions,
    and anything outside the browser window."""
    return call("GET", f"/computers/{computer_id}/page", params={"wake": "true"}).json()


@mcp.tool()
def computer_click_element(computer_id: str, ref: int, name: str = None,
                           text: str = None, screenshot: bool = False) -> dict:
    """Click element [ref] from the last computer_snapshot. Pass name (the quoted
    text from the snapshot line) so a changed page is caught: on mismatch this
    REFUSES to click and returns {ok:false, stale:true, snapshot} — use the fresh
    snapshot and retry with the right ref; never click blind after a refusal.
    The element is scrolled into view and clicked with a real OS-level mouse event
    (isTrusted true). text, when given, is typed into the element after the click
    (click focuses it) — for one field that beats computer_fill.
    One call replaces the screenshot→guess-coordinates→click→screenshot loop."""
    body = {"ref": ref}
    if name is not None:
        body["name"] = name
    if text is not None:
        body["text"] = text
    if screenshot:
        body["screenshot"] = True
    return call("POST", f"/computers/{computer_id}/click",
                params={"wake": "true"}, json=body, timeout=60).json()


@mcp.tool()
def computer_fill(computer_id: str, fields: list, submit: bool = False) -> dict:
    """Fill a whole form in ONE call: fields=[{"ref": 13, "value": "jane@x.com"}, …]
    with refs from computer_snapshot. Uses native value setters + input/change
    events, so React/Vue forms see the change. Handles text inputs, textareas,
    selects (by value or option text), checkboxes/radios (value true/false) and
    contenteditable. submit=true submits the first field's form at the end.
    NEVER for passwords or OTP codes — password fields are refused inside the page;
    vaulted computer_login owns credentials. Per-field results come back in
    {fields:[{ref, ok, ...}]}; a failed ref usually means the page changed —
    re-snapshot."""
    body = {"fields": fields}
    if submit:
        body["submit"] = True
    return call("POST", f"/computers/{computer_id}/fill",
                params={"wake": "true"}, json=body, timeout=60).json()


@mcp.tool()
def computer_wait_for(computer_id: str, selector: str = None, text: str = None,
                      gone: bool = False, network_idle: bool = False,
                      timeout_s: int = 30) -> dict:
    """Block in this one call until the page is ready — instead of polling with
    repeated computer_eval calls (each of those costs a whole turn; this costs one).
    Give exactly one of: selector (CSS, waits until it exists), text (waits until
    document.body.innerText contains it), or network_idle=true (waits until no new
    resources load for ~1s — right after actions that trigger XHR refreshes).
    gone=true inverts selector/text (wait for a spinner to disappear).
    Returns {ok, waited_ms}; {ok:false} on timeout means the condition never held —
    snapshot or screenshot to see what the page actually did. computer_navigate
    already waits for document-complete; use this for what loads after."""
    body = {"timeout_s": timeout_s}
    if selector:
        body["selector"] = selector
    if text:
        body["text"] = text
    if gone:
        body["gone"] = True
    if network_idle:
        body["network_idle"] = True
    return call("POST", f"/computers/{computer_id}/wait",
                params={"wake": "true"}, json=body, timeout=timeout_s + 30).json()


@mcp.tool()
def computer_tabs(computer_id: str, action: str = "list", target_id: str = None,
                  url: str = None) -> dict:
    """Browser tab management: action=list|activate|new|close. list returns
    [{id, title, url, active}] — the ACTIVE tab is the one computer_eval,
    computer_snapshot and computer_capture talk to. If a click opened a new tab
    (target=_blank does this constantly) and the page 'stopped responding' to eval,
    list tabs and activate the right one. activate/close need target_id from list;
    new needs an http(s) url. After activate/new, snapshot to see where you are."""
    body = {"action": action}
    if target_id:
        body["target_id"] = target_id
    if url:
        body["url"] = url
    return call("POST", f"/computers/{computer_id}/tabs",
                params={"wake": "true"}, json=body, timeout=40).json()


@mcp.tool()
def computer_capture_start(computer_id: str, url_pattern: str) -> dict:
    """Start capturing network response bodies in the computer's active browser tab
    whose URL matches url_pattern (a regex). A browser-level wiretap: survives page/SPA
    navigations and catches both fetch and XMLHttpRequest — use this instead of
    injecting JS fetch/XHR hooks (those get wiped on nav and miss XHR). Replaces
    any running capture. Then navigate/act, and drain with computer_capture_read.
    e.g. url_pattern="SearchTimeline|/graphql". Returns {ok, pattern}."""
    return call("POST", f"/computers/{computer_id}/capture", params={"wake": "true"},
                json={"pattern": url_pattern}).json()


@mcp.tool()
def computer_capture_read(computer_id: str, stop: bool = False) -> dict:
    """Drain captured network responses from the active browser tab. Returns
    {items, running, error}; each item is {ts,url,status,body,truncated} on success
    or {ts,url,status,error} if the body couldn't be fetched. The buffer clears on
    each read (call repeatedly to stream; ring of 100). Bodies truncated to 256 KB.
    stop=true also ends the capture. Response bodies only — request bodies/headers
    are never captured."""
    method = "DELETE" if stop else "GET"
    return call(method, f"/computers/{computer_id}/capture", params={"wake": "true"}).json()


@mcp.tool()
def computer_login(computer_id: str, credential: str, url: str,
                   idempotency_key: str = None, proof_spec: dict = None) -> dict:
    """Log into a site using a vaulted credential (added by the human via CLI/fill).
    Returns AuthAttemptResult / LoginResult with attempt_id always present:
    {"status":"success","attempt_id",…}, {"status":"failed"|"unverified","reason",
    "attempt_id",…}, or {"status":"handoff_pending","handoff_id","attempt_id",
    "revision",…} when a human is needed for a 2FA/OTP/captcha/device-approval wall.
    Prefer this for any login wall — do not click captchas or type OTP codes
    yourself, and do not call handoff_request for them.

    Pass idempotency_key on transport retries so a duplicate POST returns the same
    attempt instead of starting a second login. Optional proof_spec (url_contains /
    url_prefix / selector / expression) configures positive proof; omit to use the
    credential vault profile when set.

    The MCP process/connection is NOT workflow state (MCP 2026-07-28). CALL THIS
    AT MOST ONCE PER LOGIN (reuse idempotency_key only for transport retries).
    On handoff_pending, IMMEDIATELY call auth_attempt_wait(attempt_id,
    since_revision=revision) in the SAME turn — do NOT ask the user to say "go",
    do NOT create a standalone handoff_request, and do NOT re-call computer_login.
    A single wall can surface more than one challenge in sequence (captcha THEN
    email code); auth_attempt_wait follows the whole journey.

    Optional CAPTCHA auto-solve (CASE_DBC_*) is capability-gated; unsupported or
    terminal solver responses fail fast into the same handoff_pending path.
    Timeout 280s: deskd login ≤95s plus optional DBC solve (≤60s) + settle/verify
    + resume; under Caddy's 300s door budget."""
    body = {"credential": credential, "url": url}
    if idempotency_key is not None:
        body["idempotency_key"] = idempotency_key
    if proof_spec is not None:
        body["proof_spec"] = proof_spec
    return call("POST", f"/computers/{computer_id}/login", params={"wake": "true"},
                json=body, timeout=280).json()


@mcp.tool()
def computer_file_put(computer_id: str, path: str, content_b64: str) -> dict:
    """Write a file on the computer (content is base64)."""
    return call("PUT", f"/computers/{computer_id}/files",
                params={"path": path, "wake": "true"}, data=base64.b64decode(content_b64)).json()


@mcp.tool()
def computer_file_get(computer_id: str, path: str) -> dict:
    """Read a file from the computer. Text files come back readable:
    {encoding:"utf8", content, bytes}. Binary files come back as
    {encoding:"base64", content, bytes} — do not try to read base64 yourself;
    process binary files on the computer with computer_exec instead
    (e.g. extract text, convert, or inspect with file/strings)."""
    r = call("GET", f"/computers/{computer_id}/files", params={"path": path, "wake": "true"})
    try:
        return {"encoding": "utf8", "content": r.content.decode("utf-8"),
                "bytes": len(r.content)}
    except UnicodeDecodeError:
        return {"encoding": "base64", "content": base64.b64encode(r.content).decode(),
                "bytes": len(r.content)}


# ---------- skills (procedural memory, files on the computer) ----------
# Skills are Anthropic-format SKILL.md files in /home/agent/skills/<name>/ — part
# of the computer's identity, surviving sleep/wake/reboot with the volume. No new
# storage: list/read/save ride the existing exec + files routes.

SKILL_DIR = "/home/agent/skills"
SKILL_INDEX_CMD = (
    f'for f in {SKILL_DIR}/*/SKILL.md; do [ -f "$f" ] || continue; '
    'echo "## $f"; awk \'/^---$/{n++;next} n==1{print} n>=2{exit}\' "$f"; done'
)
_SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")
# Secret shapes that must never be written into a skill file: the file is
# API-retrievable, so the "credentials never in API responses" invariant applies.
_SKILL_RISKY_RE = re.compile(
    r"(?im)^\s*(?:password|passwd|secret|token|otp|totp[_-]?seed|api[_-]?key)\s*[:=]\s*\S+"
    r"|[A-Za-z0-9_-]{40,}")


def skill_name_ok(name):
    return bool(_SKILL_NAME_RE.match(name or ""))


def skill_content_risky(content):
    """True when content carries something secret-shaped (key: value secrets, or a
    40+ char unbroken token). Vault names like 'credential: coupa' pass."""
    m = _SKILL_RISKY_RE.search(content or "")
    return m.group(0)[:60] if m else None


@mcp.tool()
def case_skill(computer_id: str, action: str, name: str = "", content: str = "") -> dict:
    """Procedural memory: save a browser/desktop task you just completed as a skill,
    and reuse skills on later runs. action=list|read|save.

    list → every skill on this computer (name + description frontmatter). Check this
    BEFORE starting a multi-step task: if a skill matches, read it and follow it —
    it encodes the working route, checkpoints and pitfalls from previous runs.
    read → the full SKILL.md for `name`. When following a skill, quoted names that
    came from the original demonstration are anchors and examples: substitute the
    current task's parameters (a different recipient means search for and click THAT
    person, not the example one) while keeping the step structure and checkpoints.
    save → write `content` to /home/agent/skills/<name>/SKILL.md. After completing a
    task the user may want repeated, PROPOSE saving it as a skill.

    Authoring contract for `content` (Anthropic SKILL.md format):
    - Frontmatter: `name`, `description` (when to use it, trigger phrases).
    - Body: numbered natural-language steps in tool vocabulary (computer_navigate,
      computer_snapshot, computer_click_element with the element's quoted name,
      computer_fill, computer_wait_for). Name every element you click — the name is
      the anchor that survives redesigns and the stale-check enforces it.
    - Keep the step count honest: one computer_snapshot per navigation or state
      change, never one before every click — refs stay valid until the page changes,
      so several clicks follow one snapshot. A step is an action, not a re-look.
    - Generalize: values typed and specific people/items picked during one run are
      PARAMETERS of the skill, not part of it. Name the skill after the task class
      (x-dm-send, not x-dm-harsh), declare its parameters in the description, and
      keep the run's values only as worked examples in the steps.
    - After each state change add a checkpoint: what the snapshot/page must show,
      and what to do if it doesn't.
    - End with a "Done means" section: how to verify the task actually succeeded.
    - Logins: ONE step — computer_login(credential=<vault name>) + auth_attempt_wait.
      NEVER write usernames, passwords, OTP codes, cookies or tokens into a skill;
      save rejects secret-shaped content.
    - On later runs where reality diverged: finish the task, then update the file
      and append a dated line to a `## Drift log` section — heal loudly.
    A new skill is a draft until a later run succeeds by following it."""
    if action == "list":
        r = call("POST", f"/computers/{computer_id}/exec", params={"wake": "true"},
                 json={"command": SKILL_INDEX_CMD, "timeout_s": 15}, timeout=40).json()
        out = (r.get("stdout") or "").strip()
        return {"skills": out or "(no skills saved on this computer yet)"}
    if action == "read":
        if not skill_name_ok(name):
            raise RuntimeError("bad skill name (lowercase slug, e.g. coupa-ap-aging)")
        r = call("GET", f"/computers/{computer_id}/files",
                 params={"path": f"{SKILL_DIR}/{name}/SKILL.md", "wake": "true"})
        return {"name": name, "content": r.content.decode("utf-8", errors="replace")}
    if action == "save":
        if not skill_name_ok(name):
            raise RuntimeError("bad skill name (lowercase slug, e.g. coupa-ap-aging)")
        if not content or len(content) > 65536:
            raise RuntimeError("content required, max 64KB")
        risky = skill_content_risky(content)
        if risky:
            raise RuntimeError(
                f"content looks like it contains a secret ({risky!r}) — skills must "
                "reference vault credential NAMES only (computer_login steps)")
        call("PUT", f"/computers/{computer_id}/files",
             params={"path": f"{SKILL_DIR}/{name}/SKILL.md", "wake": "true"},
             data=content.encode("utf-8"))
        note = base64.b64encode(
            f"\n- skill `{name}` saved ({len(content)} bytes)\n".encode()).decode()
        call("POST", f"/computers/{computer_id}/exec", params={"wake": "true"},
             json={"command": ("mkdir -p /home/agent/reports && echo " + note
                               + " | base64 -d >> /home/agent/reports/$(date +%F).md"),
                   "timeout_s": 10}, timeout=30)
        return {"saved": f"{SKILL_DIR}/{name}/SKILL.md", "status": "draft until a run follows it"}
    raise RuntimeError("action must be list|read|save")


@mcp.tool()
def computer_sleep(computer_id: str) -> dict:
    """Hibernate the computer. Disk state (sessions, cookies, files) survives, and it
    wakes in seconds. Sleeping frees the host's RAM so other computers can run; on a
    hosted box it does not reduce the bill. Sleep when a task is done."""
    return call("POST", f"/computers/{computer_id}/sleep").json()


@mcp.tool()
def auth_attempt_wait(attempt_id: str, since_revision: int = None,
                      max_wait_s: int = 240) -> dict:
    """Block in this tool call until the auth attempt advances or the budget ends.
    This is also the inspection tool: for a non-blocking snapshot of where a login
    journey stands, call it with max_wait_s=1 — the timeout answer carries the full
    attempt state. Attempt statuses: created → advancing → awaiting_human → proving
    → authenticated; terminals: unverified|failed|expired|cancelled.

    Call IMMEDIATELY in the same turn after computer_login returns handoff_pending.
    Do NOT ask the user to say "go". Do NOT re-call computer_login. Do NOT mint a
    standalone handoff_request for the OTP/captcha the platform already raised.

    Chains short GET /auth-attempts/{id}/wait long-polls (≤270s each, under Caddy's
    300s door) until the attempt is terminal or max_wait_s elapses (default 240,
    cap 240 to stay under the door). Pass since_revision from the last login /
    wait response so intermediate challenges (captcha → OTP) wake the waiter.

    Returns a LoginResult-shaped dict on terminals / next handoff_pending:
    {status, attempt_id, revision, …} plus wait_status ("terminal"|"changed"|
    "timeout") and attempt. On wait_status=timeout while still awaiting_human,
    call this tool AGAIN with the returned revision — never ask the human to nudge
    the agent and never start a fresh login."""
    try:
        budget = max(1, min(int(max_wait_s), 240))
    except (TypeError, ValueError):
        budget = 240
    after_rev = 0 if since_revision is None else int(since_revision)
    after_hid = None
    deadline = time.time() + budget
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            snap = call("GET", f"/auth-attempts/{attempt_id}").json()
            out = {
                "wait_status": "timeout",
                "changed": False,
                "attempt": snap,
                "attempt_id": snap["id"],
                "revision": snap["revision"],
            }
            # Compat LoginResult fields for the agent.
            if snap["status"] == "awaiting_human":
                out["status"] = "handoff_pending"
                out["handoff_id"] = snap.get("current_handoff_id")
            elif snap["status"] == "authenticated":
                out["status"] = "success"
                out["proof_level"] = snap.get("proof_level")
            elif snap["status"] in ("failed", "cancelled", "expired"):
                out["status"] = "failed"
            elif snap["status"] == "unverified":
                out["status"] = "unverified"
            else:
                out["status"] = snap["status"]
            return out
        leg = max(1, min(int(remaining), 270))
        params = {"after_revision": after_rev, "timeout_s": leg}
        if after_hid is not None:
            params["after_handoff_id"] = after_hid
        last = call("GET", f"/auth-attempts/{attempt_id}/wait",
                    params=params, timeout=leg + 10).json()
        attempt = last["attempt"]
        after_rev = int(attempt.get("revision") or after_rev)
        after_hid = attempt.get("current_handoff_id")
        # Prefer platform-mapped login_result when present.
        lr = last.get("login_result") or {}
        if attempt["status"] in ("authenticated", "unverified", "failed",
                                 "expired", "cancelled"):
            out = {
                "wait_status": "terminal",
                "changed": True,
                "attempt": attempt,
                "attempt_id": attempt["id"],
                "revision": attempt["revision"],
            }
            out.update(lr)
            if "status" not in out:
                out["status"] = ("success" if attempt["status"] == "authenticated"
                                 else "failed" if attempt["status"] != "unverified"
                                 else "unverified")
            return out
        if last.get("wait_status") == "timeout":
            continue
        # Intermediate advance (new challenge / proving) — keep waiting inside budget.
        if attempt["status"] == "awaiting_human" and last.get("changed"):
            # Still human-needed; continue waiting for the next Assist action unless
            # budget is nearly gone — then surface handoff_pending so the agent can
            # re-enter wait without asking the user.
            if deadline - time.time() < 5:
                out = {
                    "wait_status": "timeout",
                    "changed": True,
                    "attempt": attempt,
                    "attempt_id": attempt["id"],
                    "revision": attempt["revision"],
                    "status": "handoff_pending",
                    "handoff_id": attempt.get("current_handoff_id"),
                }
                return out
            continue
        # Non-terminal progress (advancing/proving) — keep polling.
        continue


@mcp.tool()
def handoff_request(computer_id: str, prompt: str, kind: str = "approval") -> dict:
    """Ask the human for help. kind is required semantically — one of:
    'approval'|'question' → code/text Assist form;
    'device'|'captcha'|'passkey' → live /desk Assist (QR scan, visual challenge).
    Prefer computer_login for vaulted logins and 2FA/OTP — the platform creates those
    handoffs (and may auto-solve captcha). Use kind='device' when the human must act
    on the live desktop (Discord QR, passkey prompt) and login did not open a challenge.
    Agent-made captcha/device handoffs do not auto-resume a vault login.
    Never use this to "check" a computer_login journey — use auth_attempt_wait."""
    return call("POST", f"/computers/{computer_id}/handoffs",
                json={"kind": kind, "prompt": prompt}).json()


@mcp.tool()
def handoff_list() -> dict:
    """List pending handoffs across all computers (id, computer_id, kind, prompt,
    status, continuation, domain). Status values: pending | validating | completed |
    failed | expired (legacy answered reads as completed). This list filters to
    status=pending only — for login journeys use auth_attempt_wait(attempt_id),
    not this list and not a fresh computer_login. If several pending handoffs share
    a computer_id, a login was retried too many times; wait on the newest attempt
    and let the stale ones expire (15 min)."""
    return call("GET", "/handoffs", params={"status": "pending"}).json()


@mcp.tool()
def handoff_get(handoff_id: str) -> dict:
    """Fetch one handoff/challenge by id (includes screenshot). For login journeys
    prefer auth_attempt_wait(attempt_id) — this is for a specific challenge only.
    pending = still waiting on the human; validating = human acted, platform is
    verifying (keep waiting; not done yet). Never retry computer_login to discover
    completion and never ask the user to nudge the agent."""
    return call("GET", f"/handoffs/{handoff_id}").json()


# Archived from the default surface (2026-08-14): schedule schemas rode every
# request on every harness for a capability agents rarely need mid-task. The
# scheduler itself still runs (REST/CLI unchanged); set CASE_MCP_SCHEDULES=1
# to expose these tools again.
if os.environ.get("CASE_MCP_SCHEDULES") == "1":
    @mcp.tool()
    def schedule_create(computer_id: str, prompt: str, kind: str = "daily",
                        spec: str = "09:00", name: str = "", jitter_s: int = 300) -> dict:
        """Create a recurring schedule on a computer. kind=daily (spec HH:MM local) or
        interval (spec seconds as string). Fires unattended using the host brain credential."""
        body = {"prompt": prompt, "kind": kind, "spec": spec, "jitter_s": jitter_s}
        if name:
            body["name"] = name
        return call("POST", f"/computers/{computer_id}/schedules", json=body).json()


    @mcp.tool()
    def schedule_list(computer_id: str) -> list:
        """List schedules for a computer."""
        return call("GET", f"/computers/{computer_id}/schedules").json()


    @mcp.tool()
    def schedule_delete(schedule_id: str) -> str:
        """Delete a schedule by id."""
        call("DELETE", f"/schedules/{schedule_id}")
        return "deleted"


    @mcp.tool()
    def schedule_run(schedule_id: str) -> dict:
        """Fire a schedule immediately (async). Returns {status, schedule}."""
        return call("POST", f"/schedules/{schedule_id}/run").json()


    @mcp.tool()
    def schedule_runs(schedule_id: str) -> list:
        """List past runs for a schedule (status, summary, timestamps)."""
        return call("GET", f"/schedules/{schedule_id}/runs").json()


if __name__ == "__main__":
    mcp.run("streamable-http" if HTTP else "stdio")
