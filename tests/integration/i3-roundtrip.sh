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

# Wait on the compositor tree rather than on a fixed delay. The tree is the
# source of truth for whether a window has mapped, moved, or gone away.
wait_for_title() {
    local title=$1
    for _ in $(seq 1 100); do
        if i3-msg -t get_tree 2>/dev/null | grep -Fq "\"name\": \"$title\""; then
            return 0
        fi
        sleep 0.1
    done
    echo "window '$title' did not appear" >&2
    return 1
}

wait_for_title_gone() {
    local title=$1
    for _ in $(seq 1 100); do
        if ! i3-msg -t get_tree 2>/dev/null | grep -Fq "\"name\": \"$title\""; then
            return 0
        fi
        sleep 0.1
    done
    echo "window '$title' did not disappear" >&2
    return 1
}

wait_for_floating() {
    local title=$1
    for _ in $(seq 1 100); do
        if i3-msg -t get_tree 2>/dev/null | python3 -c '
import json, sys
title = sys.argv[1]

def walk(node):
    if node.get("type") == "floating_con" and node.get("name") == title:
        return True
    return any(
        walk(child)
        for child in node.get("nodes", []) + node.get("floating_nodes", [])
    )

sys.exit(0 if walk(json.load(sys.stdin)) else 1)
' "$title"; then
            return 0
        fi
        sleep 0.1
    done
    echo "window '$title' did not float" >&2
    return 1
}

wait_for_rect_stable() {
    local title=$1
    local previous=""
    local current
    for _ in $(seq 1 100); do
        current=$(i3-msg -t get_tree 2>/dev/null | python3 -c '
import json, sys
title = sys.argv[1]

def find(node):
    if node.get("name") == title:
        return node.get("rect", {})
    for child in node.get("nodes", []) + node.get("floating_nodes", []):
        found = find(child)
        if found is not None:
            return found
    return None

rect = find(json.load(sys.stdin))
print(json.dumps(rect, sort_keys=True) if rect is not None else "")
' "$title" 2>/dev/null || true)
        if [ -n "$current" ] && [ "$current" = "$previous" ]; then
            return 0
        fi
        previous=$current
        sleep 0.1
    done
    echo "window '$title' geometry did not settle" >&2
    return 1
}

wait_for_window_on_workspace() {
    local title=$1
    local workspace=$2
    for _ in $(seq 1 100); do
        if i3-msg -t get_tree 2>/dev/null | python3 -c '
import json, sys
title, workspace = sys.argv[1], sys.argv[2]

def find_workspace(node):
    if node.get("type") == "workspace" and node.get("name") == workspace:
        return node
    for child in node.get("nodes", []) + node.get("floating_nodes", []):
        found = find_workspace(child)
        if found:
            return found
    return None

def has_title(node):
    if node.get("name") == title:
        return True
    return any(
        has_title(child)
        for child in node.get("nodes", []) + node.get("floating_nodes", [])
    )

found = find_workspace(json.load(sys.stdin))
sys.exit(0 if found and has_title(found) else 1)
' "$title" "$workspace"; then
            return 0
        fi
        sleep 0.1
    done
    echo "window '$title' did not reach workspace '$workspace'" >&2
    return 1
}

wait_for_scratchpad() {
    local title=$1
    for _ in $(seq 1 100); do
        if i3-msg -t get_tree 2>/dev/null | python3 -c '
import json, sys
title = sys.argv[1]

def find_scratchpad(node):
    if node.get("name") == "__i3_scratch":
        return node
    for child in node.get("nodes", []) + node.get("floating_nodes", []):
        found = find_scratchpad(child)
        if found:
            return found
    return None

def has_title(node):
    if node.get("name") == title:
        return True
    return any(
        has_title(child)
        for child in node.get("nodes", []) + node.get("floating_nodes", [])
    )

scratchpad = find_scratchpad(json.load(sys.stdin))
sys.exit(0 if scratchpad and has_title(scratchpad) else 1)
' "$title"; then
            return 0
        fi
        sleep 0.1
    done
    echo "window '$title' did not move to the scratchpad" >&2
    return 1
}

verify_restored() {
    local saved=$1
    local log=/tmp/verify.log
    for _ in $(seq 1 100); do
        i3-msg -t get_tree >/tmp/tree.json
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

# Workspace 1 gets a tiled terminal. Workspace 2 gets a floating one.
i3-msg 'workspace 1' >/dev/null
xterm -T alpha -e sleep 600 >/tmp/xterm-alpha.log 2>&1 &
wait_for_title alpha
i3-msg 'workspace 2' >/dev/null
xterm -T beta -e sleep 600 >/tmp/xterm-beta.log 2>&1 &
wait_for_title beta
i3-msg '[title="^beta$"] floating enable, resize set width 600 px height 400 px, move position 100 100' >/dev/null
wait_for_floating beta
wait_for_rect_stable beta

# A hidden scratchpad window must stay hidden after restore.
i3-msg 'workspace 1' >/dev/null
xterm -T epsilon -e sleep 600 >/tmp/xterm-epsilon.log 2>&1 &
wait_for_title epsilon
i3-msg '[title="^epsilon$"] move scratchpad' >/dev/null
wait_for_scratchpad epsilon

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
assert len(payload.get('hidden_scratchpad', [])) == 1, payload.get('hidden_scratchpad')
print('saved windows:', windows)
PY

echo '--- restore over the live session reuses its windows ---'
python3 -m session_manager restore --quiet
verify_restored "$XDG_STATE_HOME/i3_session.json"

# A partly live session must heal, not double. One window drifted to another
# workspace and the other is gone; restore has to move the survivor back,
# relaunch only the missing window, and duplicate neither.
echo '--- restore heals a partly live session ---'
i3-msg '[title="^alpha$"] move container to workspace 2' >/dev/null
i3-msg '[title="^beta$"] kill' >/dev/null
wait_for_window_on_workspace alpha 2
wait_for_title_gone beta
python3 -m session_manager restore --quiet
verify_restored "$XDG_STATE_HOME/i3_session.json"

pkill -x xterm 2>/dev/null || true
wait_for_title_gone alpha
wait_for_title_gone beta
wait_for_title_gone epsilon
python3 -m session_manager restore --quiet
verify_restored "$XDG_STATE_HOME/i3_session.json"

echo '--- restore with no DISPLAY/I3SOCK ---'
pkill -x xterm 2>/dev/null || true
wait_for_title_gone alpha
wait_for_title_gone beta
wait_for_title_gone epsilon
unset DISPLAY I3SOCK SWAYSOCK WAYLAND_DISPLAY
python3 -m session_manager restore --quiet
export DISPLAY=:99
verify_restored "$XDG_STATE_HOME/i3_session.json"

echo 'PASS: i3 roundtrip'
