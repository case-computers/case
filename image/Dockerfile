# case-desk — persistent agent desktop. Debian slim + xfwm4 + Chromium + noVNC + deskd.
FROM debian:bookworm-slim

# Debian's apt chromium: SIGTRAPped on arm64/docker at v150, retested working at
# v151.0.7922 (2026-08-18) — headed, CDP opens, stable. If a bookworm point release
# regresses it, the fallback is Playwright's pinned build (see git history).
# No desktop environment: xfwm4 alone gives windows titlebars/focus/maximize, which
# is all the agent and the noVNC viewer need; a session manager, desktop, and panel
# were ~150MB RAM of laptop cosplay per computer.
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    xvfb x11vnc novnc websockify \
    chromium xfwm4 \
    xdotool scrot \
    python3 python3-venv python3-pip \
    dbus-x11 ca-certificates curl procps \
    fonts-dejavu fonts-liberation \
    git wget jq unzip nano sudo xfce4-terminal \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv /opt/deskd \
    && /opt/deskd/bin/pip install --no-cache-dir fastapi uvicorn websocket-client requests

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
