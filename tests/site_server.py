# SPDX-License-Identifier: MIT
"""Test login site — uploaded into a case computer and run there via exec.

Routes:
  GET  /plain        login form -> POST /plain/submit -> welcome (+session cookie) | failed
  GET  /totp         login form -> POST /totp/submit  -> TOTP page -> POST /totp/code -> welcome
  GET  /whoami       <title>signed-in</title> if session cookie present, else <title>anonymous</title>
Every POST body is appended to /tmp/received.json (test fixture storage, not product logs).
"""
import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs

USER = os.environ["SITE_USER"]
PASS = os.environ["SITE_PASS"]
RECORD = "/tmp/received.json"

FORM = """<html><head><title>Test Login</title></head><body><h1>Sign in</h1>{msg}
<form method="post" action="{action}">
<p><input name="email" type="email" autocomplete="username" placeholder="Email"></p>
<p><input name="password" type="password" autocomplete="current-password" placeholder="Password"></p>
<p><button type="submit">Sign in</button></p></form></body></html>"""
TOTP = """<html><head><title>Two-factor</title></head><body>
<h1>Two-factor authentication</h1><p>Enter the one-time code from your authenticator app.</p>
<form method="post" action="/totp/code">
<p><input name="code" autocomplete="one-time-code" inputmode="numeric" placeholder="6-digit code"></p>
<p><button type="submit">Verify</button></p></form></body></html>"""
WELCOME = ('<html><head><title>Dashboard</title></head><body>'
           '<h1>Welcome, agent. You are signed in.</h1></body></html>')


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, html, cookie=None):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(html.encode())

    def form(self):
        n = int(self.headers.get("Content-Length", 0))
        data = {k: v[0] for k, v in parse_qs(self.rfile.read(n).decode()).items()}
        rec = json.load(open(RECORD)) if os.path.exists(RECORD) else []
        rec.append({"path": self.path, "data": data})
        json.dump(rec, open(RECORD, "w"))
        return data

    def do_GET(self):
        if self.path.startswith("/plain"):
            self.send(FORM.format(action="/plain/submit", msg=""))
        elif self.path.startswith("/totp"):
            self.send(FORM.format(action="/totp/submit", msg=""))
        elif self.path.startswith("/whoami"):
            ok = "session=letmein" in (self.headers.get("Cookie") or "")
            self.send(f"<html><head><title>{'signed-in' if ok else 'anonymous'}</title></head>"
                      f"<body><h1>{'Signed in as agent.' if ok else 'Not signed in.'}</h1></body></html>")
        else:
            self.send("<html><title>home</title><body>test site</body></html>")

    def do_POST(self):
        d = self.form()
        ok = d.get("email") == USER and d.get("password") == PASS
        if self.path == "/plain/submit":
            if ok:
                self.send(WELCOME, cookie="session=letmein; Path=/; Max-Age=2592000")
            else:
                self.send(FORM.format(action="/plain/submit",
                                      msg="<p>Incorrect password, try again.</p>"))
        elif self.path == "/totp/submit":
            if ok:
                self.send(TOTP)
            else:
                self.send(FORM.format(action="/totp/submit",
                                      msg="<p>Incorrect password, try again.</p>"))
        elif self.path == "/totp/code":
            self.send(WELCOME, cookie="session=letmein; Path=/; Max-Age=2592000")
        else:
            self.send("<html><body>?</body></html>")


HTTPServer(("127.0.0.1", 8088), H).serve_forever()
