# Case API — v0 Specification

**Status:** Build contract for Phase 1. Greenfield — no EmulatedHumanoid or case-sdk/api code dependencies (patterns may be referenced, code is not imported). Direction context: `VISION.md`.
**Scope:** single-tenant, localhost, one Mac. No auth, no billing, no multi-user — see Non-Goals.
**Definition of done:** all acceptance tests A1–A10 pass.

---

## 1. System shape

Three components, one repo (`prod/`):

```
prod/
├── control-plane/     # "cased" — FastAPI app on host, owns Docker + SQLite + vault + Telegram
├── image/             # Dockerfile + in-container daemon "deskd"
├── mcp/               # MCP server wrapping the REST API
└── API_SPEC.md        # this file
```

- **cased** (host): REST API on `http://127.0.0.1:8787`. Manages computers via the Docker SDK. State in SQLite (`~/.case/case.db`). Secrets encrypted at rest (Fernet, key file `~/.case/key`, chmod 600). Owns the notification channel (ntfy). Proxies per-computer calls to that computer's daemon.
- **deskd** (in each container): HTTP on container port 8000, published to an ephemeral host port. Owns the display: screenshot, input, exec, file I/O, credential injection, blocker detection. Shares a per-computer bearer token (`DESK_TOKEN`) with cased; nothing else may call it.
- **case-mcp**: stdio MCP server exposing the tools in §9. Talks only to cased.

**The machine (image):** Ubuntu 24.04 (or Debian slim), Xvfb `:0` at **1280×800×24**, Xfce4, Chromium with persistent profile, noVNC/KasmVNC for human viewing (substrate spike picks the VNC stack — two-way door), `xdotool`, `scrot`, Python 3.12 + deskd. Non-root user `agent` (uid 1000). **Named Docker volume mounted at `/home/agent`** — this volume IS the durable identity (Chrome profile, files, dotfiles). One volume per computer, created at `POST /computers`, destroyed only by `DELETE`.

**Persistence semantics (the wedge):**
- `sleep` = `docker stop` (SIGTERM, 10s grace). RAM state is lost; disk state (sessions, cookies, files) survives. <!-- ponytail: stop/start hibernate; CRIU/snapshot when wake-latency or in-RAM state matters -->
- `wake` = `docker start`. Target ≤ 10s to `running`.
- Guarantee to document and test: a logged-in Chrome session survives sleep → wake → even a host reboot (A8).

---

## 2. Resources & state machine

### Computer

```json
{
  "id": "c_a1b2c3d4e5",
  "name": "sdr-1",
  "state": "running",
  "image": "case-desk:0.1",
  "created_at": "2026-07-08T10:00:00Z",
  "last_active_at": "2026-07-08T10:31:12Z",
  "resources": { "cpus": 1, "ram_mb": 2048, "disk_volume": "case-c_a1b2c3d4e5" },
  "display": { "width": 1280, "height": 800 },
  "vnc_url": "http://127.0.0.1:32771/vnc.html",
  "credentials": ["github", "gmail"],
  "pending_handoffs": 0,
  "tasks": 2,                                   // enabled schedules on this computer
  "next_run_at": "2026-07-09T06:00:00Z"         // soonest of them, or null
}
```

`tasks` and `next_run_at` ride along so a dashboard needs no second call per computer.

States: `creating` → `running` ⇄ `asleep`, plus transient `waking`, and terminal `deleted`.

| From | Action | To |
|---|---|---|
| — | `POST /computers` | `creating` → `running` (automatic) |
| `running` | `POST .../sleep` | `asleep` |
| `asleep` | `POST .../wake` | `waking` → `running` |
| `running` or `asleep` | `DELETE` | `deleted` (container **and volume** removed) |

Rules:
- `sleep` on `asleep` and `wake` on `running` are idempotent no-ops → `200` current object.
- `screenshot`, `action`, `exec`, `files`, `login` require `running`. On `asleep` → **`409`** `{"error":{"code":"asleep", ...}}`. With query `?wake=true` cased wakes first, then executes (adds wake latency).
- Any operation on `deleted`/unknown id → `404`.

### Handoff

```json
{
  "id": "h_9f8e7d",
  "computer_id": "c_a1b2c3d4e5",
  "kind": "otp",                    // otp | captcha | device | passkey | approval | question
  "prompt": "GitHub asks for a 6-digit code sent by SMS.",
  "screenshot_png_b64": "…",
  "status": "pending",              // pending | validating | completed | failed | expired
                                    // (legacy `answered` is read as `completed` for one release)
  "continuation": "submit_value",   // submit_value | verify_page | wait_external
  "created_at": "2026-07-08T10:31:00Z",
  "answer": null,                   // approve|deny|done only — OTP/codes never stored or returned
  "domain": "secure.chase.com"      // the site this is about, or null
}
```

**Status (definitive):** `pending` (waiting on human) → `validating` (human submitted;
platform verifying) → terminal `completed` | `failed`; TTL → `expired`. Agents must
poll `GET /handoffs/{id}` for these states after `handoff_pending` — never retry
`POST …/login` / `computer_login` to discover completion. `validating` is still in
progress. Legacy rows may still store `answered`; public JSON maps that to `completed`.

**`answer`:** only non-secret markers (`approve` / `deny` / `done`) are persisted or
returned. OTP and free-text challenge values are never written to SQLite or API JSON
(A11). Use `handoff_answered.value_present` on the event stream to know a value was
submitted without reading it back.

**Continuation:** `otp`/`approval`/`question` → `submit_value`; `captcha`/`device`/
`passkey` → `verify_page` (human clears the live desk; platform confirms the gate is
gone before completing).

`domain` is a field, not a substring of `prompt`: a human surface headlines the handoff
with the site name, and parsing deskd's free-text prompt for it is not acceptable. Set
from the `url` the login caller passed; `null` for handoffs with no site (approvals).

Created by: (a) the login flow when it detects a challenge, (b) deskd's blocker watchdog (§7), (c) a client explicitly (`POST .../handoffs`) when a brain wants human approval. Expire after 15 minutes → `expired`; the blocked operation fails with `handoff_expired`.

**List routes omit `screenshot_png_b64`.** A pending handoff holds a full-display PNG as base64, and a dashboard polls the pending list every 30 seconds while reading four scalar fields. Fetch `GET /handoffs/{id}` when the image (or definitive status) is actually wanted.

### Credential

Stored server-side encrypted; **the secret is never returned by any endpoint and never written to logs.**

```json
{ "name": "github", "username": "ishant@…", "domains": ["github.com"], "has_totp": true,
  "has_otp_phone": false, "created_at": "…",
  "last_verified_at": "2026-07-27T09:05:00Z",   // null until a login has been attempted
  "last_status": "ok",                          // ok | failed | challenge, or null
  "probe_url": "https://github.com/",           // optional session-keeper probe
  "has_proof_spec": true,                       // positive proof configured (raw spec never returned)
  "verification_hosts": ["github.com"] }        // Assist open_url allowlist (else domains)
```

`last_status` is the outcome of the most recent login attempt, written on every path:
success/failure at login time, `challenge` provisionally when 2FA is raised, then
overwritten with the real verdict when the human answers and the session resumes (or
`failed` if the handoff expires unanswered). Deliberately **not** the run vocabulary — a
login challenge is not a run outcome. A credential nobody has used lately reports its
last real result, however old; that is what `last_verified_at` is for.

`POST /computers/{id}/credentials` accepts optional `probe_url`, `proof_spec` (object),
and `verification_hosts` (list). Re-POSTing the same name without those fields **preserves**
the prior auth profile (password updates must not wipe keeper/Assist config).

---

## 3. REST endpoints

Base: `http://127.0.0.1:8787/v1`. JSON in/out unless noted. Errors: `{"error":{"code":"<slug>","message":"<human>"}}` with proper HTTP status (`400` bad input, `404` not found, `409` bad state, `423` locked, `504` daemon timeout).

### Lifecycle

| Method & path | Body | Returns |
|---|---|---|
| `POST /computers` | `{"name"?: str, "cpus"?: 1, "ram_mb"?: 2048}` | `201` Computer (wait until `running`, ≤60s else `504`). `cpus` 0.25–32, `ram_mb` 512–65536; outside that, or non-numeric → `400 bad_request`. Both are stored and reused whenever the container is rebuilt from the volume. |
| `GET /computers` | — | `200` `{"computers":[…]}` |
| `GET /computers/{id}` | — | `200` Computer |
| `DELETE /computers/{id}` | — on loopback; `{"name": str}` through the `/console` door | `204`. Destroys container + volume + credentials. Irreversible. Through the console door the computer's own name must be echoed back or `400 confirm_name`: a browser-held token is a month-long capability, and a client-side confirm dialog is not a control. |
| `POST /computers/{id}/sleep` | — | `200` Computer. **`409 auth_in_progress`** while a non-terminal AuthAttempt exists for this computer. |
| `POST /computers/{id}/wake` | — | `200` Computer (blocks until `running`, ≤30s) |
| `GET /health` | — | `200` `{"ok":true,"computers":n,"docker":true,"max_running":N,"running":n,"max_ram_mb":M,"ram_mb":m}` — `running` counts creating/waking/running against the awake cap; `ram_mb` is the memory those same computers hold, `max_ram_mb` the budget (`0` = none) |

Limit: max concurrent `running`/`creating`/`waking` computers = `CASE_MAX_RUNNING` (default **8** on Mac; hosted golden sets **1**). `POST /computers`, `POST …/wake`, and any observation/action call with `?wake=true` (screenshot, action, exec, eval, capture, login, files) beyond it → `409 too_many_running` (asleep computers don't count).

Second limit, because a count is not a memory guard once computers can be different sizes: the awake computers' `ram_mb` must sum to no more than `CASE_MAX_RAM_MB`, else `409 not_enough_ram`. Unset defaults to 75% of the RAM the engine reports (`/proc/meminfo`, i.e. the Docker VM on a Mac); `0`, or a host with no `/proc`, disables the check.

Shutdown: on `SIGTERM` cased sleeps every awake computer before exiting, so `docker compose down` (or `systemctl stop cased`) does not leave desktops running with nothing driving them. Compose gives it a 120s `stop_grace_period` to finish.

### Observation & action

| Method & path | Body / params | Returns |
|---|---|---|
| `GET /computers/{id}/screenshot` | — | `200` `image/png` (full display). **`423 credential_injection`** while injection in progress. |
| `POST /computers/{id}/action` | Action object (below) | `200` `{"ok":true, "screenshot_png_b64"?: str}` |
| `POST /computers/{id}/exec` | `{"command": str, "timeout_s"?: 30 (max 600), "cwd"?: "/home/agent"}` | `200` `{"exit_code":int,"stdout":str,"stderr":str,"truncated":bool}` (each stream capped 64 KB) |
| `POST /computers/{id}/eval` | `{"expression": str, "timeout_s"?: 20 (max 120)}` | `200` `{"ok":true,"value":any,"truncated":bool}` or `{"ok":false,"error":str}` — JS in the active browser tab (CDP `Runtime.evaluate`, by-value, promises awaited, result capped 64 KB). **`423 credential_injection`** during injection, same rule as screenshots. |
| `POST /computers/{id}/navigate` | `{"url": str, "timeout_s"?: 30 (max 120)}` | `200` `{"ok":true,"url":str,"title":str}` (url is post-redirect; `title` best-effort) or `{"ok":false,"error":str}` on timeout. Loads the URL and blocks until the *new* document reports `readyState:"complete"` — a sentinel on the outgoing document means a stale "complete" can't be mistaken for arrival, and reloading the same URL still counts. Same-page `#anchor` jumps never clear the sentinel → timeout; use `/eval`. Built on `/eval`, so **`423 credential_injection`** propagates. |
| `PUT /computers/{id}/files?path=/home/agent/x.csv` | raw bytes body | `201` `{"path":…,"bytes":n}` |
| `GET /computers/{id}/files?path=…` | — | `200` raw bytes / `404` |
| `POST /computers/{id}/capture` | `{"pattern": str}` (URL regex) | `200` `{"ok":true,"pattern":str}` — start a browser-level network wiretap (CDP `Network` domain) buffering response bodies whose URL matches. Survives SPA navigations, catches fetch **and** XHR (a page-world JS hook does neither reliably). Replaces any running capture. Bad regex → `400`. |
| `GET /computers/{id}/capture` | — | `200` `{"items":[…],"running":bool,"error":str\|null}` — drains the buffer (cleared each read; ring of 100, bodies capped 256 KB). Each item is `{"ts","url","status","body","truncated"}` on success, or `{"ts","url","status","error"}` if the body couldn't be fetched (request failed / evicted) — failures are surfaced, never dropped silently. **`423 credential_injection`** during injection. Response bodies only — request bodies and headers (Set-Cookie, Authorization) are never captured. |
| `DELETE /computers/{id}/capture` | — | `200` same shape, `running:false` — stop + final drain. Capture also stops on sleep (browser closes); restart it after wake. |
| `POST /computers/{id}/links` | `{"kind": "fill"\|"vnc", "ttl_s"?: int}` | `201` `{"token","kind","path","expires_at"}`. Mints a human URL token (loopback/operator only — never behind the public bearer). `fill` = single-use credential form (15 min default); `vnc` = multi-use desktop-view token (60 min default; `path` is a ready noVNC entry URL). Deliberately refuses `console`, so a console token — which *is* allowed to call this route — can never renew its own access. |
| `POST /v1/links` | `{"kind": "console"}` | `201` same shape with `path: null`. Box-scoped, console only: the computer-scoped mint 404s on a box with no computers, which is every new box. `path` is null because the dashboard HTML is not on the box (see `/console/*` below). 30 days, fixed — `ttl_s` is rejected rather than silently discarded, because every successful use slides the expiry back out to the full term. |
| `DELETE /v1/links` | — | `200` `{"burned": n}` — kills every outstanding fill/desk/console link on the box. `case-give --rotate` calls it: a link token is a bearer capability too. |
| `GET\|POST /fill/{token}` | HTML form / urlencoded fields `domains,username,secret,totp_seed?` | Outside `/v1`; reachable through Caddy without the bearer. GET serves the form (`410` if dead), with the computer name HTML-escaped — it is agent-chosen. POST normalises each website to a bare host (`https://Mail.Google.com/x` → `mail.google.com`; non-hosts → `400`), burns the token compare-and-set, then writes the credential (name = first domain). Request bodies are `[redacted]` in the audit log and the token is masked out of the logged path. |
| `GET /v1/desk/check` | headers `X-Forwarded-Uri`, `Cookie` | Caddy `forward_auth` target for `/desk/*`: `302` + `Set-Cookie case_desk` (first hit, token in the query — forward_auth only relays a non-2xx auth response to the browser; cookie Max-Age = the token's remaining life), `200` (cookie already held), `409` + an explanation page when the token's computer is asleep or its container predates `CASE_VNC_PORT`, else `401`. Never called by agents. |
| `GET /v1/console/check` | header `Authorization: Link <token>` | Caddy `forward_auth` target for `/console/*`: `200` or `401`. Every success slides the token's expiry back out to 30 days, so a bookmark someone opens daily never goes stale; a revoked token is not slid. `Link`, deliberately not `Bearer`: a console token cannot be pasted into an agent config, and a leaked `cs_` bearer cannot open the console. |

**Action object** — discriminated union on `type`. Coordinates: integer pixels, origin top-left of 1280×800.

```json
{"type":"click",        "x":640, "y":400, "button":"left"}      // button: left|right|middle, default left
{"type":"double_click", "x":640, "y":400}
{"type":"move",         "x":640, "y":400}
{"type":"drag",         "from":{"x":10,"y":10}, "to":{"x":200,"y":300}}
{"type":"scroll",       "x":640, "y":400, "dy":-3}               // dy in wheel ticks; +down, -up
{"type":"type",         "text":"hello world"}                    // types into focused element
{"type":"key",          "keys":"ctrl+l"}                         // xdotool key syntax
{"type":"wait",         "ms":500}                                // max 5000
```

Optional on every action: `"screenshot": true` → response includes post-action screenshot (saves the agent loop a round-trip). Optional `"delay_ms"`: pause before capture (default 300).

### Credentials & login

| Method & path | Body | Returns |
|---|---|---|
| `POST /computers/{id}/credentials` | `{"name":str, "username":str, "secret":str, "totp_seed"?: str, "otp_phone"?: str, "domains":[str], "probe_url"?: str, "proof_spec"?: object, "verification_hosts"?: [str]}` | `201` Credential (public view; `has_proof_spec` not raw spec) |
| `GET /computers/{id}/credentials` | — | `200` list (public views only) |
| `GET /credentials` | — | `200` `{"credentials":[…]}` across every computer, each row plus `computer_id` + `computer_name`. Same public view; this widens the audience, never the shape. |
| `DELETE /computers/{id}/credentials/{name}` | — | `204` |
| `POST /computers/{id}/login` | `{"credential":str, "url":str, "idempotency_key"?: str, "proof_spec"?: object}` | `200` AuthAttemptResult (compat LoginResult fields retained; blocks ≤95s deskd; with `CASE_DBC_*` captcha auto-solve + settle/verify, worst case ~240s — MCP client uses 280s; Caddy door is 300s) |
| `GET /auth-attempts/{id}` | — | `200` AuthAttempt — one-shot snapshot |
| `GET /auth-attempts/{id}/wait` | `after_revision?`, `after_handoff_id?`, `timeout_s?` (default 30, max 270) | `200` `{changed, wait_status, attempt, login_result?}` — long-poll until cursor advances, attempt ends, or timeout. Subscribe-before-read; emits `auth_attempt_updated` on CAS / handoff pointer change. |
| `POST /auth-attempts/{id}/cancel` | `{"expected_revision"?: int}` | `200` AuthAttempt (also fails any open child handoff) |

### Durable authentication attempts

One vault login that needs human help is **one AuthAttempt** spanning zero or more
child Handoffs (challenges). MCP connections/processes carry **no** workflow state —
every call names an explicit `attempt_id` (MCP 2026-07-28 stateless core). Site-specific
behavior lives in optional adapters/config (`probe_url`, `proof_spec`, verification-host
policy); core orchestration never branches on a named website.

**Attempt states:** `created` → `advancing` → `awaiting_human` → `advancing` → `proving`
→ `authenticated`. Terminal alternatives: `unverified` | `failed` | `expired` |
`cancelled`. One active (non-terminal) attempt per computer (SQLite partial unique index
+ CAS). Active attempts pin the computer awake: `POST …/sleep` → `409 auth_in_progress`.

**AuthAttempt (public):**

```json
{
  "id": "a_…",
  "computer_id": "c_…",
  "credential": "github",
  "status": "awaiting_human",
  "revision": 3,
  "current_handoff_id": "h_…",
  "target_url": "https://example.com/login",
  "proof_level": "configured",
  "created_at": "…",
  "updated_at": "…"
}
```

`proof_level` is `configured` when a positive `proof_spec` is present, else `heuristic`
(compat only — session keeper must not treat heuristic success as durable health).

**Login / advance flow (cased-owned; deskd is observation/action only):**

1. `start_attempt` (idempotent on `idempotency_key`) → navigate → inject credentials via
   CDP `Input.insertText` (never xdotool / API / logs; `423` during injection).
2. `advance_attempt` classifies generic observations (`visible_fields`,
   `challenge_signals`, `frame_markers`, `page_state`). TOTP with vault seed or capable
   CAPTCHA auto-solve may clear a step without a human.
3. Human-needed step → create child Handoff (`attempt_id`, `sequence`, `revision`) →
   notify Assist → attempt `awaiting_human`. One pending/validating handoff per attempt.
4. On challenge completion, **do not** mark credential success. Re-enter `advancing`
   (supports CAPTCHA → OTP → device → email verification).
5. No challenge left → `proving`. Positive `proof_spec` (URL predicate and/or host-bound
   selector / protected-page expression) must verify. Missing/false proof →
   `unverified` (never `authenticated`). Only `authenticated` emits
   `login_completed(success)` and updates `last_verified_at` / `last_status=ok`.

**Compat LoginResult** still returned by `POST …/login`:

- `{"status":"success","attempt_id","revision","proof_level"}` when authenticated
- `{"status":"handoff_pending","handoff_id","attempt_id","revision"}` when awaiting human
- `{"status":"failed"|"unverified","reason","attempt_id","revision"}` otherwise

Agents prefer MCP `auth_attempt_wait` (backed by `GET /auth-attempts/{id}/wait`)
immediately after `handoff_pending` — same turn, no user nudge. Use
`GET /auth-attempts/{id}` / `auth_attempt_get` for one-shot inspection only.
Never retry `POST …/login` to discover completion; transport retries reuse
`idempotency_key`.

Domain guard: deskd refuses injection if the page's origin isn't in the credential's
`domains` (exact host or subdomain) → `400 domain_mismatch`.

**Credential auth profile (optional public fields):** `probe_url`, `has_proof_spec`,
`verification_hosts` (exact/subdomain allowlist for Assist `open_url`). Secrets never
leave the vault. URLs must stay inside credential domains / server-owned adapter policy.

### Handoffs

Child challenges of an AuthAttempt when `attempt_id` is set; standalone agent/watchdog
handoffs when null.

| Method & path | Body | Returns |
|---|---|---|
| `GET /handoffs?status=pending` | — | `200` list (all computers), **without `screenshot_png_b64`**. `status` filter optional; omit for all. Prefer `GET /handoffs/{id}` / `GET /auth-attempts/{id}` for definitive state. |
| `GET /handoffs/{id}` | — | `200` Handoff, screenshot included — definitive for a single challenge |
| `GET /computers/{id}/handoffs` | — | `200` list |
| `POST /computers/{id}/handoffs` | `{"kind":"approval"|"question"|"device"|"captcha"|"passkey", "prompt":str}` | `201` Handoff. `approval`/`question` → Assist code/text form; `device`/`captcha`/`passkey` → live `/desk` Assist. Agent-minted captcha/device/passkey do **not** auto-resume a vault login (no `login_credential`). Prefer `computer_login` for vaulted 2FA/OTP walls. |
| `POST /handoffs/{id}/answer` | `{"value":str, "expected_revision"?: int}` | `200` Handoff (Assist / notify path in production; this is the API path + test hook). Moves `pending` → `validating` → `completed`\|`failed` (or back to `pending` on soft verify fail). Stale revision → `409`. |

**Assist (attempt-scoped):** one magic-link exchange/session may cover the whole attempt.
`GET /assist/{token}` resolves current attempt + current handoff each request. Typed
actions only: `submit_value`, `open_desk`, `mark_done`, `wait_external`, `open_url`.
`GET /assist/{token}/state` (`Cache-Control: no-store`) returns status, revision,
instructions, allowed actions — never OTPs or submitted URLs. Poll every 2–3s via
same-origin static JS. `POST /assist/{token}/open` navigates the **remote** Chromium to
a human-pasted HTTPS URL after strict host policy (no IP/localhost/private, no userinfo,
final-origin check); the URL is never persisted or logged. Website relay stays
transport-only (Assist URL + display metadata + expiry).

**Notification channel:** one interface in cased — `notify(handoff)` outbound. Default adapter: central email relay (`RelayNotifier`); `CASE_NOTIFY_CHANNEL=ntfy` keeps the ntfy path for local/operator use.

- Relay: `CASE_HANDOFF_RELAY_URL` + `CASE_NOTIFY_TOKEN` (`cn_…`) on the box; owner email is derived centrally — never accepted from the box. Assist magic link in the email; no screenshots/codes in the body.
- ntfy (dev override): `CASE_NTFY_URL` / `CASE_NTFY_TOPIC` / `CASE_NTFY_ANSWER_TOPIC` as before.

### Runs

A run is one execution of a schedule. `status` exists because `exit_code` cannot tell a
skipped run from a timed-out one — both are `-1`.

| Method & path | Body / params | Returns |
|---|---|---|
| `GET /runs?limit=50` | `limit` clamped to 1…200 | `200` `{"runs":[…]}` newest-first across every schedule. Each: `{id, schedule_id, computer_id, computer_name, started_at, ended_at, exit_code, summary, status, has_screenshot}` where `status` is `ok\|fail\|skipped`. `artifact_path` is deliberately absent — it is a host path; callers get a boolean and fetch bytes by id. |
| `GET /runs/{rid}/screenshot` | — | `200` `image/png`, the final screenshot the run captured. `404 no_screenshot` if the run has none. Serves only files whose recorded path resolves inside `~/.case/runs`, so a poisoned row cannot turn this into an arbitrary file read. |

`GET /schedules/{sid}/runs` still returns raw per-schedule rows, `artifact_path` included.
It is loopback/agent-only and is **not** reachable through the console door.

### The console door (`/console/*`)

A third human door beside `/fill` and `/desk`, on the same `links` table. Outside `/v1`
from the caller's point of view: Caddy authenticates with `forward_auth` →
`GET /v1/console/check`, strips the `/console` prefix, and reverse-proxies to cased
stamping `X-Case-Door: console` (`header_up` *replaces* any client-supplied value, so a
browser cannot forge it).

The dashboard HTML is **not** served here — it is static, off-box, and fetches this door
cross-origin, so the door answers JSON only and carries CORS headers plus an
unauthenticated `OPTIONS` preflight. Exactly one origin is allowed, from
`CASE_CONSOLE_ORIGIN` (empty by default — this door is a hosted-deployment feature and
stays shut on a self-hosted box); the operator tool that mints console links reads that
same value back off the box when composing the
URL, so a link can never point at an origin the door will refuse. A minted console URL therefore points at the
dashboard's own origin with the box host as a query parameter and the token in the
fragment (`…/console?box=<host>#t=<token>`); a fragment never reaches a server or an
access log.

**The door does not proxy all of `/v1`.** cased enforces an allowlist (`CONSOLE_ROUTES`)
against the stamped header; everything else answers `404`:

| Reachable through `/console` | Why |
|---|---|
| `GET /v1/computers` | the COMPUTERS tab |
| `GET /v1/runs`, `GET /v1/runs/{rid}/screenshot` | the ACTIVITY tab |
| `GET /v1/credentials` | the CREDENTIALS tab |
| `GET /v1/handoffs`, `POST /v1/handoffs/{id}/answer` | the handoff strip — the whole point |
| `POST /v1/computers/{id}/wake` | WAKE |
| `POST /v1/computers/{id}/links` | DESK and ADD LOGIN mint `fill`/`vnc` here |
| `DELETE /v1/computers/{id}` | human-only by design; it destroys the volume |
| `GET /v1/connect` | CONNECT modal — one-shot MCP paste reveal |
| `POST /v1/mcp/rotate` | CONNECT modal — mint new `cs_`, burn fill/desk, spare caller console |

Not reachable, and each for a reason: `exec` and `eval` (arbitrary execution), `files`
(arbitrary read/write), `capture`, `login`, `POST /v1/computers` (creates and bills),
`POST /v1/computers/{id}/credentials` (plaintext secret — `/fill` exists precisely so
this never crosses a human surface), `DELETE /v1/links` (revoking everyone else's
access), `POST /v1/links` (minting itself a fresh console token),
`POST /v1/mcp/seed` (loopback/`case-give` only — deposits the reveal copy), and
`GET /schedules/{sid}/runs` (host paths).

**Connect reveal** returns `{host, token, paste:{claude,json}, seen_at}` once from a
Fernet copy in SQLite seeded at provision; a second GET has `token: null` and
`seen_at` set. **Rotate** requires `{"confirm":"rotate"}`, rewrites
`/etc/caddy/case.env` via `case-door-write`, burns outstanding fill/desk links while
sparing the caller's console `Link`, and returns the new paste once. Live MCP auth
remains Caddy-only; the SQLite row is never a durable re-fetchable copy after first
view. A console bookmark can obtain the agent door — that is deliberate product
surface (reveal-once + rotate), not an accident.

### Events

`GET /v1/events` — **SSE stream** (`text/event-stream`), all computers. Event types:

```
event: state_changed      data: {"computer_id":…,"from":"running","to":"asleep"}
event: handoff_created    data: {Handoff minus screenshot}
event: handoff_answered   data: {"handoff_id":…,"value_present":true}
event: login_completed    data: {"computer_id":…,"credential":…,"status":"success"|"failed"}
```

Also `GET /v1/computers/{id}/events` filtered to one computer. Heartbeat comment every 15s.

---

## 4. deskd internal API (container port 8000)

Not public; cased-only (bearer `DESK_TOKEN`, injected as env at container create). Mirrors the proxied surface: `GET /screenshot`, `POST /action`, `POST /exec`, `PUT|GET /file`, `POST /login`, `POST /login/resume` (handoff answer), `GET /blocker`, `POST /capture/start` + `GET|DELETE /capture` (network wiretap; own persistent CDP ws — a daemon thread pumping `Network.*` events, since `Tab.cmd`'s recv loop drops events). Implementation detail beyond this spec — the contract is the public API in §3.

---

## 5. Storage

- SQLite `~/.case/case.db`: tables `computers`, `credentials` (secret + totp_seed as Fernet ciphertext), `handoffs`, `events` (ring, last 1000).
- Fernet key `~/.case/key`, created on first run, `chmod 600`.
- Log policy: bodies of `/credentials` and `/handoffs/*/answer` are never logged; login flows log status only.
- Audit log: cased writes one JSONL line per API call to `~/.case/audit/<date>.jsonl` — `{ts,session,method,path,status,ms,req}`. `session` = the `X-Case-Session` request header (case-mcp sends one id per process, so a run's calls group). Request bodies truncated to 2 KB; bodies of secret-bearing routes redacted (`/credentials`, `/handoffs/*/answer` OTP relays, `/files` uploads); **response bodies never logged** (screenshots, file/capture contents stay out). `/health` and `*/events` skipped. Answers "what did this agent do on this machine" without agents self-logging transcripts.

## 6. Sizing defaults

Image ≤ 3 GB. Per computer: 2 GB RAM / 1 CPU (`docker run --memory --cpus`), volume unbounded. Defaults overridable at create. 16 GB Mac target: 8 running (limit above), unlimited asleep.

## 7. Blocker watchdog (v0, minimal)

deskd polls active Chrome tab (CDP, 2s interval) for challenge signals — URL + DOM text regex set: `captcha|verify you.?re human|two.?factor|2fa|one.?time (code|password)|enter the code|unusual activity|suspicious login`. Match during a login flow → handoff (login path above). Match outside a login flow → emit `handoff_created` with `kind:"question"` so a brain/human can intervene.

**Login-path captcha auto-solve (optional, capability-gated):** local detection and verification stay authoritative. Auto-solve is quarantined behind `solve_if_capable` — Death By Captcha is the only vendor this release, and only for declared capabilities: `recaptcha_v2` (DBC type 4), `recaptcha_enterprise` (type 25, requires `CASE_DBC_PROXY`; non-overload upload failure may try type 4 per DBC FAQ #18), and `arkose` (type 6). Off unless `CASE_DBC_USERNAME`+`CASE_DBC_PASSWORD` or `CASE_DBC_AUTHTOKEN` is set. Unsupported families, missing enterprise proxy, `is_correct=false`, and `service-overload` return **immediately** (no long poll) so the caller creates a typed captcha/`verify_page` handoff (Assist email). Flow when capable: detect → solve (≤60s wall-clock only while DBC is actually working) → inject → settle (~9s) → verify (challenge phrases **and** no visible password field) → only then `/login/resume` approve. On any verify failure the solve is reported bad, resume is **not** called (deskd `state["login"]` stays intact), and the flow falls through to human handoff — a failed/unverified solve must never look like a successful login. No CapSolver/2Captcha this release.

The same solve also runs on the **post-login gate**: LinkedIn's `/checkpoint/challenge` keeps its reCAPTCHA in an iframe, which deskd's top-document text scan cannot see, so it reports `success` while the session is not established. cased probes the tab after every successful login and, when gated, tries the optional solver with `resume=False` (or skips straight to handoff when DBC is off). Failure / unavailability raises a CAPTCHA handoff carrying **no** login credential: the human clears the check via Assist `/desk` and marks done; nothing tries to resume a login deskd has already released. **Data egress:** only the sitekey/publickey, page URL, and configured proxy leave the box. Screenshots, credentials, cookies, and page text never go to DBC. Image-mode DBC is out of scope. Mid-session (watchdog) captchas are not auto-solved.

## 8. Non-goals (v0 — do not build)

Auth/API keys · multi-tenant · billing/metering · cloud deploy · warm pools · CRIU/live snapshots · managed brain · agent-native identity · Windows/macOS machines · per-computer networking policies · UI beyond noVNC.

## 9. MCP server (`case-mcp`)

stdio server, tools mapping 1:1 onto §3 (thin — no logic client-side):

| Tool | Args | Returns |
|---|---|---|
| `computer_create` | `name?` | Computer JSON |
| `computer_list` | — | list |
| `computer_screenshot` | `computer_id` | MCP image content |
| `computer_action` | `computer_id`, action fields (§3) | ok + optional image |
| `computer_exec` | `computer_id`, `command`, `timeout_s?` | exit/stdout/stderr |
| `computer_eval` | `computer_id`, `expression`, `timeout_s?` | `{ok,value,truncated}` — JS in the active tab |
| `computer_navigate` | `computer_id`, `url`, `timeout_s?` | `{ok,url,title}` — load and wait for the new document |
| `computer_capture_start` | `computer_id`, `url_pattern` | `{ok,pattern}` — start network wiretap |
| `computer_capture_read` | `computer_id`, `stop?` | `{items,running,error}` — drain (stop ends it) |
| `computer_login` | `computer_id`, `credential`, `url`, `idempotency_key?` | AuthAttemptResult / LoginResult (`attempt_id` always present). On `handoff_pending`, immediately call `auth_attempt_wait` in the same turn. |
| `auth_attempt_get` | `attempt_id` | AuthAttempt — one-shot snapshot; prefer `auth_attempt_wait` after login |
| `auth_attempt_wait` | `attempt_id`, `since_revision?`, `max_wait_s?` (default/cap 240) | LoginResult-shaped wait result — chains REST long-polls under Caddy's 300s door; never restarts login |
| `computer_file_put` / `computer_file_get` | `computer_id`, `path`, (`content_b64`) | ack / bytes |
| `computer_sleep` | `computer_id` | Computer (no `computer_wake`: every other tool wakes implicitly; REST `/wake` stays). `409 auth_in_progress` when pinned. |
| `handoff_request` | `computer_id`, `prompt`, `kind?` | Handoff (`approval`\|`question`\|`device`\|`captcha`\|`passkey`) — not for vault-login journey checks |
| `handoff_list` | — | pending handoffs (prefer `auth_attempt_wait` for login journeys) |
| `handoff_get` | `handoff_id` | Handoff (challenge status; prefer `auth_attempt_wait` for journey state) |
| `schedule_create` | `computer_id`, `prompt`, `kind?`, `spec?`, `name?`, `jitter_s?` | Schedule |
| `schedule_list` | `computer_id` | list |
| `schedule_delete` | `schedule_id` | `"deleted"` |
| `schedule_run` | `schedule_id` | `{status, schedule}` (async) |
| `schedule_runs` | `schedule_id` | run history |

Note: `credentials` add/delete is deliberately **not** an MCP tool — secrets enter via human-driven CLI/HTTP only, never through a model's tool call.

Plus a tiny CLI: `case new|ls|sleep|wake|rm|vnc|cred add` (thin curl wrappers; `cred add` prompts for secret without echo).

## 10. Acceptance tests (Definition of Done)

Automatable unless marked manual. These are the build's exit gates.

- **A1 boot:** `POST /computers` → `running` ≤ 60s; screenshot is a non-black 1280×800 PNG showing a desktop.
- **A2 exec:** `exec {"command":"echo hi"}` → exit 0, stdout `hi`.
- **A3 act:** key `ctrl+l` in Chromium (address bar), type `https://news.ycombinator.com`, key `Return`, wait, screenshot contains visible HN header (assert via OCR or manual).
- **A4 files:** PUT 1 MB file → GET → byte-identical; file visible at path via exec.
- **A5 vault hygiene:** add credential, run login; grep cased+deskd logs and all API responses captured during the test for the secret string → **zero hits**; screenshot during injection returns 423.
- **A6 TOTP:** login to a TOTP-enabled test account (e.g. GitHub test acct) with `totp_seed` stored → `success`, no handoff created.
- **A7 handoff loop (manual):** trigger `handoff_request` → ntfy push with screenshot arrives on phone → reply via answer topic (and Approve button for `kind=approval`) → handoff `answered`, event emitted.
- **A8 THE WEDGE:** login to a real session site (Gmail/GitHub) → `sleep` → restart Docker Desktop (host-reboot proxy) → `wake` → screenshot shows logged-in state, no login page. Cookie jar intact.
- **A9 first brain:** from Claude Code via case-mcp only: create computer → open HN → return the #1 story title → sleep. No manual API calls.
- **A10 fleet:** 6 computers running concurrently on this Mac, A2 passes on each; then all 6 asleep → host RAM usage back to baseline.
- **A11 durable auth:** one `attempt_id` survives cased/deskd restart between start and Assist answer; CAPTCHA→OTP→proof stays one attempt; challenge completion alone never writes credential `ok`; missing `proof_spec` ends `unverified`; sleep blocked during active attempt; Assist `open_url` rejects private/IP hosts and never persists the URL; secrets absent from DB answers / audit / events / relay.

## 11. Open (decided during build, not blockers)

Substrate base (Cua OSS image vs kasmweb vs hand-rolled Dockerfile — timeboxed spike, pick what passes A1–A3 fastest) · VNC stack · Chromium vs Chrome · OCR assert helper for A3.
