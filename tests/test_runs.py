# SPDX-License-Identifier: MIT
"""Read routes: runs, credential health, handoff list.
Run: .venv/bin/python tests/test_runs.py"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "control-plane"))
# assignment, NOT setdefault — these tests DELETE FROM real tables, and an inherited
# CASE_HOME would point them at a live box's vault. Same reasoning as tests/test_links.py.
os.environ["CASE_HOME"] = "/tmp/case-runs-test"
import cased  # noqa: E402
from store import store  # noqa: E402

# WP-B store helpers, mocked until that branch merges: approvals sign their ntfy
# answer URL, and expire_stale reaps abandoned attempts.
store.sign = lambda text: "sig-" + text
store.stale_active_auth_attempts = lambda cutoff: []


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
    # has to reach the vault, or credential health reporting is decorative
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


# ---- computer + credential summaries ----

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


def test_the_handoff_list_does_not_ship_screenshots():
    import handoffs as ho
    store.q("DELETE FROM handoffs")
    store.insert_handoff("h_shot", "c_1", "otp", "code?", "QUl0aXNhUE5H", "chase.com",
                         "chase.com")
    listed = ho.list_handoffs("pending")
    # clients poll this list and read four scalars; a full-display PNG as
    # base64 on every poll is bytes nobody looks at
    assert "screenshot_png_b64" not in listed[0], listed[0]
    assert listed[0]["domain"] == "chase.com", listed[0]
    # ...and it is still reachable one at a time
    assert ho.get_handoff("h_shot")["screenshot_png_b64"] == "QUl0aXNhUE5H"
    store.q("DELETE FROM handoffs")




if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("PASS")
