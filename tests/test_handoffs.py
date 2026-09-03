# SPDX-License-Identifier: MIT
"""Typed handoff state + verified continuation (restart recovery + Assist foundation).
Run: .venv/bin/python tests/test_handoffs.py"""
import os
import sys
import unittest.mock as mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "control-plane"))
# assignment, NOT setdefault: the tests below write handoff rows, and an inherited
# CASE_HOME would put them in a live box's DB. Same reasoning as tests/test_links.py.
os.environ["CASE_HOME"] = "/tmp/case-handoffs-test"
import handoffs  # noqa: E402
import links  # noqa: E402
from errors import ApiError  # noqa: E402
from store import store  # noqa: E402

IDS = ("h_l3", "h_plain", "h_otp", "h_cap", "h_seq1", "h_seq2", "h_leg", "h_val")
# fixed ids: must not survive the test, or the next run hits UNIQUE.

# create_handoff reaches notifier.notify(..., computer_row["name"]) at handoffs.py:46,
# so the row stub needs a name or it dies with KeyError before asserting anything.
ROW = {"id": "c_1", "name": "ava", "state": "running"}

# Ntfy.notify is a no-op with CASE_NTFY_TOPIC unset, but that is an env accident, not a
# guarantee — stub it so these tests can never publish a real handoff to a real phone.
handoffs.notifier = type("N", (), {"notify": lambda self, h, name: None})()

# WP-B store helpers, mocked until that branch merges: approvals sign their ntfy
# answer URL, and expire_stale reaps abandoned attempts.
store.sign = lambda text: "sig-" + text
store.stale_active_auth_attempts = lambda cutoff: []


def _cleanup(*ids):
    for hid in ids or IDS:
        store.delete_handoff(hid)
    handoffs.LOGIN_CTX.clear()


def _mk(*a, **kw):
    """create_handoff, then drop the row and its LOGIN_CTX entry — these tests assert on
    the returned shape, not on persistence, and must not leave state for the next run."""
    h = handoffs.create_handoff(*a, **kw)
    store.delete_handoff(h["id"])
    store.q("DELETE FROM assist_tokens")
    handoffs.LOGIN_CTX.pop(h["id"], None)
    return h


def _persist(hid, kind, prompt, login_credential=None, domain=None, **kw):
    store.delete_handoff(hid)
    store.insert_handoff(hid, "c_1", kind, prompt, None, login_credential, domain, **kw)
    if login_credential:
        handoffs.LOGIN_CTX[hid] = {"computer_id": "c_1", "credential": login_credential}
    return store.get_handoff(hid)


def test_only_approvals_carry_a_signed_answer_url():
    _cleanup()
    seen = []
    handoffs.notifier = type("N", (), {"notify": lambda self, h, name: seen.append(h)})()
    try:
        _mk(ROW, "approval", "ok?")
        _mk(ROW, "question", "who?")
        assert seen[0]["answer_url"].endswith(f"/answer/{seen[0]['id']}/sig-answer:{seen[0]['id']}")
        assert seen[1]["answer_url"] == ""
    finally:
        handoffs.notifier = type("N", (), {"notify": lambda self, h, name: None})()
        _cleanup()


def test_expire_stale_fails_abandoned_auth_attempts():
    # store.stale_active_auth_attempts lands with WP-B; the reaper loop is what's under test.
    import auth_attempts
    _cleanup()
    store.q("DELETE FROM auth_attempts WHERE computer_id='c_stale'")
    try:
        a = auth_attempts.start_attempt("c_stale", "github", "https://example.com/login")
        with mock.patch.object(store, "stale_active_auth_attempts",
                               return_value=[{"id": a["id"]}], create=True):
            handoffs.expire_stale()
        assert auth_attempts.get_attempt(a["id"])["status"] == "failed"
    finally:
        store.q("DELETE FROM auth_attempts WHERE computer_id='c_stale'")


def test_rebuild_login_ctx_recovers_pending_login_handoff():
    # a login handoff persisted before a (simulated) restart, with the in-memory map wiped
    _cleanup()
    try:
        store.insert_handoff("h_l3", "c_x", "otp", "enter code", None, "mycred")
        store.insert_handoff("h_plain", "c_x", "question", "not a login", None, None)  # ignored
        handoffs.LOGIN_CTX.clear()

        handoffs.rebuild_login_ctx()

        assert handoffs.LOGIN_CTX == {"h_l3": {"computer_id": "c_x", "credential": "mycred"}}, \
            handoffs.LOGIN_CTX
    finally:
        _cleanup()


def test_rebuild_login_ctx_recovers_validating_login_handoff():
    _cleanup()
    try:
        store.insert_handoff("h_val", "c_x", "otp", "enter code", None, "mycred",
                             continuation="submit_value")
        store.set_handoff_status("h_val", "validating", answer="123456")
        handoffs.LOGIN_CTX.clear()

        handoffs.rebuild_login_ctx()

        assert "h_val" in handoffs.LOGIN_CTX, handoffs.LOGIN_CTX
        assert handoffs.LOGIN_CTX["h_val"]["credential"] == "mycred"
        # interrupted validation must not stay stuck validating forever
        assert store.get_handoff("h_val")["status"] == "pending"
        assert store.get_handoff("h_val")["answer"] is None
    finally:
        _cleanup()


def test_handoff_json_exposes_domain_and_still_hides_login_credential():
    h = _mk(ROW, "otp", "secure.chase.com: enter the code",
            login_credential="chase.com", domain="secure.chase.com")
    # the strip's headline is "hit a code at chase.com" — that host has to be a field,
    # not a substring of whatever prose deskd happened to format into `prompt`
    assert h["domain"] == "secure.chase.com", h
    assert "login_credential" not in h, h


def test_handoff_without_a_domain_is_still_valid():
    h = _mk(ROW, "approval", "ok to send this email?")
    assert h["domain"] is None, h


def test_a_login_handoff_gets_its_domain_from_the_url_the_caller_passed():
    # cased.login already has body["url"]; this is the seam that must not be lost
    assert links.normalize_domain("https://secure.chase.com/auth?next=/x") == "secure.chase.com"
    h = _mk(ROW, "otp", "…", domain=links.normalize_domain("https://secure.chase.com/auth"))
    assert h["domain"] == "secure.chase.com", h


def test_create_sets_continuation_by_kind():
    otp = _mk(ROW, "otp", "code")
    assert otp["continuation"] == "submit_value", otp
    assert otp["status"] == "pending", otp

    cap = _mk(ROW, "captcha", "solve me")
    assert cap["continuation"] == "verify_page", cap

    device = _mk(ROW, "device", "approve on phone")
    assert device["continuation"] == "verify_page", device

    q = _mk(ROW, "question", "which account?")
    assert q["continuation"] == "submit_value", q

    appr = _mk(ROW, "approval", "ok?")
    assert appr["continuation"] == "submit_value", appr


def test_legacy_answered_status_reads_as_completed():
    _cleanup("h_leg")
    try:
        store.insert_handoff("h_leg", "c_1", "otp", "code", None, None,
                             continuation="submit_value")
        store.set_handoff_status("h_leg", "answered", answer="999999")
        j = handoffs.get_handoff("h_leg")
        assert j["status"] == "completed", j
    finally:
        _cleanup("h_leg")


def test_otp_submit_without_login_ctx_completes():
    _cleanup("h_otp")
    try:
        _persist("h_otp", "otp", "enter code", continuation="submit_value")
        handoffs.LOGIN_CTX.pop("h_otp", None)
        events = []
        with mock.patch.object(handoffs, "emit", side_effect=lambda *a, **k: events.append(a)):
            row = handoffs.submit_handoff_value("h_otp", "123456")
        assert row["status"] == "completed", row
        assert row["answer"] is None, row  # OTP codes never returned
        assert store.get_handoff("h_otp")["answer"] is None  # nor persisted
        assert any(e[0] == "handoff_answered" and e[1].get("verified") is True
                   and e[1].get("value_present") is True for e in events), events
    finally:
        _cleanup("h_otp")


def test_otp_submit_resume_success_completes_and_pops_ctx():
    _cleanup("h_otp")
    try:
        _persist("h_otp", "otp", "enter code", login_credential="chase",
                 continuation="submit_value")
        events = []
        with mock.patch.object(handoffs, "get_computer", return_value=ROW), \
             mock.patch.object(handoffs, "desk_json",
                               return_value={"status": "success"}) as desk, \
             mock.patch.object(handoffs, "emit", side_effect=lambda *a, **k: events.append(a)), \
             mock.patch.object(store, "record_credential_result") as rec:
            row = handoffs.submit_handoff_value("h_otp", "123456")
        assert row["status"] == "completed", row
        assert "h_otp" not in handoffs.LOGIN_CTX
        desk.assert_called_once()
        assert desk.call_args.args[2] == "/login/resume"
        assert desk.call_args.kwargs["json"] == {"value": "123456"}
        rec.assert_called_once_with("c_1", "chase", "success")
        assert any(e[0] == "login_completed" and e[1]["status"] == "success" for e in events), events
    finally:
        _cleanup("h_otp")


def test_otp_submit_resume_fail_stays_pending_keeps_ctx():
    _cleanup("h_otp")
    try:
        _persist("h_otp", "otp", "enter code", login_credential="chase",
                 continuation="submit_value")
        with mock.patch.object(handoffs, "get_computer", return_value=ROW), \
             mock.patch.object(handoffs, "desk_json",
                               return_value={"status": "failed", "reason": "bad code"}), \
             mock.patch.object(store, "record_credential_result") as rec:
            row = handoffs.submit_handoff_value("h_otp", "000000")
        assert row["status"] == "pending", row
        assert "h_otp" in handoffs.LOGIN_CTX, handoffs.LOGIN_CTX
        rec.assert_not_called()
    finally:
        _cleanup("h_otp")


def test_captcha_verify_fail_stays_pending_no_login_success():
    _cleanup("h_cap")
    try:
        _persist("h_cap", "captcha", "solve", login_credential="chase",
                 continuation="verify_page", challenge_fingerprint="recaptcha:abc")
        # Phrase must match captcha.RE_VERIFY (e.g. "you're" / "captcha" / "not a robot").
        verify = {"value": {"text": "Please verify you're human", "hasPassword": False, "href": "x"}}
        with mock.patch.object(handoffs, "get_computer", return_value=ROW), \
             mock.patch.object(handoffs, "eval_js", return_value=verify), \
             mock.patch.object(handoffs, "desk_json") as desk, \
             mock.patch.object(store, "record_credential_result") as rec:
            row = handoffs.verify_handoff_page("h_cap")
        assert row["status"] == "pending", row
        assert row["answer"] is None, row
        assert "h_cap" in handoffs.LOGIN_CTX
        desk.assert_not_called()
        rec.assert_not_called()
    finally:
        _cleanup("h_cap")


def test_captcha_verify_success_without_login_completes():
    _cleanup("h_cap")
    try:
        _persist("h_cap", "captcha", "solve", continuation="verify_page")
        handoffs.LOGIN_CTX.pop("h_cap", None)
        verify = {"value": {"text": "Welcome home", "hasPassword": False, "href": "/feed"}}
        gate = {"value": {"gated": False}}
        events = []

        def fake_eval(row, expression, timeout_s=20):
            if "gated" in expression or "checkpoint" in expression:
                return gate
            return verify

        with mock.patch.object(handoffs, "get_computer", return_value=ROW), \
             mock.patch.object(handoffs, "eval_js", side_effect=fake_eval), \
             mock.patch.object(handoffs, "emit", side_effect=lambda *a, **k: events.append(a)):
            row = handoffs.verify_handoff_page("h_cap")
        assert row["status"] == "completed", row
        assert any(e[0] == "handoff_answered" and e[1].get("verified") is True for e in events), events
    finally:
        _cleanup("h_cap")


def test_captcha_verify_success_with_login_resumes_approve():
    _cleanup("h_cap")
    try:
        _persist("h_cap", "captcha", "solve", login_credential="chase",
                 continuation="verify_page")
        verify = {"value": {"text": "Welcome", "hasPassword": False, "href": "/in"}}
        gate = {"value": {"gated": False}}

        def fake_eval(row, expression, timeout_s=20):
            if "gated" in expression or "checkpoint" in expression:
                return gate
            return verify

        with mock.patch.object(handoffs, "get_computer", return_value=ROW), \
             mock.patch.object(handoffs, "eval_js", side_effect=fake_eval), \
             mock.patch.object(handoffs, "desk_json",
                               return_value={"status": "success"}) as desk, \
             mock.patch.object(store, "record_credential_result") as rec:
            row = handoffs.verify_handoff_page("h_cap")
        assert row["status"] == "completed", row
        assert "h_cap" not in handoffs.LOGIN_CTX
        assert desk.call_args.kwargs["json"] == {"value": "approve"}
        rec.assert_called_once_with("c_1", "chase", "success")
    finally:
        _cleanup("h_cap")


def test_answer_handoff_routes_submit_and_verify():
    _cleanup("h_otp", "h_cap")
    try:
        _persist("h_otp", "otp", "code", continuation="submit_value")
        handoffs.LOGIN_CTX.pop("h_otp", None)
        row = handoffs.answer_handoff("h_otp", "111111")
        assert row["status"] == "completed", row

        _persist("h_cap", "captcha", "solve", continuation="verify_page")
        handoffs.LOGIN_CTX.pop("h_cap", None)
        verify = {"value": {"text": "ok", "hasPassword": False, "href": "/"}}
        gate = {"value": {"gated": False}}

        def fake_eval(row, expression, timeout_s=20):
            if "gated" in expression or "checkpoint" in expression:
                return gate
            return verify

        with mock.patch.object(handoffs, "get_computer", return_value=ROW), \
             mock.patch.object(handoffs, "eval_js", side_effect=fake_eval):
            row = handoffs.answer_handoff("h_cap", "done")
        assert row["status"] == "completed", row

        _persist("h_cap", "captcha", "solve", continuation="verify_page")
        try:
            handoffs.answer_handoff("h_cap", "not-a-done-token")
            assert False, "expected 400"
        except ApiError as e:
            assert e.status == 400, e
    finally:
        _cleanup("h_otp", "h_cap")


def test_approval_deny_is_terminal_failed():
    _cleanup("h_otp")
    try:
        _persist("h_otp", "approval", "ok to send?", login_credential="chase",
                 continuation="submit_value")
        with mock.patch.object(handoffs, "get_computer", return_value=ROW), \
             mock.patch.object(handoffs, "desk_json",
                               return_value={"status": "failed", "reason": "user denied"}), \
             mock.patch.object(store, "record_credential_result") as rec:
            row = handoffs.submit_handoff_value("h_otp", "deny")
        assert row["status"] == "failed", row
        assert row["answer"] == "deny", row
        assert "h_otp" not in handoffs.LOGIN_CTX
        rec.assert_called_once_with("c_1", "chase", "failed")
    finally:
        _cleanup("h_otp")


def test_approval_approve_persists_approve_not_done():
    """'approve' is also a verify_page synonym — must not collapse approval answers."""
    _cleanup("h_plain")
    try:
        _persist("h_plain", "approval", "Ship it?", continuation="submit_value")
        with mock.patch.object(handoffs, "get_computer", return_value=ROW):
            row = handoffs.answer_handoff("h_plain", "approve")
        assert row["status"] == "completed", row
        assert row["answer"] == "approve", row
    finally:
        _cleanup("h_plain")


def test_wait_external_rejects_answer_handoff():
    _cleanup("h_otp")
    try:
        _persist("h_otp", "device", "approve on phone", continuation="wait_external")
        try:
            handoffs.answer_handoff("h_otp", "done")
            assert False, "expected 400"
        except ApiError as e:
            assert e.status == 400, e
            assert "wait_external" in str(e), e
    finally:
        _cleanup("h_otp")


def test_request_handoff_allows_desk_kinds():
    with mock.patch.object(handoffs, "get_computer", return_value=ROW), \
         mock.patch.object(handoffs, "screenshot_b64", return_value=None), \
         mock.patch.object(handoffs, "create_handoff",
                           return_value={"id": "h_dev", "kind": "device",
                                         "continuation": "verify_page"}) as create:
        out = handoffs.request_handoff("c_1", "device", "scan QR")
    assert out["kind"] == "device", out
    create.assert_called_once()
    assert create.call_args.args[1] == "device"


def test_request_handoff_rejects_unknown_kind():
    with mock.patch.object(handoffs, "get_computer", return_value=ROW):
        try:
            handoffs.request_handoff("c_1", "otp", "enter code")
            assert False, "expected 400"
        except ApiError as e:
            assert e.status == 400, e
            assert "kind must be" in str(e), e


def test_expire_stale_clears_login_ctx():
    _cleanup("h_otp")
    try:
        _persist("h_otp", "otp", "code", login_credential="chase",
                 continuation="submit_value")
        # force created_at into the past
        store.q("UPDATE handoffs SET created_at=? WHERE id=?",
                ("2000-01-01T00:00:00Z", "h_otp"))
        events = []
        with mock.patch.object(handoffs, "emit", side_effect=lambda *a, **k: events.append(a)), \
             mock.patch.object(store, "record_credential_result") as rec:
            handoffs.expire_stale()
        assert store.get_handoff("h_otp")["status"] == "expired"
        assert "h_otp" not in handoffs.LOGIN_CTX
        rec.assert_called_once_with("c_1", "chase", "failed")
        assert any(e[0] == "login_completed" and e[1]["status"] == "failed" for e in events), events
    finally:
        _cleanup("h_otp")


def test_sequential_captcha_then_otp():
    """Complete a captcha handoff, then a fresh otp handoff can still resume login."""
    _cleanup("h_seq1", "h_seq2")
    try:
        _persist("h_seq1", "captcha", "solve", login_credential="chase",
                 continuation="verify_page")
        verify = {"value": {"text": "next", "hasPassword": False, "href": "/otp"}}
        gate = {"value": {"gated": False}}

        def fake_eval(row, expression, timeout_s=20):
            if "gated" in expression or "checkpoint" in expression:
                return gate
            return verify

        with mock.patch.object(handoffs, "get_computer", return_value=ROW), \
             mock.patch.object(handoffs, "eval_js", side_effect=fake_eval), \
             mock.patch.object(handoffs, "desk_json", return_value={"status": "success"}), \
             mock.patch.object(store, "record_credential_result"):
            row = handoffs.verify_handoff_page("h_seq1")
        assert row["status"] == "completed", row
        assert "h_seq1" not in handoffs.LOGIN_CTX

        # subsequent OTP challenge (new handoff, same credential)
        _persist("h_seq2", "otp", "enter code", login_credential="chase",
                 continuation="submit_value")
        with mock.patch.object(handoffs, "get_computer", return_value=ROW), \
             mock.patch.object(handoffs, "desk_json", return_value={"status": "success"}) as desk, \
             mock.patch.object(store, "record_credential_result") as rec:
            row = handoffs.submit_handoff_value("h_seq2", "654321")
        assert row["status"] == "completed", row
        assert desk.call_args.kwargs["json"] == {"value": "654321"}
        rec.assert_called_with("c_1", "chase", "success")
        assert "h_seq2" not in handoffs.LOGIN_CTX
    finally:
        _cleanup("h_seq1", "h_seq2")


def test_attempt_child_completion_advances_without_credential_ok():
    """Login handoff with attempt_id: complete child → advance; no vault success yet."""
    _cleanup("h_att")
    try:
        store.q("DELETE FROM auth_attempts WHERE id=?", ("a_child",))
        store.insert_auth_attempt(
            "a_child", "c_1", "chase", "https://example.com/login",
            proof_spec={"url_contains": "/home"})
        store.cas_auth_attempt_status("a_child", "created", "awaiting_human", 0)
        _persist("h_att", "otp", "enter code", login_credential="chase",
                 continuation="submit_value", attempt_id="a_child", sequence=1)
        with mock.patch.object(handoffs, "get_computer", return_value=ROW), \
             mock.patch.object(handoffs, "auth_submit_challenge",
                               return_value={"ok": True}), \
             mock.patch.object(store, "record_credential_result") as rec, \
             mock.patch("auth_attempts.advance_attempt",
                        return_value={"id": "a_child", "status": "advancing"}) as adv:
            row = handoffs.submit_handoff_value("h_att", "111111")
        assert row["status"] == "completed", row
        adv.assert_called_once_with("a_child")
        for call in rec.call_args_list:
            assert call.args[2] != "success", call
    finally:
        store.q("DELETE FROM auth_attempts WHERE id=?", ("a_child",))
        _cleanup("h_att")


def test_transition_handoff_bumps_revision_and_guards_terminals():
    _cleanup("h_tr")
    try:
        _persist("h_tr", "otp", "code?")
        r0 = store.get_handoff("h_tr")
        assert int(r0["revision"] or 0) == 0
        # Every guarded write bumps revision — the Assist poll fingerprint depends on it.
        r1 = store.transition_handoff("h_tr", "validating", answer=None)
        assert r1["status"] == "validating" and r1["revision"] == 1, dict(r1)
        r2 = store.transition_handoff("h_tr", "pending", answer=None)   # soft-fail retry
        assert r2["status"] == "pending" and r2["revision"] == 2, dict(r2)
        r3 = store.transition_handoff("h_tr", "expired")
        assert r3["status"] == "expired" and r3["revision"] == 3
        # Terminal is terminal: no revival, no double-expire.
        assert store.transition_handoff("h_tr", "pending", answer=None) is None
        assert store.transition_handoff("h_tr", "completed") is None
        assert store.get_handoff("h_tr")["status"] == "expired"
    finally:
        _cleanup("h_tr")


def test_expire_stale_loses_race_to_a_finished_answer():
    # Sweeper reads a stale open row, but the answer path completes it first:
    # the expiry write must lose and never fire failure side effects.
    _cleanup("h_race")
    try:
        _persist("h_race", "otp", "code?", login_credential="mycred")
        store.q("UPDATE handoffs SET created_at=? WHERE id=?", ("2000-01-01T00:00:00Z", "h_race"))
        stale = [store.get_handoff("h_race")]
        with mock.patch.object(store, "stale_pending_handoffs", return_value=stale), \
             mock.patch.object(store, "record_credential_result") as rec:
            store.transition_handoff("h_race", "completed", answer="done")  # answer wins
            handoffs.expire_stale()
        assert store.get_handoff("h_race")["status"] == "completed"
        rec.assert_not_called()
    finally:
        _cleanup("h_race")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("PASS")
