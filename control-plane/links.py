# SPDX-License-Identifier: AGPL-3.0-only
"""Human link tokens (/fill, /desk, /console), minted URLs are the whole auth.

The human side of a hosted box has no account system: a minted unguessable
token IS the auth. `fill` tokens are single-use (they gate a credential
write); `vnc` tokens are multi-use until expiry (a noVNC session is dozens of
asset requests plus a websocket, all carrying the same cookie); `console`
tokens are a bookmark, long-lived, box-scoped, and renewed by use.
"""
import ipaddress
import json
import os
import re
import secrets
from datetime import datetime, timezone
from html import escape as _html_escape
from urllib.parse import parse_qs, parse_qsl, urlencode, urlparse, urlsplit

from store import store
from util import iso_in, now

# A console link is a bookmark, not an errand: 30 days, and every successful use
# slides it forward (see console_check). A short TTL would lock every customer out
# daily and turn the operator into a link-vending machine. An abandoned link still
# dies on its own, and --rotate still kills it instantly.
TTL_S = {"fill": 900, "vnc": 3600, "console": 2592000}
# Per-kind ceiling. A single global min(ttl, 86400) would silently clamp the console's
# 30 days to 1 day while console_check's slide pushed it back out to 30, three
# different lifetimes for one token.
TTL_MAX = {"fill": 3600, "vnc": 86400, "console": 2592000}
# noVNC is served by websockify inside the container; Caddy strips /desk and
# proxies to it. path=desk/websockify keeps the websocket behind the same door.
VNC_ENTRY = "/desk/vnc.html?token={t}&autoconnect=1&resize=scale&path=desk/websockify"


def mint(cid, kind, ttl_s=None):
    # `is None`, not truthiness: ttl_s=0 must mean "dead on arrival", not "default".
    ttl = min(max(int(TTL_S[kind] if ttl_s is None else ttl_s), 0), TTL_MAX[kind])
    token = secrets.token_urlsafe(32)
    expires = iso_in(ttl)
    store.insert_link(token, cid, kind, expires)
    # No path for `console`: that page is static on Vercel, not on the box. A
    # box-relative URL would send a browser to a door that answers 401 JSON, because
    # a navigation carries no Authorization header. The caller composes the URL.
    path = {"vnc": VNC_ENTRY.format(t=token), "fill": f"/fill/{token}"}.get(kind)
    return {"token": token, "kind": kind, "path": path, "expires_at": expires}


def valid(token, kind):
    """The row if `token` is a live link of `kind`, else None."""
    row = store.get_link(token)
    if not row or row["kind"] != kind or row["used_at"] is not None:
        return None
    if row["expires_at"] <= now():   # ISO-Z strings: lexicographic == chronological
        return None
    return row


def seconds_left(row):
    """Whole seconds until this link dies, the cookie must not outlive its token."""
    exp = datetime.strptime(row["expires_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return max(int((exp - datetime.now(timezone.utc)).total_seconds()), 0)


HOSTNAME = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")


def normalize_domain(s):
    """A bare hostname from whatever a human typed, or None.

    People paste `https://mail.google.com/inbox`, `WWW.Gmail.com`, `user@gmail.com`.
    Stored raw, that string matches no page (deskd compares hosts) and, as the
    credential name, contains a `/`, which the DELETE route cannot even address.
    """
    s = (s or "").strip().lower()
    s = s.split("//", 1)[1] if "//" in s else s
    s = s.split("/")[0].split("?")[0].split("#")[0].split("@")[-1].split(":")[0]
    s = s[4:] if s.startswith("www.") else s
    return s if HOSTNAME.match(s) else None


def host_allowed(host, allowlist):
    """Exact host or subdomain of an allowlisted registrable/name, same rule as deskd."""
    host = (host or "").lower().rstrip(".")
    if not host or not allowlist:
        return False
    return any(host == d.lower() or host.endswith("." + d.lower()) for d in allowlist)


def _is_ip_literal(host):
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _blocked_ip(host):
    """True when host is an IP that must never be opened (private/loopback/…)."""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return bool(ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


def validate_assist_open_url(url):
    """Return hostname if `url` is a public HTTPS URL safe for Assist open_url.

    Rejects non-https, userinfo, IP literals, localhost, private/link-local, and
    malformed hosts. Raises ApiError(400). Does not check credential allowlists.
    """
    from errors import ApiError
    if not url or not isinstance(url, str):
        raise ApiError(400, "bad_request", "url is required")
    url = url.strip()
    parts = urlparse(url)
    if parts.scheme != "https":
        raise ApiError(400, "bad_request", "url must be https")
    if parts.username is not None or parts.password is not None or "@" in (parts.netloc or ""):
        raise ApiError(400, "bad_request", "url must not contain userinfo")
    host = parts.hostname
    if not host:
        raise ApiError(400, "bad_request", "url host required")
    host = host.lower().rstrip(".")
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        raise ApiError(400, "bad_request", "url host not allowed")
    if _is_ip_literal(host) or _blocked_ip(host):
        raise ApiError(400, "bad_request", "url host not allowed")
    if not HOSTNAME.match(host):
        raise ApiError(400, "bad_request", "url host not allowed")
    return host


def verification_allowlist(computer_id, credential_name):
    """Hosts Assist may open: credential.verification_hosts JSON, else domains."""
    row = store.get_credential(computer_id, credential_name)
    if not row:
        return []
    keys = row.keys() if hasattr(row, "keys") else row
    raw = row["verification_hosts"] if "verification_hosts" in keys else None
    if raw:
        try:
            hosts = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError):
            hosts = None
        if isinstance(hosts, list):
            out = [h.lower().rstrip(".") for h in hosts
                   if isinstance(h, str) and h.strip()]
            if out:
                return out
    try:
        domains = json.loads(row["domains"] or "[]")
    except (TypeError, ValueError):
        domains = []
    return [d.lower().rstrip(".") for d in domains if isinstance(d, str) and d.strip()]


def strip_token(uri, prefix="/desk/"):
    """`uri` minus its token param, where the browser goes once it holds the cookie.

    Always a same-origin path under `prefix`: a Location echoed raw from the request
    could start with `//host` and turn the door into an open redirect.
    """
    parts = urlsplit(uri or "")
    path = parts.path if parts.path.startswith(prefix) else prefix + "vnc.html"
    q = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "token"]
    return path + ("?" + urlencode(q) if q else "")


def desk_check(forwarded_uri, cookie_header):
    """Caddy forward_auth contract for /desk/*: returns (link_row|None, set_cookie_token|None).

    The first request carries ?token= in the URI; we answer with the row plus the
    token to hand back as a cookie, so the page's asset and websocket requests (which
    drop the query string) keep passing. The cookie is the token itself, no signing
    machinery; the DB row already enforces kind and expiry. The row goes back so the
    caller can check *which* computer the token was minted for.
    """
    q = parse_qs(urlsplit(forwarded_uri or "").query).get("token", [""])[0]
    row = valid(q, "vnc") if q else None
    if row:
        return row, q
    cookies = dict(p.strip().split("=", 1) for p in (cookie_header or "").split(";") if "=" in p)
    tok = cookies.get("case_desk", "")
    if tok:
        v = valid(tok, "vnc")
        if v:
            return v, None
    return None, None


def console_check(auth_header):
    """The console door's auth. `Authorization: Link <token>`, deliberately NOT
    `Bearer`, so a console token can never be pasted into an agent config and a
    leaked cs_ bearer can never open the console.

    Sliding expiry: a live bookmark must not go stale under someone who is using it
    every day. Revocation is unaffected, burn_all_links sets used_at, which valid()
    checks first, and slide_link refuses to touch a burned row.
    """
    if not auth_header or not auth_header.startswith("Link "):
        return None
    row = valid(auth_header[5:].strip(), "console")
    if row:
        store.slide_link(row["token"], iso_in(TTL_S["console"]))
    return row


FILL_HTML = """<!doctype html><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Add a login — Case</title>
<style>
 body{font:16px/1.5 system-ui;margin:0;background:#f5f5f4;color:#1c1917}
 main{max-width:26rem;margin:8vh auto;padding:0 1rem}
 label{display:block;margin:.9rem 0 .25rem;font-weight:600;font-size:.9rem}
 input{width:100%;padding:.6rem;border:1px solid #d6d3d1;border-radius:8px;font-size:1rem;box-sizing:border-box}
 button{margin-top:1.2rem;width:100%;padding:.7rem;border:0;border-radius:8px;background:#1c1917;color:#fff;font-size:1rem}
 p.note{font-size:.85rem;color:#57534e}
</style>
<main>
 <h1>Add a login to {computer}</h1>
 <p class=note>Stored encrypted on <b>your</b> computer. The machine types it into the
 site's own login page — your agent never sees it. Delete it any time.</p>
 <form method=post>
  <label>Website</label><input name=domains required placeholder="gmail.com (comma-separate more)">
  <label>Username / email</label><input name=username required autocomplete=off>
  <label>Password</label><input name=secret type=password required>
  <label>2FA secret (TOTP, optional)</label><input name=totp_seed autocomplete=off
    placeholder="the long code under 'can't scan the QR?'">
  <button>Save to vault</button>
 </form>
</main>"""

# {back} is replaced by with_console_back(), a real button back to the Vercel
# console, never a history.back(). After Save the fill token is burned, so the
# browser's Back lands on "Link expired"; refreshing that page does too. The
# console bookmark still lives in sessionStorage on the console origin, so a
# ?box= link (no token in the URL) is enough to reopen the CREDENTIALS tab.
_BACK_CSS = ("a.back{display:inline-block;margin-top:1.5rem;padding:.7rem 1.4rem;"
             "border:0;border-radius:8px;background:#1c1917;color:#fff;"
             "font-size:1rem;text-decoration:none}")

DONE_HTML = """<!doctype html><meta charset=utf-8><title>Saved — Case</title>
<style>body{font:16px/1.5 system-ui;background:#f5f5f4;color:#1c1917}
main{max-width:26rem;margin:16vh auto;text-align:center;padding:0 1rem}
""" + _BACK_CSS + """</style>
<main><h1>Saved ✓</h1><p>The login is in the vault. This link is now dead —
ask for a new one to add another.</p>{back}</main>"""

GONE_HTML = """<!doctype html><meta charset=utf-8><title>Link expired — Case</title>
<style>body{font:16px/1.5 system-ui;background:#f5f5f4;color:#1c1917}
main{max-width:26rem;margin:16vh auto;text-align:center;padding:0 1rem}
""" + _BACK_CSS + """</style>
<main><h1>Link expired</h1><p>This link was already used or has expired.
Ask for a fresh one.</p>{back}</main>"""


def console_return_url(box_host, origin=None, tab="credentials"):
    """URL back to the static console for this box. Empty when we cannot form one.

    Hosted-only: a self-hosted box has no console, so with no origin configured
    this returns "" and the Back button is dropped.

    Never embeds a console or fill token, the console page already holds its
    Link token in sessionStorage on that origin. Putting a capability in this
    href would put it in Referer logs on the next hop.
    """
    host = (box_host or "").strip().lower()
    if not HOSTNAME.match(host):
        return ""
    # Caller passes the seeded box_meta origin when it has one (preview deploys);
    # env is the fallback for boxes that never ran mcp/seed.
    base = (origin or os.environ.get("CASE_CONSOLE_ORIGIN") or "").rstrip("/")
    if not base.startswith("https://"):
        return ""
    frag = tab if tab in ("computers", "activity", "credentials") else "credentials"
    return f"{base}/console?box={host}#{frag}"


def with_console_back(page_html, box_host, origin=None, tab="credentials"):
    """Inject the Back-to-console button, or strip the placeholder when we can't."""
    href = console_return_url(box_host, origin=origin, tab=tab)
    if not href:
        return page_html.replace("{back}", "")
    # href is built from a regex-pinned host + https origin, still escape for the
    # attribute, so a future change to console_return_url cannot open an XSS hole.
    btn = (f'<p><a class=back href="{_html_escape(href, quote=True)}">'
           f'← Back to console</a></p>')
    return page_html.replace("{back}", btn)

# Served instead of Caddy's bare 502 when the door is open but there is nothing behind
# it, the desktop is asleep, or its container predates the pinned VNC port.
NOTREADY_HTML = """<!doctype html><meta charset=utf-8><title>Desktop not ready — Case</title>
<style>body{font:16px/1.5 system-ui;background:#f5f5f4;color:#1c1917}
main{max-width:28rem;margin:16vh auto;text-align:center}
code{background:#e7e5e4;padding:.1rem .3rem;border-radius:4px}</style>
<main><h1>Desktop not ready</h1><p>{why}</p></main>"""

ASLEEP = "This computer is asleep. Ask your agent to wake it, then reopen this link."
STALE_PORT = ("This computer was created before the view door existed. The operator "
              "needs to recreate its container once (the disk, logins and files survive).")
