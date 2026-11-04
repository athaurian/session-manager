#!/usr/bin/env bash
set -euo pipefail

export DISPLAY=${DISPLAY:-:99}
export XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR:-/tmp/xdg}
export XDG_STATE_HOME=${XDG_STATE_HOME:-/tmp/state}
export XDG_CURRENT_DESKTOP=i3
export XDG_SESSION_TYPE=x11
export PYTHONPATH=/src/src
# Containers have no user systemd or desktop daemons; skip those health checks.
export SWAY_SESSION_SKIP_DAEMON_CHECKS=1

mkdir -p "$XDG_RUNTIME_DIR" "$XDG_STATE_HOME"
chmod 700 "$XDG_RUNTIME_DIR"

cleanup() {
    pkill -x xterm 2>/dev/null || true
    if [ -n "${I3_PID:-}" ]; then kill "$I3_PID" 2>/dev/null || true; fi
    if [ -n "${XVFB_PID:-}" ]; then kill "$XVFB_PID" 2>/dev/null || true; fi
}
trap cleanup EXIT

Xvfb "$DISPLAY" -screen 0 1280x800x24 -nolisten tcp >/tmp/xvfb.log 2>&1 &
XVFB_PID=$!
for _ in $(seq 1 100); do
    if xdpyinfo >/dev/null 2>&1; then break; fi
    sleep 0.1
done
if ! xdpyinfo >/dev/null 2>&1; then
    echo "Xvfb did not start" >&2
    cat /tmp/xvfb.log >&2
    exit 1
fi

cat >/tmp/i3.conf <<'EOF'
font pango:monospace 8
EOF
i3 -c /tmp/i3.conf >/tmp/i3.log 2>&1 &
I3_PID=$!
for _ in $(seq 1 100); do
    if i3-msg -t get_version >/dev/null 2>&1; then break; fi
    sleep 0.1
done
if ! i3-msg -t get_version >/dev/null 2>&1; then
    echo "i3 did not start" >&2
    cat /tmp/i3.log >&2
    exit 1
fi
echo "i3: $(i3-msg -t get_version | python3 -c 'import json,sys; print(json.load(sys.stdin)["human_readable"])')"

# Workspace 1 gets a tiled terminal. Workspace 2 gets a floating one.
i3-msg 'workspace 1' >/dev/null
xterm -T alpha -e sleep 600 >/tmp/xterm-alpha.log 2>&1 &
sleep 2
i3-msg 'workspace 2' >/dev/null
xterm -T beta -e sleep 600 >/tmp/xterm-beta.log 2>&1 &
sleep 2
i3-msg '[title="^beta$"] floating enable, resize set width 600 px height 400 px, move position 100 100' >/dev/null
sleep 1

python3 -m session_manager save --quiet
echo "saved i3 state"

python3 - <<'PY'
import json, pathlib
payload = json.loads(pathlib.Path('/tmp/state/i3_session.json').read_text())
windows = [
    (ws['name'], n['app_id'], n['rect'])
    for ws in payload['workspaces']
    for n in ws['nodes'] + ws['floating_nodes']
]
assert len(windows) == 2, windows
assert {w[0] for w in windows} == {'1', '2'}, windows
print('saved windows:', windows)
PY

pkill -x xterm 2>/dev/null || true
sleep 2
python3 -m session_manager restore --quiet
sleep 4
i3-msg -t get_tree >/tmp/tree.json
python3 /src/tests/integration/verify.py \
    --saved "$XDG_STATE_HOME/i3_session.json" --tree /tmp/tree.json

echo '--- restore with no DISPLAY/I3SOCK ---'
pkill -x xterm 2>/dev/null || true
sleep 2
unset DISPLAY I3SOCK SWAYSOCK WAYLAND_DISPLAY
python3 -m session_manager restore --quiet
sleep 4
export DISPLAY=:99
i3-msg -t get_tree >/tmp/tree.json
python3 /src/tests/integration/verify.py \
    --saved "$XDG_STATE_HOME/i3_session.json" --tree /tmp/tree.json

echo 'PASS: i3 roundtrip'
