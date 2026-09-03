# SPDX-License-Identifier: AGPL-3.0-only
"""Handoff engine, the human-fallback loop.

A handoff pauses a run when the machine hits something only a human can clear
(2FA/CAPTCHA/approval). This module owns handoff lifetime and the login-resume
bridge; deskd only executes the mechanical login/resume. LOGIN_CTX is a
rebuildable cache mapping an open login handoff → the credential to resume;
when `attempt_id` is set, the AuthAttempt row owns the journey (challenge
completion does not record credential success).

Continuation modes (typed state for Assist):
  submit_value , human supplies a code/answer; we may /login/resume with it
  verify_page  , human clears a challenge in the live desk; we confirm it is gone
  wait_external, reserved (no answer path yet)

Status: pending → validating → completed|failed; TTL → expired. All status
writes go through store.transition_handoff (CAS + revision bump). Legacy
`answered` is treated as completed on read for one release.
"""
import hmac
import os

import assist
import captcha
from datetime import datetime, timezone

from config import API_BASE, HANDOFF_TTL, log
from deskclient import auth_submit_challenge, desk_json, eval_js, screenshot_b64
from errors import ApiError
from events import emit
from lifecycle import get_computer
from notify import notifier
from store import store
from util import new_id, row_get


def _public_host():
    """FQDN for assist URLs. Set CASE_PUBLIC_HOST when a reverse proxy fronts
    the API; unset, assist links are simply not minted into notifications."""
    return (os.environ.get("CASE_PUBLIC_HOST") or "").strip().rstrip("/")

LOGIN_CTX = {}   # handoff_id -> {"computer_id", "credential"}

# Default continuation by challenge kind. Callers may override (e.g. approval → wait_external).
CONTINUATION_BY_KIND = {
    "otp": "submit_value",
    "captcha": "verify_page",
    "device": "verify_page",
    "passkey": "verify_page",
    "approval": "submit_value",
    "question": "submit_value",
}

VERIFY_DONE_VALUES = frozenset({"done", "approve", "i'm done", "im done", "i am done"})

TERMINAL_STATUSES = frozenset({"completed", "answered", "failed", "expired"})


def continuation_for(kind, continuation=None):
    if continuation:
        return continuation
    return CONTINUATION_BY_KIND.get(kind, "submit_value")


def _public_status(status):
    # Prefer writing only new statuses; treat legacy answered as completed on read.
    return "completed" if status == "answered" else status


def handoff_json(row, with_screenshot=True):
    # OTP / free-text codes are never returned: secrets stay absent from API answers.
    # Only non-secret continuation markers (approve/deny/done) may appear in `answer`.
    raw_answer = row["answer"]
    public_answer = None
    if isinstance(raw_answer, str) and raw_answer.lower() in (
            "approve", "deny", "done"):
        public_answer = raw_answer.lower()
    d = {"id": row["id"], "computer_id": row["computer_id"], "kind": row["kind"],
         "prompt": row["prompt"], "status": _public_status(row["status"]),
         "created_at": row["created_at"], "answer": public_answer, "domain": row["domain"],
         "continuation": row_get(row, "continuation"),
         "challenge_fingerprint": row_get(row, "challenge_fingerprint")}
    if with_screenshot:
        d["screenshot_png_b64"] = row["screenshot"]
    return d


def _durable_answer(kind, continuation, value):
    """What (if anything) may be persisted on handoffs.answer.

    OTP/question codes are one-time secrets, never written to SQLite. Approve/deny/done
    are non-secret status markers and stay for audit/compat.
    """
    if value is None:
        return None
    s = str(value).strip()
    low = s.lower()
    # Approval markers first, "approve" is also a verify_page synonym, but an
    # approval handoff must persist approve/deny, not collapse to "done".
    if kind == "approval":
        return low if low in ("approve", "deny") else None
    if continuation == "verify_page" or low in VERIFY_DONE_VALUES:
        return "done"
    if low in ("approve", "deny"):
        return low
    return None  # otp / question / anything else


def rebuild_login_ctx():
    """After a cased restart, repopulate the in-memory login map from pending login
    handoffs, so answering one still resumes the login instead of silently dropping it.

    Also resets interrupted `validating` rows back to `pending`, a mid-verify crash
    must not leave the handoff stuck where neither answer nor expire can finish it cleanly.
    """
    for h in store.pending_login_handoffs():
        LOGIN_CTX[h["id"]] = {"computer_id": h["computer_id"], "credential": h["login_credential"]}
        if h["status"] == "validating":
            store.transition_handoff(h["id"], "pending", answer=None)


def create_handoff(computer_row, kind, prompt, screenshot=None, login_credential=None,
                   domain=None, continuation=None, challenge_fingerprint=None,
                   attempt_id=None, sequence=None, revision=None):
    hid = new_id("h")  # 40-bit default, relay scopes by (box, id); still avoid collisions
    cont = continuation_for(kind, continuation)
    store.insert_handoff(
        hid, computer_row["id"], kind, prompt, screenshot, login_credential,
        domain, continuation=cont, challenge_fingerprint=challenge_fingerprint,
        attempt_id=attempt_id, sequence=sequence,
        revision=0 if revision is None else int(revision))
    if login_credential:
        LOGIN_CTX[hid] = {"computer_id": computer_row["id"], "credential": login_credential}
    row = store.get_handoff(hid)
    emit("handoff_created", handoff_json(row, with_screenshot=False))
    # Mint Assist exchange token before notify, plaintext rides the email link once;
    # API/events never see it. Hash-at-rest only (assist.mint_assist_token).
    raw_token, expires_at = assist.mint_assist_token(hid)
    host = _public_host()
    assist_url = f"https://{host}/assist/{raw_token}" if host else ""
    if not host:
        log.warning("CASE_PUBLIC_HOST unset — notification carries no assist link")
    answer_url = ""
    if kind == "approval":
        answer_url = f"{API_BASE.removesuffix('/v1')}/answer/{hid}/{store.sign('answer:' + hid)}"
    notifier.notify({
        "id": hid,
        "computer_id": computer_row["id"],
        "kind": kind,
        "prompt": prompt,
        "screenshot": screenshot,
        "domain": domain,
        "assist_url": assist_url,
        "answer_url": answer_url,
        "expires_at": expires_at,
    }, computer_row["name"])
    return handoff_json(row)


# Agent-minted kinds. `device`/`captcha`/`passkey` → verify_page (live /desk assist).
# OTP walls still go through computer_login → typed challenge, not this door.
_REQUEST_KINDS = frozenset({"approval", "question", "device", "captcha", "passkey"})


def request_handoff(cid, kind, prompt):
    row = get_computer(cid)
    if kind not in _REQUEST_KINDS:
        raise ApiError(400, "bad_request",
                       "kind must be approval|question|device|captcha|passkey")
    if not prompt:
        raise ApiError(400, "bad_request", "missing 'prompt'")
    shot = screenshot_b64(row) if row["state"] == "running" else None
    return create_handoff(row, kind, prompt, screenshot=shot)


def _attempt_id_of(row):
    return row_get(row, "attempt_id") if row is not None else None


def expire_stale():
    cutoff = (datetime.now(timezone.utc) - HANDOFF_TTL).strftime("%Y-%m-%dT%H:%M:%SZ")
    for h in store.stale_pending_handoffs(cutoff):
        if store.transition_handoff(h["id"], "expired") is None:
            continue   # another writer (answer/resume) won the race; not ours to expire
        ctx = LOGIN_CTX.pop(h["id"], None)
        aid = _attempt_id_of(h)
        if aid:
            # Attempt owns credential outcome, do not double-write from LOGIN_CTX.
            try:
                import auth_attempts  # cycle: auth_attempts → handoffs on raise_challenge
                auth_attempts.fail_attempt(aid, reason="handoff_expired")
            except Exception as e:
                log.warning("expire_stale fail_attempt %s: %s", aid, e)
            continue
        if ctx:   # login handoffs only, a plain approval must never touch the vault
            # nobody answered: the login did not happen, and the vault says so
            store.record_credential_result(ctx["computer_id"], ctx["credential"], "failed")
            emit("login_completed", {"computer_id": ctx["computer_id"],
                                     "credential": ctx["credential"], "status": "failed"})
    import auth_attempts  # cycle: auth_attempts → handoffs on raise_challenge
    # An attempt whose challenge was answered elsewhere (or never raised one) has no
    # handoff to expire, so it would sit `active` forever and 409 every later login.
    for a in store.stale_active_auth_attempts(cutoff):
        try:
            auth_attempts.fail_attempt(a["id"], reason="stale")
        except Exception as e:
            log.warning("expire_stale attempt %s: %s", a["id"], e)


# Lists never carry the screenshot. A pending 2FA handoff holds a full-display PNG as
# base64, and the console polls this every 30s while using four scalar fields, that is
# a few hundred KB per poll for bytes nothing reads. Fetch one by id when you want it.
def list_handoffs(status=None):
    expire_stale()
    return [handoff_json(r, with_screenshot=False) for r in store.list_handoffs(status)]


def list_computer_handoffs(cid):
    get_computer(cid)
    expire_stale()
    return [handoff_json(r, with_screenshot=False)
            for r in store.list_handoffs_for(cid)]


def get_handoff(hid):
    row = store.get_handoff(hid)
    if not row:
        raise ApiError(404, "not_found", f"no handoff {hid!r}")
    return handoff_json(row)


def _require_open_handoff(hid):
    expire_stale()
    row = store.get_handoff(hid)
    if not row:
        raise ApiError(404, "not_found", f"no handoff {hid!r}")
    status = row["status"]
    if status == "expired":
        raise ApiError(409, "handoff_expired", "handoff expired after 15 minutes")
    if status in TERMINAL_STATUSES:
        raise ApiError(409, "already_answered", "handoff already answered")
    if status == "validating":
        raise ApiError(409, "validating", "handoff is already being validated")
    if status != "pending":
        raise ApiError(409, "bad_status", f"handoff status {status!r} cannot be answered")
    return row


def _continuation_of(row):
    return row_get(row, "continuation") or continuation_for(row["kind"])


def _complete(hid, *, answer=None, value_present=False):
    # Persist only non-secret markers (approve/deny/done); OTP codes stay out of SQLite.
    row = store.get_handoff(hid)
    stored = _durable_answer(row["kind"], _continuation_of(row), answer) if row else None
    done = store.transition_handoff(hid, "completed", answer=stored)
    if done is not None:
        emit("handoff_answered", {"handoff_id": hid, "value_present": bool(value_present),
                                  "verified": True})
    return done or store.get_handoff(hid)


def _claim_validating(row, answer):
    """CAS pending → validating (claim_challenge) before desk work."""
    rev = int(row["revision"] or 0)
    import auth_attempts  # cycle: auth_attempts → handoffs on raise_challenge
    auth_attempts.claim_challenge(row["id"], rev)
    # Never park an OTP in validating.answer, soft-fail / restart paths read this row.
    stored = _durable_answer(row["kind"], _continuation_of(row), answer)
    store.transition_handoff(row["id"], "validating", answer=stored)
    return store.get_handoff(row["id"])


def _continue_attempt(attempt_id):
    """Re-enter attempt orchestration after a child challenge completes."""
    try:
        import auth_attempts  # cycle: auth_attempts → handoffs on raise_challenge
        return auth_attempts.advance_attempt(attempt_id)
    except Exception as e:
        log.warning("advance_attempt after challenge %s: %s", attempt_id, e)
        return None


def _finish_attempt_child(hid, hrow, *, answer=None, value_present=False, ctx=None):
    """Complete a child challenge; attempt owner records credential success/fail."""
    LOGIN_CTX.pop(hid, None)
    done = _complete(hid, answer=answer, value_present=value_present)
    aid = _attempt_id_of(hrow)
    if aid:
        _continue_attempt(aid)
    elif ctx:
        # Legacy login handoff without an attempt, preserve prior success path.
        store.record_credential_result(ctx["computer_id"], ctx["credential"], "success")
        emit("login_completed", {"computer_id": ctx["computer_id"],
                                 "credential": ctx["credential"], "status": "success"})
    return done


def _fail_attempt_child(hid, hrow, value, ctx=None):
    LOGIN_CTX.pop(hid, None)
    stored = _durable_answer(hrow["kind"], _continuation_of(hrow), value)
    store.transition_handoff(hid, "failed", answer=stored)
    aid = _attempt_id_of(hrow)
    if aid:
        try:
            import auth_attempts  # cycle: auth_attempts → handoffs on raise_challenge
            auth_attempts.fail_attempt(aid, reason="denied")
        except Exception as e:
            log.warning("fail_attempt after deny %s: %s", aid, e)
    elif ctx:
        store.record_credential_result(ctx["computer_id"], ctx["credential"], "failed")
        emit("login_completed", {"computer_id": ctx["computer_id"],
                                 "credential": ctx["credential"], "status": "failed"})
    return store.get_handoff(hid)


def _resume_and_finish(hid, ctx, value, *, value_present=True, hrow=None):
    """Call deskd /login/resume synchronously; only complete on success.

    Failed verify stays pending and keeps LOGIN_CTX, never records credential success
    and never emits login_completed(success). Deny is terminal (failed).

    When the handoff carries attempt_id, success completes the child and re-enters
    advance_attempt, the attempt (not LOGIN_CTX) owns credential ok / prove.
    """
    hrow = hrow or store.get_handoff(hid)
    status, reason = "failed", None
    try:
        row = get_computer(ctx["computer_id"])
        out = desk_json(row, "POST", "/login/resume", json={"value": value}, timeout=90)
        status = out.get("status", "failed")
        reason = out.get("reason")
    except Exception as e:
        log.warning("login resume failed: %s", e)
        status, reason = "failed", str(e)

    if status == "success":
        return _finish_attempt_child(
            hid, hrow,
            answer=value if value_present else (hrow["answer"] if hrow else None),
            value_present=value_present, ctx=ctx)

    denied = isinstance(reason, str) and "denied" in reason.lower()
    if denied or (isinstance(value, str) and value.lower() == "deny"):
        return _fail_attempt_child(hid, hrow, value, ctx=ctx)

    # Soft fail: challenge still present / bad code / transient, human can retry.
    # Never leave a one-time code sitting in answer.
    store.transition_handoff(hid, "pending", answer=None)
    return store.get_handoff(hid)


def submit_handoff_value(hid, value):
    """submit_value continuation: store the human value, resume login if held, verify."""
    row = _require_open_handoff(hid)
    if _continuation_of(row) != "submit_value":
        raise ApiError(400, "bad_request",
                       f"handoff continuation is {_continuation_of(row)!r}, not submit_value")
    _claim_validating(row, value)
    row = store.get_handoff(hid)
    aid = _attempt_id_of(row)
    ctx = LOGIN_CTX.get(hid)

    if aid:
        # Durable attempt: prefer generic challenge submit (works after prior resume
        # cleared deskd state["login"]); fall back to /login/resume while held.
        computer = get_computer(row["computer_id"])
        submitted = False
        try:
            out = auth_submit_challenge(computer, row["kind"], value=value)
            submitted = bool(isinstance(out, dict) and out.get("ok"))
        except Exception as e:
            log.warning("auth_submit_challenge failed: %s", e)
            submitted = False
        if submitted:
            return _finish_attempt_child(hid, row, answer=value, value_present=True, ctx=None)
        if ctx:
            return _resume_and_finish(hid, ctx, value, value_present=True, hrow=row)
        # Soft fail, stay pending for retry; never keep the OTP in SQLite.
        store.transition_handoff(hid, "pending", answer=None)
        return store.get_handoff(hid)

    if ctx:
        return _resume_and_finish(hid, ctx, value, value_present=True, hrow=row)
    return _complete(hid, answer=value, value_present=True)


def _page_still_challenged(computer_row):
    """True when the live tab still shows a captcha/challenge gate."""
    try:
        verify = eval_js(computer_row, captcha.VERIFY_JS, timeout_s=15)
    except Exception as e:
        log.warning("handoff verify eval failed: %s", e)
        return True
    v = (verify or {}).get("value") if isinstance(verify, dict) else None
    page_blob, has_password = "", False
    if isinstance(v, dict):
        page_blob = v.get("text") if isinstance(v.get("text"), str) else ""
        has_password = bool(v.get("hasPassword"))
    elif isinstance(v, str):
        page_blob = v
    if captcha.still_challenge(page_blob, has_password):
        return True
    try:
        gate = eval_js(computer_row, captcha.GATE_JS, timeout_s=10)
    except Exception as e:
        log.warning("handoff gate eval failed: %s", e)
        return True
    g = (gate or {}).get("value") if isinstance(gate, dict) else gate
    return captcha.gate_open(g if isinstance(g, dict) else {})


def verify_handoff_page(hid):
    """verify_page continuation: confirm the challenge is gone, then approve-resume if login held."""
    row = _require_open_handoff(hid)
    if _continuation_of(row) != "verify_page":
        raise ApiError(400, "bad_request",
                       f"handoff continuation is {_continuation_of(row)!r}, not verify_page")
    _claim_validating(row, "done")
    row = store.get_handoff(hid)
    computer = get_computer(row["computer_id"])
    if _page_still_challenged(computer):
        # Never claim login success; leave LOGIN_CTX for retry.
        store.transition_handoff(hid, "pending", answer=None)
        return store.get_handoff(hid)

    aid = _attempt_id_of(row)
    ctx = LOGIN_CTX.get(hid)
    if aid:
        # Best-effort clear of deskd login hold; page verify already passed, so a
        # soft-fail resume (e.g. next OTP wall) must not block the child completing.
        if ctx:
            try:
                desk_json(computer, "POST", "/login/resume",
                          json={"value": "approve"}, timeout=90)
            except Exception as e:
                log.warning("attempt captcha resume (best-effort): %s", e)
        return _finish_attempt_child(hid, row, answer="done", value_present=False, ctx=None)

    if ctx:
        # Approve-style path only, human already cleared the page; do not type a code.
        return _resume_and_finish(hid, ctx, "approve", value_present=False, hrow=row)
    return _complete(hid, answer="done", value_present=False)


def answer_handoff(hid, value):
    """Thin compat wrapper: route by continuation. Prefer submit_/verify_ directly."""
    row = _require_open_handoff(hid)
    cont = _continuation_of(row)
    if cont == "submit_value":
        return submit_handoff_value(hid, value)
    if cont == "verify_page":
        if str(value).strip().lower() in VERIFY_DONE_VALUES:
            return verify_handoff_page(hid)
        raise ApiError(400, "bad_request",
                       "verify_page handoff expects value 'done' (or 'approve')")
    raise ApiError(400, "bad_request",
                   f"handoff continuation {cont!r} cannot be answered this way")


def answer_token_ok(hid, token):
    return hmac.compare_digest(store.sign("answer:" + hid), token or "")


def answer_by_token(hid, token, value):
    """Public ntfy-button door: the token is the only credential, so a bad one is a 404."""
    if not answer_token_ok(hid, token):
        raise ApiError(404, "not_found", "no such handoff")
    return answer_handoff(hid, value)


def on_ntfy_answer(hid, value):
    if hid is None:
        pending = store.pending_handoff_ids()
        if len(pending) != 1:
            raise ApiError(400, "ambiguous", f"{len(pending)} pending handoffs; prefix with handoff id")
        hid = pending[0]
    answer_handoff(hid, value)
