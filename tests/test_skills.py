# SPDX-License-Identifier: MIT
"""case_skill — name validation, secret screening, and call shapes.
Pure: HTTP mocked. Run: .venv/bin/python tests/test_skills.py"""
import importlib
import os
import sys
from unittest import mock

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "mcp"))


def _load():
    for k in ("CASE_MCP_HTTP", "CASE_MCP_PORT", "CASE_MCP_SCHEDULES"):
        os.environ.pop(k, None)
    mod = importlib.import_module("case_mcp")
    return importlib.reload(mod)


def _ok(payload=None, content=b""):
    r = mock.Mock()
    r.status_code = 200
    r.json.return_value = payload or {}
    r.content = content
    r.text = ""
    return r


SKILL = """---
name: coupa-ap-aging
description: Pull this week's AP aging report from Coupa.
metadata:
  credential: coupa
---

# Steps
1. computer_navigate to https://acme.coupahost.com/analytics/reports
   - On a login wall: computer_login(credential="coupa"), auth_attempt_wait.
2. computer_click_element on the link "AP Aging Detail" (pass the name).

## Done means
~/reports/ap-aging-<today>.csv exists with > 10 lines.
"""


def test_skill_name_rules():
    m = _load()
    assert m.skill_name_ok("coupa-ap-aging")
    assert m.skill_name_ok("x2")
    assert not m.skill_name_ok("")
    assert not m.skill_name_ok("Coupa")          # no uppercase
    assert not m.skill_name_ok("a/b")            # no path chars
    assert not m.skill_name_ok("-lead")
    assert not m.skill_name_ok("a" * 65)


def test_secret_screen_catches_secret_shapes():
    m = _load()
    assert m.skill_content_risky("password: hunter2secret")
    assert m.skill_content_risky("API_KEY=sk-abcdef123456")
    assert m.skill_content_risky("otp: 123456")
    assert m.skill_content_risky("x" * 40)       # unbroken token
    # legit skill content passes — vault NAME references and prose about passwords
    assert m.skill_content_risky(SKILL) is None
    assert m.skill_content_risky("NEVER type credentials or OTP codes yourself.") is None
    assert m.skill_content_risky("credential: coupa") is None


def test_save_rejects_secrets_and_bad_names():
    m = _load()
    with mock.patch.object(m, "call") as call:
        for bad in (("Bad Name", SKILL), ("ok-name", "token: abc123XYZplenty")):
            try:
                m.case_skill("c_1", "save", name=bad[0], content=bad[1])
                assert False, bad
            except RuntimeError:
                pass
        call.assert_not_called()   # nothing risky ever leaves the tool


def test_save_writes_file_then_logbook():
    m = _load()
    with mock.patch.object(m, "call", return_value=_ok({})) as call:
        out = m.case_skill("c_1", "save", name="coupa-ap-aging", content=SKILL)
    assert out["saved"].endswith("/skills/coupa-ap-aging/SKILL.md")
    puts = [c for c in call.call_args_list if c.args[0] == "PUT"]
    execs = [c for c in call.call_args_list if c.args[1].endswith("/exec")]
    assert len(puts) == 1 and len(execs) == 1
    assert puts[0].kwargs["params"]["path"] == "/home/agent/skills/coupa-ap-aging/SKILL.md"
    assert "base64 -d" in execs[0].kwargs["json"]["command"]   # logbook append, injection-safe


def test_list_returns_index_text():
    m = _load()
    with mock.patch.object(m, "call", return_value=_ok(
            {"stdout": "## /home/agent/skills/coupa-ap-aging/SKILL.md\nname: coupa-ap-aging\n"})) as call:
        out = m.case_skill("c_1", "list")
    assert "coupa-ap-aging" in out["skills"]
    cmd = call.call_args.kwargs["json"]["command"]
    assert "SKILL.md" in cmd and "awk" in cmd


def test_read_returns_utf8():
    m = _load()
    with mock.patch.object(m, "call", return_value=_ok(content=SKILL.encode())):
        out = m.case_skill("c_1", "read", name="coupa-ap-aging")
    assert out["content"].startswith("---\nname: coupa-ap-aging")


def test_tool_registered():
    m = _load()
    assert "case_skill" in list(m.mcp._tool_manager._tools)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("PASS")
