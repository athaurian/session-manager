FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        i3 i3-wm x11-utils x11-apps xdotool xterm xvfb python3 procps \
        libgtk-3-bin dbus-x11 \
    && rm -rf /var/lib/apt/lists/*

# Give the test client a desktop entry so the generic launcher finds xterm.
RUN cp /usr/share/applications/debian-xterm.desktop /usr/share/applications/xterm.desktop \
    && sed -i 's/^Exec=.*/Exec=xterm/' /usr/share/applications/xterm.desktop

CMD ["bash"]
