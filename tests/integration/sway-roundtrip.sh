#!/usr/bin/env bash
set -euo pipefail

# Prefer the configured runtime directory, but fall back to /tmp when it is
# missing and cannot be created by the unprivileged test user.
if [ -z "${XDG_RUNTIME_DIR:-}" ] || ! mkdir -p "$XDG_RUNTIME_DIR" 2>/dev/null; then
    export XDG_RUNTIME_DIR=/tmp/xdg
fi
export XDG_STATE_HOME=${XDG_STATE_HOME:-/tmp/state}
export XDG_CURRENT_DESKTOP=sway
export XDG_SESSION_TYPE=wayland
export PYTHONPATH=/src/src
export LIBGL_ALWAYS_SOFTWARE=1
# Containers have no user systemd or desktop daemons; skip those health checks.
export SWAY_SESSION_SKIP_DAEMON_CHECKS=1

mkdir -p "$XDG_RUNTIME_DIR" "$XDG_STATE_HOME"
chmod 700 "$XDG_RUNTIME_DIR"

cleanup() {
    pkill -x foot 2>/dev/null || true
    if [ -n "${SWAY_PID:-}" ]; then kill "$SWAY_PID" 2>/dev/null || true; fi
}
trap cleanup EXIT

cat >/tmp/sway.conf <<'EOF'
font pango:monospace 8
EOF
WLR_BACKENDS=headless WLR_RENDERER=pixman WLR_LIBINPUT_NO_DEVICES=1 \
    sway --unsupported-gpu -c /tmp/sway.conf >/tmp/sway.log 2>&1 &
SWAY_PID=$!
for _ in $(seq 1 100); do
    SWAYSOCK=$(find "$XDG_RUNTIME_DIR" -maxdepth 1 -name 'sway-ipc.*.sock' | head -1)
    export SWAYSOCK
    if [ -n "${SWAYSOCK:-}" ] && swaymsg -t get_version >/dev/null 2>&1; then break; fi
    sleep 0.1
done
if ! swaymsg -t get_version >/dev/null 2>&1; then
    echo "sway did not start" >&2
    cat /tmp/sway.log >&2
    exit 1
fi
export SWAYSOCK
export WAYLAND_DISPLAY=wayland-1
echo "sway: $(swaymsg -t get_version | python3 -c 'import json,sys; print(json.load(sys.stdin)["human_readable"])')"

# Workspace 1 gets three tiled terminals in a nested split. Workspace 2 gets
# a floating one. The nested shape is what the arrangement walk has to
# reproduce after the windows are materialized concurrently.
swaymsg 'workspace 1' >/dev/null
foot -T alpha -e sleep 600 >/tmp/foot-alpha.log 2>&1 &
sleep 2
foot -T beta -e sleep 600 >/tmp/foot-beta.log 2>&1 &
sleep 2
swaymsg '[title="^beta$"] focus' >/dev/null
swaymsg 'split v' >/dev/null
foot -T gamma -e sleep 600 >/tmp/foot-gamma.log 2>&1 &
sleep 2
swaymsg 'workspace 2' >/dev/null
foot -T delta -e sleep 600 >/tmp/foot-delta.log 2>&1 &
sleep 2
swaymsg '[title="^delta$"] floating enable' >/dev/null
swaymsg '[title="^delta$"] resize set 600 400' >/dev/null
swaymsg '[title="^delta$"] move position 100 100' >/dev/null
sleep 1

# A hidden scratchpad window must still be parked out of sight after restore.
swaymsg 'workspace 1' >/dev/null
foot -T epsilon -e sleep 600 >/tmp/foot-epsilon.log 2>&1 &
sleep 2
swaymsg '[title="^epsilon$"] move scratchpad' >/dev/null
sleep 1

python3 -m session_manager save --quiet
echo "saved sway state"

python3 - <<'PY'
import json, pathlib
payload = json.loads(pathlib.Path('/tmp/state/sway_session.json').read_text())

windows = []
def walk(node, workspace):
    if node.get('type') == 'window':
        windows.append((workspace, node['app_id'], node['rect']))
    for child in node.get('nodes', []) + node.get('floating_nodes', []):
        walk(child, workspace)

for ws in payload['workspaces']:
    for child in ws['nodes'] + ws['floating_nodes']:
        walk(child, ws['name'])

assert len(windows) == 4, windows
assert {w[0] for w in windows} == {'1', '2'}, windows

ws1 = next(ws for ws in payload['workspaces'] if ws['name'] == '1')
nested = [n for n in ws1['nodes'] if n.get('type') == 'con' and n.get('nodes')]
assert nested, ws1
assert len(payload.get('hidden_scratchpad', [])) == 1, payload.get('hidden_scratchpad')
print('saved windows:', windows)
PY

pkill -x foot 2>/dev/null || true
sleep 2
python3 -m session_manager restore --quiet
sleep 5
swaymsg -t get_tree >/tmp/tree.json
python3 /src/tests/integration/verify.py \
    --saved "$XDG_STATE_HOME/sway_session.json" --tree /tmp/tree.json

echo '--- restore with no WAYLAND_DISPLAY/SWAYSOCK ---'
pkill -x foot 2>/dev/null || true
sleep 2
unset WAYLAND_DISPLAY SWAYSOCK I3SOCK DISPLAY
python3 -m session_manager restore --quiet
sleep 5
SWAYSOCK=$(find "$XDG_RUNTIME_DIR" -maxdepth 1 -name 'sway-ipc.*.sock' | head -1)
export SWAYSOCK
export WAYLAND_DISPLAY=wayland-1
swaymsg -t get_tree >/tmp/tree.json
python3 /src/tests/integration/verify.py \
    --saved "$XDG_STATE_HOME/sway_session.json" --tree /tmp/tree.json

echo 'PASS: sway roundtrip'
