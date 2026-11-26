# SPDX-License-Identifier: MIT
"""Page arrival: elements ride along with navigate, and a wall is flagged.
Run: .venv/bin/python tests/test_wall.py"""
import unittest.mock as mock

import _helpers

_helpers.isolated_home()
import browse  # noqa: E402
from errors import ApiError  # noqa: E402

REDDIT_BLOCK = ("You've been blocked by network security.\n"
                "To continue, log in to your Reddit account or use your developer token\n"
                "Log in\nFile a ticket")


def fake_snapshot(elements, count=None, truncated=False):
    def _snap(row, timeout_s=15, target_id=None):
        return {"ok": True, "url": "u", "title": "t", "count": count or len(elements),
                "truncated": truncated, "elements": elements}
    return _snap


def test_credential_for():
    creds = [{"name": "reddit.com", "domains": ["reddit.com"]},
             {"name": "x.com", "domains": ["x.com", "twitter.com"]}]
    assert browse.credential_for(creds, "https://www.reddit.com/r/popular") == "reddit.com"
    assert browse.credential_for(creds, "https://twitter.com/home") == "x.com"
    assert browse.credential_for(creds, "https://old.reddit.com/") == "reddit.com"
    assert browse.credential_for(creds, "https://notreddit.com/") is None
    assert browse.credential_for(creds, "https://example.org/") is None
    assert browse.credential_for([], "https://reddit.com") is None
    assert browse.credential_for(creds, "") is None


def test_wall_flagged_and_no_credential_named():
    with mock.patch.object(browse, "snapshot", fake_snapshot(['[0] link "Log in"'])):
        out = browse.with_elements({}, {"ok": True, "url": "https://www.reddit.com/",
                                        "text": REDDIT_BLOCK}, creds=[])
    assert out["elements"] == ['[0] link "Log in"']
    assert out["elements_count"] == 1
    assert out["wall"] is True
    assert "vault_credential" not in out
    assert "handoff_request(kind=device)" in out["hint"]


def test_wall_names_the_vault_credential_when_one_covers_the_host():
    with mock.patch.object(browse, "snapshot", fake_snapshot(['[0] link "Log in"'])):
        out = browse.with_elements({}, {"ok": True, "url": "https://www.reddit.com/",
                                        "text": REDDIT_BLOCK},
                                   creds=[{"name": "reddit.com", "domains": ["reddit.com"]}])
    assert out["wall"] is True
    assert out["vault_credential"] == "reddit.com"
    assert "computer_login" in out["hint"] and "handoff_request" not in out["hint"]


def test_ordinary_page_gets_elements_but_no_wall_note():
    with mock.patch.object(browse, "snapshot", fake_snapshot(['[0] button "Buy"'])):
        out = browse.with_elements({}, {"ok": True, "url": "https://shop.example/",
                                        "text": "Welcome to the shop. Add to cart."}, creds=[])
    assert out["elements"] == ['[0] button "Buy"']
    assert "wall" not in out and "hint" not in out


def test_elements_are_capped_and_say_so():
    many = [f'[{i}] button "b{i}"' for i in range(100)]
    with mock.patch.object(browse, "snapshot", fake_snapshot(many)):
        out = browse.with_elements({}, {"ok": True, "url": "https://x/", "text": "hi"}, creds=[])
    assert len(out["elements"]) == browse.NAV_ELS
    assert out["elements_truncated"] is True
    assert out["elements_count"] == 100


def test_failed_navigation_is_left_alone():
    def boom(*a, **k):
        raise AssertionError("must not snapshot a page that never arrived")
    with mock.patch.object(browse, "snapshot", boom):
        out = browse.with_elements({}, {"ok": False, "error": "timeout"}, creds=[])
    assert out == {"ok": False, "error": "timeout"}


def test_snapshot_failure_does_not_undo_the_arrival():
    def boom(*a, **k):
        raise ApiError(502, "bad_gateway", "no page target")
    with mock.patch.object(browse, "snapshot", boom):
        out = browse.with_elements({}, {"ok": True, "url": "u", "text": "hi"}, creds=[])
    assert out["ok"] is True and "elements" not in out


def test_with_elements_forwards_target_id():
    calls = []

    def snap(row, timeout_s=15, target_id=None):
        calls.append(target_id)
        return {"ok": True, "url": "u", "title": "t", "count": 0, "truncated": False,
                "elements": []}
    with mock.patch.object(browse, "snapshot", snap):
        browse.with_elements({}, {"ok": True, "url": "u", "text": "hi"}, target_id="BBBB-2222")
    assert calls == ["BBBB-2222"]


if __name__ == "__main__":
    _helpers.run_tests(globals())
