# SPDX-License-Identifier: AGPL-3.0-only
"""Durable authentication attempts, orchestration.

One vault login that needs human help is one AuthAttempt spanning zero or more
child Handoffs. MCP connections carry no workflow state; every call names an
explicit attempt_id.

deskd is observation/action only. This module owns attempt transitions, proof,
and challenge binding. Captcha auto-solve stays in cased (optional hook) to
avoid a captcha↔auth_attempts import cycle.
"""
import asyncio
import base64
import hashlib
import hmac
import json
import struct
import time

from errors import ApiError
from store import store
from util import new_id, row_get

ACTIVE_STATUSES = frozenset(store.AUTH_ATTEMPT_ACTIVE)
TERMINAL_STATUSES = frozenset({"authenticated", "unverified", "failed", "expired", "cancelled"})

# Long-poll ceiling stays under a typical reverse-proxy read timeout (300s)
# and the MCP wait budget.
WAIT_TIMEOUT_MAX_S = 270
WAIT_TIMEOUT_DEFAULT_S = 30

# Optional: fn(computer_row, computer_id, credential_name) -> {"status": "success"|"failed"}|None
_CAPTCHA_AUTO = None

# Signal priority matches deskd.classify order (generic tags only, no site names).
_SIGNAL_PRIORITY = ("captcha", "otp", "approval", "email_verify", "passkey")


def set_captcha_auto(fn):
    """Register cased's captcha auto-solver (keeps captcha imports out of this module)."""
    global _CAPTCHA_AUTO
    _CAPTCHA_AUTO = fn


# Positive proof predicates only, unknown keys never count as configured proof.
_PROOF_KEYS = frozenset({"url_prefix", "url_contains", "selector", "expression"})


def _normalize_proof_predicates(proof_spec):
    """Return recognized non-empty predicates, or None if the spec is unusable."""
    if not isinstance(proof_spec, dict) or not proof_spec:
        return None
    out = {}
    for key in _PROOF_KEYS:
        if key not in proof_spec:
            continue
        val = proof_spec[key]
        if not isinstance(val, str) or not val.strip():
            continue
        out[key] = val
    return out or None


def _proof_level(proof_spec):
    """configured when a positive proof_spec is present, else heuristic (compat)."""
    return "configured" if parse_proof_spec(proof_spec) else "heuristic"


def parse_proof_spec(raw):
    """Parse + validate. Unknown/empty predicates → None (missing proof → unverified)."""
    if raw is None or raw == "" or raw == "{}":
        return None
    if isinstance(raw, dict):
        return _normalize_proof_predicates(raw)
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return None
        return _normalize_proof_predicates(parsed) if isinstance(parsed, dict) else None
    return None


def attempt_public(row):
    """Public AuthAttempt, never secrets, OTP answers, or raw proof_spec."""
    if row is None:
        return None
    proof_spec = row_get(row, "proof_spec")
    return {
        "id": row["id"],
        "computer_id": row["computer_id"],
        "credential": row["credential"],
        "status": row["status"],
        "revision": int(row["revision"] or 0),
        "current_handoff_id": row["current_handoff_id"],
        "target_url": row["target_url"],
        "proof_level": _proof_level(proof_spec),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def login_result(attempt, reason=None):
    """Compat LoginResult from a public AuthAttempt (attempt_id always present)."""
    base = {
        "attempt_id": attempt["id"],
        "revision": attempt["revision"],
    }
    st = attempt["status"]
    if st == "authenticated":
        return {**base, "status": "success", "proof_level": attempt["proof_level"]}
    if st == "awaiting_human":
        return {**base, "status": "handoff_pending",
                "handoff_id": attempt["current_handoff_id"]}
    if st == "unverified":
        return {**base, "status": "unverified",
                "reason": reason or "proof_missing_or_failed"}
    if st == "failed":
        return {**base, "status": "failed",
                "reason": reason or "authentication_failed"}
    if st == "cancelled":
        return {**base, "status": "failed", "reason": reason or "cancelled"}
    if st == "expired":
        return {**base, "status": "failed", "reason": reason or "expired"}
    # still active, caller usually shouldn't hit this via login_result
    return {**base, "status": st}


def _require(attempt_id):
    row = store.get_auth_attempt(attempt_id)
    if not row:
        raise ApiError(404, "not_found", f"auth attempt {attempt_id} not found")
    return row


def _publish_updated(pub):
    """Wake long-poll waiters / SSE after a material attempt change."""
    if not pub:
        return
    from events import emit
    emit("auth_attempt_updated", {
        "attempt_id": pub["id"],
        "computer_id": pub["computer_id"],
        "status": pub["status"],
        "revision": pub["revision"],
        "current_handoff_id": pub["current_handoff_id"],
    })


def _cas_or_conflict(aid, from_status, to_status, revision_expect):
    n = store.cas_auth_attempt_status(aid, from_status, to_status, revision_expect)
    if n != 1:
        raise ApiError(409, "revision_conflict",
                       "auth attempt revision or status changed")
    pub = attempt_public(store.get_auth_attempt(aid))
    _publish_updated(pub)
    return pub


def _cursor_changed(pub, after_revision, after_handoff_id):
    """True when the attempt has moved past the client's last-seen cursor."""
    if pub["status"] in TERMINAL_STATUSES:
        return True
    if int(pub["revision"] or 0) > int(after_revision or 0):
        return True
    if after_handoff_id is not None and pub["current_handoff_id"] != after_handoff_id:
        return True
    return False


def _wait_payload(pub, *, changed, wait_status=None):
    """Public wait response, attempt snapshot + optional compat LoginResult."""
    st = "changed" if changed else "timeout"
    if wait_status:
        st = wait_status
    elif pub["status"] in TERMINAL_STATUSES:
        st = "terminal"
    # login_result on every payload: the MCP client relays it verbatim instead of
    # keeping its own copy of the status vocabulary.
    return {"changed": bool(changed), "wait_status": st, "attempt": pub,
            "login_result": login_result(pub)}


def _totp(seed, at=None):
    # RFC-6238; mirrored in image/deskd.py totp (image cannot import control-plane).
    key = base64.b32decode(seed.replace(" ", "").upper(), casefold=True)
    ctr = int((at or time.time()) // 30)
    h = hmac.new(key, struct.pack(">Q", ctr), hashlib.sha1).digest()
    o = h[-1] & 15
    return str((int.from_bytes(h[o:o + 4], "big") & 0x7FFFFFFF) % 10 ** 6).zfill(6)


def _next_sequence(attempt_id):
    return store.next_handoff_sequence(attempt_id)


def _ensure_advancing(row, expected_revision=None):
    """CAS into advancing from created|awaiting_human; return (row, revision)."""
    status = row["status"]
    if status in TERMINAL_STATUSES:
        raise ApiError(409, "illegal_transition",
                       f"attempt already terminal ({status})")
    rev = int(row["revision"] or 0) if expected_revision is None else int(expected_revision)
    if expected_revision is not None and int(row["revision"] or 0) != rev:
        raise ApiError(409, "revision_conflict",
                       "auth attempt revision or status changed")
    if status in ("created", "awaiting_human"):
        _cas_or_conflict(row["id"], status, "advancing", rev)
        row = store.get_auth_attempt(row["id"])
        rev = int(row["revision"] or 0)
    elif status == "proving":
        return row, rev
    elif status != "advancing":
        raise ApiError(409, "illegal_transition",
                       f"cannot advance from status {status}")
    return row, rev


def start_attempt(computer_id, credential_name, target_url, proof_spec=None,
                  idempotency_key=None):
    """Create an attempt in status=created, or return the idempotent prior row.

    Idempotency: same (computer_id, idempotency_key) returns the existing attempt
    whether it is still active or already terminal. A different key (or no key)
    while another attempt is active → 409 auth_in_progress.
    """
    # Drop unknown/empty predicates so a typo never looks "configured".
    proof_spec = parse_proof_spec(proof_spec)
    if idempotency_key:
        existing = store.get_auth_attempt_by_idempotency(computer_id, idempotency_key)
        if existing:
            return attempt_public(existing)

    active = store.get_active_auth_attempt(computer_id)
    if active:
        raise ApiError(409, "auth_in_progress",
                       "computer already has an active authentication attempt")

    aid = new_id("a")
    try:
        store.insert_auth_attempt(
            aid, computer_id, credential_name, target_url,
            proof_spec=proof_spec, idempotency_key=idempotency_key, status="created")
    except Exception as e:
        # Partial unique / idempotency race: re-read and return if another writer won.
        if idempotency_key:
            raced = store.get_auth_attempt_by_idempotency(computer_id, idempotency_key)
            if raced:
                return attempt_public(raced)
        active = store.get_active_auth_attempt(computer_id)
        if active:
            raise ApiError(409, "auth_in_progress",
                           "computer already has an active authentication attempt") from e
        raise
    return attempt_public(store.get_auth_attempt(aid))


def get_attempt(attempt_id):
    pub = attempt_public(_require(attempt_id))
    return {**pub, "login_result": login_result(pub)}


def cancel_attempt(attempt_id, expected_revision=None):
    row = _require(attempt_id)
    if row["status"] in TERMINAL_STATUSES:
        if row["status"] == "cancelled":
            return attempt_public(row)
        raise ApiError(409, "illegal_transition",
                       f"cannot cancel terminal attempt in status {row['status']}")
    rev = int(row["revision"] or 0) if expected_revision is None else int(expected_revision)
    hid = row_get(row, "current_handoff_id")
    pub = _cas_or_conflict(attempt_id, row["status"], "cancelled", rev)
    # Terminalize the open child so handoff_list cannot leave a stale pending pin.
    if hid:
        h = store.get_handoff(hid)
        if h and h["status"] in ("pending", "validating"):
            store.transition_handoff(hid, "failed", answer=None)
            try:
                import handoffs  # cycle: handoffs → auth_attempts on answer paths
                handoffs.LOGIN_CTX.pop(hid, None)
            except Exception:
                pass
    return pub


def reobserve_if_solved(attempt_id):
    """Humans can clear a challenge directly on the desk (the Drive UI has no
    Assist surface), which bumps nothing — the attempt would wait forever.
    Peek at the page; only when the challenge is gone, advance so it can prove.
    Returns the advanced public attempt, or None when nothing changed."""
    row = store.get_auth_attempt(attempt_id)
    if not row or row["status"] != "awaiting_human":
        return None
    hid = row_get(row, "current_handoff_id")

    from deskclient import observe_auth
    from lifecycle import get_computer

    try:
        computer = get_computer(row["computer_id"])
        if row_get(computer, "state") != "running":
            return None
        resp = observe_auth(computer)
    except Exception:
        return None
    observation = (resp or {}).get("observation") if isinstance(resp, dict) else None
    if observation is None and isinstance(resp, dict) and "challenge_signals" in resp:
        observation = resp
    if observation is None or _classify_kind(observation):
        return None  # challenge still up (or unreadable) — keep waiting
    try:
        pub = advance_attempt(attempt_id, observation=observation)
    except ApiError:
        return None  # raced with an Assist submit; the waiter sees that change
    # The pending child is answered — the human did it on the desk itself.
    if hid and pub["status"] != "awaiting_human":
        h = store.get_handoff(hid)
        if h and h["status"] in ("pending", "validating"):
            store.transition_handoff(hid, "completed", answer=None)
    return pub


async def wait_attempt(attempt_id, after_revision=0, after_handoff_id=None,
                       timeout_s=WAIT_TIMEOUT_DEFAULT_S):
    """Long-poll until the attempt cursor advances, the attempt ends, or timeout.

    Subscribe before the first read so a concurrent Assist completion cannot be
    missed between check and wait. Returns a wait payload (never secrets).
    """
    from events import subscribe, unsubscribe

    try:
        timeout_s = int(timeout_s)
    except (TypeError, ValueError):
        timeout_s = WAIT_TIMEOUT_DEFAULT_S
    timeout_s = max(1, min(timeout_s, WAIT_TIMEOUT_MAX_S))
    try:
        after_revision = int(after_revision or 0)
    except (TypeError, ValueError):
        after_revision = 0
    if after_handoff_id == "":
        after_handoff_id = None

    def _matches(type_, data):
        if not isinstance(data, dict):
            return False
        if data.get("attempt_id") != attempt_id:
            return False
        return type_ in ("auth_attempt_updated", "login_completed")

    q = subscribe()
    try:
        pub = attempt_public(_require(attempt_id))
        if _cursor_changed(pub, after_revision, after_handoff_id):
            return _wait_payload(pub, changed=True)

        deadline = asyncio.get_running_loop().time() + float(timeout_s)
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                pub = attempt_public(_require(attempt_id))
                if _cursor_changed(pub, after_revision, after_handoff_id):
                    return _wait_payload(pub, changed=True)
                if pub["status"] == "awaiting_human":
                    # Once per wait call, before reporting timeout: the human may
                    # have solved the challenge on the desk directly.
                    adv = await asyncio.to_thread(reobserve_if_solved, attempt_id)
                    if adv and _cursor_changed(adv, after_revision, after_handoff_id):
                        return _wait_payload(adv, changed=True)
                return _wait_payload(pub, changed=False, wait_status="timeout")
            try:
                type_, data = await asyncio.wait_for(
                    q.get(), timeout=min(remaining, 15.0))
            except asyncio.TimeoutError:
                # Periodic re-read covers missed publishes (no LOOP / race).
                pub = attempt_public(_require(attempt_id))
                if _cursor_changed(pub, after_revision, after_handoff_id):
                    return _wait_payload(pub, changed=True)
                continue
            if not _matches(type_, data):
                continue
            pub = attempt_public(_require(attempt_id))
            if _cursor_changed(pub, after_revision, after_handoff_id):
                return _wait_payload(pub, changed=True)
    finally:
        unsubscribe(q)


def claim_challenge(handoff_id, expected_revision):
    """CAS handoff pending → validating (Assist / answer path scaffolding)."""
    row = store.get_handoff(handoff_id)
    if not row:
        raise ApiError(404, "not_found", f"handoff {handoff_id} not found")
    n = store.cas_handoff_status(
        handoff_id, "pending", "validating", int(expected_revision))
    if n != 1:
        raise ApiError(409, "revision_conflict",
                       "handoff revision or status changed")
    h = store.get_handoff(handoff_id)
    # Public challenge view, never surface OTP/password answers.
    return {
        "id": h["id"],
        "computer_id": h["computer_id"],
        "kind": h["kind"],
        "prompt": h["prompt"],
        "status": h["status"],
        "created_at": h["created_at"],
        "domain": h["domain"],
        "continuation": row_get(h, "continuation"),
        "attempt_id": row_get(h, "attempt_id"),
        "sequence": row_get(h, "sequence"),
        "revision": int(h["revision"] or 0),
    }


def raise_challenge(attempt_id, kind, prompt, screenshot=None, domain=None,
                    challenge_fingerprint=None, expected_revision=None):
    """Bind a child handoff and move the attempt to awaiting_human."""
    row = _require(attempt_id)
    row, rev = _ensure_advancing(row, expected_revision)
    # One pending/validating child at a time.
    if row["current_handoff_id"]:
        cur = store.get_handoff(row["current_handoff_id"])
        if cur and cur["status"] in ("pending", "validating"):
            # Ensure status is awaiting_human if we somehow still hold a live child.
            if row["status"] == "advancing":
                return _cas_or_conflict(attempt_id, "advancing", "awaiting_human", rev)
            return attempt_public(row)

    from deskclient import screenshot_b64  # cycle: handoffs → auth_attempts → deskclient
    from handoffs import create_handoff  # cycle: handoffs → auth_attempts
    from lifecycle import get_computer

    computer = get_computer(row["computer_id"])
    # store rows are sqlite3.Row, no dict.get
    if screenshot is None and row_get(computer, "state") == "running":
        try:
            screenshot = screenshot_b64(computer)
        except Exception:
            screenshot = None
    seq = _next_sequence(attempt_id)
    h = create_handoff(
        computer, kind, prompt, screenshot=screenshot,
        login_credential=row["credential"], domain=domain,
        challenge_fingerprint=challenge_fingerprint,
        attempt_id=attempt_id, sequence=seq, revision=rev)
    store.set_attempt_handoff(attempt_id, h["id"])
    # Pointer can change before the awaiting_human CAS; wake waiters early.
    _publish_updated(attempt_public(store.get_auth_attempt(attempt_id)))
    # Provisional vault health, definitive answer arrives on prove/fail/expire.
    store.record_credential_result(row["computer_id"], row["credential"], "challenge")
    return _cas_or_conflict(attempt_id, "advancing", "awaiting_human", rev)


def fail_attempt(attempt_id, reason=None, expected_revision=None):
    """Terminal failure; updates credential last_status=failed.

    `reason` is for callers building a LoginResult; not stored on the attempt row.
    """
    _ = reason
    row = _require(attempt_id)
    if row["status"] in TERMINAL_STATUSES:
        return attempt_public(row)
    rev = int(row["revision"] or 0) if expected_revision is None else int(expected_revision)
    pub = _cas_or_conflict(attempt_id, row["status"], "failed", rev)
    store.record_credential_result(row["computer_id"], row["credential"], "failed")
    from events import emit
    emit("login_completed", {
        "computer_id": row["computer_id"],
        "credential": row["credential"],
        "status": "failed",
        "attempt_id": attempt_id,
    })
    return pub


def _classify_kind(observation):
    """Pick the next human/auto challenge kind from a generic observation."""
    obs = observation or {}
    signals = obs.get("challenge_signals") or []
    if not isinstance(signals, list):
        signals = []
    vf = obs.get("visible_fields") or {}
    if not isinstance(vf, dict):
        vf = {}
    for tag in _SIGNAL_PRIORITY:
        if tag in signals:
            # Text alone can't claim an OTP challenge: logged-in security-settings
            # pages say "Two-factor authentication" too. Require a code input.
            if tag == "otp" and not vf.get("code"):
                continue
            return tag
    if vf.get("code") and not vf.get("pass"):
        return "otp"
    return None


def observation_looks_logged_out(observation):
    """Heuristic: still on a password form / open challenge → not authenticated."""
    obs = observation or {}
    vf = obs.get("visible_fields") or {}
    if isinstance(vf, dict) and vf.get("pass"):
        return True
    signals = obs.get("challenge_signals") or []
    return bool(signals)


def check_proof(computer, proof_spec, observation=None):
    """Evaluate proof_spec against the live tab / last observation. Never logs secrets.

    Requires at least one recognized non-empty predicate; otherwise False (never
    authenticate on typo/empty specs).
    """
    from deskclient import eval_js, eval_value

    predicates = _normalize_proof_predicates(proof_spec)
    if not predicates:
        return False

    href = ""
    if observation and isinstance(observation.get("href"), str):
        href = observation["href"]
    if not href:
        try:
            href = eval_value(computer, "location.href", timeout_s=10, default="")
            if not isinstance(href, str):
                href = ""
        except Exception:
            href = ""

    if "url_prefix" in predicates:
        if not href.startswith(predicates["url_prefix"]):
            return False
    if "url_contains" in predicates:
        if predicates["url_contains"] not in href:
            return False

    if "selector" in predicates:
        sel = predicates["selector"].replace("\\", "\\\\").replace("'", "\\'")
        try:
            out = eval_js(computer, f"!!document.querySelector('{sel}')", timeout_s=10)
            val = (out or {}).get("value") if isinstance(out, dict) else out
            if not val:
                return False
        except Exception:
            return False

    if "expression" in predicates:
        try:
            out = eval_js(computer, predicates["expression"], timeout_s=15)
            val = (out or {}).get("value") if isinstance(out, dict) else out
            if not val:
                return False
        except Exception:
            return False

    return True


def prove_attempt(attempt_id, expected_revision=None, observation=None):
    """Prove step: missing/false proof_spec → unverified; verified → authenticated.

    Only `authenticated` records credential success / login_completed(success).
    """
    row = _require(attempt_id)
    if row["status"] in TERMINAL_STATUSES:
        raise ApiError(409, "illegal_transition",
                       f"attempt already terminal ({row['status']})")
    rev = int(row["revision"] or 0) if expected_revision is None else int(expected_revision)
    if expected_revision is not None and int(row["revision"] or 0) != rev:
        raise ApiError(409, "revision_conflict",
                       "auth attempt revision or status changed")

    if row["status"] != "proving":
        # Accept created|advancing|awaiting_human → proving
        if row["status"] not in ("created", "advancing", "awaiting_human"):
            raise ApiError(409, "illegal_transition",
                           f"cannot prove from status {row['status']}")
        _cas_or_conflict(attempt_id, row["status"], "proving", rev)
        row = store.get_auth_attempt(attempt_id)
        rev = int(row["revision"] or 0)

    proof_spec = parse_proof_spec(row["proof_spec"])
    from events import emit
    from lifecycle import get_computer

    if not proof_spec:
        # Spec: missing proof ends unverified, never authenticated.
        pub = _cas_or_conflict(attempt_id, "proving", "unverified", rev)
        store.record_credential_result(row["computer_id"], row["credential"], "unverified")
        emit("login_completed", {
            "computer_id": row["computer_id"],
            "credential": row["credential"],
            "status": "unverified",
            "attempt_id": attempt_id,
        })
        return pub

    computer = get_computer(row["computer_id"])
    ok = check_proof(computer, proof_spec, observation=observation)
    if ok:
        pub = _cas_or_conflict(attempt_id, "proving", "authenticated", rev)
        store.record_credential_result(row["computer_id"], row["credential"], "success")
        emit("login_completed", {
            "computer_id": row["computer_id"],
            "credential": row["credential"],
            "status": "success",
            "attempt_id": attempt_id,
        })
        return pub

    pub = _cas_or_conflict(attempt_id, "proving", "unverified", rev)
    store.record_credential_result(row["computer_id"], row["credential"], "unverified")
    emit("login_completed", {
        "computer_id": row["computer_id"],
        "credential": row["credential"],
        "status": "unverified",
        "attempt_id": attempt_id,
    })
    return pub


def advance_attempt(attempt_id, expected_revision=None, observation=None, _depth=0):
    """Observe → classify → auto-step / child handoff / prove.

    On challenge completion, callers re-enter here (do not record credential ok).
    `observation` may be injected by unit tests (skips deskclient).
    """
    if _depth > 4:
        return fail_attempt(attempt_id, reason="advance_depth_exceeded",
                            expected_revision=expected_revision)

    row = _require(attempt_id)
    if row["status"] in TERMINAL_STATUSES:
        return attempt_public(row)
    row, rev = _ensure_advancing(row, expected_revision)
    # Clear finished child pointer while re-entering from awaiting_human.
    if row["current_handoff_id"]:
        cur = store.get_handoff(row["current_handoff_id"])
        if cur and cur["status"] not in ("pending", "validating"):
            store.set_attempt_handoff(attempt_id, None)
            row = store.get_auth_attempt(attempt_id)

    from deskclient import auth_submit_challenge, observe_auth
    from lifecycle import get_computer

    computer = get_computer(row["computer_id"])
    if observation is None:
        try:
            resp = observe_auth(computer)
            observation = (resp or {}).get("observation") if isinstance(resp, dict) else None
            if observation is None and isinstance(resp, dict) and "challenge_signals" in resp:
                observation = resp
        except Exception as e:
            return fail_attempt(attempt_id, reason=f"observe_failed:{type(e).__name__}")

    kind = _classify_kind(observation)

    # Auto TOTP when vault has a seed.
    if kind == "otp":
        material = store.credential_material(row["computer_id"], row["credential"])
        if material and material.get("totp_seed"):
            try:
                code = _totp(material["totp_seed"])
                out = auth_submit_challenge(computer, "otp", value=code)
                if isinstance(out, dict) and out.get("ok"):
                    return advance_attempt(attempt_id, observation=None, _depth=_depth + 1)
            except Exception:
                pass  # fall through to human handoff

    # Captcha auto via cased hook (optional).
    if kind == "captcha" and _CAPTCHA_AUTO is not None:
        try:
            auto = _CAPTCHA_AUTO(computer, row["computer_id"], row["credential"])
        except Exception:
            auto = None
        if isinstance(auto, dict) and auto.get("status") == "success":
            return advance_attempt(attempt_id, observation=None, _depth=_depth + 1)
        if isinstance(auto, dict) and auto.get("status") == "failed":
            return fail_attempt(attempt_id, reason=auto.get("reason") or "captcha_auto_failed")

    if kind:
        host = ""
        href = (observation or {}).get("href") or ""
        if isinstance(href, str) and "://" in href:
            try:
                from urllib.parse import urlparse
                host = urlparse(href).hostname or ""
            except Exception:
                host = ""
        domain = host or None
        prompt = f"{host}: authentication challenge ({kind})" if host else \
            f"authentication challenge ({kind})"
        # Map email_verify → device-like verify_page handoff kind.
        handoff_kind = "device" if kind == "email_verify" else kind
        if handoff_kind not in ("otp", "captcha", "approval", "device", "passkey", "question"):
            handoff_kind = "approval"
        return raise_challenge(
            attempt_id, handoff_kind, prompt, domain=domain, expected_revision=rev)

    # No challenge left → prove (missing proof_spec → unverified).
    if observation_looks_logged_out(observation) and parse_proof_spec(row["proof_spec"]):
        # Password form still up with a configured proof, treat as failed login.
        return fail_attempt(attempt_id, reason="still_on_login_form",
                            expected_revision=rev)

    return prove_attempt(attempt_id, expected_revision=rev, observation=observation)
