# SPDX-License-Identifier: MIT
"""Lock the shape of what we send the model.

Two properties, neither of which any other test covers:

1. Per-turn varying bytes (the step/round counter, wall-clock date) must live at the
   TAIL of the message. Everything before them is a cacheable prefix, and a prompt
   edit that moves a timestamp earlier silently halves the cache hit rate. That shows
   up as a bill, never as a failing test.
2. The wall policy the box-03 failure produced must actually reach the model. A
   prompt edit that drops it is a silent regression to URL shopping.
Run: .venv/bin/python tests/test_prompt_prefix.py"""
from pathlib import Path

import _helpers

ROOT = Path(__file__).resolve().parents[1]
SERVE = (ROOT / "web/web-ui/serve.mjs").read_text()


def dev_prompt_template():
    """The Drive developer message, as the template literal in runTurn."""
    start = SERVE.index("const dev = { role: 'developer', content: `")
    body = SERVE[start + SERVE[start:].index("`") + 1:]
    end = body.index("` };")
    return body[:end]


def test_drive_prompt_has_no_per_turn_clock():
    tpl = dev_prompt_template()
    for banned in ("Date(", "toISOString", "now()", "Date.now"):
        assert banned not in tpl, f"{banned} in the dev prompt makes every turn a cache miss"


def test_computer_id_is_named_once_and_early():
    tpl = dev_prompt_template()
    assert tpl.index("${id}") < 80, "the computer id belongs at the top, before the loop rules"


def test_wall_policy_survives_in_the_drive_prompt():
    tpl = dev_prompt_template()
    assert "Look before you route" in tpl, "lost the look-before-you-route rule"
    assert "handoff_request(kind=device)" in tpl, "lost the human escape hatch"
    assert "handoff_wait" in tpl, "lost the wait: an away human ends the run"
    assert "never a different url for the same goal" in tpl.lower(), "lost the no-URL-shopping rule"


def test_vault_names_are_interpolated_not_hardcoded():
    tpl = dev_prompt_template()
    assert "${vault}" in tpl, "vault names must come from the computer, never be baked in"


if __name__ == "__main__":
    _helpers.run_tests(globals())
