# SPDX-License-Identifier: MIT
"""Optional CASE_TOKEN gate. Pure — no Docker.
Run: .venv/bin/python tests/test_token.py"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "control-plane"))
os.environ.setdefault("CASE_HOME", "/tmp/case-token-test")
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
        assert cased.bearer_ok("Bearer share-meX") is False  # length mismatch, no throw
    finally:
        os.environ.pop("CASE_TOKEN", None)


def test_blank_token_is_open():
    os.environ["CASE_TOKEN"] = "   "
    try:
        assert cased.case_token() == ""
        assert cased.bearer_ok(None) is True
    finally:
        os.environ.pop("CASE_TOKEN", None)


if __name__ == "__main__":
    test_open_when_unset()
    test_requires_matching_bearer()
    test_blank_token_is_open()
    print("test_token: ok")
