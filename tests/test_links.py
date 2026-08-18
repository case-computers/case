# SPDX-License-Identifier: MIT
"""Human link tokens: mint/expiry/burn + the /desk forward_auth check.
Run: .venv/bin/python tests/test_links.py"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "control-plane"))
# assignment, NOT setdefault: _cleanup() truncates the links table, and an inherited
# CASE_HOME (exported in a dev shell, or ~/.case/env) would point that at the real
# vault and kill every outstanding partner link.
os.environ["CASE_HOME"] = "/tmp/case-links-test"
import links  # noqa: E402
from store import store  # noqa: E402


def _cleanup():
    store.q("DELETE FROM links")


def _running_computer(cid):
    store.q("DELETE FROM computers WHERE id=?", (cid,))
    store.insert_computer(cid, "ava", "case-desk:0.1", 1, 2048, "vol", "tok")
    store.set_state(cid, "running")


def _desk_check_ep(uri, cookie):
    from starlette.requests import Request
    import cased
    scope = {"type": "http", "headers": [
        (b"x-forwarded-uri", uri.encode()),
        (b"cookie", cookie.encode()),
    ], "method": "GET", "path": "/v1/desk/check", "query_string": b""}
    return cased.desk_check_ep(Request(scope))


def test_mint_fill_returns_fill_path_and_stores_row():
    _cleanup()
    out = links.mint("c_1", "fill")
    assert out["path"] == f"/fill/{out['token']}", out
    assert store.get_link(out["token"])["kind"] == "fill"


def test_mint_vnc_returns_novnc_entry_path():
    _cleanup()
    out = links.mint("c_1", "vnc")
    assert out["path"].startswith("/desk/vnc.html?token="), out
    assert "path=desk/websockify" in out["path"], out


def test_fill_token_single_use():
    _cleanup()
    t = links.mint("c_1", "fill")["token"]
    assert links.valid(t, "fill") is not None
    store.burn_link(t)
    assert links.valid(t, "fill") is None


def test_expired_token_invalid():
    _cleanup()
    t = links.mint("c_1", "fill", ttl_s=0)["token"]
    # ttl 0 -> expires_at == now-ish; valid() uses <=, so it is already dead
    assert links.valid(t, "fill") is None


def test_kind_mismatch_invalid():
    _cleanup()
    t = links.mint("c_1", "fill")["token"]
    assert links.valid(t, "vnc") is None


def test_desk_check_query_token_sets_cookie():
    _cleanup()
    t = links.mint("c_1", "vnc")["token"]
    ok, cookie = links.desk_check(f"/desk/vnc.html?token={t}&autoconnect=1", "")
    assert ok and cookie == t, (ok, cookie)


def test_desk_check_cookie_alone_passes_without_resetting():
    _cleanup()
    t = links.mint("c_1", "vnc")["token"]
    ok, cookie = links.desk_check("/desk/app/webutil.js", f"other=1; case_desk={t}")
    assert ok and cookie is None, (ok, cookie)


def test_desk_check_vnc_token_is_multi_use():
    _cleanup()
    t = links.mint("c_1", "vnc")["token"]
    for _ in range(3):
        assert links.desk_check(f"/x?token={t}", "")[0]


def test_desk_check_garbage_rejected():
    _cleanup()
    assert links.desk_check("/desk/vnc.html?token=nope", "case_desk=nope") == (None, None)


def test_desk_check_returns_the_row_so_callers_can_bind_the_computer():
    _cleanup()
    t = links.mint("c_bound", "vnc")["token"]
    row, _ = links.desk_check(f"/x?token={t}", "")
    assert row["computer_id"] == "c_bound", dict(row)


def test_burn_link_is_compare_and_set():
    _cleanup()
    t = links.mint("c_1", "fill")["token"]
    assert store.burn_link(t) == 1        # the write decides single-use…
    assert store.burn_link(t) == 0        # …so a racing second submit loses


def test_burn_all_links_kills_every_live_token():
    _cleanup()
    a, b = links.mint("c_1", "fill")["token"], links.mint("c_1", "vnc")["token"]
    assert store.burn_all_links() == 2
    assert links.valid(a, "fill") is None and links.valid(b, "vnc") is None


def test_seconds_left_never_outlives_the_token():
    _cleanup()
    row = store.get_link(links.mint("c_1", "vnc", ttl_s=120)["token"])
    assert 110 <= links.seconds_left(row) <= 120, links.seconds_left(row)
    dead = store.get_link(links.mint("c_1", "vnc", ttl_s=0)["token"])
    assert links.seconds_left(dead) == 0


def test_normalize_domain_takes_what_humans_actually_type():
    for typed, want in [("https://mail.google.com/inbox", "mail.google.com"),
                        ("WWW.Gmail.com", "gmail.com"),
                        ("  x.co:8443/path?q=1 ", "x.co"),
                        ("user@gmail.com", "gmail.com")]:
        assert links.normalize_domain(typed) == want, typed


def test_normalize_domain_rejects_what_would_break_the_vault():
    # a name with a slash cannot be addressed by DELETE /credentials/{name} — an
    # undeletable secret. Bare words never match a host either.
    for junk in ["", "localhost", "not a domain", "a/b.com", "-bad.com", "http://"]:
        assert links.normalize_domain(junk) is None, junk


def test_strip_token_drops_only_the_token():
    got = links.strip_token("/desk/vnc.html?token=abc&autoconnect=1&path=desk/websockify")
    assert got == "/desk/vnc.html?autoconnect=1&path=desk%2Fwebsockify", got


def test_strip_token_refuses_a_foreign_path():
    # a Location starting // is protocol-relative — an open redirect if echoed raw
    assert links.strip_token("//evil.example/x?token=abc") == "/desk/vnc.html"


def test_fill_token_never_opens_the_desk():
    _cleanup()
    t = links.mint("c_1", "fill")["token"]
    assert links.desk_check(f"/x?token={t}", "")[0] is None


def test_assist_cookie_opens_desk_but_not_fill_or_console():
    # Assist is a parallel capability: HttpOnly session cookie scoped to a live
    # handoff. It must unlock /desk for that computer and nothing else.
    import assist
    store.q("DELETE FROM assist_tokens")
    store.delete_handoff("h_link_assist")
    store.insert_handoff("h_link_assist", "c_assist", "captcha", "solve", None, None,
                         continuation="verify_page")
    raw, _ = assist.mint_assist_token("h_link_assist")
    session, _ = assist.exchange(raw)
    _running_computer("c_assist")

    assert links.desk_check("/desk/vnc.html", f"case_assist={session}") == (None, None)
    resp = _desk_check_ep("/desk/vnc.html", f"case_assist={session}")
    assert resp.status_code == 200, resp.status_code

    # negatives: assist session is not a fill/vnc/console link token
    assert links.valid(session, "fill") is None
    assert links.valid(session, "vnc") is None
    assert links.console_check(f"Link {session}") is None
    # a fill token still cannot open the desk (unchanged)
    fill = links.mint("c_assist", "fill")["token"]
    assert links.desk_check(f"/x?token={fill}", "")[0] is None
    # assist cookie still authorizes desk via desk_check_ep even when fill token is in query
    assert _desk_check_ep(f"/x?token={fill}", f"case_assist={session}").status_code == 200

    store.set_handoff_status("h_link_assist", "completed")
    assert links.desk_check("/desk/", f"case_assist={session}") == (None, None)
    assert _desk_check_ep("/desk/", f"case_assist={session}").status_code == 401
    store.delete_handoff("h_link_assist")
    store.q("DELETE FROM assist_tokens WHERE handoff_id=?", ("h_link_assist",))


def test_validate_assist_open_url_https_public_only():
    from errors import ApiError
    assert links.validate_assist_open_url("https://github.com/path") == "github.com"
    assert links.validate_assist_open_url("https://mail.google.com/") == "mail.google.com"
    for bad in ["http://github.com/", "https://127.0.0.1/", "https://10.1.2.3/x",
                "https://192.168.0.1/", "https://169.254.9.9/", "https://localhost/x",
                "https://user:pass@github.com/", "https://[::1]/", "not-a-url", ""]:
        try:
            links.validate_assist_open_url(bad)
            assert False, bad
        except ApiError as e:
            assert e.status == 400, (bad, e)


def test_host_allowed_exact_and_subdomain():
    assert links.host_allowed("github.com", ["github.com"])
    assert links.host_allowed("api.github.com", ["github.com"])
    assert not links.host_allowed("evil.com", ["github.com"])
    assert not links.host_allowed("notgithub.com", ["github.com"])


def test_verification_allowlist_prefers_verification_hosts():
    import json
    store.q("DELETE FROM credentials WHERE computer_id=?", ("c_allow",))
    store.upsert_credential("c_allow", "site", "u", "s", None, None, ["example.com"])
    assert links.verification_allowlist("c_allow", "site") == ["example.com"]
    store.q("UPDATE credentials SET verification_hosts=? WHERE computer_id=? AND name=?",
            (json.dumps(["verify.example.com"]), "c_allow", "site"))
    assert links.verification_allowlist("c_allow", "site") == ["verify.example.com"]
    store.q("DELETE FROM credentials WHERE computer_id=?", ("c_allow",))


def test_console_return_url_pins_box_host_and_drops_capabilities():
    # After Save, the human needs a way home that does not depend on the burned
    # fill token and does not put the console Link token into a new URL.
    url = links.console_return_url("demo.case.example",
                                   origin="https://console.example")
    assert url == ("https://console.example/console"
                   "?box=demo.case.example#credentials"), url
    assert "#t=" not in url and "token" not in url.lower()
    # reject anything the console page itself would refuse
    for bad in ["evil.example", "demo.case.example.evil.com",
                "https://demo.case.example", "", "localhost"]:
        assert links.console_return_url(bad) == "", bad
    assert links.console_return_url("demo.case.example",
                                    origin="http://insecure.example") == ""


def test_prune_expired_links_keeps_live():
    _cleanup()
    dead = links.mint("c_1", "fill", ttl_s=0)["token"]
    live = links.mint("c_1", "fill")["token"]
    store.prune_expired_links()
    assert store.get_link(dead) is None
    assert store.get_link(live) is not None


def test_with_console_back_injects_button_or_strips_placeholder():
    done = links.with_console_back(links.DONE_HTML, "demo.case.example",
                                   origin="https://console.example")
    assert "← Back to console" in done
    assert 'href="https://console.example/console?box=demo.case.example#credentials"' in done
    assert "{back}" not in done
    # no usable host → no broken link, and the placeholder must not leak into HTML
    gone = links.with_console_back(links.GONE_HTML, "localhost")
    assert "{back}" not in gone
    assert "Back to console" not in gone
    assert "Saved" in links.with_console_back(links.DONE_HTML, "x.case.example",
                                              origin="https://console.example")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("PASS")
