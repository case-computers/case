# SPDX-License-Identifier: MIT
"""Assist door: hashed exchange token → HttpOnly session scoped to one handoff/attempt.
Run: .venv/bin/python tests/test_assist.py"""
import hashlib
import json
import os
import sys
import unittest.mock as mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "control-plane"))
# assignment, NOT setdefault: these tests write assist_tokens + handoffs, and an
# inherited CASE_HOME would put them in a live box's DB.
os.environ["CASE_HOME"] = "/tmp/case-assist-test"
import assist  # noqa: E402
import cased  # noqa: E402
import handoffs  # noqa: E402
import links  # noqa: E402
from errors import ApiError  # noqa: E402
from store import store  # noqa: E402
from util import iso_in, now  # noqa: E402

IDS = ("h_otp", "h_cap", "h_dead", "h_exp", "h_otp2", "h_wait", "h_bind_a", "h_bind_b")
ATTEMPTS = ("aa_1", "aa_term", "aa_bind")
handoffs.notifier = type("N", (), {"notify": lambda self, h, name: None})()


def _cleanup():
    for hid in IDS:
        store.delete_handoff(hid)
    for aid in ATTEMPTS:
        store.q("DELETE FROM auth_attempts WHERE id=?", (aid,))
    store.q("DELETE FROM assist_tokens")
    store.q("DELETE FROM links")
    store.q("DELETE FROM credentials WHERE computer_id IN ('c_1','c_bind')")
    handoffs.LOGIN_CTX.clear()


def _running_computer(cid="c_1"):
    store.q("DELETE FROM computers WHERE id=?", (cid,))
    store.insert_computer(cid, "ava", "case-desk:0.1", 1, 2048, "vol", "tok")
    store.set_state(cid, "running")


def _desk_check_ep(uri, cookie):
    from starlette.requests import Request
    scope = {"type": "http", "headers": [
        (b"x-forwarded-uri", uri.encode()),
        (b"cookie", cookie.encode()),
    ], "method": "GET", "path": "/v1/desk/check", "query_string": b""}
    return cased.desk_check_ep(Request(scope))


def _hash(raw):
    return hashlib.sha256(raw.encode()).hexdigest()


def _pending(hid, kind, continuation=None, attempt_id=None, revision=0, login_credential=None,
             domain=None):
    store.delete_handoff(hid)
    store.insert_handoff(hid, "c_1", kind, "prompt for " + kind, None, login_credential,
                         domain, continuation=continuation or handoffs.continuation_for(kind),
                         attempt_id=attempt_id, revision=revision)
    return store.get_handoff(hid)


def _attempt(aid, status="awaiting_human", credential="github", current_handoff_id=None,
             revision=0):
    store.q("DELETE FROM auth_attempts WHERE id=?", (aid,))
    store.insert_auth_attempt(aid, "c_1", credential, "https://github.com/login",
                              status=status)
    if revision:
        store.q("UPDATE auth_attempts SET revision=? WHERE id=?", (revision, aid))
    if current_handoff_id is not None:
        store.set_attempt_handoff(aid, current_handoff_id)
    return store.get_auth_attempt(aid)


def test_mint_stores_hash_only_never_plaintext():
    _cleanup()
    _pending("h_otp", "otp")
    raw, expires = assist.mint_assist_token("h_otp")
    assert raw and expires and expires > now()
    row = store.get_assist_by_token_hash(_hash(raw))
    assert row is not None
    assert row["handoff_id"] == "h_otp"
    assert row["token_hash"] == _hash(raw)
    assert row["burned_at"] is None
    assert row["session_hash"] is None
    # plaintext must not appear anywhere in the row
    blob = " ".join(str(row[k]) for k in row.keys())
    assert raw not in blob


def test_exchange_burns_token_and_returns_session():
    _cleanup()
    _pending("h_otp", "otp")
    raw, _ = assist.mint_assist_token("h_otp")
    session, handoff = assist.exchange(raw)
    assert handoff["id"] == "h_otp"
    assert session and session != raw
    row = store.get_assist_by_token_hash(_hash(raw))
    assert row["burned_at"] is not None
    assert row["session_hash"] == _hash(session)
    assert row["session_expires_at"] > now()


def test_exchange_replay_of_burned_token_fails():
    _cleanup()
    _pending("h_otp", "otp")
    raw, _ = assist.mint_assist_token("h_otp")
    assist.exchange(raw)
    try:
        assist.exchange(raw)
        assert False, "replay should fail"
    except ApiError as e:
        assert e.status == 410, e.status


def test_valid_session_for_live_pending_handoff():
    _cleanup()
    _pending("h_otp", "otp")
    raw, _ = assist.mint_assist_token("h_otp")
    session, _ = assist.exchange(raw)
    h = assist.valid_session(session)
    assert h and h["id"] == "h_otp" and h["computer_id"] == "c_1"


def test_valid_session_rejects_garbage_and_expired_session():
    _cleanup()
    assert assist.valid_session("") is None
    assert assist.valid_session("nope") is None
    _pending("h_otp", "otp")
    raw, _ = assist.mint_assist_token("h_otp")
    session, _ = assist.exchange(raw)
    # force session expiry
    store.q("UPDATE assist_tokens SET session_expires_at=? WHERE session_hash=?",
            (now(), _hash(session)))
    assert assist.valid_session(session) is None


def test_valid_session_dies_when_handoff_terminal():
    _cleanup()
    _pending("h_otp", "otp")
    raw, _ = assist.mint_assist_token("h_otp")
    session, _ = assist.exchange(raw)
    store.set_handoff_status("h_otp", "completed", answer="123456")
    assert assist.valid_session(session) is None
    store.set_handoff_status("h_otp", "failed")
    assert assist.valid_session(session) is None
    store.set_handoff_status("h_otp", "expired")
    assert assist.valid_session(session) is None


def test_exchange_rejects_expired_exchange_token():
    _cleanup()
    _pending("h_exp", "otp")
    raw, _ = assist.mint_assist_token("h_exp")
    store.q("UPDATE assist_tokens SET expires_at=? WHERE token_hash=?",
            (now(), _hash(raw)))
    try:
        assist.exchange(raw)
        assert False, "expired exchange should fail"
    except ApiError as e:
        assert e.status == 410


def test_desk_check_accepts_assist_cookie_for_bound_computer():
    _cleanup()
    _pending("h_cap", "captcha")
    raw, _ = assist.mint_assist_token("h_cap")
    session, _ = assist.exchange(raw)
    _running_computer("c_1")
    assert links.desk_check("/desk/vnc.html", f"case_assist={session}") == (None, None)
    assert _desk_check_ep("/desk/vnc.html", f"case_assist={session}").status_code == 200


def test_assist_cookie_does_not_unlock_fill_or_console():
    _cleanup()
    _pending("h_cap", "captcha")
    raw, _ = assist.mint_assist_token("h_cap")
    session, _ = assist.exchange(raw)
    # the fill door is a separate capability surface
    assert links.valid(session, "fill") is None
    assert links.valid(session, "vnc") is None


def test_otp_submit_via_assist_completes_without_login_ctx():
    _cleanup()
    _pending("h_otp", "otp", continuation="submit_value")
    raw, _ = assist.mint_assist_token("h_otp")
    session, handoff = assist.exchange(raw)
    assert handoff["status"] == "pending"
    # route-layer helper: resolve session → submit
    got = assist.submit_with_session(session, "123456")
    assert got["status"] == "completed", dict(got)
    assert got["answer"] is None  # OTP never leaves Assist → API as plaintext
    # desk access dies once handoff is terminal
    assert assist.valid_session(session) is None
    assert links.desk_check("/desk/", f"case_assist={session}") == (None, None)
    assert _desk_check_ep("/desk/", f"case_assist={session}").status_code == 401
    assert store.get_handoff("h_otp")["answer"] is None


def test_captcha_done_via_assist_verify_page():
    _cleanup()
    _pending("h_cap", "captcha", continuation="verify_page")
    raw, _ = assist.mint_assist_token("h_cap")
    session, _ = assist.exchange(raw)
    with mock.patch.object(handoffs, "get_computer", return_value={"id": "c_1", "name": "ava",
                                                                   "state": "running"}), \
         mock.patch.object(handoffs, "_page_still_challenged", return_value=False):
        got = assist.done_with_session(session)
    assert got["status"] == "completed", dict(got)
    assert assist.valid_session(session) is None


def test_cookie_header_flags():
    hdr = assist.session_cookie_header("sess_raw", max_age=1800)
    assert hdr.startswith("case_assist=sess_raw;")
    assert "Path=/" in hdr
    assert "Secure" in hdr and "HttpOnly" in hdr and "SameSite=Lax" in hdr
    assert "Max-Age=1800" in hdr


def test_resolve_get_exchanges_then_replay_needs_cookie():
    _cleanup()
    _pending("h_otp", "otp")
    raw, _ = assist.mint_assist_token("h_otp")
    handoff, set_sess = assist.resolve(raw, cookie_header="")
    assert handoff["id"] == "h_otp" and set_sess
    # burned exchange alone fails
    try:
        assist.resolve(raw, cookie_header="")
        assert False, "burned exchange without cookie must fail"
    except ApiError as e:
        assert e.status == 410
    # same URL + session cookie still opens the page
    handoff2, set2 = assist.resolve(raw, cookie_header=f"case_assist={set_sess}")
    assert handoff2["id"] == "h_otp" and set2 is None


# ---- Wave 3: dynamic phases, state shape, open_url policy, attempt scope ----

def test_state_payload_shape_never_secrets_or_urls():
    _cleanup()
    _pending("h_otp", "otp", continuation="submit_value", revision=2)
    raw, _ = assist.mint_assist_token("h_otp")
    session, _ = assist.exchange(raw)
    view, _ = assist.resolve_view(raw, f"case_assist={session}")
    payload = assist.state_payload(view)
    assert set(payload) == {"status", "revision", "kind", "continuation",
                            "instructions", "allowed_actions"}
    assert payload["status"] == "pending"
    assert payload["revision"] == 2
    assert payload["kind"] == "otp"
    assert payload["continuation"] == "submit_value"
    assert payload["allowed_actions"] == ["submit_value"]
    blob = json.dumps(payload)
    assert "answer" not in blob
    assert "https://" not in blob
    assert "secret" not in blob.lower()
    assert "token_hash" not in blob


def test_render_phases_submit_verify_wait_and_terminal_attempt():
    _cleanup()
    _pending("h_otp", "otp", continuation="submit_value")
    html = assist.render_page(
        assist.build_view(store.get_handoff("h_otp"), store.get_handoff("h_otp"), None), "tok")
    assert "Enter the code" in html and "/assist/tok/submit" in html
    assert "assist.js" in html

    _pending("h_cap", "captcha", continuation="verify_page")
    html = assist.render_page(
        assist.build_view(store.get_handoff("h_cap"), store.get_handoff("h_cap"), None), "tok")
    assert "I'm done" in html and "/desk/vnc.html" in html
    assert "Open on computer" in html  # open_url allowed on verify_page

    _pending("h_wait", "device", continuation="wait_external")
    html = assist.render_page(
        assist.build_view(store.get_handoff("h_wait"), store.get_handoff("h_wait"), None), "tok")
    assert "Waiting on you" in html and "refreshes automatically" in html

    _attempt("aa_term", status="authenticated", current_handoff_id=None)
    store.insert_handoff("h_dead", "c_1", "otp", "x", None, None, None,
                         continuation="submit_value", attempt_id="aa_term")
    store.set_handoff_status("h_dead", "completed", answer="1")
    view = assist.build_view(store.get_handoff("h_dead"), None, store.get_auth_attempt("aa_term"))
    html = assist.render_page(view, "tok")
    assert "Signed in" in html
    payload_ok = assist.state_payload(view)
    assert payload_ok["status"] == "authenticated"
    assert payload_ok["allowed_actions"] == []


def test_attempt_scoped_session_follows_current_handoff():
    _cleanup()
    _attempt("aa_1", status="awaiting_human", current_handoff_id="h_cap")
    _pending("h_cap", "captcha", continuation="verify_page", attempt_id="aa_1")
    raw, _ = assist.mint_assist_token("h_cap")
    session, _ = assist.exchange(raw)
    # First challenge completes; attempt advances to OTP on a new handoff.
    store.set_handoff_status("h_cap", "completed", answer="done")
    _pending("h_otp2", "otp", continuation="submit_value", attempt_id="aa_1", revision=1)
    store.set_attempt_handoff("aa_1", "h_otp2")
    # Same session must act on the *current* handoff, not the burned mint target.
    cur = assist.valid_session(session)
    assert cur and cur["id"] == "h_otp2", cur
    view, _ = assist.resolve_view(raw, f"case_assist={session}")
    assert view["handoff"]["id"] == "h_otp2"
    assert view["continuation"] == "submit_value"
    assert "submit_value" in view["allowed_actions"]
    # desk still unlocks for the computer
    _running_computer("c_1")
    assert links.desk_check("/desk/", f"case_assist={session}") == (None, None)
    assert _desk_check_ep("/desk/", f"case_assist={session}").status_code == 200


def test_proving_state_has_no_actions():
    _cleanup()
    _attempt("aa_1", status="proving", revision=3, current_handoff_id=None)
    store.insert_handoff("h_otp", "c_1", "otp", "code", None, None, None,
                         continuation="submit_value", attempt_id="aa_1")
    store.set_handoff_status("h_otp", "completed", answer="1")
    raw, _ = assist.mint_assist_token("h_otp")
    # Force a live session despite completed bound handoff + proving attempt.
    session = "sess_proving_test"
    store.q("UPDATE assist_tokens SET burned_at=?, session_hash=?, session_expires_at=? "
            "WHERE handoff_id=?", (now(), _hash(session), iso_in(1800), "h_otp"))
    view, _ = assist.resolve_view(raw, f"case_assist={session}")
    payload = assist.state_payload(view)
    assert payload["status"] == "proving"
    assert payload["revision"] == 3
    assert payload["allowed_actions"] == []
    assert "Verifying" in assist.render_page(view, raw)


def test_token_binding_rejects_foreign_session():
    _cleanup()
    _pending("h_bind_a", "otp", continuation="submit_value")
    _pending("h_bind_b", "otp", continuation="submit_value")
    raw_a, _ = assist.mint_assist_token("h_bind_a")
    raw_b, _ = assist.mint_assist_token("h_bind_b")
    sess_a, _ = assist.exchange(raw_a)
    assist.exchange(raw_b)  # burn B — path token is spent
    # Cookie for A must not open B's burned URL
    try:
        assist.resolve_view(raw_b, f"case_assist={sess_a}")
        assert False, "foreign session must not bind"
    except ApiError as e:
        assert e.status == 410, e
    # A's own burned URL + A's cookie still works
    view, set2 = assist.resolve_view(raw_a, f"case_assist={sess_a}")
    assert view["handoff"]["id"] == "h_bind_a" and set2 is None


def test_stale_revision_on_submit_conflicts():
    _cleanup()
    _pending("h_otp", "otp", continuation="submit_value", revision=1)
    raw, _ = assist.mint_assist_token("h_otp")
    session, _ = assist.exchange(raw)
    try:
        assist.submit_with_session(session, "123456", expected_revision=0)
        assert False, "stale revision should 409"
    except ApiError as e:
        assert e.status == 409 and e.code == "revision_conflict", e


def test_submit_page_stops_polling_once_form_submission_starts():
    assert 'addEventListener("submit"' in assist.ASSIST_JS
    assert "clearInterval(timer)" in assist.ASSIST_JS


def test_repeat_submit_on_terminal_attempt_renders_status_not_expired():
    _cleanup()
    _attempt("aa_term", status="awaiting_human", current_handoff_id="h_otp")
    _pending("h_otp", "otp", continuation="submit_value",
             attempt_id="aa_term", revision=1)
    raw, _ = assist.mint_assist_token("h_otp")
    session, _ = assist.exchange(raw)
    store.set_handoff_status("h_otp", "completed", answer=None)
    store.q("UPDATE auth_attempts SET status='authenticated', current_handoff_id=NULL "
            "WHERE id='aa_term'")

    import cased
    from fastapi.testclient import TestClient
    client = TestClient(cased.app, raise_server_exceptions=False)
    response = client.post(
        f"/assist/{raw}/submit",
        data={"value": "123456", "expected_revision": "1"},
        cookies={assist.COOKIE: session},
        headers={"Origin": "http://testserver"})
    assert response.status_code == 200, response.text
    assert "Signed in" in response.text
    assert "Link expired" not in response.text


def test_open_rejects_private_ip_and_http():
    _cleanup()
    _pending("h_wait", "device", continuation="wait_external", login_credential="github",
             domain="github.com")
    store.upsert_credential("c_1", "github", "u", "s", None, None, ["github.com"])
    raw, _ = assist.mint_assist_token("h_wait")
    session, _ = assist.exchange(raw)
    for bad in ("http://github.com/x",
                "https://127.0.0.1/x",
                "https://10.0.0.5/x",
                "https://169.254.1.1/x",
                "https://localhost/x",
                "https://user:pass@github.com/x"):
        try:
            assist.open_with_session(session, bad)
            assert False, f"should reject {bad}"
        except ApiError as e:
            assert e.status == 400, (bad, e)


def test_open_allows_listed_host_mocked_navigate():
    _cleanup()
    _pending("h_wait", "device", continuation="wait_external", login_credential="github",
             domain="github.com", revision=0)
    store.upsert_credential("c_1", "github", "u", "s", None, None, ["github.com"])
    store.q("UPDATE credentials SET verification_hosts=? WHERE computer_id=? AND name=?",
            (json.dumps(["github.com"]), "c_1", "github"))
    raw, _ = assist.mint_assist_token("h_wait")
    session, _ = assist.exchange(raw)
    import deskclient
    import lifecycle
    with mock.patch.object(lifecycle, "get_computer",
                           return_value={"id": "c_1", "name": "ava", "state": "running"}), \
         mock.patch.object(deskclient, "auth_navigate_verification",
                           return_value={"ok": True}) as nav:
        out = assist.open_with_session(session, "https://github.com/sessions/verified")
    assert out == {"ok": True}
    assert nav.called
    args, kwargs = nav.call_args
    assert args[1] == "https://github.com/sessions/verified"
    assert kwargs.get("domains") == ["github.com"]


def test_open_rejects_host_outside_allowlist():
    _cleanup()
    _pending("h_wait", "device", continuation="wait_external", login_credential="github")
    store.upsert_credential("c_1", "github", "u", "s", None, None, ["github.com"])
    raw, _ = assist.mint_assist_token("h_wait")
    session, _ = assist.exchange(raw)
    try:
        assist.open_with_session(session, "https://evil.example/phish")
        assert False, "off-allowlist must fail"
    except ApiError as e:
        assert e.status == 400 and e.code == "domain_mismatch", e


def test_prune_expired_assist_tokens_keeps_live_session():
    _cleanup()
    _pending("h_otp", "otp")
    _pending("h_dead", "otp")
    _pending("h_cap", "captcha")
    store.insert_assist_token("h_dead", "hash_dead", "2020-01-01T00:00:00Z")
    store.insert_assist_token("h_otp", "hash_live", "2099-01-01T00:00:00Z")
    store.insert_assist_token("h_cap", "hash_sess", "2020-01-01T00:00:00Z")
    store.q("UPDATE assist_tokens SET burned_at=?, session_hash=?, session_expires_at=? "
            "WHERE handoff_id=?",
            (now(), "sess_live", "2099-01-01T00:00:00Z", "h_cap"))
    store.prune_expired_assist_tokens()
    assert store.get_assist_by_handoff("h_dead") is None
    assert store.get_assist_by_handoff("h_otp") is not None
    assert store.get_assist_by_handoff("h_cap") is not None


def test_check_same_origin():
    class R:
        def __init__(self, headers):
            self.headers = headers
    assert assist.check_same_origin(R({"host": "acme.case.example",
                                       "origin": "https://acme.case.example"}))
    assert not assist.check_same_origin(R({"host": "acme.case.example",
                                           "origin": "https://evil.example"}))
    assert not assist.check_same_origin(R({"host": "acme.case.example"}))
    assert assist.check_same_origin(R({
        "host": "acme.case.example",
        "referer": "https://acme.case.example/assist/x",
    }))


def test_first_click_of_fresh_link_sets_session_cookie_over_http():
    # Route-level: the emailed link's FIRST hit burns the token and must answer
    # 200 with the case_assist cookie — a failure here strands the human on a
    # burned token (the exchange is not retryable).
    _cleanup()
    _pending("h_otp", "otp", continuation="submit_value", revision=1)
    raw, _ = assist.mint_assist_token("h_otp")

    from fastapi.testclient import TestClient
    client = TestClient(cased.app, raise_server_exceptions=False)
    r = client.get(f"/assist/{raw}")
    assert r.status_code == 200, r.text
    set_cookie = r.headers.get("set-cookie") or ""
    assert assist.COOKIE + "=" in set_cookie, set_cookie
    assert f"Max-Age={assist.SESSION_TTL_S}" in set_cookie, set_cookie
    # Follow-up with the cookie (passed by hand — Secure cookies don't survive
    # the TestClient's http:// jar): 200, no new Set-Cookie.
    sess = set_cookie.split(assist.COOKIE + "=", 1)[1].split(";", 1)[0]
    r2 = client.get(f"/assist/{raw}", cookies={assist.COOKIE: sess})
    assert r2.status_code == 200, r2.text
    assert assist.COOKIE + "=" not in (r2.headers.get("set-cookie") or "")
    # Burned token, no cookie → 410.
    bare = TestClient(cased.app, raise_server_exceptions=False)
    assert bare.get(f"/assist/{raw}").status_code == 410


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("PASS")
