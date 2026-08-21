# case-desk — persistent agent desktop. Debian slim + xfwm4 + Chromium + noVNC + deskd.
FROM debian:bookworm-slim

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
RUN curl -fsSL "https://github.com/novnc/noVNC/archive/refs/tags/v${NOVNC_VERSION}.tar.gz" \
    | tar xz -C /usr/share && mv "/usr/share/noVNC-${NOVNC_VERSION}" /usr/share/novnc

RUN python3 -m venv /opt/deskd \
    && /opt/deskd/bin/pip install --no-cache-dir fastapi uvicorn websocket-client requests \
    && /opt/deskd/bin/pip install --no-cache-dir --no-deps websockify

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

CMD ["/usr/local/bin/start.sh"]
