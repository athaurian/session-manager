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

# Wait on the compositor tree rather than on a fixed delay. The tree is the
# source of truth for whether a window has mapped, moved, or gone away.
# Foot does not keep the title from -T in Sway's tree, so the process id is
# the stable identity for the helpers below.
wait_for_pid() {
    local pid=$1
    for _ in $(seq 1 100); do
        if swaymsg -t get_tree 2>/dev/null | python3 -c '
import json, sys
pid = int(sys.argv[1])

def walk(node):
    if node.get("pid") == pid:
        return True
    return any(
        walk(child)
        for child in node.get("nodes", []) + node.get("floating_nodes", [])
    )

sys.exit(0 if walk(json.load(sys.stdin)) else 1)
' "$pid"; then
            return 0
        fi
        sleep 0.1
    done
    echo "window with pid '$pid' did not appear" >&2
    return 1
}

wait_for_app_gone() {
    local app=$1
    for _ in $(seq 1 100); do
        if ! swaymsg -t get_tree 2>/dev/null | grep -Fq "\"app_id\": \"$app\""; then
            return 0
        fi
        sleep 0.1
    done
    echo "app '$app' did not disappear" >&2
    return 1
}

wait_for_floating() {
    local pid=$1
    for _ in $(seq 1 100); do
        if swaymsg -t get_tree 2>/dev/null | python3 -c '
import json, sys
pid = int(sys.argv[1])

def walk(node):
    if node.get("type") == "floating_con" and node.get("pid") == pid:
        return True
    return any(
        walk(child)
        for child in node.get("nodes", []) + node.get("floating_nodes", [])
    )

sys.exit(0 if walk(json.load(sys.stdin)) else 1)
' "$pid"; then
            return 0
        fi
        sleep 0.1
    done
    echo "window with pid '$pid' did not float" >&2
    return 1
}

wait_for_rect_stable() {
    local pid=$1
    local previous=""
    local current
    for _ in $(seq 1 100); do
        current=$(swaymsg -t get_tree 2>/dev/null | python3 -c '
import json, sys
pid = int(sys.argv[1])

def find(node):
    if node.get("pid") == pid:
        return node.get("rect", {})
    for child in node.get("nodes", []) + node.get("floating_nodes", []):
        found = find(child)
        if found is not None:
            return found
    return None

rect = find(json.load(sys.stdin))
print(json.dumps(rect, sort_keys=True) if rect is not None else "")
' "$pid" 2>/dev/null || true)
        if [ -n "$current" ] && [ "$current" = "$previous" ]; then
            return 0
        fi
        previous=$current
        sleep 0.1
    done
    echo "window with pid '$pid' geometry did not settle" >&2
    return 1
}

wait_for_floating_rect_stable() {
    local workspace=$1
    local app_id=$2
    local previous=""
    local current
    for _ in $(seq 1 100); do
        current=$(swaymsg -t get_tree 2>/dev/null | python3 -c '
import json, sys
workspace, app_id = sys.argv[1], sys.argv[2]

def find(node, current_workspace=None):
    if node.get("type") == "workspace":
        current_workspace = node.get("name")
    if (
        node.get("type") == "floating_con"
        and node.get("app_id") == app_id
        and current_workspace == workspace
    ):
        return node.get("rect", {})
    for child in node.get("nodes", []) + node.get("floating_nodes", []):
        found = find(child, current_workspace)
        if found is not None:
            return found
    return None

rect = find(json.load(sys.stdin))
print(json.dumps(rect, sort_keys=True) if rect is not None else "")
' "$workspace" "$app_id" 2>/dev/null || true)
        if [ -n "$current" ] && [ "$current" = "$previous" ]; then
            return 0
        fi
        previous=$current
        sleep 0.1
    done
    echo "floating '$app_id' on workspace '$workspace' did not settle" >&2
    return 1
}

wait_for_scratchpad() {
    local pid=$1
    for _ in $(seq 1 100); do
        if swaymsg -t get_tree 2>/dev/null | python3 -c '
import json, sys
pid = int(sys.argv[1])

def find_scratchpad(node):
    if node.get("name") == "__i3_scratch":
        return node
    for child in node.get("nodes", []) + node.get("floating_nodes", []):
        found = find_scratchpad(child)
        if found:
            return found
    return None

def has_pid(node):
    if node.get("pid") == pid:
        return True
    return any(
        has_pid(child)
        for child in node.get("nodes", []) + node.get("floating_nodes", [])
    )

scratchpad = find_scratchpad(json.load(sys.stdin))
sys.exit(0 if scratchpad and has_pid(scratchpad) else 1)
' "$pid"; then
            return 0
        fi
        sleep 0.1
    done
    echo "window with pid '$pid' did not move to the scratchpad" >&2
    return 1
}

verify_restored() {
    local saved=$1
    local log=/tmp/verify.log
    for _ in $(seq 1 100); do
        swaymsg -t get_tree >/tmp/tree.json
        if python3 /src/tests/integration/verify.py \
            --saved "$saved" --tree /tmp/tree.json >"$log" 2>&1; then
            cat "$log"
            return 0
        fi
        sleep 0.2
    done
    cat "$log" >&2
    return 1
}

restore_without_failures() {
    local output
    output=$(python3 -m session_manager restore --quiet 2>&1)
    if grep -Fq "Partial restore failures:" <<<"$output" \
        || grep -Fq "Windows not restored:" <<<"$output"; then
        echo "$output" >&2
        echo "restore reported missing or partial windows" >&2
        return 1
    fi
}

# Workspace 1 gets three tiled terminals in a nested split. Workspace 2 gets
# a floating one. The nested shape is what the arrangement walk has to
# reproduce after the windows are materialized concurrently.
swaymsg 'workspace 1' >/dev/null
foot -T alpha -e sleep 600 >/tmp/foot-alpha.log 2>&1 &
ALPHA_PID=$!
wait_for_pid "$ALPHA_PID"
foot -T beta -e sleep 600 >/tmp/foot-beta.log 2>&1 &
BETA_PID=$!
wait_for_pid "$BETA_PID"
swaymsg "[pid=$BETA_PID] focus" >/dev/null
swaymsg 'split v' >/dev/null
foot -T gamma -e sleep 600 >/tmp/foot-gamma.log 2>&1 &
GAMMA_PID=$!
wait_for_pid "$GAMMA_PID"
swaymsg 'workspace 2' >/dev/null
foot -T delta -e sleep 600 >/tmp/foot-delta.log 2>&1 &
DELTA_PID=$!
wait_for_pid "$DELTA_PID"
swaymsg "[pid=$DELTA_PID] floating enable" >/dev/null
wait_for_floating "$DELTA_PID"
swaymsg "[pid=$DELTA_PID] resize set 600 400" >/dev/null
swaymsg "[pid=$DELTA_PID] move position 100 100" >/dev/null
wait_for_rect_stable "$DELTA_PID"

# A hidden scratchpad window must still be parked out of sight after restore.
swaymsg 'workspace 1' >/dev/null
foot -T epsilon -e sleep 600 >/tmp/foot-epsilon.log 2>&1 &
EPSILON_PID=$!
wait_for_pid "$EPSILON_PID"
swaymsg "[pid=$EPSILON_PID] move scratchpad" >/dev/null
wait_for_scratchpad "$EPSILON_PID"

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
wait_for_app_gone foot
restore_without_failures
wait_for_floating_rect_stable 2 foot
verify_restored "$XDG_STATE_HOME/sway_session.json"

echo '--- restore with no WAYLAND_DISPLAY/SWAYSOCK ---'
pkill -x foot 2>/dev/null || true
wait_for_app_gone foot
unset WAYLAND_DISPLAY SWAYSOCK I3SOCK DISPLAY
restore_without_failures
SWAYSOCK=$(find "$XDG_RUNTIME_DIR" -maxdepth 1 -name 'sway-ipc.*.sock' | head -1)
export SWAYSOCK
export WAYLAND_DISPLAY=wayland-1
wait_for_floating_rect_stable 2 foot
verify_restored "$XDG_STATE_HOME/sway_session.json"

echo 'PASS: sway roundtrip'
