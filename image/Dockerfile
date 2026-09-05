# case-desk — persistent agent desktop. Debian slim + xfwm4 + Chromium + noVNC + deskd.
FROM debian:bookworm-slim@sha256:88200866dfff7ea7f5cbcb6ec7c8a701889efe6fe859fe64d6990e4b07ea4171

# No desktop environment: xfwm4 alone gives windows titlebars/focus/maximize,
# which is all the agent and the noVNC viewer need.
ENV DEBIAN_FRONTEND=noninteractive
# Deliberately absent (each hides a heavy dependency chain):
#   apt novnc      — pulls nodejs + OpenStack python libs; noVNC is static JS,
#                    vendored below from the pinned release tarball instead.
#   apt websockify — hard-depends on numpy; pip --no-deps in the venv works,
#                    numpy only speeds a legacy encoding path we never proxy.
#   scrot          — pulls imlib2 → ghostscript + poppler; deskd reads Xvfb's
#                    -fbdir framebuffer and PNG-encodes with stdlib zlib.
RUN apt-get update && apt-get install -y --no-install-recommends \
    xvfb x11vnc \
    chromium xfwm4 \
    xfce4-panel xwallpaper greybird-gtk-theme elementary-xfce-icon-theme \
    xdotool \
    python3 python3-venv python3-pip \
    dbus-x11 ca-certificates curl procps \
    fonts-dejavu fonts-liberation \
    git jq unzip nano sudo xterm \
    && rm -rf /var/lib/apt/lists/*

# De-Google: Debian's chromium ships real Google API keys, activating browser-
# level Google services (sync, GCM push, geolocation, speech) that phone home
# at startup. Invalid dummy keys keep those services off without the "API keys
# missing" infobar. Web logins (gmail.com etc.) are cookie logins, unaffected.
# Policies disable the service machinery itself; matching kill flags in start.sh.
# MemorySaverModeSavings (1 = balanced) rides along here because it is a policy too:
# desks are long-lived and now restore their tabs across wakes, so background tabs
# accumulate. Discarded tabs reload from cookies on return, which agents tolerate.
RUN printf 'export GOOGLE_API_KEY="no"\nexport GOOGLE_DEFAULT_CLIENT_ID="no"\nexport GOOGLE_DEFAULT_CLIENT_SECRET="no"\n' > /etc/chromium.d/apikeys \
    && mkdir -p /etc/chromium/policies/managed \
    && printf '%s' '{"BrowserSignin":0,"SyncDisabled":true,"MetricsReportingEnabled":false,"SafeBrowsingProtectionLevel":0,"BackgroundModeEnabled":false,"ComponentUpdatesEnabled":false,"PromotionalTabsEnabled":false,"UrlKeyedAnonymizedDataCollectionEnabled":false,"MemorySaverModeSavings":1,"DefaultSearchProviderEnabled":true,"DefaultSearchProviderName":"DuckDuckGo","DefaultSearchProviderKeyword":"duckduckgo.com","DefaultSearchProviderSearchURL":"https://duckduckgo.com/?q={searchTerms}","DefaultSearchProviderSuggestURL":"https://duckduckgo.com/ac/?q={searchTerms}&type=list"}' \
       > /etc/chromium/policies/managed/case.json

ARG NOVNC_VERSION=1.5.0
ARG NOVNC_SHA256=6a73e41f98388a5348b7902f54b02d177cb73b7e5eb0a7a0dcf688cc2c79b42a
RUN curl -fsSL -o /tmp/novnc.tgz "https://github.com/novnc/noVNC/archive/refs/tags/v${NOVNC_VERSION}.tar.gz" \
    && echo "${NOVNC_SHA256}  /tmp/novnc.tgz" | sha256sum -c - \
    && tar xz -f /tmp/novnc.tgz -C /usr/share && mv "/usr/share/noVNC-${NOVNC_VERSION}" /usr/share/novnc && rm /tmp/novnc.tgz

# Transitives pinned alongside the four direct deps: the image is the computer,
# and a rebuild months from now must produce the deskd that was tested.
RUN python3 -m venv /opt/deskd \
    && /opt/deskd/bin/pip install --no-cache-dir --upgrade pip==26.2 setuptools==83.0.0 \
    && /opt/deskd/bin/pip install --no-cache-dir \
       fastapi==0.141.1 uvicorn==0.52.4 websocket-client==1.9.2 requests==2.34.2 \
       anyio==4.15.0 certifi==2026.7.22 charset-normalizer==3.5.1 click==8.5.0 \
       h11==0.16.0 idna==3.19 pydantic==2.13.5 starlette==1.6.0 urllib3==2.7.0 \
    && /opt/deskd/bin/pip install --no-cache-dir --no-deps websockify==0.13.0

# Passwordless sudo: the container IS the agent's sandbox — self-serve apt
# installs. Note system installs die on container recreate; only the home
# volume survives.
RUN useradd -m -u 1000 -s /bin/bash agent \
    && echo 'agent ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/agent

# Separate layer so adding it didn't re-download the big apt layer above;
# fold into the main install at the next base-layer rebuild.
RUN apt-get update && apt-get install -y --no-install-recommends thunar xfdesktop4 \
    && rm -rf /var/lib/apt/lists/*

# Desktop look: wallpaper, single-bar panel config, Greybird theme, and a custom
# cursor theme ("case", left_ptr in 24/32/48px). Per-user configs are seeded from
# /usr/share/case by start.sh only when missing, so user changes survive.
COPY assets/wallpaper.png /usr/share/backgrounds/case.png
COPY assets/cursor-left_ptr /usr/share/icons/case/cursors/left_ptr
RUN printf '[Icon Theme]\nName=case\n' > /usr/share/icons/case/index.theme
COPY assets/gtk-settings.ini assets/xfce4-panel.xml assets/xfwm4.xml assets/xfce4-desktop.xml /usr/share/case/

COPY deskd.py /opt/deskd/deskd.py
COPY start.sh /usr/local/bin/start.sh
RUN chmod +x /usr/local/bin/start.sh
# Unlock PEP-668 so pip works. Non-root pip defaults to --user (~/.local),
# which lives on the volume — pip installs survive container recreation.
RUN printf '[global]\nbreak-system-packages = true\n' > /etc/pip.conf

USER agent
WORKDIR /home/agent
# Nothing application-specific is baked in past this point. Tools an agent needs
# for one job (media fetchers, scrapers, SDKs) belong in ~/.local or ~/skills on
# the volume, where `pip install --user` survives container recreation and one
# tenant's dependency is not every tenant's image layer.
ENV DISPLAY=:0
# Without a UTF-8 locale, xdotool type dies on the first non-ASCII char
# ("Invalid multi-byte sequence") — em-dashes and ellipses in agent-written
# text made every such type action fail after partially typing.
ENV LANG=C.UTF-8 LC_ALL=C.UTF-8
EXPOSE 8000 6080

HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
  CMD curl -sf -H "Authorization: Bearer $DESK_TOKEN" http://127.0.0.1:8000/health || exit 1

CMD ["/usr/local/bin/start.sh"]
