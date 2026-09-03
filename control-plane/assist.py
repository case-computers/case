# SPDX-License-Identifier: AGPL-3.0-only
"""Assist door, hashed exchange token → short-lived HttpOnly session cookie.

An emailed /assist/{token} link is a one-shot exchange credential. The first GET
burns it (hash-at-rest only) and issues `case_assist`. The session is attempt-
scoped when the bound handoff carries `attempt_id`: each request resolves the
attempt's current handoff (via `auth_attempts.current_handoff_id`) so one magic
link covers CAPTCHA → OTP → device without a fresh email. Cookie authorizes
/desk for the bound computer only, never console, fill, MCP, or /v1.
"""
import hashlib
import html as html_mod
import secrets
from urllib.parse import parse_qs, urlsplit

from errors import ApiError
from store import store
from util import iso_in, now, row_get

COOKIE = "case_assist"
EXCHANGE_TTL_S = 900    # 15 minutes, the emailed link
SESSION_TTL_S = 1800    # 30 minutes, or until attempt/handoff is gone
LIVE_STATUSES = frozenset({"pending", "validating"})
_ATTEMPT_TERMINAL = frozenset({
    "authenticated", "unverified", "failed", "expired", "cancelled",
})
_ATTEMPT_ACTIVE = frozenset({"created", "advancing", "awaiting_human", "proving"})


def _hash(raw):
    return hashlib.sha256(raw.encode()).hexdigest()


def _cookies(cookie_header):
    return dict(p.strip().split("=", 1) for p in (cookie_header or "").split(";") if "=" in p)


def _attempt_id_of(handoff):
    return row_get(handoff, "attempt_id")


def mint_assist_token(handoff_id):
    """Mint an exchange token for a handoff. Returns (raw_token, expires_at).
    Only the SHA-256 hash is stored, plaintext rides the email link once."""
    raw = secrets.token_urlsafe(32)
    expires = iso_in(EXCHANGE_TTL_S)
    store.insert_assist_token(handoff_id, _hash(raw), expires)
    return raw, expires


def exchange(raw_token):
    """Burn the exchange token and issue a session. Returns (session_raw, handoff_row)."""
    th = _hash(raw_token)
    row = store.get_assist_by_token_hash(th)
    if not row:
        raise ApiError(410, "gone", "assist link invalid or expired")
    if row["burned_at"] is not None:
        raise ApiError(410, "gone", "assist link already used")
    if row["expires_at"] <= now():
        raise ApiError(410, "gone", "assist link expired")
    handoff = store.get_handoff(row["handoff_id"])
    if not handoff or handoff["status"] not in LIVE_STATUSES:
        # Attempt-scoped: allow exchange while the attempt still needs a human,
        # even if this mint's handoff row already moved on (rare race).
        aid = _attempt_id_of(handoff) if handoff else None
        attempt = store.get_auth_attempt(aid) if aid else None
        if not attempt:
            raise ApiError(410, "gone", "handoff is no longer open")
    session = secrets.token_urlsafe(32)
    sess_exp = iso_in(SESSION_TTL_S)
    if store.burn_assist_exchange(th, _hash(session), sess_exp) == 0:
        raise ApiError(410, "gone", "assist link already used")
    return session, handoff


def _session_row(raw_session):
    if not raw_session:
        return None
    row = store.get_assist_by_session_hash(_hash(raw_session))
    if not row or not row["session_hash"]:
        return None
    if not row["session_expires_at"] or row["session_expires_at"] <= now():
        return None
    return row


def session_view(raw_session):
    """(bound_handoff, current_handoff|None, attempt|None) if the cookie may view Assist.

    Follows `handoff.attempt_id` → attempt.current_handoff_id so one session covers
    the whole attempt. Returns None when the cookie is dead.
    """
    row = _session_row(raw_session)
    if not row:
        return None
    bound = store.get_handoff(row["handoff_id"])
    if not bound:
        return None
    aid = _attempt_id_of(bound)
    attempt = store.get_auth_attempt(aid) if aid else None

    if bound["status"] in LIVE_STATUSES and not attempt:
        return bound, bound, None

    if attempt:
        current = None
        cur_id = attempt["current_handoff_id"]
        if cur_id:
            current = store.get_handoff(cur_id)
            if current and current["status"] not in LIVE_STATUSES:
                # Stale pointer, ignore for action; keep for attempt terminal view.
                if attempt["status"] in _ATTEMPT_ACTIVE:
                    current = None
        if current and current["status"] in LIVE_STATUSES:
            return bound, current, attempt
        # proving / terminal / between challenges, still viewable
        return bound, current, attempt

    if bound["status"] in LIVE_STATUSES:
        return bound, bound, attempt
    return None


def valid_session(raw_session):
    """Live current handoff if `raw_session` can act (submit/done/open/desk)."""
    view = session_view(raw_session)
    if not view:
        return None
    _bound, current, _attempt = view
    if current and current["status"] in LIVE_STATUSES:
        return current
    return None


def session_cookie_header(session_raw, max_age=None):
    """Set-Cookie value for the assist session. Path=/ so /desk sees it too.

    Only minted on the exchange hit, where the stored session expiry is also
    now + SESSION_TTL_S — so the default max_age matches the DB exactly."""
    if max_age is None:
        max_age = SESSION_TTL_S
    return (f"{COOKIE}={session_raw}; Path=/; Max-Age={int(max_age)}; "
            "Secure; HttpOnly; SameSite=Lax")


def resolve_view(raw_token, cookie_header=""):
    """Auth + attempt-scoped view. Returns (view_dict, set_session_raw|None).

    view_dict keys: bound, handoff, attempt, status, revision, kind, continuation,
    instructions, allowed_actions.
    """
    cookies = _cookies(cookie_header)
    sess = cookies.get(COOKIE, "")
    th = _hash(raw_token)
    row = store.get_assist_by_token_hash(th)
    set_sess = None

    if row and row["burned_at"] is None and row["expires_at"] > now():
        set_sess, _handoff = exchange(raw_token)
        sess = set_sess
    elif not sess:
        raise ApiError(410, "gone", "assist link invalid or expired")

    view = session_view(sess)
    if not view:
        raise ApiError(410, "gone", "assist link invalid or expired")
    bound, current, attempt = view
    if row and row["handoff_id"] != bound["id"]:
        raise ApiError(410, "gone", "assist link does not match this session")
    return build_view(bound, current, attempt), set_sess


def continuation_of(handoff):
    import handoffs
    cont = row_get(handoff, "continuation")
    return cont or handoffs.continuation_for(handoff["kind"])


def allowed_actions_for(handoff, attempt):
    """Typed Assist actions for the current phase, never implies secrets."""
    if attempt and attempt["status"] in _ATTEMPT_TERMINAL:
        return []
    if attempt and attempt["status"] == "proving":
        return []
    if not handoff or handoff["status"] not in LIVE_STATUSES:
        return []
    cont = continuation_of(handoff)
    if cont == "submit_value":
        return ["submit_value"]
    if cont == "verify_page":
        return ["open_desk", "mark_done", "open_url"]
    if cont == "wait_external":
        return ["wait_external", "open_url"]
    return []


def build_view(bound, current, attempt):
    """Public Assist view, no secrets, answers, or URLs."""
    handoff = current
    if attempt and attempt["status"] == "proving":
        return {
            "bound": bound,
            "handoff": None,
            "attempt": attempt,
            "status": "proving",
            "revision": int(attempt["revision"] or 0),
            "kind": None,
            "continuation": None,
            "instructions": "Verifying your login…",
            "allowed_actions": [],
        }
    if attempt and attempt["status"] in _ATTEMPT_TERMINAL:
        st = attempt["status"]
        instructions = {
            "authenticated": "You're signed in. You can close this page.",
            "unverified": "Login finished but could not be verified. Ask for a new attempt.",
            "failed": "Authentication failed. Ask for a new link.",
            "expired": "This login attempt expired. Ask for a new link.",
            "cancelled": "This login attempt was cancelled.",
        }.get(st, "This login attempt is closed.")
        return {
            "bound": bound,
            "handoff": None,
            "attempt": attempt,
            "status": st,
            "revision": int(attempt["revision"] or 0),
            "kind": None,
            "continuation": None,
            "instructions": instructions,
            "allowed_actions": [],
        }
    if not handoff:
        handoff = bound
    status = handoff["status"] if handoff else "gone"
    # Map legacy answered → completed for Assist surfaces.
    if status == "answered":
        status = "completed"
    cont = continuation_of(handoff) if handoff and status in LIVE_STATUSES else None
    kind = handoff["kind"] if handoff else None
    instructions = (handoff["prompt"] or "") if handoff else ""
    revision = int(row_get(handoff, "revision", 0) or 0) if handoff else 0
    actions = allowed_actions_for(handoff, attempt)
    if status in ("completed", "failed", "expired") and not actions:
        instructions = {
            "completed": "Challenge cleared. You can close this page.",
            "failed": "This challenge could not be completed. Ask for a new link.",
            "expired": "This challenge expired. Ask for a new link.",
        }.get(status, instructions)
    return {
        "bound": bound,
        "handoff": handoff,
        "attempt": attempt,
        "status": status,
        "revision": revision,
        "kind": kind,
        "continuation": cont,
        "instructions": instructions,
        "allowed_actions": actions,
    }


def state_payload(view):
    """JSON for GET /assist/{token}/state, never secrets/URLs/answers."""
    return {
        "status": view["status"],
        "revision": view["revision"],
        "kind": view["kind"],
        "continuation": view["continuation"],
        "instructions": view["instructions"],
        "allowed_actions": list(view["allowed_actions"]),
    }


def submit_with_session(raw_session, value, expected_revision=None):
    """OTP / submit_value path for an authenticated assist session."""
    import handoffs
    handoff = valid_session(raw_session)
    if not handoff:
        raise ApiError(410, "gone", "assist session invalid or expired")
    if continuation_of(handoff) != "submit_value":
        raise ApiError(409, "bad_status", "current challenge does not accept a code")
    if expected_revision is not None and int(row_get(handoff, "revision", 0) or 0) != int(
            expected_revision):
        raise ApiError(409, "revision_conflict", "stale assist revision")
    return handoffs.submit_handoff_value(handoff["id"], value)


def done_with_session(raw_session, expected_revision=None):
    """CAPTCHA / verify_page path, human clicked I'm done."""
    import handoffs
    handoff = valid_session(raw_session)
    if not handoff:
        raise ApiError(410, "gone", "assist session invalid or expired")
    if continuation_of(handoff) != "verify_page":
        raise ApiError(409, "bad_status", "current challenge is not a page verify")
    if expected_revision is not None and int(row_get(handoff, "revision", 0) or 0) != int(
            expected_revision):
        raise ApiError(409, "revision_conflict", "stale assist revision")
    return handoffs.verify_handoff_page(handoff["id"])


def _credential_allowlist(handoff, attempt):
    """Host allowlist: credential.verification_hosts, else credential.domains."""
    import links
    cid = handoff["computer_id"] if handoff else None
    name = None
    if handoff:
        name = row_get(handoff, "login_credential")
    if not name and attempt:
        name = attempt["credential"]
    if not cid or not name:
        # Fall back to handoff.domain alone when present.
        dom = row_get(handoff, "domain") if handoff else None
        return [dom] if dom else []
    return links.verification_allowlist(cid, name)


def open_with_session(raw_session, url, expected_revision=None):
    """Navigate remote Chromium to a human-pasted HTTPS URL after host policy.

    The URL is never persisted. Callers must not log it.
    """
    import links
    from deskclient import auth_navigate_verification
    from lifecycle import get_computer

    view = session_view(raw_session)
    if not view:
        raise ApiError(410, "gone", "assist session invalid or expired")
    bound, current, attempt = view
    handoff = current
    if not handoff or handoff["status"] not in LIVE_STATUSES:
        raise ApiError(410, "gone", "no open challenge to navigate")
    built = build_view(bound, current, attempt)
    if "open_url" not in built["allowed_actions"]:
        raise ApiError(409, "bad_status", "open_url is not allowed for this challenge")
    if expected_revision is not None and int(row_get(handoff, "revision", 0) or 0) != int(
            expected_revision):
        raise ApiError(409, "revision_conflict", "stale assist revision")

    host = links.validate_assist_open_url(url)
    allow = _credential_allowlist(handoff, attempt)
    if not links.host_allowed(host, allow):
        raise ApiError(400, "domain_mismatch", "url host is not on the allowlist")

    computer = get_computer(handoff["computer_id"])
    # deskd final-origin check uses the same allowlist; never store the URL.
    auth_navigate_verification(computer, url, domains=allow)
    return {"ok": True}


def check_same_origin(request):
    """CSRF: POST must carry Origin or Referer matching this Host."""
    host = (request.headers.get("host") or "").lower()
    if not host:
        return False
    origin = request.headers.get("origin") or ""
    if origin:
        return urlsplit(origin).netloc.lower() == host
    referer = request.headers.get("referer") or ""
    if referer:
        return urlsplit(referer).netloc.lower() == host
    return False


# ---- HTML (minimal; no framing of secrets into email, pages only) ----

_SHELL = """<!doctype html><html lang=en data-assist-fp="{fp}">
<meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<meta name=referrer content=same-origin>
<title>{title} — Case</title>
<style>
 body{{font:16px/1.5 system-ui;margin:0;background:#f5f5f4;color:#1c1917}}
 main{{max-width:{maxw};margin:{marg};padding:0 1rem;text-align:{align}}}
 input{{width:100%;padding:.7rem;border:1px solid #d6d3d1;border-radius:8px;font-size:1.25rem;
  box-sizing:border-box;letter-spacing:.1em}}
 button{{margin-top:1rem;width:100%;padding:.7rem;border:0;border-radius:8px;
  background:#1c1917;color:#fff;font-size:1rem}}
 p.note{{font-size:.85rem;color:#57534e}}
 iframe{{width:100%;height:70vh;border:1px solid #d6d3d1;border-radius:8px;background:#fff}}
 a.open{{display:inline-block;margin:.5rem 0;color:#1c1917}}
</style>
<main>{body}</main>
{script}
</html>"""

GONE_HTML = """<!doctype html><meta charset=utf-8><meta name=referrer content=no-referrer>
<title>Link expired — Case</title>
<style>body{font:16px/1.5 system-ui;background:#f5f5f4;color:#1c1917}
main{max-width:26rem;margin:16vh auto;text-align:center;padding:0 1rem}</style>
<main><h1>Link expired</h1><p>This assist link was already used or has expired.
Ask for a fresh one.</p></main>"""

DONE_HTML = """<!doctype html><meta charset=utf-8><meta name=referrer content=no-referrer>
<title>Done — Case</title>
<style>body{font:16px/1.5 system-ui;background:#f5f5f4;color:#1c1917}
main{max-width:26rem;margin:16vh auto;text-align:center;padding:0 1rem}</style>
<main><h1>{title}</h1><p>{body}</p></main>"""

# noVNC entry without a vnc ?token=, assist cookie (Path=/) is the auth.
DESK_ENTRY = "/desk/vnc.html?autoconnect=1&resize=scale&path=desk/websockify"

ASSIST_JS = """(function(){
var el=document.documentElement;
var path=location.pathname.replace(/\\/$/,"");
var prev=el.getAttribute("data-assist-fp")||"";
var submitting=false,timer;
document.addEventListener("submit",function(){
submitting=true;
clearInterval(timer);
var button=document.querySelector("button[type=submit],button:not([type])");
if(button)button.disabled=true;
});
function poll(){
if(submitting)return;
fetch(path+"/state",{credentials:"same-origin",cache:"no-store"})
.then(function(r){return r.ok?r.json():null})
.then(function(s){
if(!s)return;
var fp=[s.status,String(s.revision),s.continuation||"",(s.allowed_actions||[]).join(",")].join("|");
if(prev&&fp!==prev)location.reload();
prev=fp;
}).catch(function(){});
}
timer=setInterval(poll,2500);
})();
"""


def _fp(view):
    return "|".join([
        str(view["status"]),
        str(view["revision"]),
        view["continuation"] or "",
        ",".join(view["allowed_actions"]),
    ])


def _shell(title, body, view, *, maxw="26rem", marg="8vh auto", align="left", poll=True):
    script = '<script src="/assist/static/assist.js" defer></script>' if poll else ""
    return _SHELL.format(
        fp=html_mod.escape(_fp(view), quote=True),
        title=html_mod.escape(title),
        maxw=maxw, marg=marg, align=align,
        body=body, script=script,
    )


def _open_form(token, revision):
    return (
        f'<form method=post action="/assist/{html_mod.escape(token)}/open">'
        f'<input type=hidden name=expected_revision value="{int(revision)}">'
        f'<label class=note>Open a verification link on the computer</label>'
        f'<input name=url type=url required placeholder="https://…"'
        f' autocomplete=off spellcheck=false>'
        f'<button type=submit>Open on computer</button></form>'
    )


def render_page(view, token):
    """HTML body for GET /assist/{token} after auth."""
    st = view["status"]
    cont = view["continuation"]
    prompt = html_mod.escape(view["instructions"] or "")
    tok = html_mod.escape(token)
    rev = int(view["revision"] or 0)

    if st in _ATTEMPT_TERMINAL or st in ("completed", "failed", "expired"):
        title = {
            "authenticated": "Signed in",
            "unverified": "Unverified",
            "failed": "Failed",
            "expired": "Expired",
            "cancelled": "Cancelled",
            "completed": "Done",
        }.get(st, "Done")
        body = f"<h1>{html_mod.escape(title)}</h1><p>{prompt}</p>"
        return _shell(title, body, view, marg="16vh auto", align="center", poll=False)

    if st == "proving":
        body = f"<h1>Verifying</h1><p class=note>{prompt}</p>" \
               f"<p class=note>This page updates automatically.</p>"
        return _shell("Verifying", body, view, marg="16vh auto", align="center", poll=True)

    if cont == "submit_value":
        body = (
            f"<h1>Enter the code</h1><p class=note>{prompt}</p>"
            f'<form method=post action="/assist/{tok}/submit">'
            f'<input type=hidden name=expected_revision value="{rev}">'
            f'<input name=value required autocomplete=one-time-code inputmode=numeric '
            f'placeholder="verification code" autofocus>'
            f"<button>Submit</button></form>"
        )
        return _shell("Enter code", body, view, poll=True)

    if cont == "wait_external":
        open_bit = _open_form(token, rev) if "open_url" in view["allowed_actions"] else ""
        body = (
            f"<h1>Waiting on you</h1><p class=note>{prompt}</p>"
            f"<p class=note>Approve or finish the step outside this page. "
            f"This screen refreshes automatically.</p>{open_bit}"
        )
        return _shell("Waiting", body, view, poll=True)

    # verify_page (captcha / device / passkey), live desk
    open_bit = _open_form(token, rev) if "open_url" in view["allowed_actions"] else ""
    body = (
        f"<h1>Clear the challenge</h1><p class=note>{prompt}</p>"
        f'<p><a class=open href="{DESK_ENTRY}" target=_blank rel=noopener>'
        f"Open desktop in a new tab</a></p>"
        f'<iframe src="{DESK_ENTRY}" title="Live desktop" '
        f'allow="clipboard-read; clipboard-write"></iframe>'
        f"{open_bit}"
        f'<form method=post action="/assist/{tok}/done">'
        f'<input type=hidden name=expected_revision value="{rev}">'
        f"<button>I'm done</button></form>"
    )
    return _shell("Clear the challenge", body, view, maxw="40rem", marg="4vh auto", poll=True)


def parse_form(body: bytes):
    return {k: v[0] for k, v in parse_qs(body.decode()).items()}
