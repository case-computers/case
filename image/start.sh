#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-only
# PID 1 of a case-desk container. Brings up display, window manager, VNC, Chromium, deskd.
# Traps SIGTERM (docker stop = sleep) so Chromium exits cleanly and flushes its profile.

RES="${DESK_RESOLUTION:-1280x800x24}"

# libXcursor honors this in every X client — the one switch that themes the
# cursor everywhere (chromium reads it via GTK settings too, seeded below).
export XCURSOR_THEME=case XCURSOR_SIZE=32

# Seed per-user desktop config when missing (first boot, or volumes from older
# images); never overwrite — the volume is the user's.
seed() { [ -f "$2" ] || { mkdir -p "$(dirname "$2")"; cp "$1" "$2"; }; }
seed /usr/share/case/gtk-settings.ini ~/.config/gtk-3.0/settings.ini
seed /usr/share/case/xfce4-panel.xml ~/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-panel.xml
seed /usr/share/case/xfwm4.xml ~/.config/xfce4/xfconf/xfce-perchannel-xml/xfwm4.xml

rm -f /tmp/.X0-lock /tmp/.X11-unix/X0   # stale after docker stop; blocks Xvfb on wake
# -fbdir /dev/shm: mmap the framebuffer to a file deskd reads for screenshots
Xvfb :0 -screen 0 "$RES" -nolisten tcp -fbdir /dev/shm &
XVFB_PID=$!

for _ in $(seq 1 100); do [ -e /tmp/.X11-unix/X0 ] && break; sleep 0.1; done

x11vnc -display :0 -forever -shared -nopw -quiet -bg
/opt/deskd/bin/websockify --web /usr/share/novnc 6080 localhost:5900 &

# Bare WM, no desktop environment. --compositor=off: xfwm4's XRender compositor
# turns chromium's content area white on Xvfb. dbus session bus for xfconfd.
# bookworm's xfwm4 has no --daemon flag; it runs foreground, so background it.
dbus-launch xfwm4 --compositor=off &
xwallpaper --zoom /usr/share/backgrounds/case.png
xfce4-panel --disable-wm-check &

# chromium exits if stdin ever reads EOF — hold a never-EOF fifo open for it
mkfifo /tmp/.chrome-stdin 2>/dev/null
exec 9<>/tmp/.chrome-stdin

chrome_loop() {
  while true; do
    chromium <&9 \
      --no-sandbox \
      --disable-gpu \
      --user-data-dir=/home/agent/chrome-profile \
      --remote-debugging-port=9222 \
      --no-first-run --no-default-browser-check \
      --disable-session-crashed-bubble --hide-crash-restore-bubble \
      --disable-background-networking --disable-component-update \
      --disable-domain-reliability --disable-sync --disable-default-apps \
      --disable-breakpad --metrics-recording-only --no-pings \
      --disable-component-extensions-with-background-pages \
      --disable-features=NetworkTimeServiceQuerying,OptimizationHints \
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
