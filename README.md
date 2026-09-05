# Case: persistent computers for AI agents

Case gives AI agents a persistent Linux desktop with Chromium, files, and saved
logins. Run it with Docker and connect your agent over MCP, or use Drive, the web
interface. Files and saved logins stay with the computer across sleep, wake, and
restart.

![Case demo](.github/demo.gif)

**[Managed Case](https://case.computer):** We run the computers for you, including
DNS, HTTPS, and managed images.

**Self-hosted:** Run the open source version on your own machine or server.
Start with Docker below.

## Quick start

You need Git and Docker 20.10+ with Compose v2. Start Docker, then run:

```bash
git clone https://github.com/case-computers/case.git
cd case
docker compose up --build -d
```

The first build may take a few minutes. Case runs in the background once it starts.

Open the [computers page](http://127.0.0.1:4174/deploy), click **+ New computer**,
enter a name, and press Enter. Wait for the computer to show **AWAKE**.

This setup is for local use. For remote access, follow the
[server hosting instructions](#server-hosting) and read [SECURITY.md](SECURITY.md).

## Run your first task

### Use Drive in your browser

Drive needs an OpenAI or Anthropic API key to run tasks.

1. Click **DRIVE** next to your computer.
2. Click **KEY**, choose your provider, enter its API key, and click **SAVE**.
3. Send a task, such as: `Open example.com and save a summary to /home/agent/example.txt.`

Watch the desktop as the agent works, then open **FILES** to read the summary.
The file stays on the computer when you sleep and wake it. Use **CREDENTIALS**
to save logins for that computer.

### Connect an existing agent

Case supports MCP (Model Context Protocol), which lets an agent use the desktop
as a set of tools. The local MCP address is `http://127.0.0.1:8788/mcp`.

For Claude Code:

```bash
claude mcp add --transport http case http://127.0.0.1:8788/mcp
```

Then ask the agent to list your Case computers and run a task on the one you
created. This uses your agent's model connection; you do not need to add a key
in Drive. Cursor:

```json
{ "mcpServers": { "case": { "type": "http", "url": "http://127.0.0.1:8788/mcp" } } }
```

## Features

- **A real desktop:** navigate pages, click and fill elements, upload files,
  take screenshots, run commands, and inspect network activity.
- **Vault logins:** save a credential once through a one-time link or Drive.
  Case types it into the site's login page without returning the password to
  the agent. See the [security model](SECURITY.md) for the limits of this protection.
- **Human handoff:** 2FA codes, captchas, and approvals pause the run for a human
  to help through Drive, Telegram, or an Assist link.
- **Skills:** the agent saves a completed task as a `SKILL.md` on the computer
  and follows it next time. The file survives restarts.
- **Schedules:** recurring runs use the computer's saved identity.
- **Phone chat:** send tasks and answer handoffs through Telegram or ntfy.
  See [phone setup](#phone-chat).

## Optional setup

The defaults are enough to try Case locally. Optional settings are listed in
[.env.example](.env.example). For Compose, put them in `.env` next to
`compose.yaml`, then run `docker compose up -d` to apply changes. Edit an existing
`.env` rather than replacing it.

<a id="stop-and-start-again"></a>
<details>
<summary>Stop and start again</summary>

From the repository directory, stop Case with:

```bash
docker compose down
```

Your computers' files and saved logins remain on their Docker volumes. To start
Case again:

```bash
docker compose up -d
```

Open the [computers page](http://127.0.0.1:4174/deploy) and click **WAKE** next
to the computer you want to use.

</details>

<a id="phone-chat"></a>
<details>
<summary>Phone chat: Telegram and ntfy</summary>

Send tasks and answer handoffs through Telegram or ntfy. Both are optional.
Drive connects out to the service, so phone chat works without exposing a local
port. Your host and Docker must stay running to receive messages and run tasks.

Phone tasks use a shared thread named `Phone`. They need a provider key on the
server because there is no browser tab to supply one. If you do not have a `.env`
file yet, copy `.env.example` to `.env` next to `compose.yaml`. Add these settings
to that file, replacing the placeholder with your key:

```dotenv
CASE_DRIVE_PROVIDER=openai              # or anthropic
CASE_DRIVE_API_KEY=<your-provider-api-key>
```

You can also set `CASE_DRIVE_MODEL`. Choose either service below to finish setup.

### Telegram

1. Open [@BotFather](https://t.me/BotFather) in Telegram, send `/newbot`, and
   follow the prompts to create a bot. Copy its token. Use `/setjoingroups` to
   disable adding the bot to groups.
2. Add the token to `.env`:

   ```dotenv
   CASE_TELEGRAM_TOKEN=<your-bot-token>
   ```

3. Start or update Drive from the repository directory:

   ```bash
   docker compose up -d ui
   ```

4. Send `/start` to your bot. It replies with your chat ID. Add that ID to `.env`:

   ```dotenv
   CASE_TELEGRAM_CHAT_ID=<your-chat-id>
   ```

5. Run `docker compose up -d ui` again to apply the setting, then send a task
   such as `What is on the screen?`.

Only the configured chat can drive the computer. The bot shows a typing indicator
while it works, then sends the result or error. Long replies arrive in separate
messages. Approval handoffs have **Approve** and **Deny** buttons; for a code
handoff, reply to its prompt with the code. Pending handoffs are sent again when
Drive reconnects after a restart.

### ntfy

[ntfy](https://ntfy.sh) delivers messages through named topics. Anyone who knows
an unprotected topic's name can read and write to it. Generate a random topic
name and treat it like a password:

```bash
openssl rand -hex 32
```

1. Install the [ntfy app](https://docs.ntfy.sh/subscribe/phone/) and subscribe
   to the generated topic. If you use your own ntfy server, point the app at it.
2. Add these settings to `.env`, replacing the topic placeholder:

   ```dotenv
   CASE_NTFY_CHAT=1
   CASE_NTFY_URL=https://ntfy.sh
   CASE_NTFY_TOPIC=<your-random-topic>
   CASE_NTFY_TOKEN=
   ```

   Change `CASE_NTFY_URL` if you use another server. For a protected topic, set
   `CASE_NTFY_TOKEN` to an access token that can publish and subscribe. See
   ntfy's [authentication instructions](https://docs.ntfy.sh/publish/#authentication).

3. Apply the settings to Drive and cased:

   ```bash
   docker compose up -d
   ```

4. Send a task using one of these methods. Replace `<topic>` with your topic
   name, and use your server's URL if you host ntfy yourself:

   - Android: use the message bar in the topic view. Enable **Show message bar**
     in settings if it is hidden.
   - iOS: create a Shortcut with **Ask for Input**, then **Get Contents of URL**.
     Use `https://ntfy.sh/<topic>`, method POST, and the input as the request body.
     Add the Shortcut to your home screen or run it with Siri.
   - Terminal: run `curl -d "check my mail" "https://ntfy.sh/<topic>"`.

For a protected topic, include an `Authorization: Bearer <token>` header when
sending from a Shortcut or curl. Drive posts `Working`, then the result or error,
to the same topic. It marks its own posts so it does not read them as new tasks.

### Replies and handoffs

When one handoff is waiting, your next message answers it. If several are
waiting, prefix the answer with its handoff ID, such as `h_ab12 483920`.
With no handoff waiting, a message steers the current Phone task or starts a
new task on the first computer returned by cased. Create a computer before
sending your first task.

`approve`, `deny`, `done`, or a bare code with nothing waiting gets a
"Nothing waiting" reply. Telegram skips ordinary messages more than ten minutes
old when Drive reconnects and tells you which ones it skipped. ntfy does not
replay messages sent while Drive was stopped. Send the task again if it was missed.

ntfy handoff notifications can include an Assist link and signed approval buttons.
These require `CASE_PUBLIC_HOST` and an HTTPS reverse proxy in front of cased.
They are omitted without a public hostname. See [server hosting](#server-hosting)
to enable them; phone chat itself does not require this setup.

</details>

<a id="computer-size-and-memory"></a>
<details>
<summary>Computer size and memory</summary>

Choose a computer's size under **+ New computer**, then **SIZE**. The default is
2 GB of RAM and 1 CPU. Case keeps that choice when it recreates the container.

| Setting | What it controls | Compose default |
| --- | --- | --- |
| `CASE_MAX_RUNNING` | Maximum number of awake computers | 4 |
| `CASE_MAX_RAM_MB` | Total RAM that awake computers may reserve, in MB | 75% of the memory visible to cased |

On macOS with Compose, that memory comes from the Docker VM. A 4 GB VM has room
for one 2 GB computer within the default budget. To give Colima more memory when
starting it:

```bash
colima start --cpu 4 --memory 8
```

If creating or waking a computer exceeds a limit, Case returns `409`. Sleep
another computer or adjust the limits before retrying. Asleep computers use
disk space only; Case does not limit their volume size.

</details>

<a id="server-hosting"></a>
<details>
<summary>Server hosting and remote access</summary>

Keep ports 4174, 8787, and 8788 bound to loopback. For remote access, put an HTTPS
reverse proxy in front of Case. Generate a token:

```bash
openssl rand -hex 32
```

Set `CASE_TOKEN` to that value in `.env`. Add your proxy hostname to
`CASE_ALLOWED_HOSTS`, using commas for multiple hostnames. Run
`docker compose up -d` to apply the settings.

`CASE_TOKEN` protects Drive and the REST API. Open Drive through the HTTPS proxy
with `?token=<your-token>` on the first visit. MCP on port 8788 has no built-in
client authentication: keep it local or configure authentication at its proxy.
Setting `CASE_TOKEN` alone does not protect the MCP endpoint.

For ntfy Assist links and approval buttons, set `CASE_PUBLIC_HOST` to the public
hostname of your cased proxy, without a scheme. That hostname is allowed without
repeating it in `CASE_ALLOWED_HOSTS`. These links carry their own access tokens;
treat them as secrets.

Case does not configure DNS or HTTPS for you. Read the
[self-hosting trust model](SECURITY.md#self-host-trust-model) before providing
remote access.

</details>

<details>
<summary>Run the API and MCP without Drive</summary>

```bash
docker compose up cased mcp --build -d
```

An MCP agent or the CLI can create and use computers in this mode. The MCP
address remains `http://127.0.0.1:8788/mcp`.

</details>

<details>
<summary>Install without Compose</summary>

This runs cased and Drive as host processes. Desktops still run in Docker.
Install Python 3.12, Node 22, and Docker first. On macOS, `bin/case up` uses
Colima; start it before building the image.

Use either this setup or Compose at a time. Both use port 8787, but their vaults
are separate: this setup uses `~/.case`, while Compose uses a Docker volume.
Computers created through one setup do not appear in the other.

With Docker running, install the dependencies and build the desktop image:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
npm --prefix web ci
docker build -t case-desk:0.1 image
```

Start cased, then Drive:

```bash
bin/case up
CASE_LOCAL=1 CASE_URL=http://127.0.0.1:8787 node web/web-ui/serve.mjs
```

Open the [computers page](http://127.0.0.1:4174/deploy). Drive runs in the
foreground in this terminal. Host processes use environment variables, not the
Compose `.env` file. If cased runs directly on macOS, set `CASE_MAX_RAM_MB`
explicitly; its automatic memory budget requires Linux's `/proc/meminfo`.

For a client that uses stdio MCP, [case-mcp.json](case-mcp.json) starts
`mcp/case_mcp.py` with Python. It needs the installed Python dependencies and
access to cased. The scheduler uses this file with Claude's `--mcp-config`.

</details>

<details>
<summary>Use a published desktop image</summary>

If a desktop image has been published, you can pull it instead of building it:

```bash
docker pull ghcr.io/case-computers/case-desk:latest
```

After a successful pull, set this value in `.env`:

```dotenv
CASE_IMAGE=ghcr.io/case-computers/case-desk:latest
```

Then run `docker compose up -d`. If the image is unavailable, use the source
build in the [quick start](#quick-start).

</details>

<a id="storage-and-backups"></a>
<details>
<summary>Storage and backups</summary>

Each computer has a Docker volume mounted at `/home/agent`. It holds files,
Chromium's profile, saved logins, and skills. It survives sleep, wake, and
container recreation. Deleting a computer through Case deletes this volume too.
Changes outside `/home/agent`, such as installed system packages, do not survive
container recreation.

cased stores the credential database and encryption key in `~/.case` for a host
installation, or the `case-home` volume mounted at `/data` in Compose. Back up
the database and key together, along with the desktop volumes you want to keep.
Treat these backups as sensitive data.

Drive keeps `threads.json`, screenshots under `drive/shots`, and attachments
under `drive/inbox` in its home directory. This is `~/.case` by default; Compose
uses the `ui-data` volume mounted at `/data`. Deleting a thread does not remove
its screenshots or attachments. Files added through the plus menu stay on the
Drive host for the model to read; they are not copied onto the desktop computer.

</details>

<a id="how-case-works"></a>
<details>
<summary>How Case works</summary>

Drive (`web/web-ui/`) and MCP agents (`mcp/`) send requests to cased
(`control-plane/`), which manages computers, the vault, and handoffs. The CLI
(`bin/case`) uses the same REST API. Each desktop runs deskd inside the Docker
image built from `image/`.

Desktop tools cover navigation, numbered element snapshots, clicking and
filling by reference, hovering, uploads, screenshots, commands, files, and
network capture. Uploads select files already under `/home/agent`. Navigate and
click responses include the first 2000 characters of page text.

Compose puts desktops on `case-desks` with no published host ports. cased joins
that network and the application network. It relays the live desktop view and
adds the desktop's token. Desktops can reach their peers and cased, so service
tokens remain necessary. See [SECURITY.md](SECURITY.md) for the trust model.

`CASE_TURN_TOKENS` defaults to 2 million and caps cumulative input tokens for a
Drive turn. Messages sent during a turn use `/api/chat/steer`. Client development
details are in the [Drive README](web/web-ui/README.md).

</details>

## Troubleshooting

- If Docker cannot connect to its daemon, start Docker and check `docker info`.
- If a port is already in use, check whether another Case installation is running.
  Stop that installation before starting this one.
- If Drive does not open, check `docker compose ps` and
  `docker compose logs cased ui` for startup errors.
- If creating or waking a computer fails because of memory or capacity, check
  [computer size and memory](#computer-size-and-memory) and Docker's available RAM.

<a id="desktop-network-migration"></a>
<details>
<summary>Move an existing desktop to the new network</summary>

A desktop created before the network split keeps its original network until
its container is recreated. Waking it alone does not change the network.

Sleep the computer, remove its container with `docker rm case-<id>`, then wake
it again. Replace `<id>` with the computer ID. Keep its home volume: that is
where its files, browser profile, and saved logins live.

</details>

## Contributing and security

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and tests, and
[SECURITY.md](SECURITY.md) for the trust model and vulnerability reporting.

## License

AGPL-3.0 for `control-plane/` and `image/` (the box and API). MIT for `mcp/`,
`bin/case`, and `web/` (clients). A **commercial license** is available if the
AGPL does not fit your deployment. See [LICENSE.md](LICENSE.md).

Contributions come in under a [CLA](CLA.md), signed once by adding your GitHub
username to [`CLA-SIGNERS`](CLA-SIGNERS) in your first pull request. You keep
your copyright.
