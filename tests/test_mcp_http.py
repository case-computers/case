# SPDX-License-Identifier: MIT
"""The remote door: case_mcp's HTTP mode must stay loopback-only and stateless,
stdio must stay the default, and the Caddyfile must keep the two lines that make
the pair work (bearer check + Host rewrite). No Docker, no network.
Run: .venv/bin/python tests/test_mcp_http.py"""
import importlib
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "mcp"))


def _load(**env):
    for k in ("CASE_MCP_HTTP", "CASE_MCP_PORT", "CASE_MCP_BIND"):
        os.environ.pop(k, None)
    os.environ.update(env)
    mod = importlib.import_module("case_mcp")
    return importlib.reload(mod)


def test_stdio_is_the_default():
    assert _load().HTTP is False          # unset env → every existing flow untouched


def test_http_mode_binds_loopback_only():
    m = _load(CASE_MCP_HTTP="1", CASE_MCP_PORT="8899")
    assert m.HTTP is True
    assert m.mcp.settings.host == "127.0.0.1"      # default; compose overrides CASE_MCP_BIND
    assert m.mcp.settings.port == 8899
    assert m.mcp.settings.stateless_http is True


def test_http_mode_honours_bind_env():
    m = _load(CASE_MCP_HTTP="1", CASE_MCP_BIND="0.0.0.0")
    assert m.mcp.settings.host == "0.0.0.0"


def test_http_app_serves_mcp_path():
    m = _load(CASE_MCP_HTTP="1")
    assert m.mcp.settings.port == 8788             # what Caddy proxies to
    paths = [getattr(r, "path", None) for r in m.mcp.streamable_http_app().routes]
    assert "/mcp" in paths, paths


def test_no_credential_write_tool():
    # security invariant: secrets enter via `case cred add` only, never a tool call
    m = _load()
    names = list(m.mcp._tool_manager._tools)
    assert not [n for n in names if "cred" in n], names


def _caddyfile():
    """The hosted door's config. deploy/ is not part of the public tree, so these
    two checks skip there rather than fail a self-hoster's CI over a file they
    were never shipped."""
    path = os.path.join(ROOT, "deploy", "Caddyfile")
    if not os.path.exists(path):
        print("skip: no deploy/Caddyfile (public tree)")
        return None
    return open(path).read()


def test_caddyfile_keeps_the_door_shut_and_the_host_rewritten():
    caddy = _caddyfile()
    if caddy is None:
        return
    # bearer check, or the box is open to the internet
    assert '@authed header Authorization "Bearer {$CASE_MCP_TOKEN}"' in caddy
    assert "handle @authed" in caddy
    assert "respond" in caddy and "401" in caddy
    # without this the SDK's DNS-rebinding guard 421s every proxied request
    assert "header_up Host {upstream_hostport}" in caddy
    # auth_attempt_wait / long computer_login stay under this door budget
    assert "read_timeout 300s" in caddy
    # exactly one upstream definition — two doors reaching 8788 by hand would drift
    assert caddy.count("reverse_proxy 127.0.0.1:8788") == 1


def test_url_token_door_demands_the_whole_token():
    """The path form is what claude.ai and Claude Desktop can actually use, so it must
    be as shut as the header form. A `*` on the token would make it a PREFIX match:
    /mcp/<token-prefix>anything would open the box to a partial guess."""
    caddy = _caddyfile()
    if caddy is None:
        return
    matcher = [ln.strip() for ln in caddy.splitlines() if ln.strip().startswith("@urltoken ")]
    assert len(matcher) == 1, matcher
    assert matcher[0] == "@urltoken path /mcp/{$CASE_MCP_TOKEN} /mcp/{$CASE_MCP_TOKEN}/", matcher
    assert "handle @urltoken" in caddy
    assert "rewrite * /mcp" in caddy


def test_oauth_discovery_is_404_not_401():
    """claude.ai probes OAuth discovery before connecting. 404 → 'no sign-in service',
    it connects plain. 401 (the site fallback) → it attempts Dynamic Client Registration
    against nothing and the partner sees 'Couldn't register with case's sign-in
    service'. Found live 2026-08-07."""
    caddy = _caddyfile()
    if caddy is None:
        return
    for p in ("oauth-protected-resource", "oauth-authorization-server",
              "openid-configuration"):
        assert f"handle /.well-known/{p}*" in caddy, p


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
