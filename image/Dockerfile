# case-desk — persistent agent desktop. Debian slim + xfwm4 + Chromium + noVNC + deskd.
FROM debian:bookworm-slim

# Debian's apt chromium: SIGTRAPped on arm64/docker at v150, retested working at
# v151.0.7922 (2026-08-18) — headed, CDP opens, stable. If a bookworm point release
# regresses it, the fallback is Playwright's pinned build (see git history).
# No desktop environment: xfwm4 alone gives windows titlebars/focus/maximize, which
# is all the agent and the noVNC viewer need; a session manager, desktop, and panel
# were ~150MB RAM of laptop cosplay per computer.
ENV DEBIAN_FRONTEND=noninteractive
# Deliberately absent (each hid a heavy dependency chain, traced 2026-08-19):
#   apt novnc      — pulls nodejs + OpenStack python libs (~70MB); noVNC is static
#                    JS, vendored below from the pinned release tarball instead.
#   apt websockify — hard-depends numpy (26MB); pip --no-deps in the venv works,
#                    numpy only speeds a legacy encoding path we never proxy.
#   scrot          — pulls imlib2 → ghostscript + poppler (~50MB); deskd reads
#                    Xvfb's -fbdir framebuffer and PNG-encodes with stdlib zlib.
RUN apt-get update && apt-get install -y --no-install-recommends \
    xvfb x11vnc \
    chromium xfwm4 \
    xdotool \
    python3 python3-venv python3-pip \
    dbus-x11 ca-certificates curl procps \
    fonts-dejavu fonts-liberation \
    git jq unzip nano sudo xterm \
    && rm -rf /var/lib/apt/lists/*

ARG NOVNC_VERSION=1.5.0
RUN curl -fsSL "https://github.com/novnc/noVNC/archive/refs/tags/v${NOVNC_VERSION}.tar.gz" \
    | tar xz -C /usr/share && mv "/usr/share/noVNC-${NOVNC_VERSION}" /usr/share/novnc

RUN python3 -m venv /opt/deskd \
    && /opt/deskd/bin/pip install --no-cache-dir fastapi uvicorn websocket-client requests \
    && /opt/deskd/bin/pip install --no-cache-dir --no-deps websockify

# passwordless sudo: the container IS the agent's sandbox — self-serve apt installs.
# ponytail: system installs still die on container recreate; volume-only tools survive.
RUN useradd -m -u 1000 -s /bin/bash agent \
    && echo 'agent ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/agent

COPY deskd.py /opt/deskd/deskd.py
COPY start.sh /usr/local/bin/start.sh
RUN chmod +x /usr/local/bin/start.sh
# bookworm pip is PEP-668-locked; unlock it. Non-root pip then defaults to
# --user (~/.local) which lives on the volume — pip installs survive recreation.
RUN printf '[global]\nbreak-system-packages = true\n' > /etc/pip.conf

USER agent
WORKDIR /home/agent
# Nothing application-specific is baked in past this point. Tools an agent needs
# for one job (media fetchers, scrapers, SDKs) belong in ~/.local or ~/skills on
# the volume, where `pip install --user` survives container recreation and one
# tenant's dependency is not every tenant's image layer.
ENV DISPLAY=:0
EXPOSE 8000 6080

CMD ["/usr/local/bin/start.sh"]
