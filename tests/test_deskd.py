# SPDX-License-Identifier: MIT
"""Unit tests for deskd's pure, security-critical functions — previously reachable
only through a live browser login. Run: .venv/bin/python tests/test_deskd.py

`websocket` lives only in the container's deskd venv, so we stub it to import the
module on the host; the functions under test don't use it.
"""
import base64
import os
import sys
import types
import unittest.mock as mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "image"))
os.environ.setdefault("DESK_TOKEN", "test")
sys.modules.setdefault("websocket", types.ModuleType("websocket"))
import deskd  # noqa: E402


def test_glide_points_end_on_target_and_ease():
    pts = deskd._glide_points(0, 0, 600, 400)
    assert pts[-1] == (600, 400)
    assert 6 <= len(pts) <= 12
    # ease-out: first hop covers more ground than the last
    d = lambda p, q: ((p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2) ** 0.5
    assert d((0, 0), pts[0]) > d(pts[-2], pts[-1])
    # monotonic approach — never overshoots past the target
    dists = [d(p, (600, 400)) for p in pts]
    assert dists == sorted(dists, reverse=True)


def test_glide_points_short_hop_is_single_move():
    assert deskd._glide_points(100, 100, 110, 130) == [(110, 130)]


# ---- VIS: X keeps an opacity:0 password input on the email step; offsetParent alone
# treats it as visible and makes login incorrectly one-step. ----

def test_vis_rejects_opacity_zero_and_zero_size():
    assert "opacity" in deskd.VIS
    assert "getBoundingClientRect" in deskd.VIS
    assert "visibility" in deskd.VIS
    # Observe uses its own vis helper; keep the same opacity/rect guards there.
    assert "opacity" in deskd.OBSERVE_AUTH_JS
    assert "getBoundingClientRect" in deskd.OBSERVE_AUTH_JS
    assert "visibility" in deskd.OBSERVE_AUTH_JS


def test_focus_helpers_select_before_insert():
    # select() so Input.insertText replaces rather than concatenating on reused focus.
    assert "select()" in deskd.FOCUS_USER
    assert "select()" in deskd.FOCUS_PASS
    assert "select()" in deskd.FOCUS_CODE


# ---- domain_ok: the "login only on a matching host" invariant (CLAUDE.md security) ----

def test_domain_ok_exact_and_subdomain():
    assert deskd.domain_ok("google.com", ["google.com"])
    assert deskd.domain_ok("mail.google.com", ["google.com"])       # subdomain allowed
    assert deskd.domain_ok("MAIL.GOOGLE.COM", ["google.com"])       # case-insensitive


def test_domain_ok_rejects_lookalikes():
    assert not deskd.domain_ok("evil.com", ["google.com"])
    assert not deskd.domain_ok("nutgoogle.com", ["google.com"])     # not a real subdomain
    assert not deskd.domain_ok("google.com.evil.com", ["google.com"])
    assert not deskd.domain_ok("", ["google.com"])


# ---- totp: RFC 6238 test vector (seed = ascii "12345678901234567890") ----

def test_totp_rfc6238_vector():
    seed = base64.b32encode(b"12345678901234567890").decode()      # GEZDGNBVGY3TQOJQ...
    assert deskd.totp(seed, at=59) == "287082"                     # RFC 6238 appendix B
    assert deskd.totp(seed, at=1111111109) == "081804"


# ---- classify: the challenge/handoff decision tree ----

class FakeTab:
    def __init__(self, text="", href="https://site.com/login", fields=None):
        self._text, self._href, self._fields = text, href, (fields or {})

    def js(self, expr):
        if expr == deskd.PAGE_TEXT:
            return self._text
        if expr == "location.href":
            return self._href
        if expr == deskd.HAS_FIELDS:
            return self._fields
        return None

    def cmd(self, *a, **k):
        return {}


def test_classify_captcha_becomes_challenge():
    r = deskd.classify(FakeTab(text="Please verify you're human with this CAPTCHA"), {"name": "x"})
    assert r["status"] == "challenge" and r["kind"] == "captcha"


def test_classify_otp_without_totp_asks_human():
    r = deskd.classify(FakeTab(text="Enter the 6-digit verification code we sent"), {"name": "x"})
    assert r["status"] == "challenge" and r["kind"] == "otp"


def test_classify_wrong_password_is_failure():
    r = deskd.classify(FakeTab(text="Your password is incorrect, try again",
                               fields={"pass": True}), {"name": "x"})
    assert r["status"] == "failed"


def test_classify_clean_dashboard_is_success():
    r = deskd.classify(FakeTab(text="Welcome to your dashboard",
                               fields={"pass": False, "user": False}), {"name": "x"})
    assert r["status"] == "success"


# `%2F` is a percent-encoded slash, so `%2Fa` reads as "2fa" case-insensitively and every
# OAuth redirect_uri carries one. Matched against the href, an ordinary Google sign-in hop
# classified as an OTP challenge — and with a totp_seed on the credential that meant deskd
# typing a live TOTP code into a page that never asked for one. Match the text, not the URL.
OAUTH_HREF = ("https://accounts.google.com/v3/signin/identifier"
              "?opparams=%253Fredirect_uri%253Dhttps%25253A%25252Fapi.x.com%25252Foauth")


def test_encoded_slash_in_url_is_not_a_2fa_signal():
    assert deskd.RE_OTP.search(OAUTH_HREF) is None
    assert deskd.RE_BLOCK.search(OAUTH_HREF) is None


def test_classify_ignores_the_url_entirely():
    r = deskd.classify(FakeTab(text="Sign in to continue to x.com", href=OAUTH_HREF,
                               fields={"pass": False, "user": True}), {"name": "x"})
    assert r["status"] == "success", r


def test_classify_still_catches_a_real_otp_page():
    r = deskd.classify(FakeTab(text="Two-factor authentication: enter the code",
                               href=OAUTH_HREF), {"name": "x"})
    assert r["status"] == "challenge" and r["kind"] == "otp"


def test_classify_catches_codeentry_before_body_paints():
    r = deskd.classify(
        FakeTab(text="", href="https://www.instagram.com/auth_platform/codeentry/?apc=secret"),
        {"name": "instagram"})
    assert r["status"] == "challenge" and r["kind"] == "otp", r


def test_blocker_signal_is_typed_and_ignores_query_churn():
    first = deskd.blocker_signal(
        "Check your email. Enter the code we sent.",
        "https://www.instagram.com/auth_platform/codeentry/?apc=one")
    second = deskd.blocker_signal(
        "Check your email. Enter the code we sent.",
        "https://www.instagram.com/auth_platform/codeentry/?apc=two")
    assert first["kind"] == "otp", first
    assert first["fingerprint"] == second["fingerprint"], (first, second)


def test_generic_page_blocker_requires_manual_verification():
    signal = deskd.blocker_signal(
        "Suspicious login activity detected.",
        "https://site.com/account")
    assert signal["kind"] == "device", signal


# Outreach DMs that *mention* 2FA/captcha must not trip the background blocker watchdog.
# Rehearsal 2026-08-08: 21 emails while Case DMed "asking about 2fa and captcha flows".
X_OUTREACH = ("hey, you were the one asking about 2fa and captcha flows in browser "
              "automation back in december")
X_PITCH = ("log into any 2fa'd account, watch the handoff hit your phone, poke around")


def test_watchdog_ignores_2fa_mentioned_in_dm_copy():
    assert deskd.RE_BLOCK.search(X_OUTREACH) is None
    assert deskd.RE_BLOCK.search(X_PITCH) is None
    assert deskd.RE_BLOCK.search(
        "regulated portal logins and 2fa handoffs sound like the daily grind") is None
    assert deskd.blocker_signal(X_OUTREACH, "https://x.com/messages") is None


def test_watchdog_still_catches_real_challenge_phrasing():
    assert deskd.RE_BLOCK.search("Two-factor authentication required")
    assert deskd.RE_BLOCK.search("Enter the code we sent you")
    assert deskd.RE_BLOCK.search("unusual activity on your account")
    assert deskd.RE_BLOCK.search("Please complete the captcha to continue")
    assert deskd.RE_BLOCK.search("verify you're human")


def test_question_challenge_is_never_injected_into_page():
    tab = FakeTab()
    with mock.patch.object(deskd, "fill") as fill, \
         mock.patch.object(deskd, "press_enter") as press, \
         mock.patch.object(deskd, "settle"):
        reason = deskd.apply_challenge_action(tab, "question", "private answer")
    assert reason == "unknown challenge kind 'question'", reason
    fill.assert_not_called()
    press.assert_not_called()


# ---- challenge_signals_from_text: generic tags for durable-auth observations ----

def test_challenge_signals_captcha_otp_approval():
    assert deskd.challenge_signals_from_text(
        "Please verify you're human with this CAPTCHA") == ["captcha"]
    assert deskd.challenge_signals_from_text(
        "Enter the 6-digit verification code we sent") == ["otp"]
    assert deskd.challenge_signals_from_text(
        "Approve this sign-in on your phone") == ["approval"]


def test_challenge_signals_email_verify_and_passkey():
    assert deskd.challenge_signals_from_text(
        "Verify your email — we sent a link") == ["email_verify"]
    assert deskd.challenge_signals_from_text(
        "Use a passkey or security key to continue") == ["passkey"]


def test_challenge_signals_multiple_and_stable_order():
    text = ("CAPTCHA required. Then enter the verification code. "
            "Or approve this on your device.")
    assert deskd.challenge_signals_from_text(text) == ["captcha", "otp", "approval"]


def test_challenge_signals_frame_markers_imply_captcha():
    assert deskd.challenge_signals_from_text(
        "Continue", {"recaptcha": True}) == ["captcha"]
    assert deskd.challenge_signals_from_text(
        "Enter the code", {"hcaptcha": True}) == ["captcha", "otp"]


def test_challenge_signals_ignores_site_names_without_phrases():
    # No website-name branching — bare product names alone are not signals.
    assert deskd.challenge_signals_from_text("Welcome back") == []


# ---- wait_login_fields: SPA shells report readyState before React mounts inputs ----

class DelayedFieldsTab:
    """First N HAS_FIELDS polls return empty; later polls return visible fields."""

    def __init__(self, empty_polls=2, fields=None):
        self._n = 0
        self._empty_polls = empty_polls
        self._fields = fields or {"user": True, "pass": True}

    def js(self, expr):
        if expr == deskd.HAS_FIELDS:
            self._n += 1
            if self._n <= self._empty_polls:
                return {"user": False, "pass": False}
            return self._fields
        return None


def test_wait_login_fields_polls_until_visible():
    tab = DelayedFieldsTab(empty_polls=2)
    fields = deskd.wait_login_fields(tab, timeout=2.0)
    assert fields == {"user": True, "pass": True}, fields
    assert tab._n >= 3


def test_wait_login_fields_times_out_empty():
    tab = DelayedFieldsTab(empty_polls=10_000, fields={"user": False, "pass": False})
    fields = deskd.wait_login_fields(tab, timeout=0.7)
    assert fields == {"user": False, "pass": False}, fields
    assert tab._n >= 2


class TwoStepNoPasswordTab:
    """Username step that never surfaces a password field; the constructor describes
    what the page shows once the identifier has been submitted."""

    def __init__(self, text="", href="https://site.com/login", user_after=True):
        self._text, self._href, self._user_after = text, href, user_after
        self.submitted = False

    def js(self, expr):
        if expr == deskd.HAS_FIELDS:
            user = self._user_after if self.submitted else True
            return {"user": user, "pass": False}
        if expr == deskd.PAGE_TEXT:
            return self._text if self.submitted else "Sign in"
        if expr == "location.href":
            return self._href
        return None

    def cmd(self, *a, **k):
        return {}


def _run_fill_login_form(tab):
    cred = {"username": "ava", "secret": "s3cret", "name": "x"}
    with mock.patch.object(deskd, "fill") as fill, \
         mock.patch.object(deskd, "press_enter") as press, \
         mock.patch.object(deskd, "settle"):
        press.side_effect = lambda *_a, **_k: setattr(tab, "submitted", True)
        reason = deskd.fill_login_form(tab, cred)
    return reason, fill, press


def test_fill_login_form_falls_through_to_an_otp_wall():
    # Email/SMS OTP: no password field ever appears, but the page did move on.
    tab = TwoStepNoPasswordTab(text="Enter the code we sent to your email")
    reason, fill, press = _run_fill_login_form(tab)
    assert reason is None, reason
    fill.assert_called_once_with(tab, deskd.FOCUS_USER, "ava")   # password never typed
    press.assert_called_once()


def test_fill_login_form_falls_through_when_user_gone_and_challenge_present():
    # SPA unmounts the username field before painting the OTP wall — still safe
    # because challenge phrasing (or a known path) is positive evidence.
    tab = TwoStepNoPasswordTab(
        text="Enter the code we sent to your email",
        href="https://www.instagram.com/auth_platform/codeentry/?apc=x",
        user_after=False)
    reason, _, _ = _run_fill_login_form(tab)
    assert reason is None, reason


def test_fill_login_form_fails_on_spa_gap_with_no_challenge():
    # Username unmounted, password not yet mounted, interim copy only. Treating
    # "user gone" alone as progress made classify() report a false success.
    tab = TwoStepNoPasswordTab(text="Welcome back", user_after=False)
    reason, _, _ = _run_fill_login_form(tab)
    assert reason == "password field never appeared", reason


def test_fill_login_form_fails_when_identifier_step_never_moved():
    # Nothing advanced: still the same username box, no challenge in sight. Failing
    # loudly beats classify() reading an unfinished login as success.
    tab = TwoStepNoPasswordTab(text="Sign in to continue")
    reason, _, _ = _run_fill_login_form(tab)
    assert reason == "password field never appeared", reason


def test_finalize_auth_observation_caps_page_state_and_signals():
    raw = {
        "href": "https://example.com/login",
        "ready": True,
        "title": "Sign in",
        "visible_fields": {"user": True, "pass": True, "code": False},
        "frame_markers": {"recaptcha": False, "hcaptcha": False, "arkose": False,
                          "turnstile": False, "generic_captcha": False},
        "challenge_signals": ["stale"],
        "page_state": "x" * 600 + " enter the verification code",
    }
    obs = deskd.finalize_auth_observation(raw)
    assert len(obs["page_state"]) == 500
    assert obs["challenge_signals"] == []  # truncated slice lost the phrase
    raw["page_state"] = ("Please enter the verification code now. " + "y" * 400)
    obs = deskd.finalize_auth_observation(raw)
    assert "otp" in obs["challenge_signals"]
    assert obs["visible_fields"] == {"user": True, "pass": True, "code": False}


class ObserveFakeTab:
    """Tab stub whose js() returns a canned OBSERVE_AUTH_JS-shaped dict."""

    def __init__(self, observation):
        self._obs = observation

    def js(self, expr):
        if expr == deskd.OBSERVE_AUTH_JS:
            return self._obs
        return None

    def cmd(self, *a, **k):
        return {}


def test_finalize_matches_observe_pipeline_shape():
    raw = ObserveFakeTab({
        "href": "https://accounts.example/challenge",
        "ready": True,
        "title": "Check your device",
        "visible_fields": {"user": False, "pass": False, "code": False},
        "frame_markers": {"recaptcha": False, "hcaptcha": False, "arkose": True,
                          "turnstile": False, "generic_captcha": False},
        "challenge_signals": [],
        "page_state": "Approve this sign-in on your phone",
    }).js(deskd.OBSERVE_AUTH_JS)
    obs = deskd.finalize_auth_observation(raw)
    assert obs["challenge_signals"] == ["captcha", "approval"]
    assert obs["frame_markers"]["arkose"] is True
    assert set(obs.keys()) == {
        "href", "ready", "title", "visible_fields", "frame_markers",
        "challenge_signals", "page_state",
    }


# ---- capture_step: the network-wiretap fold (two-phase CDP: headers→finish→body) ----

import re  # noqa: E402
from collections import deque  # noqa: E402


def _resp(rid, url, status=200):
    return {"method": "Network.responseReceived",
            "params": {"requestId": rid, "response": {"url": url, "status": status}}}


def _finished(rid):
    return {"method": "Network.loadingFinished", "params": {"requestId": rid}}


def test_capture_step_body_fetched_only_after_loadingFinished():
    want, pending, buf = {}, {}, deque()
    # responseReceived (headers) must NOT fetch the body — it may still be streaming
    assert deskd.capture_step(_resp("r1", "https://x.com/graphql/SearchTimeline"),
                              re.compile("SearchTimeline"), want, pending, buf) == []
    assert "r1" in want                                    # remembered, awaiting finish
    # loadingFinished → now emit getResponseBody for that request
    cmds = deskd.capture_step(_finished("r1"), re.compile("SearchTimeline"), want, pending, buf)
    assert len(cmds) == 1 and cmds[0][0] == "Network.getResponseBody"
    assert cmds[0][1]["requestId"] == "r1" and cmds[0][2]["url"].endswith("SearchTimeline")
    assert "r1" not in want


def test_capture_step_ignores_nonmatching_url():
    want, pending, buf = {}, {}, deque()
    deskd.capture_step(_resp("r2", "https://x.com/home/UnrelatedCall"),
                       re.compile("SearchTimeline"), want, pending, buf)
    assert not want                                        # never tracked
    assert deskd.capture_step(_finished("r2"), re.compile("SearchTimeline"),
                              want, pending, buf) == []    # finish for untracked id → nothing


def test_capture_step_body_reply_lands_in_buf():
    want, pending, buf = {}, {77: {"url": "u", "status": 200}}, deque()
    reply = {"id": 77, "result": {"body": '{"data":1}', "base64Encoded": False}}
    assert deskd.capture_step(reply, re.compile("x"), want, pending, buf) == []
    assert len(buf) == 1 and buf[0]["body"] == '{"data":1}' and buf[0]["truncated"] is False
    assert 77 not in pending                               # consumed


def test_capture_step_decodes_base64_body():
    want, pending, buf = {}, {5: {"url": "u", "status": 200}}, deque()
    b64 = base64.b64encode(b"hello").decode()
    deskd.capture_step({"id": 5, "result": {"body": b64, "base64Encoded": True}},
                       re.compile("x"), want, pending, buf)
    assert buf[0]["body"] == "hello"


def test_capture_step_truncates_at_cap():
    want, pending, buf = {}, {9: {"url": "u", "status": 200}}, deque()
    big = "z" * (deskd.CAPTURE_CAP + 100)
    deskd.capture_step({"id": 9, "result": {"body": big, "base64Encoded": False}},
                       re.compile("x"), want, pending, buf)
    assert len(buf[0]["body"]) == deskd.CAPTURE_CAP and buf[0]["truncated"] is True


def test_capture_step_loadingFailed_is_visible_not_dropped():
    want, pending, buf = {"r3": {"url": "u", "status": 0}}, {}, deque()
    fail = {"method": "Network.loadingFailed",
            "params": {"requestId": "r3", "errorText": "net::ERR_ABORTED"}}
    deskd.capture_step(fail, re.compile("x"), want, pending, buf)
    assert buf[0]["error"] == "net::ERR_ABORTED" and "r3" not in want   # surfaced, not silent


def test_capture_step_getResponseBody_error_is_visible():
    want, pending, buf = {}, {4: {"url": "u", "status": 200}}, deque()
    reply = {"id": 4, "error": {"message": "No data found for resource"}}
    deskd.capture_step(reply, re.compile("x"), want, pending, buf)
    assert buf[0]["error"] == "No data found for resource" and "body" not in buf[0]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("PASS")
