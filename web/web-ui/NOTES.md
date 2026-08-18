# Drive UI + deployer

Drive (`/`) is chat + live desk + files + credentials against **local cased**.
The deployer (`/deploy`) owns create / sleep / wake / delete. They share this
server and the REST proxy; they do not share chrome. Chat keys (OpenAI or
Anthropic) arrive per-request; `web/node_modules` has both SDKs.

**One seat.** Drive is pointed at exactly one computer, because a computer *is*
one screen, one Chromium profile, one set of logins — two jobs on one box fight
over the active tab, and `computer_action` clicks pixels that know nothing about
tabs. So the sidebar lists threads, never computers, and `/deploy` owns the
choice (the DRIVE button, stored in `localStorage['case.drive.computer']`).
Drive never switches computers on its own: landing on the wrong desk means
landing in someone else's sessions, so a missing pick says so and stops.

**Transport:** `CASE_LOCAL=1` (default when `CASE_URL` is loopback or compose
`cased`). Talks to cased on `CASE_URL` — no SSH tunnel. Compose sets
`http://cased:8787/v1` and `CASE_DOCKER_NETWORK=case` so `/live` proxies noVNC
at `case-<id>:6080` on the compose network.

**Files view** uses `computer_exec` `find` (`/api/fs`) and cased `GET /files`
(`/api/file`).

Run: `node web/web-ui/serve.mjs` → http://127.0.0.1:4174/  and  /deploy
(or `docker compose up` from the repo root)

Tests: `node web/web-ui/test_serve.mjs` and `node web/web-ui/test_nav.mjs`
