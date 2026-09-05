# SPDX-License-Identifier: MIT
"""Unit tests for control-plane captcha auto-solve (DBC).
Run: .venv/bin/python tests/test_captcha.py

No Docker. Mocks DBC HTTP. Failure-path tests protect the invariant:
a failed / unverified solve must fall through to create_handoff, never success,
and must NOT call /login/resume (deskd state["login"] stays intact for handoff).
"""
import json
import os
import sys
import unittest.mock as mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "control-plane"))
os.environ["CASE_HOME"] = "/tmp/case-captcha-test"

# Clear DBC env for deterministic enabled() tests; individual tests set what they need.
for _k in ("CASE_DBC_USERNAME", "CASE_DBC_PASSWORD", "CASE_DBC_AUTHTOKEN",
           "CASE_DBC_PROXY", "CASE_DBC_PROXYTYPE"):
    os.environ.pop(_k, None)

import captcha  # noqa: E402
from store import store  # noqa: E402


# ---- enabled() ----

def test_enabled_false_without_env():
    assert captcha.enabled() is False


def test_enabled_true_with_username_password():
    os.environ["CASE_DBC_USERNAME"] = "u"
    os.environ["CASE_DBC_PASSWORD"] = "p"
    try:
        assert captcha.enabled() is True
    finally:
        os.environ.pop("CASE_DBC_USERNAME", None)
        os.environ.pop("CASE_DBC_PASSWORD", None)


def test_enabled_true_with_authtoken():
    os.environ["CASE_DBC_AUTHTOKEN"] = "tok_abc"
    try:
        assert captcha.enabled() is True
    finally:
        os.environ.pop("CASE_DBC_AUTHTOKEN", None)


def test_enabled_false_with_username_only():
    os.environ["CASE_DBC_USERNAME"] = "u"
    try:
        assert captcha.enabled() is False
    finally:
        os.environ.pop("CASE_DBC_USERNAME", None)


# ---- still_challenge verify (phrases + password) ----

def test_still_challenge_detects_captcha_phrases():
    assert captcha.still_challenge("Please verify you're human to continue")
    assert captcha.still_challenge("Solve this captcha")
    assert captcha.still_challenge("I'm not a robot checkbox")
    assert not captcha.still_challenge("Welcome to your feed")


def test_still_challenge_rejects_otp_and_block_phrases():
    """RE_BLOCK-style: OTP/2FA/unusual-activity must also fail verify."""
    assert captcha.still_challenge("Enter the verification code we sent you")
    assert captcha.still_challenge("Two-factor authentication required")
    assert captcha.still_challenge("We detected unusual activity on your account")
    assert captcha.still_challenge("Please enter the code from your phone")
    assert captcha.still_challenge("Enable 2FA to continue")


def test_still_challenge_rejects_password_still_present():
    assert captcha.still_challenge("Welcome", has_password=True)
    assert captcha.still_challenge("", has_password=True)
    assert not captcha.still_challenge("Welcome to your feed", has_password=False)


def test_detect_js_iframe_form_consistent_with_inject():
    """LinkedIn rehearsal shape: form/#captcha-challenge lookup must search
    captchaInternal iframe in both DETECT and INJECT (same documents as sitekey)."""
    for blob in (captcha.DETECT_JS, captcha.INJECT_JS_TEMPLATE):
        assert 'captchaInternal' in blob
        assert 'captcha-challenge' in blob
        assert 'challenge/verify' in blob
    # has/call recaptcha callback predicates must match (no lone 'promise' in detect).
    assert "k === 'promise'" not in captcha.DETECT_JS
    assert "/callback/i.test(k)" in captcha.DETECT_JS
    assert "/callback/i.test(k)" in captcha.INJECT_JS_TEMPLATE
    # Form submit requires fields written.
    assert "field_gone" in captcha.INJECT_JS_TEMPLATE
    assert "setRecaptchaFields(token)" in captcha.INJECT_JS_TEMPLATE or \
           "const wrote = setRecaptchaFields(token)" in captcha.INJECT_JS_TEMPLATE


# ---- proxy omit ----

def test_proxy_fields_omits_empty():
    os.environ.pop("CASE_DBC_PROXY", None)
    os.environ.pop("CASE_DBC_PROXYTYPE", None)
    assert captcha._proxy_fields() == {}
    os.environ["CASE_DBC_PROXY"] = ""
    try:
        assert captcha._proxy_fields() == {}
    finally:
        os.environ.pop("CASE_DBC_PROXY", None)


def test_proxy_fields_includes_when_set():
    os.environ["CASE_DBC_PROXY"] = "1.2.3.4:8080"
    os.environ["CASE_DBC_PROXYTYPE"] = "HTTP"
    try:
        assert captcha._proxy_fields() == {"proxy": "1.2.3.4:8080", "proxytype": "HTTP"}
    finally:
        os.environ.pop("CASE_DBC_PROXY", None)
        os.environ.pop("CASE_DBC_PROXYTYPE", None)


def test_solve_omits_empty_proxy_in_params():
    _enable_dbc()
    os.environ.pop("CASE_DBC_PROXY", None)
    try:
        upload = mock.Mock(status_code=200)
        upload.json.return_value = {"captcha": 1, "is_correct": True, "text": "tok"}
        with mock.patch("captcha.requests.post", return_value=upload) as post, \
             mock.patch("captcha.time.sleep"):
            captcha.solve_if_capable("recaptcha", "https://example.com", "k", timeout_s=5)
        params = json.loads(post.call_args.kwargs["data"]["token_params"])
        assert "proxy" not in params
        assert "proxytype" not in params
    finally:
        _clear_dbc()


# ---- capability adapter / solve_if_capable ----

def _enable_dbc():
    os.environ["CASE_DBC_USERNAME"] = "u"
    os.environ["CASE_DBC_PASSWORD"] = "p"


def _clear_dbc():
    for k in ("CASE_DBC_USERNAME", "CASE_DBC_PASSWORD", "CASE_DBC_AUTHTOKEN"):
        os.environ.pop(k, None)


def test_capability_for_maps_declared_families():
    assert captcha.capability_for("recaptcha", enterprise=False) == "recaptcha_v2"
    assert captcha.capability_for("recaptcha", enterprise=True) == "recaptcha_enterprise"
    assert captcha.capability_for("arkose") == "arkose"
    assert captcha.capability_for("hcaptcha") is None
    assert captcha.capability_for("recaptcha_v3") is None
    assert captcha.DBC_CAPABILITIES == frozenset(
        {"recaptcha_v2", "recaptcha_enterprise", "arkose"})


def test_solve_if_capable_unsupported_short_circuits():
    """Unknown family never hits DBC — fail fast into human handoff."""
    _enable_dbc()
    try:
        with mock.patch("captcha.requests.post") as post, \
             mock.patch("captcha.requests.get") as get, \
             mock.patch("captcha.time.sleep") as sleep:
            out = captcha.solve_if_capable("hcaptcha", "https://x", "k", timeout_s=60)
        assert out is None
        post.assert_not_called()
        get.assert_not_called()
        sleep.assert_not_called()
    finally:
        _clear_dbc()


def test_solve_if_capable_enterprise_missing_proxy_no_http():
    _enable_dbc()
    os.environ.pop("CASE_DBC_PROXY", None)
    try:
        with mock.patch("captcha.requests.post") as post, \
             mock.patch("captcha.time.sleep") as sleep:
            out = captcha.solve_if_capable(
                "recaptcha", "https://linkedin.com/checkpoint/challenge", "k",
                enterprise=True, timeout_s=60)
        assert out is None
        post.assert_not_called()
        sleep.assert_not_called()
    finally:
        _clear_dbc()


def test_solve_if_capable_terminal_unsolvable_does_not_sleep_full_budget():
    """is_correct=false is terminal — must not burn the 60s poll cap."""
    _enable_dbc()
    try:
        upload = mock.Mock(status_code=303)
        upload.json.return_value = {"captcha": 91, "is_correct": True, "text": ""}
        refused = mock.Mock(status_code=200)
        refused.json.return_value = {"captcha": 91, "is_correct": False, "text": "?"}
        with mock.patch("captcha.requests.post", return_value=upload), \
             mock.patch("captcha.requests.get", return_value=refused) as get, \
             mock.patch("captcha.time.sleep") as sleep:
            out = captcha.solve_if_capable("recaptcha", "https://x", "k", timeout_s=60)
        assert out is None
        assert get.call_count == 1
        total_sleep = sum(c.args[0] for c in sleep.call_args_list if c.args)
        assert total_sleep < 10.0
    finally:
        _clear_dbc()


def test_solve_if_capable_overload_fail_fast_no_long_poll():
    """DBC service-overload is terminal — no type fallback poll, no 60s wait."""
    _enable_dbc()
    os.environ["CASE_DBC_PROXY"] = "http://127.0.0.1:1234"
    try:
        overload = mock.Mock(status_code=503)
        overload.json.return_value = {"error": "service-overload", "status": 255}
        with mock.patch("captcha.requests.post", return_value=overload) as post, \
             mock.patch("captcha.requests.get") as get, \
             mock.patch("captcha.time.sleep") as sleep:
            out = captcha.solve_if_capable(
                "recaptcha", "https://linkedin.com/checkpoint/challenge", "k",
                enterprise=True, timeout_s=60)
        assert out is None
        assert post.call_count == 1
        assert post.call_args.kwargs["data"]["type"] == "25"
        get.assert_not_called()
        sleep.assert_not_called()
    finally:
        os.environ.pop("CASE_DBC_PROXY", None)
        _clear_dbc()


def test_solve_if_capable_happy_path_recaptcha_v2():
    _enable_dbc()
    try:
        upload = mock.Mock(status_code=200)
        upload.json.return_value = {"captcha": 42, "is_correct": True, "text": "tok"}
        with mock.patch("captcha.requests.post", return_value=upload) as post, \
             mock.patch("captcha.time.sleep"):
            out = captcha.solve_if_capable("recaptcha", "https://x", "k", timeout_s=5)
        assert out == {"id": "42", "token": "tok"}
        assert post.call_args.kwargs["data"]["type"] == "4"
    finally:
        _clear_dbc()


# ---- solve() with mocked DBC ----


def test_solve_happy_path_type4():
    _enable_dbc()
    try:
        upload = mock.Mock(status_code=200)
        upload.json.return_value = {"captcha": 42, "is_correct": False, "text": ""}
        poll = mock.Mock(status_code=200)
        poll.json.return_value = {"captcha": 42, "is_correct": True, "text": "tok_recaptcha"}

        with mock.patch("captcha.requests.post", return_value=upload) as post, \
             mock.patch("captcha.requests.get", return_value=poll) as get, \
             mock.patch("captcha.time.sleep"):
            out = captcha.solve_if_capable("recaptcha", "https://example.com/login",
                                "6Le-wvkSAAAAAPBMRTvw0Q4Muexq9bi0DJwx_mJ-", timeout_s=5)
        assert out == {"id": "42", "token": "tok_recaptcha"}
        assert post.call_args.kwargs["data"]["type"] == "4"
        assert "token_params" in post.call_args.kwargs["data"]
        assert "token_enterprise_params" not in post.call_args.kwargs["data"]
        params = json.loads(post.call_args.kwargs["data"]["token_params"])
        assert params["googlekey"].startswith("6Le")
        assert "pageurl" in params
        get.assert_called()
    finally:
        _clear_dbc()


def test_solve_linkedin_enterprise_type25_requires_proxy():
    """LinkedIn uses reCAPTCHA Enterprise — DBC type 25 + mandatory proxy."""
    _enable_dbc()
    os.environ.pop("CASE_DBC_PROXY", None)
    try:
        with mock.patch("captcha.requests.post") as post:
            out = captcha.solve_if_capable(
                "recaptcha",
                "https://www.linkedin.com/checkpoint/challenge/x",
                "6LcIy_MqAAAAAMKiupFSbmzW3xjGSlIfRzNWYMjC",
                enterprise=True, timeout_s=5)
        assert out is None
        post.assert_not_called()
    finally:
        _clear_dbc()


def test_solve_linkedin_enterprise_type25_with_proxy():
    _enable_dbc()
    os.environ["CASE_DBC_PROXY"] = "http://user:pass@127.0.0.1:1234"
    os.environ["CASE_DBC_PROXYTYPE"] = "HTTP"
    try:
        upload = mock.Mock(status_code=200)
        upload.json.return_value = {"captcha": 42, "is_correct": True, "text": "tok_ent"}
        with mock.patch("captcha.requests.post", return_value=upload) as post, \
             mock.patch("captcha.time.sleep"):
            out = captcha.solve_if_capable(
                "recaptcha",
                "https://www.linkedin.com/checkpoint/challenge/x",
                "6LcIy_MqAAAAAMKiupFSbmzW3xjGSlIfRzNWYMjC",
                enterprise=True, timeout_s=5)
        assert out == {"id": "42", "token": "tok_ent"}
        data = post.call_args.kwargs["data"]
        assert data["type"] == "25"
        assert "token_enterprise_params" in data
        assert "token_params" not in data
        params = json.loads(data["token_enterprise_params"])
        assert params["googlekey"].startswith("6Lc")
        assert params["proxy"].startswith("http://")
        assert params["proxytype"] == "HTTP"
    finally:
        os.environ.pop("CASE_DBC_PROXY", None)
        os.environ.pop("CASE_DBC_PROXYTYPE", None)
        _clear_dbc()


def test_solve_enterprise_non_overload_upload_failure_tries_type4():
    """Non-terminal type-25 upload failure (e.g. 400) may still try type 4 per FAQ #18.
    Overload itself is terminal — see test_solve_if_capable_overload_fail_fast_no_long_poll."""
    _enable_dbc()
    os.environ["CASE_DBC_PROXY"] = "http://user:pass@127.0.0.1:1234"
    try:
        rejected = mock.Mock(status_code=400)
        rejected.json.return_value = {"error": "not-supported", "status": 255}
        accepted = mock.Mock(status_code=303)
        accepted.json.return_value = {"captcha": 77, "is_correct": True, "text": "tok_v2"}
        with mock.patch("captcha.requests.post",
                        side_effect=[rejected, accepted]) as post, \
             mock.patch("captcha.time.sleep"):
            out = captcha.solve_if_capable("recaptcha", "https://www.linkedin.com/checkpoint/challenge/x",
                                "6Lc_key", enterprise=True, timeout_s=30)
        assert out == {"id": "77", "token": "tok_v2"}
        first, second = post.call_args_list
        assert first.kwargs["data"]["type"] == "25"
        assert "token_enterprise_params" in first.kwargs["data"]
        assert second.kwargs["data"]["type"] == "4"
        assert "proxy" in json.loads(second.kwargs["data"]["token_params"])
    finally:
        os.environ.pop("CASE_DBC_PROXY", None)
        _clear_dbc()


def test_solve_stops_on_dbc_terminal_unsolvable():
    """Observed on LinkedIn's Enterprise key: DBC flips is_correct=false with text="?"
    at ~17s. That is final — keep polling and we burn the budget and delay the human
    handoff for nothing (the pre-fix run logged polls=13 over ~39s)."""
    _enable_dbc()
    try:
        upload = mock.Mock(status_code=303)
        upload.json.return_value = {"captcha": 91, "is_correct": True, "text": ""}
        refused = mock.Mock(status_code=200)
        refused.json.return_value = {"captcha": 91, "is_correct": False, "text": "?"}
        with mock.patch("captcha.requests.post", return_value=upload), \
             mock.patch("captcha.requests.get", return_value=refused) as get, \
             mock.patch("captcha.time.sleep"):
            out = captcha.solve_if_capable("recaptcha", "https://x", "k", timeout_s=60)
        assert out is None
        assert get.call_count == 1     # stopped on the first terminal answer
    finally:
        _clear_dbc()


def test_solve_non_enterprise_uses_type4_only():
    _enable_dbc()
    try:
        overload = mock.Mock(status_code=503)
        overload.json.return_value = {"error": "service-overload", "status": 255}
        with mock.patch("captcha.requests.post", return_value=overload) as post, \
             mock.patch("captcha.time.sleep"):
            assert captcha.solve_if_capable("recaptcha", "https://x", "k", timeout_s=10) is None
        assert post.call_count == 1
        assert post.call_args.kwargs["data"]["type"] == "4"
    finally:
        _clear_dbc()


def test_detect_js_sets_enterprise_from_iframe():
    assert "enterprise: false" in captcha.DETECT_JS
    assert "recaptcha\\/enterprise" in captcha.DETECT_JS
    assert "firstRecaptchaMeta" in captcha.DETECT_JS
    assert "grecaptcha.enterprise" in captcha.DETECT_JS


def test_solve_happy_path_type6():
    _enable_dbc()
    try:
        upload = mock.Mock(status_code=200)
        upload.json.return_value = {"captcha": 7, "is_correct": True, "text": "tok_arkose"}
        with mock.patch("captcha.requests.post", return_value=upload) as post, \
             mock.patch("captcha.time.sleep"):
            out = captcha.solve_if_capable("arkose", "https://example.com/login",
                                "9F35A982-C97C-EBCC-A34D-CF8ED317B596", timeout_s=5)
        assert out == {"id": "7", "token": "tok_arkose"}
        assert post.call_args.kwargs["data"]["type"] == "6"
        params = json.loads(post.call_args.kwargs["data"]["funcaptcha_params"])
        assert params["publickey"].startswith("9F35")
    finally:
        _clear_dbc()


def test_solve_never_solved_returns_none_inside_budget():
    _enable_dbc()
    try:
        upload = mock.Mock(status_code=200)
        upload.json.return_value = {"captcha": 99, "is_correct": False, "text": ""}
        pending = mock.Mock(status_code=200)
        pending.json.return_value = {"captcha": 99, "is_correct": False, "text": ""}

        # Wall-clock deadline covers upload+poll; advance past timeout_s=40.
        times = iter([0.0, 0.0, 0.1, 5.0, 10.0, 20.0, 30.0, 41.0, 42.0, 43.0])
        with mock.patch("captcha.requests.post", return_value=upload), \
             mock.patch("captcha.requests.get", return_value=pending), \
             mock.patch("captcha.time.sleep"), \
             mock.patch("captcha.time.time", side_effect=lambda: next(times, 50.0)):
            out = captcha.solve_if_capable("recaptcha", "https://example.com", "sitekey", timeout_s=40)
        assert out is None
    finally:
        _clear_dbc()


def test_solve_disabled_returns_none():
    _clear_dbc()
    assert captcha.solve_if_capable("recaptcha", "https://example.com", "k") is None


def test_inject_js_embeds_token_as_json():
    js = captcha.inject_js("recaptcha", 'tok"evil')
    assert '"tok\\"evil"' in js or '"tok\\"evil"' in js
    assert "recaptcha" in js
    # raw token with quote must not break out of the JS string unescaped
    assert "tok\"evil" not in js.replace('\\"', "")


def test_inject_js_requires_fields_before_form_success():
    js = captcha.inject_js("recaptcha", "tok")
    assert "field_gone" in js
    assert "const wrote = setRecaptchaFields(token)" in js


def test_report_posts_to_dbc():
    _enable_dbc()
    try:
        with mock.patch("captcha.requests.post") as post:
            captcha.report("42")
        assert "/captcha/42/report" in post.call_args.args[0]
    finally:
        _clear_dbc()


# ---- Failure paths through login_flow._try_captcha_auto (invariant) ----

def _detect_ok():
    return {"ok": True, "value": {"family": "recaptcha", "key": "k",
                                  "pageurl": "https://x", "callback": True}}


def test_try_captcha_auto_solve_none_falls_through():
    """solve_if_capable returns None → helper None so login creates handoff."""
    import login_flow
    import cased

    row = {"id": "c_1", "desk_port": 1, "desk_token": "t"}

    with mock.patch("login_flow.captcha.enabled", return_value=True), \
         mock.patch("deskclient.eval_js", return_value=_detect_ok()), \
         mock.patch("login_flow.captcha.solve_if_capable", return_value=None) as solve, \
         mock.patch("login_flow.captcha.report") as report, \
         mock.patch("login_flow.desk_json") as desk, \
         mock.patch("login_flow.handoffs.create_handoff") as ch:
        out = login_flow._try_captcha_auto(row, "c_1", "linkedin.com")
        assert out is None
        solve.assert_called_once()
        desk.assert_not_called()   # never resume on failed solve
        report.assert_not_called()
        ch.assert_not_called()


def test_try_captcha_auto_still_present_reports_and_falls_through():
    """Verify fails (captcha phrases) → report + None; NEVER resume."""
    import login_flow
    import cased

    row = {"id": "c_1", "desk_port": 1, "desk_token": "t"}

    def eval_side_effect(row, expr, timeout_s=15):
        if expr == captcha.DETECT_JS:
            return _detect_ok()
        if expr == "location.href":
            return {"ok": True, "value": "https://x"}
        if "setRecaptchaFields" in expr or "TOKEN_PLACEHOLDER" in expr or "tok" in expr \
                or "recaptcha_form" in expr or "family" in (expr[:80] if False else "") \
                or "INJECT" in expr:
            pass
        if expr == captcha.SETTLE_JS or "readyState" in expr:
            return {"ok": True, "value": {"href": "https://x", "ready": "complete"}}
        if expr == captcha.VERIFY_JS:
            return {"ok": True, "value": {
                "text": "Let's do a quick security check captcha remaining",
                "hasPassword": False, "href": "https://x"}}
        # inject js
        if "TOKEN_PLACEHOLDER" not in expr and ("recaptcha" in expr or "token" in expr.lower()
                                                or "setRecaptchaFields" in expr
                                                or "g-recaptcha-response" in expr):
            return {"ok": True, "value": {"ok": True, "method": "recaptcha_form"}}
        return {"ok": True, "value": {"ok": True, "method": "recaptcha_form"}}

    with mock.patch("login_flow.captcha.enabled", return_value=True), \
         mock.patch("deskclient.eval_js", side_effect=eval_side_effect), \
         mock.patch("login_flow.eval_js", side_effect=eval_side_effect), \
         mock.patch("login_flow.captcha.solve_if_capable",
                    return_value={"id": "99", "token": "tok"}), \
         mock.patch("login_flow.captcha.report") as report, \
         mock.patch("login_flow.desk_json") as desk, \
         mock.patch("login_flow._settle_after_inject"), \
         mock.patch("cased.store.record_credential_result") as rec, \
         mock.patch("cased.events.emit") as emit:
        out = login_flow._try_captcha_auto(row, "c_1", "linkedin.com")
        assert out is None
        report.assert_called_once_with("99")
        desk.assert_not_called()  # CRITICAL: no resume on verify failure
        rec.assert_not_called()
        emit.assert_not_called()


def test_try_captcha_auto_verify_eval_error_no_resume():
    """504/423 on VERIFY_JS → fail closed, no /login/resume."""
    import login_flow
    from errors import ApiError

    row = {"id": "c_1", "desk_port": 1, "desk_token": "t"}

    def eval_side_effect(row, expr, timeout_s=15):
        if expr == captcha.DETECT_JS:
            return _detect_ok()
        if expr == "location.href":
            return {"ok": True, "value": "https://x"}
        if expr == captcha.VERIFY_JS:
            raise ApiError(504, "daemon_timeout", "deskd did not respond")
        return {"ok": True, "value": {"ok": True, "method": "recaptcha_form"}}

    with mock.patch("login_flow.captcha.enabled", return_value=True), \
         mock.patch("deskclient.eval_js", side_effect=eval_side_effect), \
         mock.patch("login_flow.eval_js", side_effect=eval_side_effect), \
         mock.patch("login_flow.captcha.solve_if_capable",
                    return_value={"id": "11", "token": "tok"}), \
         mock.patch("login_flow.captcha.report") as report, \
         mock.patch("login_flow.desk_json") as desk, \
         mock.patch("login_flow._settle_after_inject"):
        out = login_flow._try_captcha_auto(row, "c_1", "linkedin.com")
        assert out is None
        report.assert_called_once_with("11")
        desk.assert_not_called()


def test_try_captcha_auto_otp_phrase_no_resume():
    """OTP/verification-code page after inject → fail, no resume."""
    import login_flow
    import cased

    row = {"id": "c_1", "desk_port": 1, "desk_token": "t"}

    def eval_side_effect(row, expr, timeout_s=15):
        if expr == captcha.DETECT_JS:
            return _detect_ok()
        if expr == "location.href":
            return {"ok": True, "value": "https://x"}
        if expr == captcha.VERIFY_JS:
            return {"ok": True, "value": {
                "text": "Enter the verification code we texted you",
                "hasPassword": False, "href": "https://x"}}
        return {"ok": True, "value": {"ok": True, "method": "recaptcha_form"}}

    with mock.patch("login_flow.captcha.enabled", return_value=True), \
         mock.patch("deskclient.eval_js", side_effect=eval_side_effect), \
         mock.patch("login_flow.eval_js", side_effect=eval_side_effect), \
         mock.patch("login_flow.captcha.solve_if_capable",
                    return_value={"id": "7", "token": "tok"}), \
         mock.patch("login_flow.captcha.report") as report, \
         mock.patch("login_flow.desk_json") as desk, \
         mock.patch("login_flow._settle_after_inject"):
        out = login_flow._try_captcha_auto(row, "c_1", "linkedin.com")
        assert out is None
        desk.assert_not_called()
        report.assert_called_once_with("7")


def test_try_captcha_auto_password_still_present_no_resume():
    """Visible password field after inject → fail, no resume."""
    import login_flow
    import cased

    row = {"id": "c_1", "desk_port": 1, "desk_token": "t"}

    def eval_side_effect(row, expr, timeout_s=15):
        if expr == captcha.DETECT_JS:
            return _detect_ok()
        if expr == "location.href":
            return {"ok": True, "value": "https://x"}
        if expr == captcha.VERIFY_JS:
            return {"ok": True, "value": {
                "text": "Looks fine actually", "hasPassword": True, "href": "https://x"}}
        return {"ok": True, "value": {"ok": True, "method": "recaptcha_form"}}

    with mock.patch("login_flow.captcha.enabled", return_value=True), \
         mock.patch("deskclient.eval_js", side_effect=eval_side_effect), \
         mock.patch("login_flow.eval_js", side_effect=eval_side_effect), \
         mock.patch("login_flow.captcha.solve_if_capable",
                    return_value={"id": "8", "token": "tok"}), \
         mock.patch("login_flow.captcha.report") as report, \
         mock.patch("login_flow.desk_json") as desk, \
         mock.patch("login_flow._settle_after_inject"):
        out = login_flow._try_captcha_auto(row, "c_1", "linkedin.com")
        assert out is None
        desk.assert_not_called()
        report.assert_called_once_with("8")


def test_try_captcha_auto_no_callback_skips_without_resume():
    import login_flow
    import cased

    row = {"id": "c_1", "desk_port": 1, "desk_token": "t"}
    with mock.patch("login_flow.captcha.enabled", return_value=True), \
         mock.patch("deskclient.eval_js", return_value={
             "ok": True,
             "value": {"family": "recaptcha", "key": "k", "pageurl": "https://x",
                       "callback": False},
         }), \
         mock.patch("login_flow.captcha.solve_if_capable") as solve, \
         mock.patch("login_flow.desk_json") as desk:
        out = login_flow._try_captcha_auto(row, "c_1", "linkedin.com")
        assert out is None
        solve.assert_not_called()
        desk.assert_not_called()


def test_try_captcha_auto_verified_success_resumes_only_then():
    import login_flow
    import cased

    row = {"id": "c_1", "desk_port": 1, "desk_token": "t"}
    calls = []

    def eval_side_effect(row, expr, timeout_s=15):
        calls.append(expr if expr in (captcha.DETECT_JS, captcha.VERIFY_JS,
                                      captcha.SETTLE_JS, "location.href")
                     else "inject")
        if expr == captcha.DETECT_JS:
            return _detect_ok()
        if expr == "location.href":
            return {"ok": True, "value": "https://www.linkedin.com/checkpoint/challenge/x"}
        if expr == captcha.VERIFY_JS:
            return {"ok": True, "value": {
                "text": "Welcome back to LinkedIn https://www.linkedin.com/feed/",
                "hasPassword": False,
                "href": "https://www.linkedin.com/feed/"}}
        return {"ok": True, "value": {"ok": True, "method": "recaptcha_form"}}

    with mock.patch("login_flow.captcha.enabled", return_value=True), \
         mock.patch("deskclient.eval_js", side_effect=eval_side_effect), \
         mock.patch("login_flow.eval_js", side_effect=eval_side_effect), \
         mock.patch("login_flow.captcha.solve_if_capable",
                    return_value={"id": "1", "token": "tok"}), \
         mock.patch("login_flow.desk_json", return_value={"status": "success"}) as desk, \
         mock.patch("login_flow._settle_after_inject") as settle, \
         mock.patch("cased.store.record_credential_result") as rec, \
         mock.patch("cased.events.emit") as emit:
        out = login_flow._try_captcha_auto(row, "c_1", "linkedin.com")
        assert out == {"status": "success", "captcha_auto": True}
        settle.assert_called_once()
        desk.assert_called_once()
        assert desk.call_args.args[1:3] == ("POST", "/login/resume")
        rec.assert_called_once_with("c_1", "linkedin.com", "success")
        emit.assert_called_once()
        # resume only after verify appeared in call sequence
        assert "inject" in calls
        assert captcha.VERIFY_JS in calls


def test_try_captcha_auto_resume_failed_returns_failed_not_none():
    """Verify ok but resume failed → return failed status (no dead handoff)."""
    import login_flow
    import cased

    row = {"id": "c_1", "desk_port": 1, "desk_token": "t"}

    def eval_side_effect(row, expr, timeout_s=15):
        if expr == captcha.DETECT_JS:
            return _detect_ok()
        if expr == "location.href":
            return {"ok": True, "value": "https://x"}
        if expr == captcha.VERIFY_JS:
            return {"ok": True, "value": {
                "text": "Welcome feed", "hasPassword": False, "href": "https://x/feed"}}
        return {"ok": True, "value": {"ok": True, "method": "recaptcha_form"}}

    with mock.patch("login_flow.captcha.enabled", return_value=True), \
         mock.patch("deskclient.eval_js", side_effect=eval_side_effect), \
         mock.patch("login_flow.eval_js", side_effect=eval_side_effect), \
         mock.patch("login_flow.captcha.solve_if_capable",
                    return_value={"id": "1", "token": "tok"}), \
         mock.patch("login_flow.desk_json",
                    return_value={"status": "failed", "reason": "gone"}), \
         mock.patch("login_flow._settle_after_inject"), \
         mock.patch("cased.store.record_credential_result") as rec, \
         mock.patch("cased.events.emit"):
        out = login_flow._try_captcha_auto(row, "c_1", "linkedin.com")
        assert out is not None
        assert out.get("status") == "failed"
        rec.assert_called_once_with("c_1", "linkedin.com", "failed")


def _seed_login_computer(cid="c_1"):
    """Durable login needs a store computer (raise_challenge → get_computer)."""
    from store import store
    store.q("DELETE FROM auth_attempts WHERE computer_id=?", (cid,))
    store.q("DELETE FROM handoffs WHERE computer_id=?", (cid,))
    store.delete_computer(cid)
    store.insert_computer(cid, "ava", "img", 1, 512, f"vol-{cid}", f"tok-{cid}")
    store.set_state(cid, "running")


def test_login_challenge_without_dbc_still_creates_handoff():
    """Integration-ish: login path with captcha challenge and DBC off → handoff."""
    import login_flow
    import cased

    _seed_login_computer()
    row = {"id": "c_1", "name": "ava", "desk_port": 1, "desk_token": "t",
           "state": "running"}
    with mock.patch("cased.lifecycle.ensure_running", return_value=row), \
         mock.patch("cased.store.credential_material", return_value={"name": "linkedin.com"}), \
         mock.patch("cased.desk_json", return_value={
             "status": "challenge", "kind": "captcha", "prompt": "captcha now",
             "screenshot_png_b64": None,
         }), \
         mock.patch("cased.store.touch"), \
         mock.patch("login_flow.captcha.enabled", return_value=False), \
         mock.patch("cased.store.record_credential_result") as rec, \
         mock.patch("deskclient.screenshot_b64", return_value=None), \
         mock.patch("handoffs.create_handoff",
                    return_value={"id": "h_1"}) as ch:
        out = cased.login("c_1", {"credential": "linkedin.com",
                                  "url": "https://www.linkedin.com/login"})
        assert out["status"] == "handoff_pending"
        assert out["handoff_id"] == "h_1"
        assert out.get("attempt_id")
        rec.assert_called_once_with("c_1", "linkedin.com", "challenge")
        ch.assert_called_once()
        assert ch.call_args.kwargs.get("attempt_id") or (
            len(ch.call_args.args) >= 1)


def test_login_creates_handoff_when_auto_returns_none():
    """Bad solve → helper None → login still creates handoff."""
    import login_flow
    import cased

    _seed_login_computer()
    row = {"id": "c_1", "name": "ava", "desk_port": 1, "desk_token": "t",
           "state": "running"}
    with mock.patch("cased.lifecycle.ensure_running", return_value=row), \
         mock.patch("cased.store.credential_material", return_value={"name": "linkedin.com"}), \
         mock.patch("cased.desk_json", return_value={
             "status": "challenge", "kind": "captcha", "prompt": "captcha now",
             "screenshot_png_b64": None,
         }), \
         mock.patch("cased.store.touch"), \
         mock.patch("login_flow.captcha.enabled", return_value=True), \
         mock.patch("login_flow._try_captcha_auto", return_value=None), \
         mock.patch("cased.store.record_credential_result") as rec, \
         mock.patch("deskclient.screenshot_b64", return_value=None), \
         mock.patch("handoffs.create_handoff",
                    return_value={"id": "h_bad"}) as ch:
        out = cased.login("c_1", {"credential": "linkedin.com",
                                  "url": "https://www.linkedin.com/login"})
        assert out["status"] == "handoff_pending"
        assert out["handoff_id"] == "h_bad"
        assert out.get("attempt_id")
        rec.assert_called_once_with("c_1", "linkedin.com", "challenge")
        ch.assert_called_once()


# ---- post-login gate (deskd reports success on LinkedIn's checkpoint page) ----

def test_gate_open_helper():
    assert captcha.gate_open({"gated": True}) is True
    assert captcha.gate_open({"gated": False}) is False
    assert captcha.gate_open(None) is False
    assert captcha.gate_open("nope") is False


def test_gate_js_covers_checkpoint_url_and_iframes():
    js = captcha.GATE_JS
    assert "checkpoint" in js and "challenge" in js
    assert "quick security check" in js
    for marker in ("recaptcha", "arkoselabs", "funcaptcha", "captchaInternal"):
        assert marker in js


def test_post_login_gate_returns_none_when_not_gated():
    """Clean feed page after login → gate path must not interfere."""
    import login_flow
    import cased

    row = {"id": "c_1", "name": "ava", "desk_port": 1, "desk_token": "t"}
    with mock.patch("login_flow.captcha.enabled", return_value=True), \
         mock.patch("login_flow.eval_value", return_value={"gated": False,
                                   "href": "https://www.linkedin.com/feed/"}), \
         mock.patch("login_flow._try_captcha_auto") as auto, \
         mock.patch("login_flow.handoffs.create_handoff") as ch:
        assert login_flow._post_login_gate(row, "c_1", "linkedin.com",
                                      "https://www.linkedin.com/login") is None
        auto.assert_not_called()
        ch.assert_not_called()


def test_post_login_gate_runs_auto_without_resume():
    """Gate open → auto-solve invoked with resume=False (deskd holds no login)."""
    import login_flow
    import cased

    row = {"id": "c_1", "name": "ava", "desk_port": 1, "desk_token": "t"}
    with mock.patch("login_flow.captcha.enabled", return_value=True), \
         mock.patch("deskclient.eval_js", return_value={
             "ok": True,
             "value": {"gated": True,
                       "href": "https://www.linkedin.com/checkpoint/challenge/x"}}), \
         mock.patch("login_flow._try_captcha_auto",
                    return_value={"status": "success", "captcha_auto": True}) as auto, \
         mock.patch("login_flow.handoffs.create_handoff") as ch:
        out = login_flow._post_login_gate(row, "c_1", "linkedin.com",
                                     "https://www.linkedin.com/login")
        assert out == {"status": "success", "captcha_auto": True}
        assert auto.call_args.kwargs["resume"] is False
        ch.assert_not_called()


def test_post_login_gate_failure_creates_handoff_without_login_ctx():
    """CRITICAL: the fallback handoff must carry no login_credential — deskd already
    cleared state["login"], so answering one would 409 on /login/resume."""
    import login_flow
    import cased

    row = {"id": "c_1", "name": "ava", "desk_port": 1, "desk_token": "t"}
    with mock.patch("login_flow.captcha.enabled", return_value=True), \
         mock.patch("deskclient.eval_js", return_value={
             "ok": True,
             "value": {"gated": True,
                       "href": "https://www.linkedin.com/checkpoint/challenge/x"}}), \
         mock.patch("login_flow._try_captcha_auto", return_value=None), \
         mock.patch("login_flow.screenshot_b64", return_value="png"), \
         mock.patch("cased.store.record_credential_result") as rec, \
         mock.patch("login_flow.handoffs.create_handoff",
                    return_value={"id": "h_gate"}) as ch:
        out = login_flow._post_login_gate(row, "c_1", "linkedin.com",
                                     "https://www.linkedin.com/login")
        assert out == {"status": "handoff_pending", "handoff_id": "h_gate"}
        rec.assert_called_once_with("c_1", "linkedin.com", "challenge")
        assert "login_credential" not in ch.call_args.kwargs
        assert ch.call_args.args[1] == "captcha"


def test_post_login_gate_dbc_off_still_creates_typed_captcha_handoff():
    """Human fallback: gate open + DBC unavailable → verify_page captcha handoff,
    never silent success."""
    import login_flow
    import cased

    row = {"id": "c_1", "name": "ava", "desk_port": 1, "desk_token": "t"}
    with mock.patch("login_flow.captcha.enabled", return_value=False), \
         mock.patch("deskclient.eval_js", return_value={
             "ok": True,
             "value": {"gated": True,
                       "href": "https://www.linkedin.com/checkpoint/challenge/x"}}), \
         mock.patch("login_flow._try_captcha_auto") as auto, \
         mock.patch("login_flow.screenshot_b64", return_value="png"), \
         mock.patch("cased.store.record_credential_result") as rec, \
         mock.patch("login_flow.handoffs.create_handoff",
                    return_value={"id": "h_human"}) as ch:
        out = login_flow._post_login_gate(row, "c_1", "linkedin.com",
                                     "https://www.linkedin.com/login")
        assert out == {"status": "handoff_pending", "handoff_id": "h_human"}
        auto.assert_not_called()
        rec.assert_called_once_with("c_1", "linkedin.com", "challenge")
        assert ch.call_args.args[1] == "captcha"
        assert "login_credential" not in ch.call_args.kwargs


def test_post_login_gate_handoff_survives_screenshot_failure():
    import login_flow
    import cased

    row = {"id": "c_1", "name": "ava", "desk_port": 1, "desk_token": "t"}
    with mock.patch("login_flow.captcha.enabled", return_value=True), \
         mock.patch("deskclient.eval_js", return_value={
             "ok": True, "value": {"gated": True, "href": "https://x/checkpoint/challenge"}}), \
         mock.patch("login_flow._try_captcha_auto", return_value=None), \
         mock.patch("login_flow.screenshot_b64", side_effect=RuntimeError("423")), \
         mock.patch("cased.store.record_credential_result"), \
         mock.patch("login_flow.handoffs.create_handoff",
                    return_value={"id": "h_gate"}) as ch:
        out = login_flow._post_login_gate(row, "c_1", "linkedin.com", "https://x/login")
        assert out == {"status": "handoff_pending", "handoff_id": "h_gate"}
        assert ch.call_args.kwargs["screenshot"] is None


def test_gate_info_survives_eval_failure():
    """Unreadable tab → {} → not gated → normal success path (no false handoff)."""
    import login_flow
    import cased

    row = {"id": "c_1", "desk_port": 1, "desk_token": "t"}
    with mock.patch("login_flow.eval_value", return_value=None):
        assert login_flow._gate_info(row) == {}


def test_auto_no_resume_requires_gate_closed():
    """resume=False path: verify clean but gate still open → fail, report, no success."""
    import login_flow
    import cased

    row = {"id": "c_1", "desk_port": 1, "desk_token": "t"}

    def eval_side_effect(row, expr, timeout_s=15):
        if expr == captcha.DETECT_JS:
            return _detect_ok()
        if expr == "location.href":
            return {"ok": True, "value": "https://x/checkpoint/challenge"}
        if expr == captcha.VERIFY_JS:
            return {"ok": True, "value": {"text": "Welcome", "hasPassword": False,
                                          "href": "https://x/checkpoint/challenge"}}
        if expr == captcha.GATE_JS:
            return {"ok": True, "value": {"gated": True,
                                          "href": "https://x/checkpoint/challenge"}}
        return {"ok": True, "value": {"ok": True, "method": "recaptcha_form"}}

    with mock.patch("login_flow.captcha.enabled", return_value=True), \
         mock.patch("deskclient.eval_js", side_effect=eval_side_effect), \
         mock.patch("login_flow.eval_js", side_effect=eval_side_effect), \
         mock.patch("login_flow.captcha.solve_if_capable", return_value={"id": "5", "token": "tok"}), \
         mock.patch("login_flow.captcha.report") as report, \
         mock.patch("login_flow.desk_json") as desk, \
         mock.patch("login_flow._settle_after_inject"), \
         mock.patch("cased.store.record_credential_result") as rec:
        out = login_flow._try_captcha_auto(row, "c_1", "linkedin.com", resume=False)
        assert out is None
        report.assert_called_once_with("5")
        desk.assert_not_called()
        rec.assert_not_called()


def test_auto_no_resume_success_never_calls_resume():
    import login_flow
    import cased

    row = {"id": "c_1", "desk_port": 1, "desk_token": "t"}

    def eval_side_effect(row, expr, timeout_s=15):
        if expr == captcha.DETECT_JS:
            return _detect_ok()
        if expr == "location.href":
            return {"ok": True, "value": "https://x/checkpoint/challenge"}
        if expr == captcha.VERIFY_JS:
            return {"ok": True, "value": {"text": "Welcome feed", "hasPassword": False,
                                          "href": "https://www.linkedin.com/feed/"}}
        if expr == captcha.GATE_JS:
            return {"ok": True, "value": {"gated": False,
                                          "href": "https://www.linkedin.com/feed/"}}
        return {"ok": True, "value": {"ok": True, "method": "recaptcha_form"}}

    with mock.patch("login_flow.captcha.enabled", return_value=True), \
         mock.patch("deskclient.eval_js", side_effect=eval_side_effect), \
         mock.patch("login_flow.eval_js", side_effect=eval_side_effect), \
         mock.patch("login_flow.captcha.solve_if_capable", return_value={"id": "6", "token": "tok"}), \
         mock.patch("login_flow.captcha.report") as report, \
         mock.patch("login_flow.desk_json") as desk, \
         mock.patch("login_flow._settle_after_inject"), \
         mock.patch("cased.store.record_credential_result") as rec, \
         mock.patch("cased.events.emit") as emit:
        out = login_flow._try_captcha_auto(row, "c_1", "linkedin.com", resume=False)
        assert out == {"status": "success", "captcha_auto": True}
        desk.assert_not_called()   # CRITICAL: no /login/resume on the gate path
        report.assert_not_called()
        rec.assert_called_once_with("c_1", "linkedin.com", "success")
        emit.assert_called_once()


def test_login_success_checks_gate():
    """deskd success + open gate → login() returns the gate path's result."""
    import login_flow
    import cased

    _seed_login_computer()
    row = {"id": "c_1", "name": "ava", "desk_port": 1, "desk_token": "t",
           "state": "running"}
    with mock.patch("cased.lifecycle.ensure_running", return_value=row), \
         mock.patch("cased.store.credential_material", return_value={"name": "linkedin.com"}), \
         mock.patch("cased.desk_json", return_value={"status": "success"}), \
         mock.patch("cased.store.touch"), \
         mock.patch("login_flow._post_login_gate",
                    return_value={"status": "handoff_pending", "handoff_id": "h_g"}), \
         mock.patch("cased.store.record_credential_result") as rec, \
         mock.patch("cased.events.emit") as emit:
        out = cased.login("c_1", {"credential": "linkedin.com",
                                  "url": "https://www.linkedin.com/login"})
        assert out == {"status": "handoff_pending", "handoff_id": "h_g"}
        rec.assert_not_called()    # gate path owns the credential verdict
        emit.assert_not_called()


def test_login_success_ungated_reports_success():
    """deskd success + closed gate + positive proof_spec → authenticated."""
    import login_flow
    import cased

    _seed_login_computer()
    row = {"id": "c_1", "name": "ava", "desk_port": 1, "desk_token": "t",
           "state": "running"}
    with mock.patch("cased.lifecycle.ensure_running", return_value=row), \
         mock.patch("cased.store.credential_material", return_value={"name": "linkedin.com"}), \
         mock.patch("cased.desk_json", return_value={"status": "success"}), \
         mock.patch("cased.store.touch"), \
         mock.patch("login_flow._post_login_gate", return_value=None), \
         mock.patch("auth_attempts.check_proof", return_value=True), \
         mock.patch("cased.store.record_credential_result") as rec, \
         mock.patch("cased.events.emit"):
        out = cased.login("c_1", {
            "credential": "linkedin.com",
            "url": "https://www.linkedin.com/login",
            "proof_spec": {"url_contains": "/feed"},
        })
        assert out["status"] == "success"
        assert out.get("attempt_id")
        rec.assert_called_once_with("c_1", "linkedin.com", "success")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("PASS")
