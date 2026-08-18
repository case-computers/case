# case-desk — persistent agent desktop. Debian slim + Xfce + Chromium + noVNC + deskd.
FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    xvfb x11vnc novnc websockify \
    xfce4-session xfwm4 xfdesktop4 xfce4-panel \
    xdotool scrot \
    python3 python3-venv python3-pip \
    dbus-x11 ca-certificates curl procps \
    fonts-dejavu fonts-liberation \
    git wget jq unzip nano sudo xfce4-terminal \
    && rm -rf /var/lib/apt/lists/*

# Debian's chromium 150 SIGTRAPs at startup and never opens CDP on arm64/docker;
# Playwright's chromium build is the battle-tested docker automation browser
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers
RUN python3 -m venv /opt/deskd \
    && /opt/deskd/bin/pip install --no-cache-dir fastapi uvicorn websocket-client requests playwright \
    && /opt/deskd/bin/playwright install --with-deps chromium \
    && chmod -R a+rX /opt/pw-browsers \
    && rm -rf /var/lib/apt/lists/*

# passwordless sudo: the container IS the agent's sandbox — self-serve apt installs.
# ponytail: system installs still die on container recreate; volume-only tools survive.
RUN useradd -m -u 1000 -s /bin/bash agent \
    && echo 'agent ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/agent
# xfwm4's XRender compositor turns chromium's content area white on Xvfb — disable it
RUN mkdir -p /home/agent/.config/xfce4/xfconf/xfce-perchannel-xml && printf '%s\n' \
    '<?xml version="1.0" encoding="UTF-8"?>' \
    '<channel name="xfwm4" version="1.0">' \
    '  <property name="general" type="empty">' \
    '    <property name="use_compositing" type="bool" value="false"/>' \
    '  </property>' \
    '</channel>' \
    > /home/agent/.config/xfce4/xfconf/xfce-perchannel-xml/xfwm4.xml \
    && chown -R agent:agent /home/agent/.config

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
