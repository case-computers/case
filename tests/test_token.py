# SPDX-License-Identifier: MIT
"""Optional CASE_TOKEN gate. Pure — no Docker.
Run: .venv/bin/python tests/test_token.py"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "control-plane"))
# assignment, NOT setdefault: an inherited CASE_HOME (a dev shell, ~/.case/env)
# would open the real vault just to read a token setting.
os.environ["CASE_HOME"] = "/tmp/case-token-test"
import cased  # noqa: E402


def test_open_when_unset():
    os.environ.pop("CASE_TOKEN", None)
    assert cased.bearer_ok(None) is True
    assert cased.bearer_ok("") is True
    assert cased.bearer_ok("Bearer anything") is True


def test_requires_matching_bearer():
    os.environ["CASE_TOKEN"] = "share-me"
    try:
        assert cased.bearer_ok(None) is False
        assert cased.bearer_ok("Bearer nope") is False
        assert cased.bearer_ok("share-me") is False          # missing scheme
        assert cased.bearer_ok("Bearer share-me") is True
        assert cased.bearer_ok("bearer share-me") is True
        assert cased.bearer_ok("Bearer share-me ") is True
        assert cased.bearer_ok("Bearer share-meX") is False
    finally:
        os.environ.pop("CASE_TOKEN", None)


def test_blank_token_is_open():
    os.environ["CASE_TOKEN"] = "   "
    try:
        assert cased.case_token() == ""
        assert cased.bearer_ok(None) is True
    finally:
        os.environ.pop("CASE_TOKEN", None)


def test_awake_touches_on_success_only():
    # The shared desk-route preamble: touch fires after the body, never on failure —
    # session_keeper reads last_active_at to avoid navigating over a live session.
    from unittest import mock
    row = {"id": "c_t", "state": "running"}
    with mock.patch.object(cased.lifecycle, "ensure_running", return_value=row), \
         mock.patch.object(cased.store, "touch") as touch:
        with cased.awake("c_t", False) as got:
            assert got is row
            touch.assert_not_called()
        touch.assert_called_once_with("c_t")
    with mock.patch.object(cased.lifecycle, "ensure_running", return_value=row), \
         mock.patch.object(cased.store, "touch") as touch:
        try:
            with cased.awake("c_t", False):
                raise RuntimeError("desk blew up")
        except RuntimeError:
            pass
        touch.assert_not_called()


if __name__ == "__main__":
    test_open_when_unset()
    test_requires_matching_bearer()
    test_blank_token_is_open()
    test_awake_touches_on_success_only()
    print("test_token: ok")
