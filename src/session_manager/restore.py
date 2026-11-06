"""Session restore orchestration."""

from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
from collections.abc import Iterator
from functools import partial
from typing import Any

from .apps.generic import launch_and_get_id
from .apps.handlers import handler_for
from .apps.helium import (
    collect_helium_nodes,
    get_helium_restored_id,
    restore_helium_group,
)
from .apps.kitty import classify_kitty
from .background import restore_background_apps
from .config import (
    DEFAULT_WINDOW_HEIGHT,
    DEFAULT_WINDOW_WIDTH,
    RESTORE_STAGING_WORKSPACE,
    SCRATCH_RESTORE_WORKSPACE,
    SCRATCHPAD_WORKSPACE,
    STAGING_WORKSPACES,
    STATE_DIR,
    STATE_FILE,
    canonical_app_id,
    detect_backend,
)
from .ipc import (
    close_connection,
    get_tree,
    is_window_node,
    node_app_id,
    node_pid,
    run_command,
    run_command_logged,
    walk_tree,
)
from .locking import operation_lock
from .logging_setup import notify
from .model import Snapshot, StateValidationError
from .proc_cache import ProcCache
from .session_env import apply_compositor_env
from .state import RestoreContext

log = logging.getLogger("session_manager")

# Log command failures but never abort the walk. Stale con_id and closed window
# failures are expected during restore.
cmd = run_command_logged

_WORKSPACE_SAFE = re.compile(r"^[A-Za-z0-9_.:-]+$")

# Marks are plain identifiers, so anything else is refused rather than
# quoted into the compositor command language.
_MARK_SAFE = re.compile(r"^[A-Za-z0-9_.:-]+$")

# Layout names each compositor accepts in its `layout` command. i3 and Sway
# disagree: Sway has no "stacked" command and i3 has no "tiling"/"floating".
_LAYOUTS: dict[str, frozenset[str]] = {
    "i3": frozenset(
        {"default", "stacked", "stacking", "tabbed", "splitv", "splith", "toggle"}
    ),
    "sway": frozenset({"default", "stacking", "tabbed", "splitv", "splith", "toggle"}),
}

# An application asks for its own size right after it maps, and that request
# can land after the restore placed the window. The tree is polled until the
# rects stop changing, so the request is seen before the geometry is judged.
_GEOMETRY_SETTLE_TIMEOUT = 2.0
_GEOMETRY_POLL_INTERVAL = 0.05


def _quote_token(name: str) -> str:
    """Quote a name for the compositor command language when it needs it."""
    if _WORKSPACE_SAFE.match(name):
        return name
    escaped = name.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _workspace_cmd(name: str) -> str:
    """Build a workspace command, quoting names that contain separators."""
    return f"workspace {_quote_token(name)}"


def _move_to_workspace_cmd(win_id: int, name: str) -> str:
    """Build a command that moves a window to a named workspace."""
    return f"[con_id={win_id}] move to workspace {_quote_token(name)}"


def _finite_int(value: Any) -> int | None:
    """Return a value as a plain int, or None when it is not a finite number.

    Booleans are not numbers here, and NaN or infinity is refused, so a
    malformed state file can never put arbitrary text into a compositor
    command.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    return int(value)


def _rect_number(rect: dict, key: str, default: int) -> int:
    """Return a rect member as a plain int, defaulted when it is not one."""
    value = _finite_int(rect.get(key, default))
    return default if value is None else value


def _normalise_restore_payload(
    payload: Any,
) -> tuple[list[dict], list[dict], list[dict[str, str]] | None, str | None]:
    """Return saved workspaces, scratchpad, background apps, and focus.

    Accept both the current object payload and the legacy list of
    workspaces. Nodes that cannot be used are dropped with a warning instead
    of rejecting the rest of the session.
    """
    snapshot = Snapshot.from_payload(payload)
    for issue in snapshot.issues:
        log.warning("Session payload issue: %s", issue)
    workspaces = [
        ws for ws in snapshot.workspaces if ws.get("name") not in STAGING_WORKSPACES
    ]
    focused = snapshot.focused_workspace
    if focused in STAGING_WORKSPACES:
        focused = None
    return (
        workspaces,
        snapshot.hidden_scratchpad,
        snapshot.background_apps,
        focused,
    )


def _iter_windows(node: dict) -> Iterator[dict]:
    """Yield the windows in a saved subtree, flattening containers."""
    if node.get("type") == "window":
        yield node
        return
    for child in node.get("nodes", []) + node.get("floating_nodes", []):
        yield from _iter_windows(child)


def _same_app_identity(live: dict, saved: dict) -> bool:
    """Report whether a live window carries a saved window's app identity."""
    app_id = saved.get("app_id")
    if isinstance(app_id, str) and app_id:
        saved_id = canonical_app_id(app_id)
        if saved_id == "kitty_drop":
            # The dropdown is a kitty window with its own instance, title, or
            # mark. A plain kitty window must not stand in for it, so the
            # same classifier that saved it decides the match.
            return classify_kitty(live, node_app_id(live)) == "kitty_drop"
        return node_app_id(live) == saved_id
    class_name = saved.get("class")
    return (
        isinstance(class_name, str)
        and bool(class_name)
        and node_app_id(live) == canonical_app_id(class_name)
    )


def _same_saved_window(live: dict, saved: dict, live_pid: int) -> bool:
    """Report whether the pid or the exact title also identifies the window.

    App identity alone is not enough, because one session can hold several
    windows of the same application.
    """
    pid = saved.get("pid")
    if isinstance(pid, int) and pid > 0 and live_pid == pid:
        return True
    name = saved.get("name")
    return isinstance(name, str) and bool(name) and live.get("name") == name


def _live_window_nodes(tree: dict) -> list[dict]:
    """Return the window containers in a live tree."""
    return [
        node
        for node in walk_tree(tree)
        if node.get("type") in ("con", "floating_con") and is_window_node(node)
    ]


def _adopt_live_windows(nodes: list[dict], ctx: RestoreContext) -> None:
    """Reuse saved windows that are already live instead of launching them.

    A restore that was interrupted, or one run over a session that is still
    up, should heal what survived rather than duplicate every window. Each
    live window is claimed at most once, so two saved windows never resolve
    to the same one.

    A grouped app is left to its own group restore, which already matches
    its windows by title.
    """
    candidates = [
        node
        for node in nodes
        if handler_for(canonical_app_id(node_app_id(node))).adoptable
    ]
    if not candidates:
        return
    try:
        live_windows = _live_window_nodes(get_tree())
    except OSError as exc:
        log.info("Cannot read the live tree to reuse windows: %s", exc)
        return

    # i3 reports no pid on the container, so the X11 lookup runs only for a
    # window that already matches the app and only once per live window.
    live_pids: dict[int, int] = {}
    reused = 0
    for node in candidates:
        for live in live_windows:
            win_id = live["id"]
            if win_id in ctx.claimed_ids:
                continue
            if not _same_app_identity(live, node):
                continue
            if win_id not in live_pids:
                live_pids[win_id] = node_pid(live)
            if not _same_saved_window(live, node, live_pids[win_id]):
                continue
            ctx.claimed_ids.add(win_id)
            ctx.registry.adopt(node, win_id)
            reused += 1
            log.info("Reusing live window %s for %s", win_id, _failure_label(node))
            break
    if reused:
        log.info("Reused %d live window(s) instead of launching them", reused)


def _launch_units(nodes: list[dict]) -> list[list[dict]]:
    """Group saved nodes into independent launch units.

    A unit launches once. Windows matched by pid are independent, so each
    gets its own unit and they all start together. Windows matched by app
    identity share one unit per app: two concurrent launches of the same app
    could otherwise claim each other's window. A grouped app is restored by
    its own group pass instead.
    """
    units: dict[tuple[str, ...], list[dict]] = {}
    for node in nodes:
        app_id = canonical_app_id(node_app_id(node))
        handler = handler_for(app_id)
        if handler.grouped:
            continue
        key: tuple[str, ...]
        if handler.pid_matched:
            key = ("node", str(id(node)))
        else:
            key = ("app", app_id, str(node.get("class", "")))
        units.setdefault(key, []).append(node)
    return list(units.values())


def _run_launch_unit(nodes: list[dict], ctx: RestoreContext) -> None:
    """Launch one unit's windows and record the ids they came back with."""
    for node in nodes:
        try:
            win_id = launch_and_get_id(node, ctx.claimed_ids)
        except Exception as exc:
            log.exception("Failed to launch %s", _failure_label(node))
            ctx.add_failure(f"{_failure_label(node)}: {exc}")
            win_id = None
        ctx.registry.record(node, win_id)


def _start_materialization(
    nodes: list[dict], ctx: RestoreContext
) -> list[threading.Event]:
    """Start launches for `nodes` and return one completion event per unit."""
    events: list[threading.Event] = []
    for unit in _launch_units(nodes):
        events.append(ctx.registry.submit(unit, partial(_run_launch_unit, unit, ctx)))
    return events


def _materialize(nodes: list[dict], ctx: RestoreContext) -> None:
    """Launch the windows in `nodes`, then wait before anything is placed.

    Windows that were reused from the live tree need no launch. The browser
    group restores alongside the other launches, and placement still waits
    for it: a window that maps while the walk is issuing `split` or `layout`
    can steal focus and reshape the wrong container.
    """
    pending = [node for node in nodes if not ctx.registry.has(node)]
    helium = _start_helium_materialization(ctx)
    events = _start_materialization(pending, ctx)
    for event in events:
        event.wait()
    if helium is not None:
        helium.wait()


def _run_helium_unit(ctx: RestoreContext) -> None:
    """Restore the saved browser group and publish each window's id."""
    try:
        restore_helium_group(ctx)
    except Exception:
        log.exception("Helium restore failed")
    finally:
        for idx, node in enumerate(ctx.helium_saved_nodes):
            ctx.registry.record(node, ctx.helium_restored_ids[idx])


def _start_helium_materialization(ctx: RestoreContext) -> threading.Event | None:
    """Start the browser group restore alongside the other launches.

    Returns the event that is set once the group has published its ids, or
    None when there is no saved browser window.
    """
    if ctx.helium_materialized or not ctx.helium_saved_nodes:
        return None
    ctx.helium_materialized = True
    return ctx.registry.submit(
        list(ctx.helium_saved_nodes), partial(_run_helium_unit, ctx)
    )


def _window_workspace_name(win_id: int) -> str | None:
    """Return the name of the workspace holding a live window."""
    try:
        tree = get_tree()
    except OSError:
        return None

    def _find(node: dict, workspace: str | None) -> str | None:
        if node.get("type") == "workspace":
            name = node.get("name")
            workspace = name if isinstance(name, str) else None
        if node.get("id") == win_id:
            return workspace
        for child in node.get("nodes", []) + node.get("floating_nodes", []):
            found = _find(child, workspace)
            if found is not None:
                return found
        return None

    return _find(tree, None)


def _focused_depth_in_workspace(name: str, tree: dict | None) -> int | None:
    """Return how many `focus parent` steps reach a workspace root.

    None means the workspace is not in the live tree, so the caller should
    fall back to a best-effort single step.
    """
    if tree is None:
        return None

    def find_workspace(node: dict) -> dict | None:
        """Find the workspace with the given name."""
        if node.get("type") == "workspace" and node.get("name") == name:
            return node
        for child in node.get("nodes", []) + node.get("floating_nodes", []):
            found = find_workspace(child)
            if found is not None:
                return found
        return None

    def focused_depth(node: dict, depth: int) -> int | None:
        """Return the depth of the focused node inside this subtree."""
        if node.get("focused"):
            return depth
        for child in node.get("nodes", []) + node.get("floating_nodes", []):
            found = focused_depth(child, depth + 1)
            if found is not None:
                return found
        return None

    workspace = find_workspace(tree)
    if workspace is None:
        return None
    depth = focused_depth(workspace, 0)
    # An empty workspace is itself focused, which is already the root.
    return 0 if depth is None else depth


def _focus_workspace_root(name: str, tree: dict | None) -> None:
    """Focus a workspace container so the next window lands at its root.

    Switching to a workspace focuses its last focused child. Without this, a
    window placed after a nested container would be moved inside that
    container instead of beside it. Workspaces cannot be focused by con_id,
    so walk up from the focused child instead.
    """
    depth = _focused_depth_in_workspace(name, tree)
    if depth is None:
        run_command("focus parent")
        return
    for _ in range(depth):
        cmd("focus parent")


def _read_tree(ctx: RestoreContext) -> dict | None:
    """Read the live tree and cache the window placements it holds.

    The arrangement asks the tree two questions per window: how deep the
    focus is, and which workspace a window is on. One read answers both,
    so the walk does not cost an IPC round trip per question.
    """
    try:
        tree = get_tree()
    except OSError:
        ctx.placements = None
        return None
    ctx.placements = _live_window_placements(tree)
    return tree


def _ensure_on_workspace(
    win_id: int, workspace: str | None, ctx: RestoreContext
) -> None:
    """Move a window to the workspace being restored, if it is elsewhere.

    Placement must never depend on which workspace happens to be focused when
    an application maps its window. The caller names the target explicitly.
    """
    if not workspace:
        return
    if ctx.placements is not None:
        current = ctx.placements.get(win_id)
    else:
        current = _window_workspace_name(win_id)
    if current == workspace:
        return
    cmd(_move_to_workspace_cmd(win_id, workspace))
    cmd(_workspace_cmd(workspace))
    if ctx.placements is not None:
        ctx.placements[win_id] = workspace


def _park_in_scratchpad(win_id: int, node: dict, ctx: RestoreContext) -> None:
    """Hide a window in the scratchpad and verify that it left the desktop.

    Every move is followed by a check, so the last move is verified too. A
    window that is no longer in the tree cannot be verified, and that is
    reported rather than counted as a successful park.
    """
    for attempt in range(3):
        cmd(f"[con_id={win_id}] move scratchpad")
        workspace = _window_workspace_name(win_id)
        if workspace == SCRATCHPAD_WORKSPACE:
            return
        if workspace is None:
            # The window is not in the tree, so the park cannot be verified.
            ctx.add_failure(
                f"{_failure_label(node)}: window vanished before it was parked"
            )
            return
        if attempt < 2:
            time.sleep(0.05)
    ctx.add_failure(f"{_failure_label(node)}: still visible after move scratchpad")


def _live_window_placements(tree: dict) -> dict[int, str | None]:
    """Map every live window id to the workspace that holds it."""
    placements: dict[int, str | None] = {}

    def walk(node: dict, workspace: str | None) -> None:
        """Collect window ids with the workspace they sit in."""
        if node.get("type") == "workspace":
            name = node.get("name")
            workspace = name if isinstance(name, str) else None
        if (
            node.get("type") in ("con", "floating_con")
            and is_window_node(node)
            and node.get("id") is not None
        ):
            placements[node["id"]] = workspace
        for child in node.get("nodes", []) + node.get("floating_nodes", []):
            walk(child, workspace)

    walk(tree, None)
    return placements


def _read_window_placements() -> dict[int, str | None] | None:
    """Return live window placements, or None when the tree is unreadable."""
    try:
        return _live_window_placements(get_tree())
    except OSError:
        return None


def _placed_window_id(node: dict, ctx: RestoreContext) -> int | None:
    """Return the live id of a saved node that went through materialization."""
    if not ctx.registry.has(node):
        return None
    return ctx.registry.resolve(node)


def _reconcile_placements(
    workspaces: list[dict], hidden_scratchpad: list[dict], ctx: RestoreContext
) -> None:
    """Move restored windows that drifted back to where they were saved.

    Placement is asserted during the walk, but an application can move its
    own window afterwards, and a window can map after its workspace was
    arranged. One pass over the live tree repairs those windows by con id
    and verifies the repair, so a partly healed session converges instead of
    reporting success with windows in the wrong place.
    """
    placements = _read_window_placements()
    if placements is None:
        ctx.placements = None
        return
    # The reconcile pass works from a fresh tree, so the cached
    # placements have to match it.
    ctx.placements = placements

    moved: dict[int, tuple[dict, str]] = {}
    for workspace in workspaces:
        name = workspace.get("name")
        if not isinstance(name, str):
            continue
        for node in _iter_windows(workspace):
            win_id = _placed_window_id(node, ctx)
            if win_id is None:
                continue
            if win_id not in placements:
                _record_missing(ctx, node)
                continue
            if placements[win_id] == name:
                continue
            _ensure_on_workspace(win_id, name, ctx)
            moved[win_id] = (node, name)

    for node in hidden_scratchpad:
        for window in _iter_windows(node):
            win_id = _placed_window_id(window, ctx)
            if win_id is None:
                continue
            if win_id not in placements:
                _record_missing(ctx, window)
                continue
            if placements[win_id] == SCRATCHPAD_WORKSPACE:
                continue
            _park_in_scratchpad(win_id, window, ctx)

    if not moved:
        return
    after = _read_window_placements()
    if after is None:
        return
    for win_id, (node, name) in moved.items():
        if win_id not in after:
            # The window closed after the move; whatever was lost is already
            # reported as missing or failed.
            continue
        if after[win_id] != name:
            ctx.add_failure(
                f"{_failure_label(node)}: still on workspace {after[win_id]!r}, "
                f"expected {name!r}"
            )
    log.info("Reconciled %d misplaced window(s)", len(moved))


def _saved_window_targets(
    workspaces: list[dict], hidden_scratchpad: list[dict]
) -> list[tuple[dict, str | None, bool]]:
    """Return every saved window with its target workspace and float state."""
    targets: list[tuple[dict, str | None, bool]] = []

    def collect(node: dict, workspace: str | None, floating: bool) -> None:
        """Collect windows under a saved node, tracking float membership."""
        if node.get("type") == "window":
            targets.append((node, workspace, floating))
            return
        for child in node.get("nodes", []):
            collect(child, workspace, floating)
        for child in node.get("floating_nodes", []):
            collect(child, workspace, True)

    for workspace in workspaces:
        name = workspace.get("name")
        if isinstance(name, str):
            collect(workspace, name, False)
    for node in hidden_scratchpad:
        collect(node, None, True)
    return targets


def _staging_workspace_names() -> list[str]:
    """Return the staging workspaces that are still in the live tree."""
    try:
        tree = get_tree()
    except OSError:
        return []
    return [
        node["name"]
        for node in walk_tree(tree)
        if node.get("type") == "workspace" and node.get("name") in STAGING_WORKSPACES
    ]


def _place_recovered_window(
    node: dict, win_id: int, workspace: str | None, floating: bool, ctx: RestoreContext
) -> None:
    """Place a window that mapped after its launch wait gave up."""
    if workspace is None:
        _restore_hidden_scratchpad(node, ctx)
        return
    previous = ctx.current_workspace
    ctx.current_workspace = workspace
    try:
        if floating:
            _restore_floating(node, ctx)
        else:
            _restore_node(node, ctx)
    finally:
        ctx.current_workspace = previous


def _late_window_candidates(
    tree: dict, placements: dict[int, str | None], workspaces: list[dict]
) -> tuple[list[dict], list[tuple[dict, str | None]]]:
    """Return the live windows the recovery pass has to consider.

    Windows still on a staging workspace and windows already on a restored
    workspace are both candidates: the geometry correction briefly focuses a
    restored workspace, so a window that maps during that short interval can
    land there instead of on staging. Only the staging ones are swept at the
    end, so an unrelated window on a restored workspace is left alone.

    Args:
        tree: The live tree to read.
        placements: Each live window id and the workspace holding it.
        workspaces: The saved workspaces being restored.

    Returns:
        The live staging windows and every candidate with the workspace it
        currently sits on.
    """
    windows = {node["id"]: node for node in _live_window_nodes(tree)}
    target_names = {
        workspace["name"]
        for workspace in workspaces
        if isinstance(workspace.get("name"), str)
    }
    staged: list[dict] = []
    candidates: list[tuple[dict, str | None]] = []
    for win_id, workspace in placements.items():
        live = windows.get(win_id)
        if live is None:
            continue
        if workspace in STAGING_WORKSPACES:
            staged.append(live)
            candidates.append((live, workspace))
        elif workspace in target_names:
            candidates.append((live, workspace))
    return staged, candidates


def _recover_late_windows(
    workspaces: list[dict], hidden_scratchpad: list[dict], ctx: RestoreContext
) -> int:
    """Adopt windows that mapped after their launch wait timed out.

    A slow application can create its window after the wait has already
    reported it missing. The window still belongs to this restore, so it is
    matched to its saved node, placed, and removed from the missing list.
    A window is only matched when its pid or its exact title identifies it
    as well, so one window of an application cannot stand in for another.

    Staging workspaces and the restored workspaces are both searched. The
    geometry correction briefly focuses a restored workspace, so a window
    that maps during that short interval can land there instead of on
    staging. Unmatched windows on restored workspaces are left alone; only
    unmatched staging windows are reported and moved out of the way.
    """
    # The recovery pass works from a fresh tree, so the cached placements
    # have to match it: `_read_tree` refreshes them from the same read.
    tree = _read_tree(ctx)
    placements = ctx.placements
    if tree is None or placements is None:
        return 0
    staged, candidates = _late_window_candidates(tree, placements, workspaces)
    if not candidates:
        return 0

    targets = _saved_window_targets(workspaces, hidden_scratchpad)
    pending = [
        target
        for target in targets
        if _placed_window_id(target[0], ctx) not in placements
    ]
    associated = {
        placed_id
        for node, _workspace, _floating in targets
        if (placed_id := _placed_window_id(node, ctx)) is not None
    }

    used: set[int] = set()
    live_pids: dict[int, int] = {}
    recovered = 0
    for live, live_workspace in candidates:
        win_id = live["id"]
        if win_id in ctx.claimed_ids or win_id in associated:
            continue
        match: tuple[dict, str | None, bool] | None = None
        for target in pending:
            node = target[0]
            if id(node) in used:
                continue
            if not _same_app_identity(live, node):
                continue
            # A hidden window belongs in the scratchpad, not on a restored
            # workspace. A visible window on one of those workspaces cannot
            # stand in for it.
            if target[1] is None and live_workspace not in STAGING_WORKSPACES:
                continue
            # The pid or the exact title has to identify the window too.
            # App identity alone would let one window of an application
            # stand in for another one.
            if win_id not in live_pids:
                live_pids[win_id] = node_pid(live)
            if not _same_saved_window(live, node, live_pids[win_id]):
                continue
            match = target
            break
        if match is None:
            continue
        node, workspace, floating = match
        used.add(id(node))
        associated.add(win_id)
        ctx.claimed_ids.add(win_id)
        ctx.registry.adopt(node, win_id)
        ctx.discard_missing(_failure_label(node))
        try:
            _place_recovered_window(node, win_id, workspace, floating, ctx)
        except Exception as exc:
            log.exception("Failed to place recovered %s", _failure_label(node))
            ctx.add_failure(f"{_failure_label(node)}: {exc}")
        recovered += 1
        log.info("Recovered late window %s for %s", win_id, _failure_label(node))

    fallback = next(
        (
            workspace["name"]
            for workspace in workspaces
            if isinstance(workspace.get("name"), str)
        ),
        "1",
    )
    for live in staged:
        win_id = live["id"]
        if win_id in associated:
            continue
        label = str(live.get("name") or node_app_id(live) or win_id)
        ctx.add_failure(f"{label}: restored window did not match a saved window")
        _ensure_on_workspace(win_id, fallback, ctx)

    if recovered:
        log.info("Recovered %d late window(s)", recovered)
    return recovered


def _finish_staging(
    workspaces: list[dict],
    hidden_scratchpad: list[dict],
    ctx: RestoreContext,
    destination: str,
) -> bool:
    """Leave the staging workspaces and sweep anything that mapped late.

    Visible windows are focused back onto staging before the sweep, so a
    window that maps while it is running lands on the staging workspace and
    is found there instead of on the destination. The sweep runs before the
    switch to the destination.
    """
    # `_iter_windows` is a generator, so `any` has to see the windows it
    # yields rather than the (always truthy) generator objects themselves.
    if any(list(_iter_windows(workspace)) for workspace in workspaces):
        _focus_staging(ctx)
    if _staging_workspace_names():
        _recover_late_windows(workspaces, hidden_scratchpad, ctx)
        _reconcile_placements(workspaces, hidden_scratchpad, ctx)
    focused = cmd(_workspace_cmd(destination))
    if not focused:
        ctx.add_failure(f"could not focus destination workspace {destination!r}")
    remaining = _staging_workspace_names()
    if remaining:
        ctx.add_failure(f"staging workspace not removed: {', '.join(remaining)}")
    return focused


def has_restorable_content() -> bool:
    """Report whether the saved session contains anything worth restoring.

    Backs the login prompt, which stays hidden when restore would be a
    no-op. Missing, unreadable, and invalid state files count as empty,
    as does a hidden_scratchpad containing only kitty_drop windows, since
    the dropdown terminal respawns on demand from its keybind.
    """
    if not STATE_FILE.exists():
        return False

    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            payload = json.load(f)
        workspaces, hidden_scratchpad, background_apps, _ = _normalise_restore_payload(
            payload
        )
    except (
        OSError,
        json.JSONDecodeError,
        UnicodeDecodeError,
        StateValidationError,
    ) as exc:
        log.warning("Session file %s not restorable: %s", STATE_FILE, exc)
        return False

    restorable_scratchpad = [
        window
        for node in hidden_scratchpad
        for window in _iter_windows(node)
        if window.get("app_id") != "kitty_drop"
    ]
    return bool(workspaces or restorable_scratchpad or background_apps)


def _output_contains(out: dict, x: int, y: int) -> bool:
    """Report whether a point lies inside an output rect."""
    ox = _rect_number(out, "x", 0)
    oy = _rect_number(out, "y", 0)
    ow = _rect_number(out, "width", 0)
    oh = _rect_number(out, "height", 0)
    return ox <= x < ox + ow and oy <= y < oy + oh


def _active_output_rects(tree: dict) -> list[dict]:
    """Return the rects of the active outputs."""
    rects = []
    for out in tree.get("nodes", []):
        if out.get("type") != "output" or out.get("name") == "__i3":
            continue
        if not out.get("active"):
            continue
        rect = out.get("rect")
        if isinstance(rect, dict) and rect.get("width") and rect.get("height"):
            rects.append(rect)
    return rects


def _output_rect_for(tree: dict, rect: dict, workspace_name: str | None) -> dict | None:
    """Pick the output a saved floating rect belongs to.

    Saved rects are output-absolute, so the output holding the window's
    centre is the one it was saved on. When the window sat outside every
    output, the output that hosts the workspace is used instead.
    """
    outputs = _active_output_rects(tree)
    if not outputs:
        return None

    x = _rect_number(rect, "x", 0)
    y = _rect_number(rect, "y", 0)
    w = _rect_number(rect, "width", DEFAULT_WINDOW_WIDTH)
    h = _rect_number(rect, "height", DEFAULT_WINDOW_HEIGHT)
    for out in outputs:
        if _output_contains(out, x + w // 2, y + h // 2):
            return out

    if workspace_name:
        for ws in walk_tree(tree):
            if ws.get("type") != "workspace" or ws.get("name") != workspace_name:
                continue
            ws_rect = ws.get("rect")
            if isinstance(ws_rect, dict):
                wx = _rect_number(ws_rect, "x", 0)
                wy = _rect_number(ws_rect, "y", 0)
                for out in outputs:
                    if _output_contains(out, wx, wy):
                        return out
            break
    return outputs[0]


def _clamp_rect_to_output(
    rect: dict, workspace_name: str | None, tree: dict | None = None
) -> dict:
    """Clamp the floating rect to stay fully inside the output when off-screen.

    Saved rects are output-absolute (for example, workspace 1 content can
    start at 4,28 after bars and gaps). Clamping therefore happens in output
    coordinates. On Sway, `_apply_geometry` converts the result to
    workspace-relative coordinates because `move position` anchors on the
    workspace content origin. i3's `move position` is already
    output-absolute, so the clamped rect is used as-is.

    Args:
        rect: Floating rect with x, y, width, and height.
        workspace_name: Workspace the rect belongs to. It names the output
            to clamp against when the rect itself sits outside every
            output.
        tree: The live tree to clamp against. It is read when it is not
            given, so a caller that already holds one does not pay for a
            second round trip.

    Returns:
        A complete rect: clamped to the output when clamping is possible,
        and the rect as given otherwise.
    """
    try:
        if tree is None:
            tree = get_tree()
        target_rect = _output_rect_for(tree, rect, workspace_name)
        if not target_rect:
            target_rect = {"x": 0, "y": 0, "width": 1920, "height": 1080}
        x = _rect_number(rect, "x", 0)
        y = _rect_number(rect, "y", 0)
        w = _rect_number(rect, "width", DEFAULT_WINDOW_WIDTH)
        h = _rect_number(rect, "height", DEFAULT_WINDOW_HEIGHT)
        max_x = (
            _rect_number(target_rect, "x", 0)
            + _rect_number(target_rect, "width", 1920)
            - w
        )
        max_y = (
            _rect_number(target_rect, "y", 0)
            + _rect_number(target_rect, "height", 1080)
            - h
        )
        clamped_x = max(_rect_number(target_rect, "x", 0), min(x, max_x))
        clamped_y = max(_rect_number(target_rect, "y", 0), min(y, max_y))
        if clamped_x != x or clamped_y != y:
            log.info(
                "Clamped floating rect %s -> (%d,%d) to fit output %s",
                rect,
                clamped_x,
                clamped_y,
                target_rect,
            )
            return {"x": clamped_x, "y": clamped_y, "width": w, "height": h}
        return rect
    except OSError:
        # The compositor is unreachable, so the rect is used as saved.
        return rect


def _workspace_origin(workspace_name: str | None) -> tuple[int, int]:
    """Return the top-left of a workspace in output coordinates.

    Fall back to (0, 0) when the workspace cannot be found or does not
    exist yet. i3's `move position` is already output-absolute, so it
    always uses (0, 0).

    Args:
        workspace_name: Name of the workspace to locate, or None.

    Returns:
        The (x, y) origin, or (0, 0) when the workspace cannot be
        found.
    """
    if not workspace_name or detect_backend() == "i3":
        return (0, 0)
    try:
        tree = get_tree()
    except OSError:
        return (0, 0)
    for ws in walk_tree(tree):
        if ws.get("type") == "workspace" and ws.get("name") == workspace_name:
            rect = ws.get("rect", {})
            try:
                return (int(rect.get("x", 0)), int(rect.get("y", 0)))
            except (ValueError, TypeError):
                return (0, 0)
    return (0, 0)


def _live_deco_height(win_id: int) -> int:
    """Return the live titlebar height of a window, or 0 for border styles
    without one.

    With `border normal`, `move position` anchors on the decoration
    top-left, so the content lands exactly on the saved position only when
    this height is subtracted from the target y. Pixel borders report a
    zero-height deco_rect and are unaffected.

    Args:
        win_id: Sway container id of the window to inspect.

    Returns:
        The decoration height, or 0 when the window has no titlebar or
        cannot be found.
    """
    try:
        tree = get_tree()
    except OSError:
        return 0

    def _find(node: dict) -> dict | None:
        """Find a node by container id, depth-first."""
        if node.get("id") == win_id:
            return node
        for child in node.get("nodes", []) + node.get("floating_nodes", []):
            found = _find(child)
            if found is not None:
                return found
        return None

    try:
        node = _find(tree)
        if not node:
            return 0
        return int(node.get("deco_rect", {}).get("height", 0) or 0)
    except (ValueError, TypeError, AttributeError):
        return 0


def _apply_sway_geometry(win_id: int, rect: dict, workspace_name: str | None) -> None:
    """Apply a saved rect to a floating Sway window.

    Sway anchors `move position` on the decoration and budgets the titlebar
    inside the height passed to `resize set`. Add the measured decoration
    height back so the saved content rect is restored exactly. The height is
    measured from the tree at apply time, so a correction sees the window as
    it is now rather than as it was when the restore last read the tree.

    Args:
        win_id: Sway container id of the window.
        rect: The saved rect, already clamped to an output.
        workspace_name: The workspace the window is on, used to turn the
            output-absolute rect into Sway's workspace-relative one.
    """
    deco_height = _live_deco_height(win_id)
    width = _rect_number(rect, "width", DEFAULT_WINDOW_WIDTH)
    height = _rect_number(rect, "height", DEFAULT_WINDOW_HEIGHT) + deco_height
    # Resize before moving: growing the window can shift its position, so
    # the move must come last.
    cmd(f"[con_id={win_id}] resize set width {width} px height {height} px")
    origin_x, origin_y = _workspace_origin(workspace_name)
    x = _rect_number(rect, "x", 0) - origin_x
    y = _rect_number(rect, "y", 0) - origin_y - deco_height
    cmd(f"[con_id={win_id}] move position {x} {y}")


def _apply_geometry(win_id: int, rect: dict, workspace_name: str | None) -> None:
    """Apply saved geometry to a window, clamped to its output."""
    rect = _clamp_rect_to_output(rect, workspace_name)
    backend = detect_backend()

    if backend == "i3":
        # i3 takes `move position` and `resize set` as output-absolute, and
        # `resize set height` includes the titlebar in the requested height.
        # No workspace-origin or titlebar offset is needed.
        width = _rect_number(rect, "width", DEFAULT_WINDOW_WIDTH)
        height = _rect_number(rect, "height", DEFAULT_WINDOW_HEIGHT)
        cmd(f"[con_id={win_id}] resize set width {width} px height {height} px")
        x = _rect_number(rect, "x", 0)
        y = _rect_number(rect, "y", 0)
        cmd(f"[con_id={win_id}] move position {x} {y}")
        return

    # Windows are launched and mapped before the walk places them, and the
    # app-matched waits hold the window until it settles, so the geometry
    # can be applied during the walk. A later verification pass catches an
    # application that overrides its size after this request. i3 applies
    # geometry directly and returns above.
    _apply_sway_geometry(win_id, rect, workspace_name)


def _live_rects(tree: dict) -> dict[int, dict]:
    """Map every live container id to the rect it occupies."""
    return {
        node["id"]: node["rect"]
        for node in walk_tree(tree)
        if isinstance(node.get("id"), int) and isinstance(node.get("rect"), dict)
    }


def _live_window_types(tree: dict) -> dict[int, str]:
    """Map every live window id to its Sway container type."""
    return {node["id"]: str(node.get("type", "")) for node in _live_window_nodes(tree)}


def _live_window_state(
    tree: dict,
) -> tuple[dict[int, dict], dict[int, str | None], dict[int, str]]:
    """Return the live rects, workspace placements, and container types.

    The geometry waits ask one tree read for all three, so they are derived
    together here instead of each caller repeating the three lines.
    """
    return (
        _live_rects(tree),
        _live_window_placements(tree),
        _live_window_types(tree),
    )


def _normalised_rect(rect: dict) -> dict:
    """Return a complete rect using the same defaults as geometry restore."""
    return {
        "x": _rect_number(rect, "x", 0),
        "y": _rect_number(rect, "y", 0),
        "width": _rect_number(rect, "width", DEFAULT_WINDOW_WIDTH),
        "height": _rect_number(rect, "height", DEFAULT_WINDOW_HEIGHT),
    }


def _rect_value(rect: dict, key: str) -> int | None:
    """Return a rect member as an int, or None when it is not a number."""
    return _finite_int(rect.get(key))


def _rect_matches(live: dict, expected: dict) -> bool:
    """Report whether a live rect is the rect that was saved."""
    keys = ("x", "y", "width", "height")
    live_values = [_rect_value(live, key) for key in keys]
    if any(value is None for value in live_values):
        return False
    return live_values == [_rect_value(expected, key) for key in keys]


def _settled_tree(
    targets: list[tuple[dict, int, str, dict]], deadline: float
) -> tuple[dict | None, bool]:
    """Read the live tree until the target windows stop changing.

    An application asks for its own size right after it maps, and that request
    can land after the restore placed the window. Two consecutive target
    states that agree mean the request has been applied, so the rect can be
    compared with the saved one instead of with a value that is about to
    change. Unrelated layout churn does not hold up this wait.

    Args:
        targets: The saved windows and their live ids to observe.
        deadline: The monotonic time after which the tree is returned as it
            is.

    Returns:
        The last tree that was read and whether the target state settled.
        The tree is None when it cannot be read before the deadline.
    """
    previous: tuple[tuple[int, tuple[int | None, ...], str | None, str], ...] | None = (
        None
    )
    while True:
        try:
            tree = get_tree()
        except OSError:
            if time.monotonic() >= deadline:
                return None, False
            time.sleep(_GEOMETRY_POLL_INTERVAL)
            continue
        rects, placements, types = _live_window_state(tree)
        state = tuple(
            (
                win_id,
                tuple(
                    _rect_value(rects.get(win_id, {}), key)
                    for key in ("x", "y", "width", "height")
                ),
                placements.get(win_id),
                types.get(win_id, ""),
            )
            for _node, win_id, _workspace, _expected in targets
        )
        if previous is not None and state == previous:
            return tree, True
        previous = state
        if time.monotonic() >= deadline:
            return tree, False
        time.sleep(_GEOMETRY_POLL_INTERVAL)


def _wait_for_floating_geometry(
    targets: list[tuple[dict, int, str, dict]], deadline: float
) -> tuple[dict[int, dict], dict[int, str | None], dict[int, str], bool]:
    """Wait until every target has its expected rect and workspace.

    Two consecutive matching reads are required so a transient tree value is
    not mistaken for the final state. The state read last is returned with a
    flag saying whether that final read succeeded, so the caller must treat
    the maps as unverified while the flag is unset.
    """
    last_rects: dict[int, dict] = {}
    last_placements: dict[int, str | None] = {}
    last_types: dict[int, str] = {}
    previous_matched = False
    while True:
        readable = True
        matched = False
        try:
            tree = get_tree()
        except OSError:
            readable = False
            tree = None
        if tree is not None:
            last_rects, last_placements, last_types = _live_window_state(tree)
            matched = True
            for _node, win_id, workspace, expected in targets:
                live = last_rects.get(win_id)
                if (
                    live is None
                    or not _rect_matches(live, expected)
                    or last_placements.get(win_id) != workspace
                    or last_types.get(win_id) != "floating_con"
                ):
                    matched = False
                    break
            if matched and previous_matched:
                return last_rects, last_placements, last_types, True
            previous_matched = matched
        else:
            previous_matched = False

        if time.monotonic() >= deadline:
            if matched:
                # Give a first matching observation one confirmation read,
                # even when it arrived at the deadline.
                continue
            return last_rects, last_placements, last_types, readable
        time.sleep(_GEOMETRY_POLL_INTERVAL)


def _focus_staging(ctx: RestoreContext) -> bool:
    """Focus the restore staging workspace without masking an active error."""
    try:
        focused = cmd(_workspace_cmd(RESTORE_STAGING_WORKSPACE))
    except Exception as exc:
        log.exception("Failed to focus the restore staging workspace")
        ctx.add_failure(f"could not focus staging workspace: {exc}")
        return False
    if not focused:
        ctx.add_failure("could not focus staging workspace")
    return focused


def _verify_floating_geometry(workspaces: list[dict], ctx: RestoreContext) -> None:
    """Check the saved floating geometry against the live tree.

    An application asks for its own size right after it maps, and that request
    can land after the restore placed the window. The live rect is therefore
    read back once the target windows have settled. The saved geometry is
    always applied once more after that wait, then the result is polled until
    the expected rect and workspace are observed twice in a row. This closes
    the window in which a late application request can override the restore.

    Sway applies ``move position`` in the focused workspace. The target
    workspace is therefore always focused before the geometry is reapplied.
    Focus then returns to staging, where late windows can still be recovered.
    The caller performs the final recovery and reconciliation before it
    leaves staging and restores the requested destination focus.

    i3 is left out: its tree reports the frame rect, which carries the
    titlebar and the border, so it cannot be compared with the size the
    restore asks for.
    """
    if detect_backend() == "i3":
        return
    targets: list[tuple[dict, int, str, dict]] = []
    # Hidden scratchpad windows are parked rather than placed on a
    # workspace, so they carry no live rect to compare against.
    for node, workspace, floating in _saved_window_targets(workspaces, []):
        if not floating or workspace is None:
            continue
        # The dropdown terminal is saved, but it is not floating restorable
        # content and has no geometry contract to verify.
        if canonical_app_id(node_app_id(node)) == "kitty_drop":
            continue
        # A fullscreen window fills its output, so its live rect is not the
        # saved one.
        if node.get("fullscreen_mode", 0):
            continue
        win_id = _placed_window_id(node, ctx)
        if win_id is None:
            continue
        saved = node.get("rect", {})
        targets.append((node, win_id, workspace, saved))
    if not targets:
        return

    # Keep late mappings on staging while the application settles. The
    # final recovery pass can then find and place those windows.
    if not _focus_staging(ctx):
        return
    tree, stable = _settled_tree(targets, time.monotonic() + _GEOMETRY_SETTLE_TIMEOUT)
    if tree is None:
        ctx.add_failure(
            "floating geometry could not be verified: live tree unavailable"
        )
        return
    if not stable:
        ctx.add_failure("floating geometry did not settle")
        return
    live_rects = _live_rects(tree)
    placements = _live_window_placements(tree)
    live_types = _live_window_types(tree)
    ctx.placements = placements

    to_apply: list[tuple[dict, int, str, dict]] = []
    for node, win_id, workspace, saved in targets:
        live = live_rects.get(win_id)
        if win_id not in placements:
            _record_missing(ctx, node)
            continue
        if live is None:
            ctx.add_failure(f"{_failure_label(node)}: geometry is unavailable")
            continue
        if live_types.get(win_id) != "floating_con":
            ctx.add_failure(f"{_failure_label(node)}: window is not floating")
            continue
        clamped = _clamp_rect_to_output(_normalised_rect(saved), workspace, tree=tree)
        to_apply.append((node, win_id, workspace, clamped))

    if not to_apply:
        return

    by_workspace: dict[str, list[tuple[dict, int, str, dict]]] = {}
    for target in to_apply:
        by_workspace.setdefault(target[2], []).append(target)

    applied: list[tuple[dict, int, str, dict]] = []
    for workspace, group in by_workspace.items():
        # `move position` acts in the focused workspace. Focus the target
        # even when it is also the requested destination. If the switch
        # fails, applying the move could relocate the window to the wrong
        # workspace, so the group is not changed. A dropped connection is
        # one of those failures: the restore has to keep going so staging
        # is still swept and the destination still ends up focused.
        try:
            focused = cmd(_workspace_cmd(workspace))
        except OSError:
            log.exception("Failed to focus workspace %r", workspace)
            focused = False
        if not focused:
            for node, _win_id, _workspace, _expected in group:
                ctx.add_failure(
                    f"{_failure_label(node)}: could not focus workspace "
                    f"{workspace!r} to restore its geometry"
                )
            continue
        try:
            for node, win_id, target_workspace, expected in group:
                try:
                    if placements.get(win_id) != target_workspace:
                        _ensure_on_workspace(win_id, target_workspace, ctx)
                    log.info(
                        "Reapplying the saved geometry of %s: %s -> %s",
                        _failure_label(node),
                        live_rects.get(win_id),
                        expected,
                    )
                    _apply_sway_geometry(win_id, expected, target_workspace)
                except OSError as exc:
                    # Report the window and keep the loop going: the other
                    # groups and the final sweep still have to run.
                    log.exception(
                        "Failed to reapply the geometry of %s", _failure_label(node)
                    )
                    ctx.add_failure(f"{_failure_label(node)}: {exc}")
                    continue
                applied.append((node, win_id, target_workspace, expected))
        finally:
            _focus_staging(ctx)

    if not applied:
        return
    after_rects, after_placements, after_types, readable = _wait_for_floating_geometry(
        applied, time.monotonic() + _GEOMETRY_SETTLE_TIMEOUT
    )
    if not readable:
        ctx.add_failure(
            "floating geometry could not be verified: live tree unavailable"
        )
        return
    ctx.placements = after_placements
    for node, win_id, workspace, expected in applied:
        live = after_rects.get(win_id)
        actual_workspace = after_placements.get(win_id)
        if win_id not in after_placements:
            _record_missing(ctx, node)
            continue
        if after_types.get(win_id) != "floating_con":
            ctx.add_failure(f"{_failure_label(node)}: window is not floating")
        if live is None:
            ctx.add_failure(
                f"{_failure_label(node)}: geometry is unavailable, expected {expected}"
            )
        elif not _rect_matches(live, expected):
            ctx.add_failure(
                f"{_failure_label(node)}: geometry is {live}, expected {expected}"
            )
        if actual_workspace != workspace:
            ctx.add_failure(
                f"{_failure_label(node)}: workspace is {actual_workspace!r}, "
                f"expected {workspace!r}"
            )


def _apply_marks(win_id: int, marks: list[str]) -> None:
    """Apply saved marks to a window."""
    for mark in marks:
        if not _MARK_SAFE.match(mark):
            log.warning("skipping mark with unsupported characters: %r", mark)
            continue
        cmd(f"[con_id={win_id}] mark {mark}")


def _apply_fullscreen(win_id: int, fullscreen_mode: int) -> None:
    """Apply saved fullscreen state to a window.

    Use the explicit enable/disable forms: the bare command toggles, so a
    rerun over an already-fullscreen window would leave it in the wrong state.
    """
    if fullscreen_mode == 2:
        cmd(f"[con_id={win_id}] fullscreen enable global")
    elif fullscreen_mode == 1:
        cmd(f"[con_id={win_id}] fullscreen enable")
    else:
        cmd(f"[con_id={win_id}] fullscreen disable")


def _apply_border(win_id: int, border: str) -> None:
    """Apply a saved floating border style (Sway and i3 names match)."""
    if border in ("normal", "pixel", "none"):
        cmd(f"[con_id={win_id}] border {border}")


def _apply_percent(win_id: int, percent: float | None, parent_layout: str) -> None:
    """Restore a window's share of its parent split."""
    # `Snapshot` already dropped a non-finite or boolean share, so the only
    # value left to refuse is a missing one.
    if percent is None:
        return
    # Skip shares near 0 or 1, including single-window splits. There is
    # nothing to resize, and sway returns a parse error for those values.
    if float(percent) >= 0.99 or float(percent) <= 0.01:
        return
    ppt = round(float(percent) * 100)
    if ppt <= 0 or ppt >= 100:
        return
    if parent_layout == "splitv":
        cmd(f"[con_id={win_id}] resize set height {ppt} ppt")
    elif parent_layout == "splith":
        cmd(f"[con_id={win_id}] resize set width {ppt} ppt")
    elif parent_layout in ("tabbed", "stacking"):
        return
    else:
        cmd(f"[con_id={win_id}] resize set width {ppt} ppt")


def _layout_cmd(layout: str) -> str:
    """Return the layout name to send, or an empty string to skip it.

    Sway's tree reports "stacked" but its command is "stacking". Both
    compositors reject names they do not know, so the name is checked
    against the ones the running compositor accepts.
    """
    name = "stacking" if layout == "stacked" else layout
    allowed = _LAYOUTS.get(detect_backend(), _LAYOUTS["sway"])
    if name not in allowed:
        log.warning("skipping unsupported layout %r", layout)
        return ""
    return name


def _apply_layout(layout: str) -> None:
    """Apply a container layout when the compositor supports it."""
    name = _layout_cmd(layout)
    if name:
        cmd(f"layout {name}")


def _resolve_window(node: dict, ctx: RestoreContext) -> int | None:
    """Return the live window id for a saved node.

    The normal path reads the id that materialization already resolved. A
    node that bypassed materialization, such as a hidden scratchpad window
    launched just before it is parked, falls back to launching on demand. A
    grouped app asks its group restore for the id instead.
    """
    if ctx.registry.has(node):
        return ctx.registry.resolve(node)
    if handler_for(canonical_app_id(node_app_id(node))).grouped:
        return get_helium_restored_id(node, ctx)
    return launch_and_get_id(node, ctx.claimed_ids)


def _record_missing(ctx: RestoreContext, node: dict) -> None:
    """Record a saved window that did not come back."""
    if handler_for(canonical_app_id(node_app_id(node))).on_demand:
        # A window spawned by its own keybinding is not lost.
        return
    ctx.add_missing(_failure_label(node))


def _restore_floating(node: dict, ctx: RestoreContext) -> None:
    """Restore one floating window."""
    if node["type"] != "window":
        return
    app_id = node_app_id(node)
    win_id = _resolve_window(node, ctx)
    if not win_id:
        _record_missing(ctx, node)
        return

    _apply_marks(win_id, node.get("marks", []))
    _ensure_on_workspace(win_id, ctx.current_workspace, ctx)

    rect = node.get("rect", {})
    fullscreen_mode = node.get("fullscreen_mode", 0)
    scratchpad_state = node.get("scratchpad_state", "none")

    if app_id == "kitty_drop":
        _apply_geometry(win_id, rect, ctx.current_workspace)
        _apply_fullscreen(win_id, fullscreen_mode)
        return

    if app_id != "helium" and scratchpad_state != "none":
        cmd(f"[con_id={win_id}] floating enable")
        _apply_border(win_id, node.get("border") or "normal")
        cmd(f"[con_id={win_id}] move scratchpad")
        cmd(f"[con_id={win_id}] scratchpad show")
        _apply_geometry(win_id, rect, ctx.current_workspace)
        _apply_fullscreen(win_id, fullscreen_mode)
        return

    cmd(f"[con_id={win_id}] floating enable")
    # A plain floating window keeps exactly the border it was saved with;
    # an empty one is left to the compositor default.
    _apply_border(win_id, node.get("border", ""))

    _apply_geometry(win_id, rect, ctx.current_workspace)
    _apply_fullscreen(win_id, fullscreen_mode)


def _restore_hidden_scratchpad(node: dict, ctx: RestoreContext) -> None:
    """Restore hidden scratchpad windows and park them out of sight.

    A scratchpad group is saved as a container. Its windows are flattened
    into the scratchpad so the group's layout is lost but none of its
    windows are dropped.
    """
    if node.get("type") != "window":
        for child in node.get("nodes", []) + node.get("floating_nodes", []):
            _restore_hidden_scratchpad(child, ctx)
        return
    app_id = node_app_id(node)
    win_id = _resolve_window(node, ctx)
    if not win_id:
        _record_missing(ctx, node)
        return

    _apply_marks(win_id, node.get("marks", []))

    if app_id == "kitty_drop":
        # Dropdown is always scratchpad; restore its mark and border so the
        # keybind can find it again, then park it out of sight.
        _apply_border(win_id, node.get("border") or "normal")
        _park_in_scratchpad(win_id, node, ctx)
        return

    # Geometry is applied on the current staging workspace, whose origin
    # matches a normal workspace on the same output. i3 refuses to switch to
    # SCRATCH_RESTORE_WORKSPACE because it is an internal name, so on i3 the
    # caller stays on the restored workspace and passes it through ctx below.
    staging = (
        SCRATCH_RESTORE_WORKSPACE if detect_backend() != "i3" else ctx.current_workspace
    )
    _ensure_on_workspace(win_id, staging, ctx)
    cmd(f"[con_id={win_id}] floating enable")
    # Every scratchpad path falls back to the chromed style an older state
    # file was restored with when it carries no border of its own.
    _apply_border(win_id, node.get("border") or "normal")
    _apply_geometry(win_id, node.get("rect", {}), staging)
    _park_in_scratchpad(win_id, node, ctx)


def _failure_label(node: dict) -> str:
    """Return a readable name for a window that failed to restore."""
    return str(node.get("name") or node_app_id(node) or node.get("type", "window"))


def _record_failure(ctx: RestoreContext, node: dict, exc: Exception) -> None:
    """Record a failed window so the rest of the session still restores."""
    ctx.add_failure(f"{_failure_label(node)}: {exc}")


def _restore_child(node: dict, ctx: RestoreContext, parent_layout: str | None) -> None:
    """Restore one tiled child, keeping a failure local to that child."""
    try:
        _restore_node(node, ctx, parent_layout=parent_layout)
    except Exception as exc:
        log.exception("Failed to restore %s", _failure_label(node))
        _record_failure(ctx, node, exc)


def _restore_floating_child(node: dict, ctx: RestoreContext) -> None:
    """Restore one floating child, keeping a failure local to that child."""
    try:
        _restore_floating(node, ctx)
    except Exception as exc:
        log.exception("Failed to restore %s", _failure_label(node))
        _record_failure(ctx, node, exc)


def _restore_node(
    node: dict, ctx: RestoreContext, parent_layout: str | None = None
) -> None:
    """Restore a saved workspace, container, or window node."""
    ntype = node["type"]

    if ntype == "workspace":
        ctx.current_workspace = node["name"]
        cmd(_workspace_cmd(node["name"]))
        _focus_workspace_root(node["name"], _read_tree(ctx))
        _apply_layout(node["layout"])
        for child in node.get("nodes", []):
            cmd(_workspace_cmd(node["name"]))
            _focus_workspace_root(node["name"], _read_tree(ctx))
            _restore_child(child, ctx, parent_layout=node["layout"])
        for child in node.get("floating_nodes", []):
            cmd(_workspace_cmd(node["name"]))
            _focus_workspace_root(node["name"], _read_tree(ctx))
            _restore_floating_child(child, ctx)

    elif ntype == "con":
        children = node.get("nodes", [])
        if children:
            _restore_child(children[0], ctx, parent_layout=node["layout"])
            orientation = "v" if node["layout"] == "splitv" else "h"
            cmd(f"split {orientation}")
            _apply_layout(node["layout"])
            for child in children[1:]:
                _restore_child(child, ctx, parent_layout=node["layout"])
        for child in node.get("floating_nodes", []):
            _restore_floating_child(child, ctx)

    elif ntype == "window":
        win_id = _resolve_window(node, ctx)
        if not win_id:
            _record_missing(ctx, node)
            return
        _apply_marks(win_id, node.get("marks", []))
        _ensure_on_workspace(win_id, ctx.current_workspace, ctx)
        cmd(f"[con_id={win_id}] focus")
        _apply_fullscreen(win_id, node.get("fullscreen_mode", 0))
        _apply_percent(win_id, node.get("percent"), parent_layout or "")


def restore_session(
    *, notify_user: bool = True, workspace_filter: str | None = None
) -> None:
    """Restore the previously saved Sway/i3 session.

    Acquire the interprocess operation lock to serialize against
    concurrent save/restore invocations (for example, from power-control
    bindings), then hand off to _restore_session_locked().

    Args:
        notify_user: Whether to show desktop notifications.
        workspace_filter: Restores only this workspace when set,
            otherwise restores everything.
    """
    if not STATE_FILE.exists():
        log.info("No session file found.")
        return

    # Fill in the compositor's display variables before any app is launched.
    # A restore triggered from a login script or a systemd unit can run
    # without them, and the apps would then fail to reach the display server.
    apply_compositor_env()

    # Check that the compositor is reachable before touching anything.
    # Without this, every window would fail one by one and the user would
    # see a traceback per window instead of one clear message.
    try:
        get_tree()
    except OSError as exc:
        log.warning("Cannot reach the window manager: %s", exc)
        if notify_user:
            notify("session restore failed: cannot reach the window manager")
        raise

    with operation_lock(STATE_DIR):
        _restore_session_locked(
            notify_user=notify_user, workspace_filter=workspace_filter
        )


def _count_windows(node: dict) -> int:
    """Count the windows under a live or saved node."""
    if node.get("type") == "window":
        return 1
    if node.get("type") in ("con", "floating_con") and node_app_id(node):
        return 1
    total = 0
    for child in node.get("nodes", []):
        total += _count_windows(child)
    for child in node.get("floating_nodes", []):
        total += _count_windows(child)
    return total


def diff_sessions() -> bool:
    """Show a plain-English diff of the live and saved sessions.

    Returns:
        True when the diff was printed, False when the saved session or the
        live tree could not be read.
    """
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            payload = json.load(f)
        saved_ws, saved_hidden, saved_bg, _ = _normalise_restore_payload(payload)
    except (
        OSError,
        json.JSONDecodeError,
        UnicodeDecodeError,
        StateValidationError,
    ) as exc:
        print(f"Saved session unreadable: {exc}")
        return False

    try:
        live_tree = get_tree()
    except OSError as exc:
        print(f"Cannot read live tree: {exc}")
        return False
    finally:
        try:
            close_connection()
        except OSError:
            pass

    live_ws: dict[str, int] = {}
    for ws in walk_tree(live_tree):
        if ws.get("type") != "workspace" or ws.get("name") == SCRATCHPAD_WORKSPACE:
            continue
        name = ws.get("name")
        if not isinstance(name, str):
            continue
        count = 0
        for n in ws.get("nodes", []):
            count += _count_windows(n)
        for n in ws.get("floating_nodes", []):
            count += _count_windows(n)
        live_ws[name] = count

    saved_ws_map: dict[str, dict] = {
        ws["name"]: ws for ws in saved_ws if isinstance(ws.get("name"), str)
    }
    print("Live vs Saved diff:")
    all_names = sorted(set(live_ws) | set(saved_ws_map))
    for name in all_names:
        live_c = live_ws.get(name)
        saved = saved_ws_map.get(name)
        if saved:
            # Both sides count with the same predicate: `_count_windows`.
            saved_c = sum(_count_windows(n) for n in saved.get("nodes", [])) + sum(
                _count_windows(n) for n in saved.get("floating_nodes", [])
            )
        else:
            saved_c = None
        if live_c is None:
            print(f"  ws {name}: would create {saved_c} windows (currently missing)")
        elif saved_c is None:
            print(f"  ws {name}: {live_c} live windows would remain (not in saved)")
        elif live_c == saved_c:
            print(f"  ws {name}: {live_c} windows matches saved")
        else:
            print(f"  ws {name}: live {live_c} vs saved {saved_c} would recreate")

    if saved_hidden:
        print(f"  hidden scratchpad: {len(saved_hidden)} windows saved")
    if saved_bg:
        print(f"  background apps: {len(saved_bg)} saved")

    if not saved_ws and not saved_hidden:
        print("  Saved session is empty")
    return True


def _restore_session_locked(
    *, notify_user: bool = True, workspace_filter: str | None = None
) -> None:
    """Perform the session restoration under the operation lock."""
    if not STATE_FILE.exists():
        log.info("No session file found.")
        return

    def _notify(msg: str, **kw) -> None:
        """Notify the user when notifications are enabled."""
        if notify_user:
            notify(msg, **kw)

    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            payload = json.load(f)
        workspaces, hidden_scratchpad, background_apps, focused_workspace = (
            _normalise_restore_payload(payload)
        )
        if workspace_filter:
            orig = len(workspaces)
            workspaces = [ws for ws in workspaces if ws.get("name") == workspace_filter]
            if not workspaces:
                _notify(
                    f"workspace {workspace_filter} not in saved session",
                    urgency="normal",
                )
                log.warning(
                    "Workspace filter %s not found in %s", workspace_filter, STATE_FILE
                )
                return
            hidden_scratchpad = []
            # A filtered restore covers one workspace only. The background
            # apps belong to the whole session, so they are left alone:
            # an empty list keeps the sidecar fallback from launching them.
            background_apps = []
            # The saved focus may name a workspace outside the filter, so
            # the restore finishes on the workspace it actually restored.
            focused_workspace = workspace_filter
            log.info(
                "Restoring only workspace %s (%d/%d)",
                workspace_filter,
                len(workspaces),
                orig,
            )
            _notify(
                f"restoring only workspace {workspace_filter}...",
                urgency="critical",
                timeout_ms=0,
            )
    except (
        OSError,
        json.JSONDecodeError,
        UnicodeDecodeError,
        StateValidationError,
    ) as exc:
        _notify("session restore skipped: invalid session file", urgency="normal")
        log.warning("Invalid session file %s: %s", STATE_FILE, exc)
        return

    ctx = RestoreContext()
    # A filtered restore covers one workspace, so the browser must not
    # replay its whole session: that would reopen the windows saved on
    # every other workspace.
    ctx.helium_restore_last_session = workspace_filter is None
    destination = focused_workspace or "1"
    destination_ready = False

    try:
        _notify(
            "<b>don't move!</b> restoring previous session...",
            timeout_ms=0,
            urgency="critical",
        )

        for ws in workspaces:
            collect_helium_nodes(ws, ctx)

        for node in hidden_scratchpad:
            collect_helium_nodes(node, ctx)

        def _arrange(group: list[dict]) -> None:
            """Place a group of workspaces, keeping failures local."""
            for ws in group:
                try:
                    _restore_node(ws, ctx)
                except Exception as exc:
                    ctx.add_failure(f"{ws.get('name', '?')}: {exc}")
                    log.exception(
                        "Partial restore failure in workspace %s", ws.get("name")
                    )

        # Launch every visible window before placing any of them. A window
        # that maps mid-walk can steal focus and split the wrong container.
        # They map on a staging workspace so placement can move each one into
        # its saved container; a window already on its target workspace would
        # be left where it is and the nesting would not be rebuilt.
        visible_windows = [win for ws in workspaces for win in _iter_windows(ws)]
        hidden_windows = [
            win for node in hidden_scratchpad for win in _iter_windows(node)
        ]
        # A restore that was interrupted leaves some of its windows live. A
        # rerun reuses those instead of launching a second copy of the app,
        # so the session converges instead of doubling.
        _adopt_live_windows(visible_windows + hidden_windows, ctx)
        if visible_windows:
            cmd(_workspace_cmd(RESTORE_STAGING_WORKSPACE))
        _materialize(visible_windows, ctx)

        # Every window exists by now, so the workspaces are arranged in the
        # order they were saved; no workspace has to wait for the browser.
        _arrange(workspaces)

        if hidden_scratchpad and detect_backend() != "i3":
            # Stage geometry on a scratch workspace before parking the windows
            # in the scratchpad. i3 rejects switching to internal workspaces,
            # so it stays on the restored workspace instead.
            cmd(_workspace_cmd(SCRATCH_RESTORE_WORKSPACE))
        if hidden_scratchpad:
            # Hidden windows are launched just before they are parked, so they
            # are not left visible on a normal workspace while the rest of the
            # session is arranged.
            for node in hidden_scratchpad:
                try:
                    _restore_hidden_scratchpad(node, ctx)
                except Exception as exc:
                    ctx.add_failure(f"hidden scratchpad {_failure_label(node)}: {exc}")
                    log.exception("Failed to restore hidden scratchpad window")

        # Reconcile after every workspace is arranged. Windows can drift
        # after the walk places them, and an interrupted restore leaves some
        # of them elsewhere; this pass moves them back by con id and verifies
        # the result before the session is reported as restored.
        _recover_late_windows(workspaces, hidden_scratchpad, ctx)
        _reconcile_placements(workspaces, hidden_scratchpad, ctx)

        # An application asks for its own size right after it maps, and that
        # request can land after the walk placed the window. The floating
        # geometry is therefore checked against the live tree and applied
        # again when the window no longer matches it. This runs while staging
        # is focused so a window that maps during the settle pass can still
        # be recovered.
        _verify_floating_geometry(workspaces, ctx)

        # Leave the staging workspaces only after they are empty. The final
        # sweep and reconciliation happen before the destination is focused,
        # so windows that mapped during the geometry pass are recovered and
        # windows that disappeared are reported as missing.
        destination_ready = _finish_staging(
            workspaces,
            hidden_scratchpad,
            ctx,
            destination,
        )

    except Exception:
        _notify("session restore failed")
        log.exception("Session restore failed.")
        raise
    finally:
        # The summary is collected before this block, so a focus problem
        # here can no longer reach this run's report: it is logged instead.
        if not destination_ready:
            try:
                if not cmd(_workspace_cmd(destination)):
                    log.warning(
                        "Could not restore destination workspace %s", destination
                    )
            except Exception:
                log.exception("Could not restore destination workspace %s", destination)
        ctx.registry.close()
        close_connection()

    message = "<b>session restored successfully.</b>"
    if ctx.failures or ctx.missing:
        # Both kinds of loss are reported: a partial failure says a window
        # came back in the wrong state, a missing one says it never came
        # back at all. Hiding either hides the reason the session is short.
        summary = []
        if ctx.failures:
            summary.append(f"Partial failures: {len(ctx.failures)}")
        if ctx.missing:
            summary.append(f"{len(ctx.missing)} window(s) did not come back")
        _notify(f"{message} {'; '.join(summary)}. See logs.", urgency="normal")
    else:
        _notify(f"{message} you can safely move now.")
    if ctx.failures:
        log.warning("Partial restore failures: %s", ", ".join(ctx.failures))
    if ctx.missing:
        log.warning("Windows not restored: %s", ", ".join(ctx.missing))
    log.info("Restore complete.")

    # Daemon checks and background launches are best effort. Running them
    # after the session is usable keeps a slow probe from holding up the
    # success notification.
    try:
        restore_background_apps(
            background_apps,
            cache=ProcCache.snapshot() if background_apps else None,
            notify_user=notify_user,
        )
    except Exception:
        log.exception("Background app restore failed")
