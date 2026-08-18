#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-only
# PID 1 of a case-desk container. Brings up display, desktop, VNC, Chromium, deskd.
# Traps SIGTERM (docker stop = sleep) so Chromium exits cleanly and flushes its profile.

RES="${DESK_RESOLUTION:-1280x800x24}"

rm -f /tmp/.X0-lock /tmp/.X11-unix/X0   # stale after docker stop; blocks Xvfb on wake
Xvfb :0 -screen 0 "$RES" -nolisten tcp &
XVFB_PID=$!
sleep 0.5

x11vnc -display :0 -forever -shared -nopw -quiet -bg
websockify --web /usr/share/novnc 6080 localhost:5900 &

for _ in $(seq 1 100); do [ -e /tmp/.X11-unix/X0 ] && break; sleep 0.1; done

dbus-launch startxfce4 &

# chromium exits (SIGTRAP) if stdin ever reads EOF — hold a never-EOF fifo open for it
mkfifo /tmp/.chrome-stdin 2>/dev/null
exec 9<>/tmp/.chrome-stdin

# Newer Playwright ships chrome-linux64; older used chrome-linux. Several installs can
# co-exist — take the last executable match, never a space-joined glob. Fail loud if missing.
CHROME=
for c in /opt/pw-browsers/chromium-*/chrome-linux*/chrome; do [ -x "$c" ] && CHROME=$c; done
[ -x "$CHROME" ] || { echo "chromium binary not found under /opt/pw-browsers" >&2; ls -la /opt/pw-browsers/*/ 2>&1; exit 1; }

chrome_loop() {
  while true; do
    "$CHROME" <&9 \
      --no-sandbox \
      --disable-gpu \
      --user-data-dir=/home/agent/chrome-profile \
      --remote-debugging-port=9222 \
      --no-first-run --no-default-browser-check \
      --disable-session-crashed-bubble --hide-crash-restore-bubble \
      --start-maximized \
      about:blank >>/tmp/chromium.log 2>&1
    echo "[chrome_loop] chromium exited rc=$?" >>/tmp/chromium.log
    sleep 1
  done
}
chrome_loop &
CHROME_PID=$!

/opt/deskd/bin/python /opt/deskd/deskd.py &
DESKD_PID=$!

term() {
  kill "$CHROME_PID" 2>/dev/null                     # stop the respawn loop first
  # Browser.close = graceful shutdown that persists fresh cookies; SIGTERM alone
  # is chromium's fast path and loses anything newer than the last commit batch
  curl -s -m 6 -X POST -H "Authorization: Bearer $DESK_TOKEN" \
    http://localhost:8000/quiesce >/dev/null 2>&1
  for _ in $(seq 1 40); do pgrep -f 'chrome-profile' >/dev/null || break; sleep 0.2; done
  pkill -TERM -f 'chrome-profile' 2>/dev/null        # fallback if CDP was unreachable
  for _ in $(seq 1 10); do pgrep -f 'chrome-profile' >/dev/null || break; sleep 0.2; done
  kill -TERM "$DESKD_PID" "$XVFB_PID" 2>/dev/null
  exit 0
}
trap term TERM INT

wait "$DESKD_PID"
