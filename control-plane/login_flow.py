# SPDX-License-Identifier: AGPL-3.0-only
"""Login orchestration: credential proof, deskd /login mapping, captcha auto, gates.

Extracted from cased.py so the composition root stays thin and captcha-auto wiring
can move into lifespan without import-time side effects.
"""
import json
import time

import auth_attempts
import captcha
import events
import handoffs
import links
from config import log
from deskclient import desk_json, eval_js, eval_value, screenshot_b64
from store import store


def _credential_proof_spec(cid, name, body_spec):
    """Body proof_spec wins; else credential vault profile (JSON column)."""
    if body_spec is not None:
        return body_spec
    crow = store.get_credential(cid, name)
    if not crow:
        return None
    raw = crow["proof_spec"] if "proof_spec" in crow.keys() else None
    if not raw:
        return None
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return None
    return raw


def _login_after_desk(row, cid, name, url, attempt, result):
    """Map a deskd /login outcome onto the durable attempt (compat LoginResult)."""
    aid = attempt["id"]
    if result.get("status") == "challenge":
        # DBC auto-solve (login captcha only, env-gated). Verify BEFORE /login/resume
        # so a stuck widget / OTP page never looks like success and deskd state["login"]
        # stays intact for human handoff on failure.
        if result.get("kind") == "captcha" and captcha.enabled():
            auto = _try_captcha_auto(row, cid, name, record=False)
            if auto is not None and auto.get("status") == "success":
                proved = auth_attempts.prove_attempt(aid)
                return auth_attempts.login_result(proved)
            if auto is not None and auto.get("status") == "failed":
                failed = auth_attempts.fail_attempt(
                    aid, reason=auto.get("reason") or "captcha_auto_failed")
                return auth_attempts.login_result(failed, reason=auto.get("reason"))
        # ponytail: Twilio SMS-OTP auto-answer unbuilt until a credential sets otp_phone
        pub = auth_attempts.raise_challenge(
            aid, result["kind"], result["prompt"],
            screenshot=result.get("screenshot_png_b64"),
            domain=links.normalize_domain(url))
        return auth_attempts.login_result(pub)

    if result.get("status") == "success":
        gated = _post_login_gate(row, cid, name, url, attempt_id=aid)
        if gated is not None:
            return gated
        late = _post_login_challenge(row, cid, name, url, attempt_id=aid)
        if late is not None:
            return late
        proved = auth_attempts.prove_attempt(aid)
        return auth_attempts.login_result(proved)

    failed = auth_attempts.fail_attempt(aid, reason=result.get("reason"))
    out = auth_attempts.login_result(failed, reason=result.get("reason"))
    return out


def _post_login_challenge(row, cid, name, url, attempt_id=None):
    """After deskd reports success, re-poll for OTP/email-code walls the SPA painted late.

    Instagram codeentry often arrives after classify already returned success. Without
    this, prove_attempt → unverified and only an orphan blocker handoff appears.
    """
    if not attempt_id:
        return None
    probe = (
        "(() => {"
        "const text=(document.body&&document.body.innerText||'').slice(0,5000);"
        "const href=location.href||'';"
        "const path=location.pathname||'';"
        "const challengePath=path.split('/').some("
        "p=>/^(codeentry|challenge|checkpoint)$/i.test(p));"
        "const otp=/two.?factor|\\b2fa\\b|one.?time|verification code|authentication code|"
        "enter the code|\\b\\d\\s?-?\\s?digit code|check your email/i.test(text)"
        "||challengePath;"
        "return {href, text:text.slice(0,240), otp};"
        "})()"
    )
    deadline = time.time() + 12.0
    while time.time() < deadline:
        v = eval_value(row, probe, timeout_s=8)
        v = v if isinstance(v, dict) else {}
        if v.get("otp"):
            prompt = f"{links.normalize_domain(url)}: {str(v.get('text') or 'enter the code')[:160]}"
            try:
                shot = screenshot_b64(row)
            except Exception:
                shot = None
            pub = auth_attempts.raise_challenge(
                attempt_id, "otp", prompt, screenshot=shot,
                domain=links.normalize_domain(url))
            return auth_attempts.login_result(pub)
        time.sleep(0.8)
    return None


def _settle_after_inject(row, seconds: float = 9.0):
    """Poll readyState/href after captcha submit, do NOT call /login/resume yet."""
    deadline = time.time() + max(1.0, float(seconds))
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        try:
            eval_js(row, captcha.SETTLE_JS, timeout_s=min(5.0, max(1.0, remaining)))
        except Exception:
            pass
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        time.sleep(min(0.8, remaining))


def _gate_info(row):
    """Best-effort read of captcha.GATE_JS. {} when the tab cannot be read."""
    v = eval_value(row, captcha.GATE_JS, timeout_s=10)
    return v if isinstance(v, dict) else {}


def _post_login_gate(row, cid, name, url, attempt_id=None):
    """deskd said success, check the tab is not still parked on a captcha gate.

    LinkedIn's /checkpoint/challenge hides its reCAPTCHA in an iframe, so deskd's
    text-only classify() never emits kind="captcha" and the login reports success
    while the session is not established. When the gate is open we try the optional
    solver (capability-gated); on None/failure, or when DBC is off, we raise a
    typed captcha handoff (verify_page). With attempt_id the child carries the
    journey; deskd already cleared state["login"], so /login/resume would 409.
    Assist clears the live desk and advance_attempt continues.

    Returns a LoginResult / handoff_pending dict when the gate was open, else None.
    """
    info = _gate_info(row)
    if not captcha.gate_open(info):
        return None
    log.info("captcha_auto=gate reason=checkpoint_after_success")
    if captcha.enabled():
        auto = _try_captcha_auto(row, cid, name, resume=False, record=False)
        if auto is not None and auto.get("status") == "success" and attempt_id:
            proved = auth_attempts.prove_attempt(attempt_id)
            return auth_attempts.login_result(proved)
        if auto is not None and auto.get("status") == "success":
            return auto
        if auto is not None and auto.get("status") == "failed" and attempt_id:
            failed = auth_attempts.fail_attempt(
                attempt_id, reason=auto.get("reason") or "captcha_auto_failed")
            return auth_attempts.login_result(failed, reason=auto.get("reason"))
    prompt = (f"{links.normalize_domain(url)}: login is held at a security check "
              "(captcha) that auto-solve could not clear. Open the Assist link /desk, "
              "finish the check by hand, then mark done.")
    try:
        shot = screenshot_b64(row)
    except Exception:
        shot = None   # a screenshot is nice-to-have; the handoff must still be raised
    if attempt_id:
        # Bind as attempt child (login_credential set, advance owns prove).
        pub = auth_attempts.raise_challenge(
            attempt_id, "captcha", prompt, screenshot=shot,
            domain=links.normalize_domain(url))
        return auth_attempts.login_result(pub)
    store.record_credential_result(cid, name, "challenge")
    # kind=captcha → continuation verify_page (typed handoff; email Assist fires)
    h = handoffs.create_handoff(row, "captcha", prompt, screenshot=shot,
                                domain=links.normalize_domain(url))
    return {"status": "handoff_pending", "handoff_id": h["id"]}


def _try_captcha_auto(row, cid, name, resume=True, record=True):
    """Capability-gated solve → inject → settle → verify → resume only on success.

    Uses captcha.solve_if_capable (DBC quarantined). Returns LoginResult on verified
    success, or the failed resume status if verify passed but resume failed (do not
    create a dead handoff). Returns None on any earlier failure, including
    unsupported capability / terminal DBC, so login() falls through to
    create_handoff (typed captcha/verify_page) and never calls /login/resume,
    leaving deskd state["login"] intact.

    resume=False is the post-login-gate path: deskd is not holding a login, so
    success is decided by verify plus a closed gate, and resume is never called.

    record=False when a durable AuthAttempt owns credential success via prove_attempt.
    """
    captcha_id = None
    try:
        info = eval_value(row, captcha.DETECT_JS, timeout_s=15)
        if not isinstance(info, dict):
            log.info("captcha_auto=skip reason=detect_failed")
            return None
        family, key, pageurl = info.get("family"), info.get("key"), info.get("pageurl")
        if not family or not key or not info.get("callback"):
            log.info("captcha_auto=skip reason=no_family_or_callback")
            return None
        enterprise = bool(info.get("enterprise"))
        # Re-assert page URL from the live tab (detect can race a soft nav).
        try:
            live_href = eval_value(row, "location.href", timeout_s=5)
            if isinstance(live_href, str) and live_href.startswith("http"):
                pageurl = live_href
        except Exception:
            pass
        solved = captcha.solve_if_capable(family, pageurl, key, enterprise=enterprise)
        if not solved:
            log.info("captcha_auto=fail reason=solve")
            return None
        captcha_id = solved.get("id")
        inj_val = eval_value(row, captcha.inject_js(family, solved["token"]), timeout_s=15)
        if not isinstance(inj_val, dict) or not inj_val.get("ok"):
            reason = (inj_val or {}).get("error") if isinstance(inj_val, dict) else "inject"
            log.info("captcha_auto=fail reason=%s", reason or "inject")
            if captcha_id:
                captcha.report(captcha_id)
            return None
        # Settle then verify BEFORE resume, resume would clear state["login"].
        _settle_after_inject(row, seconds=9.0)
        verify = eval_js(row, captcha.VERIFY_JS, timeout_s=15)
        v = (verify or {}).get("value") if isinstance(verify, dict) else None
        page_blob = ""
        has_password = False
        if isinstance(v, dict):
            page_blob = v.get("text") if isinstance(v.get("text"), str) else ""
            has_password = bool(v.get("hasPassword"))
        elif isinstance(v, str):
            page_blob = v
        if captcha.still_challenge(page_blob, has_password):
            log.info("captcha_auto=fail reason=still_present")
            if captcha_id:
                captcha.report(captcha_id)
            return None
        if not resume:
            # No deskd login hold to approve. Require the gate itself to be closed
            # before calling this a login, verify text alone can be clean on a page
            # that is still a checkpoint shell.
            if captcha.gate_open(_gate_info(row)):
                log.info("captcha_auto=fail reason=gate_still_open")
                if captcha_id:
                    captcha.report(captcha_id)
                return None
            if record:
                store.record_credential_result(cid, name, "success")
                events.emit("login_completed", {"computer_id": cid, "credential": name,
                                                "status": "success"})
            log.info("captcha_auto=ok path=gate")
            return {"status": "success", "captcha_auto": True}
        # Verified clean, only now approve the deskd login hold.
        resumed = desk_json(row, "POST", "/login/resume",
                            json={"value": "approve"}, timeout=25)
        if resumed.get("status") == "failed":
            log.info("captcha_auto=fail reason=resume_failed")
            if record:
                store.record_credential_result(cid, name, "failed")
                events.emit("login_completed", {"computer_id": cid, "credential": name,
                                                "status": "failed"})
            # Return failed to caller, do not create a dead handoff.
            return resumed if isinstance(resumed, dict) else {"status": "failed"}
        if record:
            store.record_credential_result(cid, name, "success")
            events.emit("login_completed", {"computer_id": cid, "credential": name,
                                            "status": "success"})
        log.info("captcha_auto=ok")
        return {"status": "success", "captcha_auto": True}
    except Exception as e:
        log.info("captcha_auto=fail reason=%s", type(e).__name__)
        if captcha_id:
            captcha.report(captcha_id)
        return None


def _route_blocker(row, blocker):
    """Reuse one live child per auth attempt; persist standalone dedupe by fingerprint."""
    cid = row["id"]
    active = store.get_active_auth_attempt(cid)
    if active:
        current_id = active["current_handoff_id"]
        if current_id:
            current = store.get_handoff(current_id)
            if current and current["status"] in ("pending", "validating"):
                return current_id
        status = active["status"] if "status" in active.keys() else None
        if status == "proving":
            return None
        pub = auth_attempts.raise_challenge(
            active["id"], blocker["kind"], blocker["prompt"],
            domain=blocker.get("domain"),
            challenge_fingerprint=blocker["fingerprint"])
        return pub["current_handoff_id"]

    existing = store.get_open_handoff_by_fingerprint(cid, blocker["fingerprint"])
    if existing:
        return existing["id"]
    handoff = handoffs.create_handoff(
        row, blocker["kind"], blocker["prompt"], screenshot=screenshot_b64(row),
        domain=blocker.get("domain"),
        challenge_fingerprint=blocker["fingerprint"])
    return handoff["id"]
