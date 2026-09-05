# Case: persistent computers for AI agents

Case gives AI agents a persistent Linux desktop with Chromium, files, and saved
logins. Run it with Docker and connect your agent over MCP, or use Drive, the web
interface. Files and saved logins stay with the computer across sleep, wake, and
restart.

![Case demo](.github/demo.gif)

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
[server setup guide](docs/setup.md#on-a-server).

## Run your first task

### Use Drive in your browser

Drive needs an OpenAI or Anthropic API key to run tasks.

1. Click **DRIVE** next to your computer.
2. Click **KEY**, choose your provider, enter its API key, and click **SAVE**.
3. Send a task, such as: `Open example.com and tell me what is on the page.`

You can watch the desktop as the agent works. The **FILES** and **CREDENTIALS**
tabs let you browse files and save logins for that computer.

### Connect an existing agent

Case supports MCP (Model Context Protocol), which lets an agent use the desktop
as a set of tools. The local MCP address is `http://127.0.0.1:8788/mcp`.

For Claude Code:

```bash
claude mcp add --transport http case http://127.0.0.1:8788/mcp
```

Then ask the agent to list your Case computers and run a task on the one you
created. This uses your agent's model connection; you do not need to add a key
in Drive. See [MCP configuration](docs/setup.md#mcp-configuration) for Cursor
and other clients.

## Stop and start again

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

## Configuration and documentation

The default settings are enough to try Case locally. Optional settings are
listed in [.env.example](.env.example).

- [Setup and configuration](docs/setup.md): memory limits, server hosting,
  manual installation, MCP clients, and troubleshooting.
- [Phone chat](docs/phone-chat.md): Telegram and ntfy setup, replies, and approvals.
- [Architecture and storage](docs/architecture.md): components, networking,
  files, and browser profiles.
- [Security](SECURITY.md): credentials, authentication, and self-hosting limits.
- [Contributing and tests](CONTRIBUTING.md): development setup and test commands.

This repository is the self-hosted computer. The hosted fleet, including DNS,
HTTPS, and managed images, is a separate product.

## License

AGPL-3.0 for `control-plane/` and `image/` (the box and API). MIT for `mcp/`,
`bin/case`, and `web/` (clients). A **commercial license** is available if the
AGPL does not fit your deployment. See [LICENSE.md](LICENSE.md).

Contributions come in under a [CLA](CLA.md), signed once by adding your GitHub
username to [`CLA-SIGNERS`](CLA-SIGNERS) in your first pull request. You keep
your copyright.
