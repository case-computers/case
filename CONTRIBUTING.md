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

Use Python 3.12 and Node 22, matching CI.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
for f in tests/test_*.py; do
  case "$f" in tests/test_acceptance.py) continue ;; esac
  .venv/bin/python "$f" || exit 1
done
npm --prefix web ci
npm --prefix web test
```

## Acceptance tests (Docker)

```bash
docker build -t case-desk:acceptance image
CASE_ACCEPTANCE_IMAGE=case-desk:acceptance .venv/bin/python -m pytest -q tests/test_acceptance.py
```

The suite starts its own cased on a random loopback port, with a temporary vault
and token. It removes only computers created by that run. Failed runs retain
logs and screenshots in the printed scratch directory. `CASE_KEEP=1` also keeps
the primary test computer and its volume for inspection.

A7 needs ntfy and a phone. A8 is skipped unless `CASE_A8=1` is set,
because it restarts the Docker VM and interrupts every container using it.
Run that check only on a dedicated test machine.

## Layout

- `control-plane/`: REST API (composition root: `cased.py`)
- `image/`: desktop container
- `mcp/case_mcp.py`: MCP wrapper
- `web/web-ui/`: Drive UI
- `compose.yaml`: self-host stack

Be decent to people: [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

PRs that touch credential injection (`image/deskd.py` login/inject, `store.py`,
`links.py`) need maintainer review. [SECURITY.md](SECURITY.md) is the bar.
