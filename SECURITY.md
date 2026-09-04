# Security

Case holds logins. These are promises, with code you can read.

- **The password is typed into the page, never handed to the agent.** Login
  injects via CDP `Input.insertText` (not keystrokes, not clipboard); the secret
  stays out of API responses, logs, and exec output. See `image/deskd.py`.
- **deskd returns 423 while a credential is being injected.** Not just
  screenshots: `/exec`, `/action`, `/file` (read and write), `/eval`,
  `/auth/observe` and the capture reads all refuse until the injection finishes,
  and network capture drops anything in flight. The password field is cleared
  (`CLEAR_PASS`) before the gate reopens, so the first screenshot after a login
  sees an empty box.
- **That gate is "we do not hand it over", not "cannot obtain".** `computer_exec`
  runs bash in the same container as Chromium, and Chromium's CDP port is on
  that container's loopback. An agent that goes looking can reach what the
  browser holds. The 423 closes the paths Case itself offers; it is not a
  sandbox boundary.
- **Login only fires** when the page host matches the credential's `domains`.
- **MCP has no credential-write tool.** Secrets enter via the Drive UI, `/fill`,
  or `bin/case cred add`.
- **`computer_upload` only assigns files already on the computer.** The path
  must be under `/home/agent/`, at most 5MB, and the snapshot ref must be
  `input[type=file]`. Password/OTP-like inputs are refused. Bytes travel
  through deskd `GET /file`, never command stdout.
- **cased and Drive check `Host`, and `Origin` when one is present.** Anything
  else gets a 403. Allowed by default: `127.0.0.1`, `localhost`, `[::1]`, the
  compose service name, plus `CASE_PUBLIC_HOST` and anything in
  `CASE_ALLOWED_HOSTS`. This is what stops a DNS-rebinding page or a cross-site
  WebSocket open from driving a loopback install. Drive checks every request.
  cased checks the untokened ones — the token-in-URL doors always, everything
  when `CASE_TOKEN` is unset; with `CASE_TOKEN` set the rest of the API is
  bearer-only.
- **cased binds loopback by default.** Compose publishes `127.0.0.1:8787` and
  `127.0.0.1:4174`. Set `CASE_TOKEN` before exposing those ports.
- **Desktops sit on their own Docker network (`case-desks`).** Compose joins
  only cased to both networks, so Drive, the MCP server and one desktop's
  neighbours have no route to a desktop. Desktop containers publish no host
  ports under compose.
- **The live desk is behind the desk token.** With `CASE_DOCKER_NETWORK` set,
  websockify runs under basic auth `agent:$DESK_TOKEN` (`image/start.sh`).
  Drive's `/live/<id>/…` is a proxy to cased `/v1/computers/<id>/live/…`, and
  cased adds that header; the WebSocket upgrade checks `CASE_TOKEN` itself,
  because Starlette's HTTP middleware never sees a websocket scope. Host mode
  (`bin/case up`) sets no network, so websockify is open there on the desktop's
  loopback-published port.
- **`/fill`, `/assist` and `/answer` are doors opened by a token in the URL.**
  A human on a phone has no bearer header, so these three skip `CASE_TOKEN` and
  carry their own key: `/fill` and `/assist` links expire and are single-use,
  `/answer` is an HMAC over the handoff id. Anyone holding the URL is the human.
  Treat the links like one-time passwords.
- **Audit log** (`~/.case/audit/<date>.jsonl`): one line per API call with the
  caller's address, the query string, method, path, status and duration; request
  bodies that can carry secrets are redacted; response bodies are never logged.
  The three token doors log as `/fill/[token]` and friends, so the log records
  that a door was used without storing the key.
- **ntfy handoff notifications carry a full-desktop PNG.** When the computer is
  awake, the screenshot rides along as the message attachment. Everyone
  subscribed to the topic sees whatever was on screen, which is the argument for
  a random topic name.
- **DeathByCaptcha gets scheme, host and path only.** The page URL is stripped
  of query and fragment before the solve request leaves
  (`control-plane/login_flow.py`), so the session tokens and continuation URLs a
  login page carries in its query string stay on the box. Sitekey, that URL and
  a configured proxy are all that go out.
- **Drive screenshots and chat attachments persist on disk** under
  `~/.case/drive/shots` and `~/.case/drive/inbox` (Compose: `ui-data` via
  `CASE_HOME=/data`). They are content-addressed and kept until you delete the
  files or the volume. Deleting a thread does not erase them. Treat
  `~/.case/drive` / `ui-data` as sensitive chat material. Attachments never
  copy onto the computer; the model reads them from Drive. Max 4 files per
  turn, 5MB each; allowed types are PNG/JPEG/GIF/WebP, PDF, and text (including
  JSON, JS, XML).

## Self-host trust model

When you run Case yourself, these assumptions matter:

(a) **No CASE_TOKEN means an open API on the bind address.** Anything that can
reach cased's or Drive's bind address can drive them. The Host/Origin check only
rejects callers that address the box under a name it does not know; it is not
authentication. Set `CASE_TOKEN` if the host is shared or ports are not
loopback-only.

(b) **A desktop can still reach cased.** Docker bridges are bidirectional: cased
joins `case-desks` to dial the desktops, so a desktop can dial cased back on
`:8787`. The Host check does not help, because a container writes its own
`Host:` header. This does not require a compromise: anything running inside a
desktop by design has that reach, including whatever the agent starts through
`/exec` and any gateway installed into the box. The separate network keeps
Drive, MCP and the other desktops out of a desktop's reach; it does not put
cased out of reach. `CASE_TOKEN` is the thing that stops a compromised desktop
from driving the API. This is a known limit, not a closed hole.

(c) **`~/.case/` is secret-equivalent.** The Fernet encryption key lives beside the
SQLite database. Treat the whole directory like a password manager export: back it
up accordingly, restrict filesystem permissions, and do not copy it to untrusted
storage.

(d) **Desktops keep passwordless sudo by design.** The container is the agent's
sandbox; `no-new-privileges` is deliberately not set because passwordless sudo
requires setuid. Do not run untrusted code inside a desktop you also use for
personal browsing.

(e) **ntfy topics and Telegram bot tokens are bearer secrets.** Anyone who knows
a topic name can post or subscribe; anyone who holds the bot token can read and
send as the bot. Treat both like passwords; use random topic names, and revoke
the token in @BotFather if it leaks.

Report vulnerabilities privately via GitHub Security Advisories:
https://github.com/case-computers/case/security/advisories/new
Do not file public issues that include tokens, ntfy topics, bot tokens, or vault
contents.
