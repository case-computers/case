# Case™: persistent computers for AI agents

Case gives AI agents a durable Linux desktop (Chromium, files, logins) on any machine
with Docker. Sleep, wake, or reboot: the identity stays on the volume. You bring the
brain (Claude, Cursor, Codex, or the Drive UI with your provider key).

## Quick start

Needs Docker 20.10+ with Compose v2.

1. Clone the repo.

```bash
git clone https://github.com/ishu86/case.git && cd case
```

2. Start the stack.

```bash
docker compose up --build
```

3. Open http://127.0.0.1:4174/deploy in your browser to create a computer.

4. Default MCP URL (compose): http://127.0.0.1:8788/mcp

   Point Claude Code at it (use `127.0.0.1`, not `localhost`: the SDK 421s on a
   Host mismatch).

```bash
claude mcp add --transport http case http://127.0.0.1:8788/mcp
```

## Faster start

Skip the desktop image build (Debian + Xfce + Chromium, a few minutes) by pulling it:

```bash
docker pull ghcr.io/ishu86/case-desk:latest
echo "CASE_IMAGE=ghcr.io/ishu86/case-desk:latest" >> .env
docker compose up
```

## Optional

### API-only mode

Run the control plane and MCP without the Drive UI.

```bash
docker compose up cased mcp --build
```

### Cursor config

```json
{ "mcpServers": { "case": { "type": "http", "url": "http://127.0.0.1:8788/mcp" } } }
```

### Laptop without Compose

Use the host-side CLI instead of compose.

```bash
bin/case up
```

Then run Drive locally:

```bash
CASE_LOCAL=1 CASE_URL=http://127.0.0.1:8787 node web/web-ui/serve.mjs
```

MCP over stdio (venv python, full paths):

```bash
export CASE_URL=http://127.0.0.1:8787/v1
claude mcp add case -- $(pwd)/.venv/bin/python $(pwd)/mcp/case_mcp.py
```

### Token hardening (optional)

Copy `.env.example` to `.env`, generate a token, and set `CASE_TOKEN` before
exposing ports off loopback.

```bash
cp .env.example .env
openssl rand -hex 32
```

### Stop everything

```bash
docker compose down
```

## Details

### Separate database warning

`bin/case up` runs the control plane on the host with its database in `~/.case`.
Compose uses a Docker volume instead. Same engine, same desktops, different
bookkeeping: computers you create one way are not listed by the other, and both
want port 8787, so run one at a time.

### RAM budget

Pick each computer's size when you create it (`+ New computer` → SIZE, default 2 GB and
1 CPU). The sizing sticks to the computer and is reapplied every time its container is
rebuilt, so a box you made big stays big.

Two limits keep a host from being oversold:

- `CASE_MAX_RUNNING`: how many computers may be awake at once (compose default `4`).
- `CASE_MAX_RAM_MB`: how much memory those awake computers may hold in total.
  Unset means 75% of what the Docker engine reports, so it is usually right without
  being set. A create or wake that would exceed it returns `409`, which is the polite
  version of the OOM killer.

Asleep computers cost disk only, and disk is not capped: Docker's local volume driver
has no size limit, so Case does not pretend to offer one.

On macOS the number that matters is the VM's RAM, not the Mac's. Colima defaults to
4 GB, which is one 2 GB computer plus headroom. `colima start --cpu 4 --memory 8` if you
want more.

A laptop is fine to try it. A small always-on Linux box is where it belongs: a computer
that is asleep because your Mac shut the lid is not a computer an agent can be employed
on.

### On a server

Do not publish 4174/8787/8788 off loopback. Set `CASE_TOKEN` in `.env` for Drive
and the REST API (`http://<host>:4174/?token=…`). `CASE_TOKEN` does **not** lock
`:8788`: that door is loopback-only in compose; hosted boxes put Caddy in front.
HTTPS and DNS are not shipped here.

## What you get

- `image/`: the desktop (`case-desk`)
- `control-plane/`: REST API, vault, sleep/wake, handoff
- `mcp/` + `bin/case`: drive it from an agent or the CLI
- Drive UI: chat, live desk, files, credentials, teach-a-task
- Deployer (`/deploy`): create, sleep, wake, delete computers

Hosted fleet (DNS, HTTPS door, golden images) is a separate product. This repo is the
box you can run yourself.

## Tests

No Docker:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python tests/test_lifecycle.py
.venv/bin/python tests/test_dockerd.py
.venv/bin/python tests/test_token.py
node web/web-ui/test_serve.mjs
node web/web-ui/test_nav.mjs
```

Acceptance tests need a running stack (`tests/test_acceptance.py`).

## License

AGPL-3.0 for `control-plane/` and `image/` (the box and API). MIT for `mcp/`,
`bin/case`, and `web/` (clients). A **commercial license** is available if the
AGPL does not fit your deployment. See [LICENSE.md](LICENSE.md).

Contributions come in under a [CLA](CLA.md), signed once by comment on your
first pull request. You keep your copyright.

Case™ and the Case logo are trademarks of Daemon Labs; the licenses above grant
no rights to the name. Fork freely, rename your fork.
