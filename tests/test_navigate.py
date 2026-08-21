# SPDX-License-Identifier: MIT
"""deskclient.navigate — the sentinel/poll loop that replaces an agent's manual
readyState polling. Pure: eval_js is faked, no Docker, no deskd.
Run: .venv/bin/python tests/test_navigate.py"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "control-plane"))
os.environ.setdefault("CASE_HOME", "/tmp/case-navigate-test")
import deskclient  # noqa: E402
from errors import ApiError  # noqa: E402

ROW = {"desk_port": 1, "desk_token": "t"}
OK = {"ok": True, "value": None}


def fake(polls, assigns=(OK,)):
    """Answer location.assign calls from `assigns`, everything else from `polls`
    (a bare value is wrapped as a successful eval; an Exception is raised).
    Records every expression seen."""
    seen, a, p = [], iter(assigns), iter(polls)

    def _eval(row, expression, timeout_s=20):
        seen.append(expression)
        r = next(a) if "location.assign" in expression else next(p, None)
        if isinstance(r, Exception):
            raise r
        return r if isinstance(r, dict) else {"ok": True, "value": r}
    _eval.seen = seen
    return _eval


def test_returns_when_sentinel_is_gone():
    deskclient.eval_js = fake([None, ["https://x.test/final", "Title"]])
    out = deskclient.navigate(ROW, "https://x.test", timeout_s=5)
    assert out == {"ok": True, "url": "https://x.test/final", "title": "Title"}, out


def test_old_page_complete_does_not_count_as_arrival():
    # sentinel never clears => the browser never left the old document => timeout,
    # not a false "ok". This is the whole reason for the sentinel.
    deskclient.eval_js = fake([None] * 50)
    out = deskclient.navigate(ROW, "https://x.test", timeout_s=1)
    assert out["ok"] is False and "did not finish" in out["error"], out


def test_sentinel_is_deleted_when_navigation_fails():
    # else a uniquely-named global stays on a live page for site JS to fingerprint
    ev = fake([None] * 50)
    deskclient.eval_js = ev
    deskclient.navigate(ROW, "https://x.test", timeout_s=1)
    assert any("delete window.__case_nav" in e for e in ev.seen), ev.seen


def test_torn_down_context_is_progress_not_failure():
    # mid-navigation the JS context dies and deskd answers 502 — that means the
    # navigation is happening, so keep polling.
    deskclient.eval_js = fake([ApiError(502, "eval_error", "context destroyed"),
                               ["https://x.test/", "T"]])
    assert deskclient.navigate(ROW, "https://x.test", timeout_s=5)["ok"] is True


def test_daemon_timeout_is_not_mistaken_for_a_slow_page():
    # a slept/wedged box answers 504 forever; reporting that as "page didn't load"
    # sends the agent off retrying navigation against a machine that is asleep.
    deskclient.eval_js = fake([ApiError(504, "daemon_timeout", "deskd did not respond")])
    try:
        deskclient.navigate(ROW, "https://x.test", timeout_s=30)
    except ApiError as e:
        assert e.status == 504
        return
    assert False, "504 must propagate, not be waited out"


def test_credential_injection_fails_fast():
    deskclient.eval_js = fake([ApiError(423, "credential_injection", "blocked")])
    try:
        deskclient.navigate(ROW, "https://x.test", timeout_s=30)
    except ApiError as e:
        assert e.status == 423
        return
    assert False, "423 must propagate, not be swallowed until timeout"


def test_assign_retries_while_chromium_has_no_page_target():
    # straight after a wake deskd can 502 ("no chromium page target"); the first
    # navigate must wait that out, not hard-error.
    deskclient.eval_js = fake([["https://x.test/", "T"]],
                              assigns=[ApiError(502, "eval_error", "no chromium page target"),
                                       OK])
    assert deskclient.navigate(ROW, "https://x.test", timeout_s=5)["ok"] is True


def test_assign_502_still_raises_once_the_budget_is_spent():
    deskclient.eval_js = fake([], assigns=[ApiError(502, "eval_error", "no page target")] * 20)
    try:
        deskclient.navigate(ROW, "https://x.test", timeout_s=1)
    except ApiError as e:
        assert e.status == 502
        return
    assert False, "a 502 that never clears must surface"


def test_truncated_result_is_not_indexed_as_a_list():
    # >64KB makes deskd return the JSON *string*; v[0]/v[1] would then be characters
    deskclient.eval_js = fake(['["https://x.test/verylong' + "x" * 50] * 50)
    out = deskclient.navigate(ROW, "https://x.test", timeout_s=1)
    assert out["ok"] is False, out


def test_rejected_assign_reports_instead_of_waiting_out_the_timeout():
    deskclient.eval_js = fake([], assigns=[
        {"ok": False, "error": "SyntaxError: Failed to execute 'assign'"}])
    out = deskclient.navigate(ROW, "http://", timeout_s=30)
    assert out["ok"] is False and "SyntaxError" in out["error"], out


def test_url_is_json_quoted_into_the_expression():
    # a url with a quote must not break out of the JS string literal
    ev = fake([["u", "t"]])
    deskclient.eval_js = ev
    deskclient.navigate(ROW, "https://x.test/a'b\"c", timeout_s=5)
    assert '"https://x.test/a\'b\\"c"' in ev.seen[0], ev.seen[0]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("PASS")
