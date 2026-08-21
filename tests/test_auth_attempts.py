# SPDX-License-Identifier: MIT
"""Durable auth_attempts orchestration.
Run: .venv/bin/python tests/test_auth_attempts.py"""
import json
import os
import shutil
import sys
import tempfile
import unittest.mock as mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "control-plane"))
# assignment, NOT setdefault: these tests write auth_attempt rows, and an inherited
# CASE_HOME would put them in a live box's DB.
_HOME = tempfile.mkdtemp(prefix="case-auth-attempts-")
os.environ["CASE_HOME"] = _HOME

import auth_attempts  # noqa: E402
import handoffs  # noqa: E402
from errors import ApiError  # noqa: E402
from store import store  # noqa: E402

# Never publish a real handoff notify from unit tests.
handoffs.notifier = type("N", (), {"notify": lambda self, h, name: None})()

COMP = {"id": "c_1", "name": "ava", "state": "running"}


def _cleanup():
    store.q("DELETE FROM auth_attempts")
    store.q("DELETE FROM handoffs")
    store.q("DELETE FROM credentials")
    handoffs.LOGIN_CTX.clear()


def _raises(fn, code):
    try:
        fn()
    except ApiError as e:
        assert e.code == code, (e.code, e.message)
        return e
    assert False, f"expected ApiError {code}"


def _obs(**kwargs):
    base = {
        "href": "https://example.com/login",
        "ready": True,
        "title": "Login",
        "visible_fields": {"user": False, "pass": False, "code": False},
        "frame_markers": {},
        "challenge_signals": [],
        "page_state": "",
    }
    base.update(kwargs)
    return base


def test_insert_and_get_auth_attempt():
    _cleanup()
    store.insert_auth_attempt(
        "a_t1", "c_1", "github", "https://example.com/login",
        proof_spec={"url_contains": "/home"}, idempotency_key="k1")
    row = store.get_auth_attempt("a_t1")
    assert row is not None
    assert row["computer_id"] == "c_1"
    assert row["credential"] == "github"
    assert row["status"] == "created"
    assert row["revision"] == 0
    assert json.loads(row["proof_spec"]) == {"url_contains": "/home"}
    assert store.get_auth_attempt_by_idempotency("c_1", "k1")["id"] == "a_t1"


def test_start_attempt_returns_public_dict_without_secrets():
    _cleanup()
    out = auth_attempts.start_attempt(
        "c_1", "github", "https://example.com/login",
        proof_spec={"selector": "#avatar"}, idempotency_key="pub1")
    assert out["id"].startswith("a_")
    assert out["status"] == "created"
    assert out["revision"] == 0
    assert out["credential"] == "github"
    assert out["target_url"] == "https://example.com/login"
    assert out["proof_level"] == "configured"
    assert out["current_handoff_id"] is None
    # public surface: no raw proof_spec, no secret-ish keys
    for forbidden in ("proof_spec", "secret", "totp_seed", "answer",
                      "password", "otp", "idempotency_key"):
        assert forbidden not in out, out


def test_idempotency_returns_same_attempt():
    _cleanup()
    a = auth_attempts.start_attempt(
        "c_1", "github", "https://example.com/login", idempotency_key="idem-1")
    b = auth_attempts.start_attempt(
        "c_1", "github", "https://example.com/other", idempotency_key="idem-1")
    assert a["id"] == b["id"]
    assert b["target_url"] == "https://example.com/login"  # original preserved
    # still works after terminal
    store.cas_auth_attempt_status(a["id"], "created", "cancelled", a["revision"])
    c = auth_attempts.start_attempt(
        "c_1", "github", "https://example.com/login", idempotency_key="idem-1")
    assert c["id"] == a["id"]
    assert c["status"] == "cancelled"


def test_duplicate_start_same_idempotency_key():
    """Transport retry with the same idempotency_key must not create a second attempt."""
    _cleanup()
    a = auth_attempts.start_attempt(
        "c_1", "cred", "https://example.com/a", idempotency_key="dup-key")
    b = auth_attempts.start_attempt(
        "c_1", "cred", "https://example.com/b", idempotency_key="dup-key")
    assert a["id"] == b["id"]
    listed = store.list_auth_attempts_for("c_1")
    assert len(listed) == 1


def test_one_active_attempt_per_computer():
    _cleanup()
    auth_attempts.start_attempt("c_1", "github", "https://example.com/login")
    assert store.active_attempt_exists("c_1")
    assert store.get_active_auth_attempt("c_1") is not None
    _raises(lambda: auth_attempts.start_attempt(
        "c_1", "github", "https://example.com/login", idempotency_key="other"),
            "auth_in_progress")
    # different computer is fine
    other = auth_attempts.start_attempt("c_2", "github", "https://example.com/login")
    assert other["computer_id"] == "c_2"


def test_cas_revision_conflict():
    _cleanup()
    a = auth_attempts.start_attempt("c_1", "github", "https://example.com/login")
    n = store.cas_auth_attempt_status(a["id"], "created", "advancing", 0)
    assert n == 1
    assert store.get_auth_attempt(a["id"])["revision"] == 1
    # stale revision on cancel
    _raises(lambda: auth_attempts.cancel_attempt(a["id"], expected_revision=0),
            "revision_conflict")
    # store-level CAS
    n = store.cas_auth_attempt_status(a["id"], "advancing", "proving", 0)
    assert n == 0
    n = store.cas_auth_attempt_status(a["id"], "advancing", "proving", 1)
    assert n == 1
    assert store.get_auth_attempt(a["id"])["revision"] == 2


def test_cancel_attempt():
    _cleanup()
    a = auth_attempts.start_attempt("c_1", "github", "https://example.com/login")
    out = auth_attempts.cancel_attempt(a["id"], expected_revision=0)
    assert out["status"] == "cancelled"
    assert out["revision"] == 1
    # idempotent cancel of already-cancelled
    again = auth_attempts.cancel_attempt(a["id"])
    assert again["status"] == "cancelled"
    assert not store.active_attempt_exists("c_1")


def test_claim_challenge_cas():
    _cleanup()
    store.insert_handoff(
        "h_claim", "c_1", "otp", "enter code", None, "github",
        domain="example.com", attempt_id="a_x", sequence=1, revision=0)
    claimed = auth_attempts.claim_challenge("h_claim", expected_revision=0)
    assert claimed["status"] == "validating"
    assert claimed["revision"] == 1
    assert "answer" not in claimed
    # stale / wrong status
    _raises(lambda: auth_attempts.claim_challenge("h_claim", expected_revision=0),
            "revision_conflict")
    _raises(lambda: auth_attempts.claim_challenge("h_claim", expected_revision=1),
            "revision_conflict")


def test_missing_proof_spec_ends_unverified():
    _cleanup()
    a = auth_attempts.start_attempt(
        "c_1", "github", "https://example.com/login", proof_spec=None)
    assert a["proof_level"] == "heuristic"
    with mock.patch("lifecycle.get_computer", return_value=COMP), \
         mock.patch("deskclient.observe_auth",
                    return_value={"ok": True, "observation": _obs(
                        href="https://example.com/home",
                        page_state="Welcome")}), \
         mock.patch.object(store, "record_credential_result") as rec, \
         mock.patch("events.emit") as emit:
        # no challenge signals → prove → unverified (no proof_spec)
        out = auth_attempts.advance_attempt(a["id"])
    assert out["status"] == "unverified", out
    rec.assert_called_with("c_1", "github", "unverified")
    # never authenticated
    assert store.get_auth_attempt(a["id"])["status"] == "unverified"


def test_prove_with_proof_spec_authenticated():
    _cleanup()
    a = auth_attempts.start_attempt(
        "c_1", "github", "https://example.com/login",
        proof_spec={"url_contains": "/home", "selector": "#avatar"})
    with mock.patch("lifecycle.get_computer", return_value=COMP), \
         mock.patch("deskclient.eval_js", side_effect=[
             {"value": "https://example.com/home"},  # href
             {"value": True},  # selector
         ]), \
         mock.patch.object(store, "record_credential_result") as rec:
        out = auth_attempts.prove_attempt(a["id"])
    assert out["status"] == "authenticated", out
    rec.assert_called_with("c_1", "github", "success")


def test_captcha_then_otp_one_attempt():
    """Two advances on one attempt: captcha handoff, then otp handoff, then prove."""
    _cleanup()
    a = auth_attempts.start_attempt(
        "c_1", "cred", "https://example.com/login",
        proof_spec={"url_prefix": "https://example.com/app"},
        idempotency_key="seq-1")
    handoffs_created = []

    def fake_create(computer_row, kind, prompt, **kw):
        hid = f"h_{kind}_{len(handoffs_created)}"
        store.insert_handoff(
            hid, computer_row["id"], kind, prompt, None, kw.get("login_credential"),
            kw.get("domain"), attempt_id=kw.get("attempt_id"),
            sequence=kw.get("sequence"), revision=kw.get("revision") or 0)
        if kw.get("login_credential"):
            handoffs.LOGIN_CTX[hid] = {
                "computer_id": computer_row["id"],
                "credential": kw["login_credential"],
            }
        handoffs_created.append(hid)
        return {"id": hid, "kind": kind, "status": "pending"}

    captcha_obs = _obs(
        challenge_signals=["captcha"],
        page_state="verify you're human",
        frame_markers={"recaptcha": True})
    otp_obs = _obs(
        href="https://example.com/otp",
        challenge_signals=["otp"],
        visible_fields={"user": False, "pass": False, "code": True},
        page_state="enter the code")
    clear_obs = _obs(
        href="https://example.com/app/home",
        page_state="Welcome home")

    with mock.patch("lifecycle.get_computer", return_value=COMP), \
         mock.patch("deskclient.screenshot_b64", return_value=None), \
         mock.patch("handoffs.create_handoff", side_effect=fake_create), \
         mock.patch("deskclient.observe_auth",
                    return_value={"ok": True, "observation": captcha_obs}):
        r1 = auth_attempts.advance_attempt(a["id"])
    assert r1["status"] == "awaiting_human", r1
    assert r1["current_handoff_id"] == handoffs_created[0]
    assert store.get_handoff(handoffs_created[0])["kind"] == "captcha"
    assert store.get_handoff(handoffs_created[0])["sequence"] == 1

    # Simulate captcha child completed — re-enter advancing with OTP on page.
    store.set_handoff_status(handoffs_created[0], "completed")
    with mock.patch("lifecycle.get_computer", return_value=COMP), \
         mock.patch("deskclient.screenshot_b64", return_value=None), \
         mock.patch("handoffs.create_handoff", side_effect=fake_create), \
         mock.patch("deskclient.observe_auth",
                    return_value={"ok": True, "observation": otp_obs}):
        r2 = auth_attempts.advance_attempt(a["id"])
    assert r2["status"] == "awaiting_human", r2
    assert r2["current_handoff_id"] == handoffs_created[1]
    assert store.get_handoff(handoffs_created[1])["kind"] == "otp"
    assert store.get_handoff(handoffs_created[1])["sequence"] == 2
    assert store.get_handoff(handoffs_created[1])["attempt_id"] == a["id"]

    # Clear challenges → prove authenticated
    store.set_handoff_status(handoffs_created[1], "completed")
    with mock.patch("lifecycle.get_computer", return_value=COMP), \
         mock.patch("deskclient.observe_auth",
                    return_value={"ok": True, "observation": clear_obs}), \
         mock.patch("deskclient.eval_js",
                    return_value={"value": "https://example.com/app/home"}), \
         mock.patch.object(store, "record_credential_result") as rec:
        r3 = auth_attempts.advance_attempt(a["id"])
    assert r3["status"] == "authenticated", r3
    rec.assert_called_with("c_1", "cred", "success")
    # still one attempt
    assert len(store.list_auth_attempts_for("c_1")) == 1


def test_challenge_completion_does_not_record_success_until_prove():
    """Handoff resume with attempt_id must not write credential ok before prove."""
    _cleanup()
    a = auth_attempts.start_attempt(
        "c_1", "cred", "https://example.com/login",
        proof_spec={"url_contains": "/home"})
    store.cas_auth_attempt_status(a["id"], "created", "advancing", 0)
    store.cas_auth_attempt_status(a["id"], "advancing", "awaiting_human", 1)
    store.insert_handoff(
        "h_otp", "c_1", "otp", "enter code", None, "cred",
        continuation="submit_value", attempt_id=a["id"], sequence=1, revision=0)
    store.set_attempt_handoff(a["id"], "h_otp")
    handoffs.LOGIN_CTX["h_otp"] = {"computer_id": "c_1", "credential": "cred"}

    with mock.patch.object(handoffs, "get_computer", return_value=COMP), \
         mock.patch.object(handoffs, "auth_submit_challenge",
                           return_value={"ok": True}), \
         mock.patch.object(store, "record_credential_result") as rec, \
         mock.patch("auth_attempts.advance_attempt",
                    return_value={"id": a["id"], "status": "advancing"}) as adv:
        row = handoffs.submit_handoff_value("h_otp", "123456")
    assert row["status"] == "completed", row
    # challenge path must not stamp success
    for call in rec.call_args_list:
        assert call.args[2] != "success", call
    adv.assert_called_once_with(a["id"])


def test_bad_code_stays_pending_same_challenge():
    _cleanup()
    a = auth_attempts.start_attempt(
        "c_1", "cred", "https://example.com/login",
        proof_spec={"url_contains": "/home"})
    store.cas_auth_attempt_status(a["id"], "created", "awaiting_human", 0)
    store.insert_handoff(
        "h_bad", "c_1", "otp", "enter code", None, "cred",
        continuation="submit_value", attempt_id=a["id"], sequence=1, revision=0)
    store.set_attempt_handoff(a["id"], "h_bad")
    handoffs.LOGIN_CTX["h_bad"] = {"computer_id": "c_1", "credential": "cred"}

    with mock.patch.object(handoffs, "get_computer", return_value=COMP), \
         mock.patch.object(handoffs, "auth_submit_challenge",
                           return_value={"ok": False, "reason": "bad code"}), \
         mock.patch.object(handoffs, "desk_json",
                           return_value={"status": "failed", "reason": "bad code"}), \
         mock.patch("auth_attempts.advance_attempt") as adv, \
         mock.patch.object(store, "record_credential_result") as rec:
        row = handoffs.submit_handoff_value("h_bad", "000000")
    assert row["status"] == "pending", row
    assert "h_bad" in handoffs.LOGIN_CTX
    assert store.get_auth_attempt(a["id"])["status"] == "awaiting_human"
    assert store.get_auth_attempt(a["id"])["current_handoff_id"] == "h_bad"
    adv.assert_not_called()
    for call in rec.call_args_list:
        assert call.args[2] != "success", call


def test_list_helpers_and_set_handoff():
    _cleanup()
    a = auth_attempts.start_attempt("c_1", "github", "https://example.com/login")
    store.set_attempt_handoff(a["id"], "h_1")
    row = store.get_auth_attempt(a["id"])
    assert row["current_handoff_id"] == "h_1"
    listed = store.list_auth_attempts_for("c_1")
    assert len(listed) == 1
    assert listed[0]["id"] == a["id"]
    pub = auth_attempts.get_attempt(a["id"])
    assert pub["current_handoff_id"] == "h_1"


def test_schema_has_credential_auth_profile_columns():
    cols = {r["name"] for r in store.db.execute("PRAGMA table_info(credentials)")}
    assert {"probe_url", "proof_spec", "verification_hosts"} <= cols
    hcols = {r["name"] for r in store.db.execute("PRAGMA table_info(handoffs)")}
    assert {"attempt_id", "sequence", "revision"} <= hcols


def test_login_result_compat_shape():
    _cleanup()
    a = auth_attempts.start_attempt(
        "c_1", "cred", "https://example.com/login",
        proof_spec={"url_contains": "/x"})
    store.set_attempt_handoff(a["id"], "h_9")
    store.cas_auth_attempt_status(a["id"], "created", "awaiting_human", 0)
    pub = auth_attempts.get_attempt(a["id"])
    lr = auth_attempts.login_result(pub)
    assert lr["status"] == "handoff_pending"
    assert lr["handoff_id"] == "h_9"
    assert lr["attempt_id"] == a["id"]
    assert "revision" in lr


def test_malformed_proof_spec_never_authenticates():
    """Unknown keys / empty predicates must not count as configured success."""
    for i, bad in enumerate(
            ({"typo": "/home"}, {"selector": ""}, {"url_contains": ""}, {"url_prefix": "  "})):
        _cleanup()
        a = auth_attempts.start_attempt(
            "c_1", "github", "https://example.com/login",
            proof_spec=bad, idempotency_key=f"bad-{i}")
        assert a["proof_level"] == "heuristic", (bad, a)
        assert auth_attempts._check_proof(
            COMP, bad, observation={"href": "https://evil.invalid/"}) is False
        with mock.patch("lifecycle.get_computer", return_value=COMP), \
             mock.patch("deskclient.observe_auth",
                        return_value={"ok": True, "observation": _obs(
                            href="https://example.com/home",
                            page_state="Welcome")}), \
             mock.patch.object(store, "record_credential_result"):
            out = auth_attempts.advance_attempt(a["id"])
        assert out["status"] == "unverified", (bad, out)


def test_next_handoff_sequence_starts_at_one_then_increments():
    _cleanup()
    assert store.next_handoff_sequence("a_empty") == 1
    store.insert_handoff(
        "h_seq1", "c_1", "otp", "enter code", None, "cred",
        attempt_id="a_empty", sequence=1)
    assert store.next_handoff_sequence("a_empty") == 2


def test_upsert_preserves_credential_auth_profile():
    _cleanup()
    store.upsert_credential(
        "c_1", "github", "u", "pw1", None, None, ["github.com"],
        probe_url="https://github.com/",
        proof_spec={"url_contains": "/"},
        verification_hosts=["github.com"])
    store.upsert_credential("c_1", "github", "u2", "pw2", None, None, ["github.com"])
    row = store.get_credential("c_1", "github")
    assert row["username"] == "u2"
    assert row["probe_url"] == "https://github.com/"
    assert json.loads(row["proof_spec"]) == {"url_contains": "/"}
    assert json.loads(row["verification_hosts"]) == ["github.com"]


if __name__ == "__main__":
    try:
        for name, fn in sorted(globals().items()):
            if name.startswith("test_"):
                fn()
                print("ok", name)
        print("PASS")
    finally:
        try:
            store.db.close()
        except Exception:
            pass
        shutil.rmtree(_HOME, ignore_errors=True)
