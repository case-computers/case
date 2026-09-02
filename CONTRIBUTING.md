# Contributing

## Sign the CLA (once)

Case is dual-licensed (AGPL for the box, MIT for the clients) and is also
offered under a commercial license. That second license only works if the
project can sublicense every line it ships, so contributions come in under a
[Contributor License Agreement](CLA.md).

**You keep your copyright.** It is a license with the right to sublicense, not
an assignment. To sign, add your GitHub username to [`CLA-SIGNERS`](CLA-SIGNERS)
in the same pull request as your first contribution:

```bash
echo "your-github-username" >> CLA-SIGNERS
```

That edit records your agreement and covers everything you contribute after it.
No bot, external service, or separate paperwork.

New files take the `SPDX-License-Identifier` of their directory (see
[LICENSE.md](LICENSE.md)); CI fails a file without one.


## Tests (no Docker)

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python tests/test_lifecycle.py
.venv/bin/python tests/test_dockerd.py
.venv/bin/python tests/test_token.py
.venv/bin/python tests/test_deskd.py
.venv/bin/python tests/test_browse.py
node web/web-ui/test_serve.mjs
node web/web-ui/test_phone.mjs
node web/web-ui/test_ntfy.mjs
node web/web-ui/test_telegram.mjs
node web/web-ui/test_nav.mjs
node web/web-ui/test_deploy.mjs
```

Acceptance tests (`tests/test_acceptance.py`) need Docker and a running cased.

## Layout

- `control-plane/` — REST API (composition root: `cased.py`)
- `image/` — desktop container
- `mcp/case_mcp.py` — MCP wrapper
- `web/web-ui/` — Drive UI
- `compose.yaml` — self-host stack

Be decent to people: [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

PRs that touch credential injection (`image/deskd.py` login/inject, `store.py`,
`links.py`) need maintainer review. [SECURITY.md](SECURITY.md) is the bar.
