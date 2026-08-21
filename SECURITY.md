# Security

Case holds logins. These are promises, with code you can read.

- **Secrets never appear in API responses, logs, or exec output.** Login injects
  via CDP `Input.insertText` (not keystrokes, not clipboard). See
  `image/deskd.py`.
- **Screenshots return 423** while credentials are being injected.
- **Login only fires** when the page host matches the credential's `domains`.
- **MCP has no credential-write tool.** Secrets enter via the Drive UI, `/fill`,
  or `bin/case cred add`.
- **cased binds loopback by default.** Compose publishes `127.0.0.1:8787` and
  `127.0.0.1:4174`. Set `CASE_TOKEN` before exposing those ports.
- **Audit log** (`~/.case/audit/<date>.jsonl`): one line per API call; request
  bodies that can carry secrets are redacted; response bodies are never logged.

## Self-host trust model

When you run Case yourself, these assumptions matter:

(a) **No CASE_TOKEN means open API on the bind address.** Anything that can reach
cased's bind address (including desktop containers on the compose network) can
drive the REST API. Set `CASE_TOKEN` if the host is shared or ports are not
loopback-only.

(b) **`~/.case/` is secret-equivalent.** The Fernet encryption key lives beside the
SQLite database. Treat the whole directory like a password manager export: back it
up accordingly, restrict filesystem permissions, and do not copy it to untrusted
storage.

(c) **Desktops keep passwordless sudo by design.** The container is the agent's
sandbox; `no-new-privileges` is deliberately not set because passwordless sudo
requires setuid. Do not run untrusted code inside a desktop you also use for
personal browsing.

(d) **ntfy topics are bearer secrets.** Anyone who knows a topic name can post or
subscribe. Treat topic names like passwords; use random names and rotate if leaked.

Report vulnerabilities privately via GitHub Security Advisories:
https://github.com/case-computers/case/security/advisories/new
Do not file public issues that include tokens, ntfy topics, or vault contents.
