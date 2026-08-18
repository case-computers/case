# SPDX-License-Identifier: MIT
"""browse.py — snapshot/click/fill/wait/tabs composition logic. Pure: eval_js and
desk_json are faked, no Docker, no deskd.
Run: .venv/bin/python tests/test_browse.py"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "control-plane"))
os.environ.setdefault("CASE_HOME", "/tmp/case-browse-test")
import browse  # noqa: E402
from errors import ApiError  # noqa: E402

ROW = {"desk_port": 1, "desk_token": "t"}
ELS = [
    {"tag": "a", "type": "", "name": "Home", "value": "", "href": "/"},
    {"tag": "button", "type": "submit", "name": "Save changes", "value": "", "href": ""},
    {"tag": "input", "type": "email", "name": "Work email", "value": "x@y.z", "href": ""},
]


def fake_eval(*results):
    calls = []

    def _eval(row, expression, timeout_s=20):
        calls.append(expression)
        r = results[min(len(calls) - 1, len(results) - 1)]
        if isinstance(r, Exception):
            raise r
        return r if isinstance(r, dict) else {"ok": True, "value": r}
    _eval.calls = calls
    return _eval


def fake_desk(*results):
    calls = []

    def _desk(row, method, path, timeout=35, **kw):
        calls.append((method, path, kw.get("json")))
        r = results[min(len(calls) - 1, len(results) - 1)]
        if isinstance(r, Exception):
            raise r
        return r
    _desk.calls = calls
    return _desk


# ---------- snapshot ----------

def test_snapshot_formats_numbered_lines():
    browse.eval_js = fake_eval({"ok": True, "value": {
        "url": "https://x.test", "title": "T", "count": 3, "els": ELS}})
    out = browse.snapshot(ROW)
    assert out["ok"] and out["count"] == 3, out
    assert out["elements"][0] == '[0] a "Home" -> /', out["elements"]
    assert out["elements"][1] == '[1] button(submit) "Save changes"', out["elements"]
    assert "=" in out["elements"][2], out["elements"]   # input value shown


def test_snapshot_truncation_flagged():
    browse.eval_js = fake_eval({"ok": True, "value": {
        "url": "u", "title": "t", "count": 400, "els": ELS}})
    assert browse.snapshot(ROW)["truncated"] is True


def test_snapshot_giant_dom_string_result_is_error_not_crash():
    # >64KB eval results come back as a truncated *string*, not a dict
    browse.eval_js = fake_eval({"ok": True, "value": '{"url":"x'})
    out = browse.snapshot(ROW)
    assert out["ok"] is False and "extreme" in out["error"], out


def test_snapshot_walk_never_reads_password_values():
    browse.eval_js = fake_eval({"ok": True, "value": {"url": "u", "title": "t",
                                                      "count": 0, "els": []}})
    browse.snapshot(ROW)
    assert "el.type!=='password'" in browse.eval_js.calls[0]


# ---------- click_element ----------

def test_click_fires_os_click_at_located_coords():
    browse.eval_js = fake_eval({"ok": True, "value": {
        "ok": True, "name": "Save changes", "tag": "button", "x": 640, "y": 402}})
    browse.desk_json = fake_desk({"ok": True})
    out = browse.click_element(ROW, 1, name="Save changes")
    assert out["ok"] and out["clicked"] == "Save changes", out
    m, p, j = browse.desk_json.calls[0]
    assert p == "/action" and j["type"] == "click" and (j["x"], j["y"]) == (640, 402), j


def test_stale_ref_refuses_and_returns_fresh_snapshot():
    browse.eval_js = fake_eval(
        {"ok": True, "value": {"ok": False, "stale": True, "found": "Delete", "count": 9}},
        {"ok": True, "value": {"url": "u", "title": "t", "count": 1, "els": ELS[:1]}})
    browse.desk_json = fake_desk(AssertionError("must not click a stale ref"))
    out = browse.click_element(ROW, 1, name="Save changes")
    assert out["ok"] is False and out["stale"] is True, out
    assert out["snapshot"]["elements"], out
    assert not browse.desk_json.calls   # the wrong click never happened


def test_offscreen_coords_refused():
    browse.eval_js = fake_eval({"ok": True, "value": {
        "ok": True, "name": "x", "tag": "a", "x": 5000, "y": 10}})
    browse.desk_json = fake_desk(AssertionError("must not click off-screen"))
    out = browse.click_element(ROW, 0)
    assert out["ok"] is False and "off-screen" in out["error"], out


def test_click_with_text_types_after_click():
    browse.eval_js = fake_eval({"ok": True, "value": {
        "ok": True, "name": "Work email", "tag": "input", "x": 100, "y": 100}})
    browse.desk_json = fake_desk({"ok": True}, {"ok": True})
    browse.click_element(ROW, 2, text="jane@x.com")
    kinds = [j["type"] for _, _, j in browse.desk_json.calls]
    assert kinds == ["click", "type"], kinds
    assert browse.desk_json.calls[1][2]["text"] == "jane@x.com"


def test_name_is_json_quoted_into_the_expression():
    browse.eval_js = fake_eval({"ok": True, "value": {"ok": True, "name": "x",
                                                      "tag": "a", "x": 1, "y": 1}})
    browse.desk_json = fake_desk({"ok": True})
    browse.click_element(ROW, 0, name='a"b\'c')
    assert '"a\\"b\'c"' in browse.eval_js.calls[0]


# ---------- fill ----------

def test_fill_requires_ref_and_value():
    try:
        browse.fill(ROW, [{"ref": 1}])
    except ApiError as e:
        assert e.status == 400
        return
    assert False


def test_fill_passes_fields_and_returns_page_result():
    browse.eval_js = fake_eval({"ok": True, "value": {
        "ok": True, "fields": [{"ref": 2, "ok": True, "name": "Work email"}]}})
    out = browse.fill(ROW, [{"ref": 2, "value": "jane@x.com"}], submit=True)
    assert out["ok"] is True, out
    expr = browse.eval_js.calls[0]
    assert '"jane@x.com"' in expr and "__submit=true" in expr
    assert "vault-only" in expr   # the password refusal ships inside the page script


# ---------- wait_for ----------

def test_wait_selector_returns_when_found():
    browse.eval_js = fake_eval({"ok": True, "value": False}, {"ok": True, "value": True})
    out = browse.wait_for(ROW, selector="#done", timeout_s=5)
    assert out["ok"] is True and out["waited_ms"] >= 0, out


def test_wait_times_out_with_named_condition():
    browse.eval_js = fake_eval({"ok": True, "value": False})
    out = browse.wait_for(ROW, text="Loaded", timeout_s=1)
    assert out["ok"] is False and "Loaded" in out["error"], out


def test_wait_gone_inverts():
    browse.eval_js = fake_eval({"ok": True, "value": True}, {"ok": True, "value": False})
    out = browse.wait_for(ROW, selector=".spinner", gone=True, timeout_s=5)
    assert out["ok"] is True, out


def test_wait_network_idle_needs_two_stable_polls():
    browse.eval_js = fake_eval({"ok": True, "value": 5}, {"ok": True, "value": 8},
                               {"ok": True, "value": 8}, {"ok": True, "value": 8})
    out = browse.wait_for(ROW, network_idle=True, timeout_s=10)
    assert out["ok"] is True, out
    assert len(browse.eval_js.calls) >= 4


def test_wait_502_is_churn_not_failure():
    browse.eval_js = fake_eval(ApiError(502, "eval_error", "context destroyed"),
                               {"ok": True, "value": True})
    assert browse.wait_for(ROW, selector="#x", timeout_s=5)["ok"] is True


def test_wait_423_propagates():
    browse.eval_js = fake_eval(ApiError(423, "credential_injection", "blocked"))
    try:
        browse.wait_for(ROW, selector="#x", timeout_s=5)
    except ApiError as e:
        assert e.status == 423
        return
    assert False


def test_wait_needs_a_condition():
    try:
        browse.wait_for(ROW)
    except ApiError as e:
        assert e.status == 400
        return
    assert False


# ---------- tabs ----------

def test_tabs_list_filters_pages_and_marks_active():
    listing = ('[{"type":"page","id":"AA11","title":"One","url":"https://a"},'
               '{"type":"service_worker","id":"SW","title":"w","url":"u"},'
               '{"type":"page","id":"BB22","title":"Two","url":"https://b"}]')
    browse.desk_json = fake_desk({"stdout": listing})
    out = browse.tabs(ROW)
    assert [t["id"] for t in out["tabs"]] == ["AA11", "BB22"], out
    assert out["tabs"][0]["active"] and not out["tabs"][1]["active"]
    cmd = browse.desk_json.calls[0][2]["command"]
    assert "9222" in cmd and "/json/list" in cmd


def test_tabs_activate_validates_target_id():
    try:
        browse.tabs(ROW, action="activate", target_id="bad id; rm -rf /")
    except ApiError as e:
        assert e.status == 400
        return
    assert False


def test_tabs_new_requires_http_url():
    try:
        browse.tabs(ROW, action="new", url="javascript:alert(1)")
    except ApiError as e:
        assert e.status == 400
        return
    assert False


def test_tabs_cdp_unreachable_is_502():
    browse.desk_json = fake_desk({"stdout": ""})
    try:
        browse.tabs(ROW)
    except ApiError as e:
        assert e.status == 502
        return
    assert False


# ---------- teach_tick ----------

def test_teach_tick_drains_events():
    browse.eval_js = fake_eval({"ok": True, "value": {
        "href": "https://a.test/x", "pw": False,
        "events": [{"k": "click", "tag": "a", "name": "Aging"}]}})
    out = browse.teach_tick(ROW)
    assert out["ok"] and out["events"][0]["name"] == "Aging", out
    expr = browse.eval_js.calls[0]
    # redaction lives IN the injected script: secret fields never leave the page
    assert "secretish" in expr and "type==='password'" in expr
    assert "one-time-code" in expr
    # a form containing a password field redacts ALL its fields
    assert "el.form&&el.form.querySelector('input[type=password]')" in expr


def test_teach_tick_423_is_a_quiet_gap_not_an_error():
    # during credential injection the recorder must be blind, and silently so
    browse.eval_js = fake_eval(ApiError(423, "credential_injection", "blocked"))
    out = browse.teach_tick(ROW)
    assert out["ok"] and out["events"] == [] and out["gap"] == 423, out


def test_teach_tick_502_nav_churn_is_quiet():
    browse.eval_js = fake_eval(ApiError(502, "eval_error", "context destroyed"))
    out = browse.teach_tick(ROW)
    assert out["ok"] and out["gap"] == 502, out


def test_teach_tick_504_still_raises():
    browse.eval_js = fake_eval(ApiError(504, "daemon_timeout", "deskd did not respond"))
    try:
        browse.teach_tick(ROW)
    except ApiError as e:
        assert e.status == 504
        return
    assert False


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("PASS")
