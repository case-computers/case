# SPDX-License-Identifier: MIT
"""Console door + console-facing read routes.
Run: .venv/bin/python tests/test_console.py"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "control-plane"))
# assignment, NOT setdefault — these tests DELETE FROM real tables, and an inherited
# CASE_HOME would point them at a live box's vault. Same reasoning as tests/test_links.py.
os.environ["CASE_HOME"] = "/tmp/case-console-test"
import cased  # noqa: E402
import links  # noqa: E402
from store import store  # noqa: E402


# ---- runs: the ACTIVITY feed ----

def test_run_json_hides_the_host_path_and_reports_a_screenshot_flag():
    store.q("DELETE FROM runs")
    store.insert_run("run_a", "sch_1", "c_1", "2026-07-27T09:00:00Z",
                     "2026-07-27T09:05:00Z", 0, "did the thing",
                     "/home/case/.case/runs/run_a.png", "ok")
    j = cased.run_json(store.get_run("run_a"))
    assert j["has_screenshot"] is True, j
    # the host filesystem path is operator detail; the console gets a flag, not a path
    assert "artifact_path" not in j, j
    assert j["status"] == "ok" and j["id"] == "run_a", j


def test_prune_old_runs_keeps_newest_and_returns_artifact_paths():
    store.q("DELETE FROM runs")
    for i in range(5):
        store.insert_run(f"run_{i}", "sch_1", "c_1",
                         f"2026-07-0{i + 1}T09:00:00Z",
                         f"2026-07-0{i + 1}T09:05:00Z",
                         0, "x", f"/tmp/run_{i}.png", "ok")
    paths = store.prune_old_runs(keep=3)
    remaining = {r["id"] for r in store.list_all_runs(limit=50)}
    assert remaining == {"run_2", "run_3", "run_4"}, remaining
    assert set(paths) == {"/tmp/run_0.png", "/tmp/run_1.png"}, paths


def test_unlink_run_artifacts_only_under_runs_dir():
    from config import RUNS_DIR
    os.makedirs(RUNS_DIR, exist_ok=True)
    inside = os.path.join(RUNS_DIR, "prune_me.png")
    outside = os.path.join("/tmp", "case-prune-outside.png")
    open(inside, "wb").write(b"in")
    open(outside, "wb").write(b"out")
    try:
        cased.unlink_run_artifacts([inside, outside, None, "/etc/passwd"])
        assert not os.path.exists(inside)
        assert os.path.exists(outside)
    finally:
        if os.path.exists(outside):
            os.unlink(outside)
        if os.path.exists(inside):
            os.unlink(inside)


def test_prune_old_audit_files_keeps_today_and_recent():
    import tempfile
    from datetime import datetime, timedelta, timezone
    d = tempfile.mkdtemp(prefix="case-audit-prune-")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    recent = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%d")
    old = "2020-01-01"
    for name in (today, recent, old, "not-a-date.jsonl"):
        open(os.path.join(d, name if name.endswith(".jsonl") else name + ".jsonl"), "w").write("{}\n")
    try:
        cased.prune_old_audit_files(d, keep_days=30)
        assert os.path.exists(os.path.join(d, today + ".jsonl"))
        assert os.path.exists(os.path.join(d, recent + ".jsonl"))
        assert not os.path.exists(os.path.join(d, old + ".jsonl"))
        assert os.path.exists(os.path.join(d, "not-a-date.jsonl"))
    finally:
        for fn in os.listdir(d):
            os.unlink(os.path.join(d, fn))
        os.rmdir(d)


def test_run_json_without_an_artifact():
    store.q("DELETE FROM runs")
    store.insert_run("run_b", "sch_1", "c_1", "2026-07-27T09:00:00Z",
                     "2026-07-27T09:05:00Z", 1, "broke", None, "fail")
    assert cased.run_json(store.get_run("run_b"))["has_screenshot"] is False


def test_screenshot_serves_only_from_the_runs_dir():
    from config import RUNS_DIR
    from errors import ApiError
    os.makedirs(RUNS_DIR, exist_ok=True)
    good = os.path.join(RUNS_DIR, "run_shot.png")
    with open(good, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
    store.q("DELETE FROM runs")
    store.insert_run("run_shot", "sch_1", "c_1", "2026-07-27T09:00:00Z",
                     "2026-07-27T09:05:00Z", 0, "", good, "ok")
    assert cased.run_screenshot("run_shot").body == b"\x89PNG\r\n\x1a\n"

    # a row whose artifact_path escapes RUNS_DIR is not served, however it got there —
    # the DB is not a trust boundary for the filesystem
    store.insert_run("run_esc", "sch_1", "c_1", "2026-07-27T09:00:00Z",
                     "2026-07-27T09:05:00Z", 0, "", "/etc/passwd", "ok")
    for rid in ("run_esc", "run_missing"):
        try:
            cased.run_screenshot(rid)
            assert False, f"{rid} should not have been served"
        except ApiError as e:
            assert e.status == 404, (rid, e.status)


# ---- credentials: HEALTH and LAST VERIFIED ----
# The real method is upsert_credential (store.py). It takes PLAINTEXT strings and a
# Python list for domains, and does the Fernet encryption and json.dumps itself.
# There is no `add_credential`, and passing pre-encrypted bytes or a JSON string
# double-encodes.

def test_credential_json_reports_last_login_result():
    store.q("DELETE FROM credentials")
    store.upsert_credential("c_1", "chase.com", "me@x.com", "pw", None, None, ["chase.com"])
    j = cased.credential_json(store.get_credential("c_1", "chase.com"))
    # never used yet: no verdict, and definitely not a fake "healthy"
    assert j["last_verified_at"] is None and j["last_status"] is None, j

    store.record_credential_result("c_1", "chase.com", "ok")
    j = cased.credential_json(store.get_credential("c_1", "chase.com"))
    assert j["last_status"] == "ok" and j["last_verified_at"], j


def test_a_failed_login_is_recorded_as_failure_not_silence():
    store.q("DELETE FROM credentials")
    store.upsert_credential("c_1", "linkedin.com", "me@x.com", "pw", None, None,
                            ["linkedin.com"])
    store.record_credential_result("c_1", "linkedin.com", "failed")
    assert cased.credential_json(store.get_credential("c_1", "linkedin.com"))["last_status"] \
        == "failed"


def test_a_recovered_challenge_does_not_stay_marked_needs_you():
    # the bug this test exists to prevent: login returns "challenge", the human answers
    # the 2FA code, the session resumes fine — and the vault still says NEEDS YOU forever
    store.q("DELETE FROM credentials")
    store.upsert_credential("c_1", "chase.com", "me@x.com", "pw", "SEED", None, ["chase.com"])
    store.record_credential_result("c_1", "chase.com", "challenge")
    store.record_credential_result("c_1", "chase.com", "ok")      # what _resume_login must do
    assert cased.credential_json(store.get_credential("c_1", "chase.com"))["last_status"] == "ok"


def _resume_with(deskd_answer, hid):
    """Drive handoffs._resume_and_finish against a stubbed deskd, on a login handoff
    with no attempt_id (the legacy path where LOGIN_CTX owns the credential result)."""
    import handoffs
    store.delete_handoff(hid)
    store.insert_handoff(hid, "c_1", "otp", "enter code", None, "chase.com",
                         "chase.com", continuation="submit_value")
    ctx = {"computer_id": "c_1", "credential": "chase.com"}
    handoffs.LOGIN_CTX[hid] = ctx
    old = (handoffs.get_computer, handoffs.desk_json, handoffs.emit)
    try:
        handoffs.get_computer = lambda cid: {"id": cid, "name": "ava"}
        handoffs.desk_json = lambda *a, **k: deskd_answer
        handoffs.emit = lambda *a, **k: None
        handoffs._resume_and_finish(hid, ctx, "123456")
    finally:
        (handoffs.get_computer, handoffs.desk_json, handoffs.emit) = old
        handoffs.LOGIN_CTX.pop(hid, None)
        store.delete_handoff(hid)


def test_resume_login_records_the_real_outcome():
    # the seam itself, not just the store method: whatever deskd answers on /login/resume
    # has to reach the vault, or the console's HEALTH column is decorative
    store.q("DELETE FROM credentials")
    store.upsert_credential("c_1", "chase.com", "me@x.com", "pw", "SEED", None, ["chase.com"])
    store.record_credential_result("c_1", "chase.com", "challenge")
    _resume_with({"status": "success"}, "h_resume_ok")
    assert store.get_credential("c_1", "chase.com")["last_status"] == "success"


def test_resume_login_does_not_claim_success_on_a_failed_verify():
    # the half that matters: a soft failure leaves the handoff retryable and must never
    # write success into the vault, or HEALTH lies in the direction that costs you.
    store.q("DELETE FROM credentials")
    store.upsert_credential("c_1", "chase.com", "me@x.com", "pw", "SEED", None, ["chase.com"])
    store.record_credential_result("c_1", "chase.com", "challenge")
    _resume_with({"status": "failed", "reason": "still challenged"}, "h_resume_bad")
    assert store.get_credential("c_1", "chase.com")["last_status"] == "challenge"


def test_recording_never_leaks_the_secret():
    store.q("DELETE FROM credentials")
    store.upsert_credential("c_1", "chase.com", "me@x.com", "pw", "SEED", None, ["chase.com"])
    store.record_credential_result("c_1", "chase.com", "ok")
    j = cased.credential_json(store.get_credential("c_1", "chase.com"))
    assert "secret" not in j and "totp_seed" not in j, j
    assert j["has_totp"] is True, j


# ---- the COMPUTERS and CREDENTIALS tabs ----

def test_computer_json_carries_task_count_and_next_run():
    import lifecycle
    store.q("DELETE FROM schedules")
    store.q("DELETE FROM computers")
    store.insert_computer("c_1", "ava", "case-desk:0.1", 1, 2048, "vol", "tok")
    j = lifecycle.computer_json(store.get_computer("c_1"))
    assert j["tasks"] == 0 and j["next_run_at"] is None, j

    store.insert_schedule("sch_1", "c_1", "nightly", "do the thing", "daily",
                          "06:00", 300, "2026-08-01T06:00:00Z")
    store.insert_schedule("sch_2", "c_1", "hourly", "poll", "interval",
                          "3600", 0, "2026-07-27T12:00:00Z")
    j = lifecycle.computer_json(store.get_computer("c_1"))
    # NEXT RUN shows the soonest, not the first inserted
    assert j["tasks"] == 2 and j["next_run_at"] == "2026-07-27T12:00:00Z", j


def test_list_all_credentials_spans_computers_and_names_the_owner():
    store.q("DELETE FROM credentials")
    store.upsert_credential("c_1", "chase.com", "a@x.com", "pw", None, None, ["chase.com"])
    store.upsert_credential("c_2", "coupa.com", "b@x.com", "pw", None, None, ["coupa.com"])
    owners = {r["name"]: r["computer_id"] for r in store.list_all_credentials()}
    assert owners == {"chase.com": "c_1", "coupa.com": "c_2"}, owners


def test_the_account_wide_credential_list_still_hides_secrets():
    store.q("DELETE FROM credentials")
    store.q("DELETE FROM computers")
    store.insert_computer("c_1", "ava", "case-desk:0.1", 1, 2048, "vol", "tok")
    store.upsert_credential("c_1", "chase.com", "a@x.com", "pw", "SEED", None, ["chase.com"])
    rows = cased.list_all_credentials()["credentials"]
    assert [r["computer_name"] for r in rows] == ["ava"], rows
    assert not any(k in rows[0] for k in ("secret", "totp_seed")), rows[0]


# ---- the console link kind ----

def test_console_token_is_its_own_kind():
    store.q("DELETE FROM links")
    t = links.mint(None, "console")["token"]
    # a console token must not open the desk, and a desk token must not open the console
    assert links.desk_check(f"/x?token={t}", "")[0] is None
    v = links.mint("c_1", "vnc")["token"]
    assert links.console_check(f"Link {v}") is None


def test_console_check_accepts_the_link_scheme_only():
    store.q("DELETE FROM links")
    t = links.mint(None, "console")["token"]
    assert links.console_check(f"Link {t}") is not None
    assert links.console_check(t) is None                 # bare token, no scheme
    assert links.console_check(f"Bearer {t}") is None     # not the agent's scheme
    assert links.console_check("") is None
    assert links.console_check(None) is None


def test_console_token_is_multi_use_but_dies_on_revocation():
    store.q("DELETE FROM links")
    t = links.mint(None, "console")["token"]
    for _ in range(3):
        assert links.console_check(f"Link {t}") is not None
    store.burn_all_links()
    assert links.console_check(f"Link {t}") is None


def test_console_token_slides_forward_on_use():
    store.q("DELETE FROM links")
    t = links.mint(None, "console", ttl_s=60)["token"]
    was = store.get_link(t)["expires_at"]
    # a bookmark someone opens daily must never go stale under them
    assert links.console_check(f"Link {t}") is not None
    assert store.get_link(t)["expires_at"] > was, (was, store.get_link(t)["expires_at"])


def test_a_revoked_console_token_is_not_slid_forward():
    store.q("DELETE FROM links")
    t = links.mint(None, "console", ttl_s=60)["token"]
    was = store.get_link(t)["expires_at"]
    store.burn_all_links()
    store.slide_link(t, "2099-01-01T00:00:00Z")   # a request already in flight
    assert store.get_link(t)["expires_at"] == was, store.get_link(t)["expires_at"]


def test_a_dead_console_token_does_not_resurrect_itself():
    store.q("DELETE FROM links")
    t = links.mint(None, "console", ttl_s=0)["token"]   # dead on arrival
    assert links.console_check(f"Link {t}") is None
    # sliding happens only after valid() passes — an expired link stays expired
    assert store.get_link(t)["expires_at"] <= links.now(), store.get_link(t)["expires_at"]


def test_console_mint_returns_no_path_because_the_page_is_not_on_the_box():
    store.q("DELETE FROM links")
    out = links.mint(None, "console")
    # the HTML is on Vercel; a box-relative path would send a browser to a door that
    # answers 401 JSON, because a navigation carries no Authorization header
    assert out.get("path") is None, out
    assert out["token"] and out["expires_at"], out


def test_console_ttl_is_not_clamped_to_a_day():
    store.q("DELETE FROM links")
    a = store.get_link(links.mint(None, "console")["token"])["expires_at"]
    b = store.get_link(links.mint("c_1", "vnc")["token"])["expires_at"]
    # the old global min(ttl, 86400) silently turned 30 days into 1
    assert a > b, (a, b)


def test_console_is_not_mintable_per_computer():
    # a console token IS allowed to reach POST /v1/computers/{cid}/links (that is how
    # DESK and ADD LOGIN work). If that route minted console kinds, the token could
    # renew its own access forever.
    from errors import ApiError
    try:
        cased.mint_link("c_1", {"kind": "console"})
        assert False, "computer-scoped mint must reject the console kind"
    except ApiError as e:
        assert e.status in (400, 404), e.status
    try:
        cased.mint_box_link({"kind": "vnc"})
        assert False, "box-scoped mint must reject everything but console"
    except ApiError as e:
        assert e.status == 400, e.status


# ---- the door allowlist: the reason a browser token is not an agent token ----
# These drive the REAL middleware through the REAL app, not a copy of its predicate.
# TestClient is used without `with`, so cased's startup hook (docker reconcile, the
# sweeper and blocker threads) never runs.

DOOR = {"X-Case-Door": "console"}


def _client():
    from fastapi.testclient import TestClient
    return TestClient(cased.app, raise_server_exceptions=False)


def _blocked(resp):
    """The door's refusal, told apart from a handler's own 404 by its exact body.
    A HEAD response carries no body at all, so there status is all there is — which is
    enough here, because the allowed HEAD below answers 200, not 404."""
    if resp.status_code != 404:
        return False
    return not resp.content or resp.json() == cased.DOOR_BLOCKED


def test_the_console_door_cannot_reach_anything_dangerous():
    c = _client()
    for method, path in [
        ("POST",   "/v1/computers/c_1/exec"),        # arbitrary shell
        ("POST",   "/v1/computers/c_1/eval"),        # arbitrary JS in the browser
        ("GET",    "/v1/computers/c_1/files"),       # read any file
        ("PUT",    "/v1/computers/c_1/files"),       # write any file
        ("POST",   "/v1/computers/c_1/login"),
        ("POST",   "/v1/computers/c_1/capture"),
        ("POST",   "/v1/computers"),                 # create, and bill, a computer
        ("POST",   "/v1/computers/c_1/credentials"), # plaintext secret; /fill exists for this
        ("DELETE", "/v1/computers/c_1/credentials/x"),
        ("DELETE", "/v1/links"),                     # revoke everyone else's links
        ("POST",   "/v1/links"),                     # mint itself a fresh console token
        ("GET",    "/v1/schedules/sch_1/runs"),      # leaks artifact_path (host paths)
        ("POST",   "/v1/computers/c_1/schedules"),
        ("GET",    "/v1/computers/c_1"),
        ("POST",   "/v1/computers/c_1/sleep"),
        ("GET",    "/v1/console/check"),             # no self-issued auth checks
        ("GET",    "/health"),
    ]:
        r = c.request(method, path, headers=DOOR, json={})
        assert _blocked(r), f"console door must not reach {method} {path}: {r.status_code}"


def test_the_console_door_can_reach_what_the_dashboard_needs():
    # The lifecycle collaborators are stubbed for the duration. Without this the test
    # runs the REAL do_wake and destroy: an asleep row would have this "Docker-free"
    # unit test starting a container, and the link route would mint real rows. The
    # middleware — the thing under test — is still fully in the loop.
    import lifecycle
    old = (lifecycle.do_wake, lifecycle.destroy, lifecycle.get_computer)
    try:
        lifecycle.do_wake = lambda cid: None
        lifecycle.destroy = lambda cid: None
        lifecycle.get_computer = lambda cid: {"id": cid, "name": "ava", "state": "asleep",
                                              "image": "i", "created_at": "", "cpus": 1,
                                              "last_active_at": "", "ram_mb": 1,
                                              "volume": "v", "vnc_port": 0}
        c = _client()
        for method, path, body in [
            ("GET",    "/v1/computers", None),
            ("GET",    "/v1/runs", None),
            ("GET",    "/v1/runs/run_a/screenshot", None),
            ("GET",    "/v1/credentials", None),
            ("GET",    "/v1/handoffs", None),
            ("POST",   "/v1/handoffs/h_1/answer", {"value": "x"}),
            ("POST",   "/v1/computers/c_1/wake", None),
            ("POST",   "/v1/computers/c_1/links", {"kind": "fill"}),
            ("DELETE", "/v1/computers/c_1", {"name": "ava"}),
            ("GET",    "/v1/connect", None),
        ]:
            r = c.request(method, path, headers=DOOR, json=body)
            assert not _blocked(r), f"console door needs {method} {path}"
            # a 500 also satisfies "not blocked", which would make this test vacuous
            assert r.status_code < 500, f"{method} {path} -> {r.status_code} {r.text[:200]}"
    finally:
        (lifecycle.do_wake, lifecycle.destroy, lifecycle.get_computer) = old


def test_deleting_a_computer_from_a_browser_needs_its_name():
    # the client-side confirm() is not a control: one mis-aimed click on a link that was
    # forwarded or left open must not be able to destroy a volume
    import lifecycle
    destroyed = []
    old = (lifecycle.destroy, lifecycle.get_computer)
    try:
        lifecycle.destroy = lambda cid: destroyed.append(cid)
        lifecycle.get_computer = lambda cid: {"id": cid, "name": "ava"}
        c = _client()
        for body in (None, {}, {"name": ""}, {"name": "kai"}):
            r = c.request("DELETE", "/v1/computers/c_1", headers=DOOR, json=body)
            assert r.status_code == 400, (body, r.status_code)
            assert r.json()["error"]["code"] == "confirm_name", r.json()
        assert destroyed == [], destroyed

        assert c.request("DELETE", "/v1/computers/c_1", headers=DOOR,
                         json={"name": "ava"}).status_code == 204
        assert destroyed == ["c_1"], destroyed

        # loopback is unchanged — bin/case rm sends no body and must keep working
        assert c.request("DELETE", "/v1/computers/c_1").status_code == 204
        assert destroyed == ["c_1", "c_1"], destroyed
    finally:
        (lifecycle.destroy, lifecycle.get_computer) = old


def test_a_reader_that_may_get_may_also_head():
    # Starlette answers HEAD from every GET route; a method-exact allowlist would 404 it
    assert not _blocked(_client().request("HEAD", "/v1/computers", headers=DOOR))
    assert _blocked(_client().request("HEAD", "/v1/computers/c_1/files", headers=DOOR))


def test_console_links_refuse_a_ttl_they_would_only_discard():
    from errors import ApiError
    try:
        cased.mint_box_link({"kind": "console", "ttl_s": 60})
        assert False, "ttl_s is discarded by the sliding expiry; it must not be accepted"
    except ApiError as e:
        assert e.status == 400, e.status


def test_the_handoff_list_does_not_ship_screenshots():
    import handoffs as ho
    store.q("DELETE FROM handoffs")
    store.insert_handoff("h_shot", "c_1", "otp", "code?", "QUl0aXNhUE5H", "chase.com",
                         "chase.com")
    listed = ho.list_handoffs("pending")
    # the console polls this every 30s and reads four scalars; a full-display PNG as
    # base64 on every poll is bytes nobody looks at
    assert "screenshot_png_b64" not in listed[0], listed[0]
    assert listed[0]["domain"] == "chase.com", listed[0]
    # ...and it is still reachable one at a time
    assert ho.get_handoff("h_shot")["screenshot_png_b64"] == "QUl0aXNhUE5H"
    store.q("DELETE FROM handoffs")


def test_the_guard_does_not_touch_loopback_or_the_agent():
    # no door header = cased's own callers and case-mcp, both unchanged
    r = _client().post("/v1/computers/c_1/exec", json={"command": "id"})
    assert not _blocked(r), r.status_code


def test_a_browser_cannot_talk_itself_past_the_guard():
    # the header is the whole policy input, so check the obvious forgeries. Caddy's
    # header_up REPLACES the client value, so on a real box none of these arrive.
    c = _client()
    for h in ({"X-Case-Door": "Console"}, {"X-Case-Door": "console "},
              {"X-Case-Door": "console\tconsole"}, {"X-Case-Door": ""}):
        r = c.post("/v1/computers/c_1/exec", headers=h, json={"command": "id"})
        assert not _blocked(r), h      # not the door's value -> not the door's policy


def test_a_blocked_request_is_still_audited():
    from config import AUDIT_DIR
    import time as _time
    log_path = os.path.join(AUDIT_DIR, _time.strftime("%Y-%m-%d") + ".jsonl")
    before = os.path.getsize(log_path) if os.path.exists(log_path) else 0
    _client().post("/v1/computers/c_1/exec", headers=DOOR, json={"command": "id"})
    with open(log_path) as f:
        f.seek(before)
        written = f.read()
    # audit_mw must remain OUTSIDE the guard: a refused console request is exactly the
    # one worth a record. Declaring the guard last would silently drop it.
    assert '"/v1/computers/c_1/exec"' in written and '"status": 404' in written, written


# ---- Connect: one-shot MCP paste reveal + rotate ----

def _clear_box_meta():
    store.q("DELETE FROM box_meta")


def test_mcp_token_reveal_is_one_shot():
    _clear_box_meta()
    store.mcp_token_put("cs_" + "a" * 32, host="box-a.case.example",
                        origin="https://console.example")
    tok, seen = store.mcp_token_take()
    assert tok == "cs_" + "a" * 32 and seen is None
    tok2, seen2 = store.mcp_token_take()
    assert tok2 is None and seen2, (tok2, seen2)
    st = store.mcp_token_status()
    assert st["has_pending"] is False and st["host"] == "box-a.case.example"


def test_burn_all_links_can_spare_the_caller():
    store.q("DELETE FROM links")
    keep = links.mint(None, "console")["token"]
    gone = links.mint("c_1", "fill")["token"]
    assert store.burn_all_links(except_token=keep) == 1
    assert store.get_link(keep)["used_at"] is None
    assert store.get_link(gone)["used_at"] is not None


def test_the_console_door_can_reach_connect_and_rotate():
    c = _client()
    assert not _blocked(c.request("GET", "/v1/connect", headers=DOOR))
    assert not _blocked(c.request("POST", "/v1/mcp/rotate", headers=DOOR,
                                  json={"confirm": "rotate"}))
    # seed stays loopback — console must not deposit an arbitrary bearer
    assert _blocked(c.request("POST", "/v1/mcp/seed", headers=DOOR,
                              json={"token": "cs_" + "b" * 32,
                                    "host": "x.case.example"}))


def test_connect_endpoint_consumes_the_pending_token():
    _clear_box_meta()
    tok = "cs_" + "c" * 32
    store.mcp_token_put(tok, host="box-b.case.example",
                        origin="https://console.example")
    c = _client()
    r = c.get("/v1/connect", headers=DOOR)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["token"] == tok and body["host"] == "box-b.case.example"
    assert tok in body["paste"]["claude"] and tok in body["paste"]["json"]
    # the bare URL is the form that matters: it is the only one claude.ai and Claude
    # Desktop can take, and it is what the console shows first
    assert body["paste"]["url"] == f"https://box-b.case.example/mcp/{tok}"
    r2 = c.get("/v1/connect", headers=DOOR)
    assert r2.status_code == 200 and r2.json()["token"] is None
    assert r2.json()["seen_at"]


def test_every_paste_form_is_the_same_headerless_url():
    """No form may carry a header. A client with a header field is the minority case
    and can still use the bearer door directly; a client without one is most of the
    market, and a paste it cannot use is why a partner sat stuck."""
    import json
    host = "example.case.example"
    token = "cs_" + "a" * 32
    paste = cased._paste_payload(host, token)["paste"]
    url = f"https://{host}/mcp/{token}"
    assert paste["url"] == url
    assert paste["claude"].endswith(url)
    parsed = json.loads(paste["json"])
    assert parsed["mcpServers"]["case"] == {"type": "http", "url": url}
    assert not [k for k, v in paste.items() if "Bearer" in v], paste


def test_mcp_seed_is_loopback_only_and_rejects_console():
    _clear_box_meta()
    c = _client()
    bad = c.post("/v1/mcp/seed", headers=DOOR,
                 json={"token": "cs_" + "d" * 32, "host": "a.case.example"})
    assert _blocked(bad)
    ok = c.post("/v1/mcp/seed",
                json={"token": "cs_" + "d" * 32, "host": "a.case.example",
                      "origin": "https://console.example"})
    assert ok.status_code == 200, ok.text
    assert store.mcp_token_status()["has_pending"] is True


def test_mcp_rotate_spares_console_and_returns_paste_once():
    _clear_box_meta()
    store.q("DELETE FROM links")
    console_tok = links.mint(None, "console")["token"]
    fill_tok = links.mint("c_1", "fill")["token"]
    store.mcp_token_put("cs_" + "e" * 32, host="mayank.case.example",
                        origin="https://console.example")

    wrote = []

    def fake_door_write(host, token, origin):
        wrote.append((host, token, origin))
        return None

    old = cased._door_write
    try:
        cased._door_write = fake_door_write
        c = _client()
        r = c.post("/v1/mcp/rotate",
                   headers={**DOOR, "Authorization": f"Link {console_tok}"},
                   json={"confirm": "rotate"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["token"] and body["token"].startswith("cs_")
        assert body["token"] in body["paste"]["claude"]
        assert body["burned_links"] >= 1
        assert store.get_link(console_tok)["used_at"] is None
        assert store.get_link(fill_tok)["used_at"] is not None
        assert wrote and wrote[0][0] == "mayank.case.example"
        r2 = c.get("/v1/connect", headers=DOOR)
        assert r2.json()["token"] is None
    finally:
        cased._door_write = old


def test_mcp_rotate_needs_confirm():
    c = _client()
    r = c.post("/v1/mcp/rotate", headers=DOOR, json={})
    assert r.status_code == 400 and r.json()["error"]["code"] == "confirm_rotate"


def test_connect_and_seed_bodies_are_redacted_from_audit():
    from config import AUDIT_DIR
    import time as _time
    _clear_box_meta()
    log_path = os.path.join(AUDIT_DIR, _time.strftime("%Y-%m-%d") + ".jsonl")
    before = os.path.getsize(log_path) if os.path.exists(log_path) else 0
    secret = "cs_" + "f" * 32
    _client().post("/v1/mcp/seed",
                   json={"token": secret, "host": "z.case.example",
                         "origin": "https://console.example"})
    _client().get("/v1/connect", headers=DOOR)
    with open(log_path) as f:
        f.seek(before)
        written = f.read()
    assert secret not in written
    assert "[redacted]" in written


def test_fill_saved_page_has_back_to_console():
    # ADD LOGIN navigates the human off the console onto /fill. After Save the
    # fill token is burned, so browser Back / refresh both dead-end on "Link
    # expired" and people re-mint a console link to get home. The Saved page
    # must offer a real link back to the CREDENTIALS tab (no capability in it —
    # the console token already lives in sessionStorage on that origin).
    store.q("DELETE FROM computers")
    store.q("DELETE FROM links")
    store.q("DELETE FROM credentials")
    _clear_box_meta()
    store.insert_computer("c_fill", "ava", "case-desk:0.1", 1, 2048, "vol", "tok")
    # Seeded host+origin beat import-time DEFAULT_CONSOLE_ORIGIN / CASE_MCP_HOST:
    # a preview box's console Link token lives on the seeded origin's sessionStorage,
    # not on console.example.
    store.mcp_token_put("cs_" + "b" * 32, host="demo.case.example",
                        origin="https://preview.example")
    tok = links.mint("c_fill", "fill")["token"]
    prev_host = os.environ.get("CASE_MCP_HOST")
    prev_default = cased.DEFAULT_CONSOLE_ORIGIN
    os.environ["CASE_MCP_HOST"] = "wrong.case.example"
    cased.DEFAULT_CONSOLE_ORIGIN = "https://console.example"
    try:
        r = _client().post(f"/fill/{tok}", data={
            "domains": "gmail.com", "username": "u@x.com", "secret": "pw",
        })
        assert r.status_code == 200 and "Saved" in r.text, r.text
        assert "← Back to console" in r.text, r.text
        assert ('href="https://preview.example/console'
                '?box=demo.case.example#credentials"') in r.text, r.text
        assert "console.example" not in r.text
        assert "wrong.case.example" not in r.text
        assert tok not in r.text
        # burned: a refresh of the fill URL is GONE, but still offers the same way home
        gone = _client().get(f"/fill/{tok}")
        assert gone.status_code == 410 and "Link expired" in gone.text
        assert "← Back to console" in gone.text
        assert "{back}" not in gone.text
    finally:
        if prev_host is None:
            os.environ.pop("CASE_MCP_HOST", None)
        else:
            os.environ["CASE_MCP_HOST"] = prev_host
        cased.DEFAULT_CONSOLE_ORIGIN = prev_default
        _clear_box_meta()


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("PASS")
