#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-only
# PID 1 of a case-desk container. Brings up display, window manager, VNC, Chromium, deskd.
# Traps SIGTERM (docker stop = sleep) so Chromium exits cleanly and flushes its profile.

RES="${DESK_RESOLUTION:-1280x800x24}"
# Xvfb needs WxHxD and dies on bare WxH; depth 24 is the only one deskd's
# XWD->PNG grab supports (32bpp), so it is also the only default we append.
case "$RES" in *x*x*) ;; *) RES="${RES}x24" ;; esac

# libXcursor honors this in every X client — the one switch that themes the
# cursor everywhere (chromium reads it via GTK settings too, seeded below).
export XCURSOR_THEME=case XCURSOR_SIZE=32

# Seed per-user desktop config from /usr/share/case. Two rules fight here: never
# stomp the user's own tweaks, but a shipped look change has to actually land.
# LOOK settles it. The volume outlives the image (it IS the computer's identity),
# so a desk created before a look change keeps writing back its old xfconf files
# and a missing-only seed silently skips every one of them — leaving the new
# wallpaper/panel/dock sitting unused in the image. Bump LOOK whenever the
# assets change: every desk force-seeds exactly once (resetting desktop tweaks
# that one time), then goes back to leaving the user alone.
LOOK=v2
STAMP=~/.config/.case-look
if [ "$(cat "$STAMP" 2>/dev/null)" = "$LOOK" ]; then
  seed() { [ -f "$2" ] || { mkdir -p "$(dirname "$2")"; cp "$1" "$2"; }; }
else
  echo "[start] look $LOOK — re-seeding desktop config" >&2
  seed() { mkdir -p "$(dirname "$2")"; cp -f "$1" "$2"; }
fi
seed /usr/share/case/gtk-settings.ini ~/.config/gtk-3.0/settings.ini
seed /usr/share/case/xfce4-panel.xml ~/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-panel.xml
seed /usr/share/case/xfwm4.xml ~/.config/xfce4/xfconf/xfce-perchannel-xml/xfwm4.xml
seed /usr/share/case/xfce4-desktop.xml ~/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-desktop.xml
# dock launchers: the panel reads items from its per-plugin launcher dir
seed /usr/share/applications/chromium.desktop ~/.config/xfce4/panel/launcher-10/chromium.desktop
seed /usr/share/applications/thunar.desktop ~/.config/xfce4/panel/launcher-11/thunar.desktop
seed /usr/share/applications/debian-xterm.desktop ~/.config/xfce4/panel/launcher-12/debian-xterm.desktop
mkdir -p "$(dirname "$STAMP")" && echo "$LOOK" > "$STAMP"

rm -f /tmp/.X0-lock /tmp/.X11-unix/X0   # stale after docker stop; blocks Xvfb on wake
# -fbdir /dev/shm: mmap the framebuffer to a file deskd reads for screenshots
Xvfb :0 -screen 0 "$RES" -nolisten tcp -fbdir /dev/shm &
XVFB_PID=$!

for _ in $(seq 1 100); do [ -e /tmp/.X11-unix/X0 ] && break; sleep 0.1; done

x11vnc -display :0 -forever -shared -nopw -quiet -bg
# log to file, not compose stdout: its per-connection "Plain non-SSL (ws://)"
# lines read like TLS errors to people skimming `docker compose logs`
/opt/deskd/bin/websockify --web /usr/share/novnc 6080 localhost:5900 >>/tmp/websockify.log 2>&1 &
# DESK_DEBUG=1 (docker run -e, or on cased to cover every desktop): mirror the
# in-container logs to docker logs
[ "$DESK_DEBUG" = "1" ] && tail -n +1 -F /tmp/chromium.log /tmp/websockify.log 2>/dev/null &

# Bare WM, no desktop environment. --compositor=off: xfwm4's XRender compositor
# turns chromium's content area white on Xvfb. dbus session bus for xfconfd.
# bookworm's xfwm4 has no --daemon flag; it runs foreground, so background it.
dbus-launch xfwm4 --compositor=off &
xwallpaper --zoom /usr/share/backgrounds/case.png   # instant paint; xfdesktop takes over
xfce4-panel --disable-wm-check &
xfdesktop &

# chromium exits if stdin ever reads EOF — hold a never-EOF fifo open for it
mkfifo /tmp/.chrome-stdin 2>/dev/null
exec 9<>/tmp/.chrome-stdin

# --restore-last-session and no start URL: the volume is the computer's identity, so
# a wake should hand the agent back the tabs it was working in, not a blank browser.
# Passing about:blank here instead would stack one more dead tab on every wake.
# --renderer-process-limit caps process sprawl as tabs accumulate over a desk's life;
# it does mean cross-site tabs can share a renderer, which matters less here than it
# would elsewhere because --no-sandbox has already given up that isolation.
chrome_loop() {
  while true; do
    chromium <&9 \
      --no-sandbox \
      --disable-gpu \
      --user-data-dir=/home/agent/chrome-profile \
      --remote-debugging-port=9222 \
      --no-first-run --no-default-browser-check \
      --test-type \
      --disable-session-crashed-bubble --hide-crash-restore-bubble \
      --disable-background-networking --disable-component-update \
      --disable-domain-reliability --disable-sync --disable-default-apps \
      --disable-breakpad --metrics-recording-only --no-pings \
      --disable-component-extensions-with-background-pages \
      --disable-features=NetworkTimeServiceQuerying,OptimizationHints \
      --start-maximized \
      --restore-last-session \
      --renderer-process-limit=8 \
      >>/tmp/chromium.log 2>&1
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
