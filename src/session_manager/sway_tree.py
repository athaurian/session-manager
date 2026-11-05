"""Serialization of a live Sway/i3 tree into the portable session format."""

from __future__ import annotations

import logging

from .apps.handlers import handler_for
from .ipc import node_app_id, node_class, node_instance, node_pid
from .proc_cache import ProcCache

log = logging.getLogger("session_manager")


def _has_window_identity(node: dict) -> bool:
    """Report whether a node carries a window identity."""
    return bool(
        node.get("app_id")
        or node.get("window")
        or node_class(node)
        or node_instance(node)
    )


def _merge_floating_wrapper(node: dict) -> dict | None:
    """Unwraps an i3 ``floating_con`` around its single content window.

    i3 reports floating windows as a wrapper container with one child window.
    The wrapper carries the absolute rect, marks, and scratchpad state, while
    the child carries the pid, title, and WM_CLASS. Sway sometimes reports the
    same information on the wrapper itself, so both shapes are accepted.
    """
    if _has_window_identity(node):
        return node
    if node.get("type") != "floating_con":
        return None

    children = [child for child in node.get("nodes", []) if _has_window_identity(child)]
    if len(children) != 1:
        return None

    merged = dict(children[0])
    wrapper_marks = list(node.get("marks") or [])
    child_marks = list(merged.get("marks") or [])
    if wrapper_marks or child_marks:
        merged["marks"] = list(dict.fromkeys(wrapper_marks + child_marks))

    # Scratchpad state and fullscreen live on the wrapper in i3. Do not let
    # the wrapper's neutral zero values clobber real child values.
    for key in ("scratchpad_state", "fullscreen_mode", "percent"):
        value = node.get(key)
        if value not in (None, 0, 0.0, "", [], {}):
            merged[key] = value

    rect = node.get("rect") or {}
    if rect.get("width") and rect.get("height"):
        merged["rect"] = rect
    return merged


def _identity_descendants(node: dict) -> list[dict]:
    """Return the window-bearing descendants of a container."""
    found: list[dict] = []
    for child in node.get("nodes", []) + node.get("floating_nodes", []):
        if _has_window_identity(child):
            found.append(child)
        else:
            found.extend(_identity_descendants(child))
    return found


def _window_source(node: dict) -> dict | None:
    """Return the node carrying a window's identity, unwrapping i3 floats."""
    if _has_window_identity(node):
        return node
    return _merge_floating_wrapper(node)


def clean_floating(node: dict, cache: ProcCache) -> list[dict]:
    """Clean one floating child into the windows it holds.

    i3 wraps a floating window in a ``floating_con``. A floating group (two
    tabbed windows, say) holds several of them under the wrapper, so each
    window is kept separately instead of dropping the whole group.
    """
    cleaned = clean_tree(node, cache)
    if cleaned is not None:
        return [cleaned]
    if node.get("type") != "floating_con":
        return []

    windows: list[dict] = []
    for descendant in _identity_descendants(node):
        window = clean_tree(descendant, cache)
        if window is None or window.get("type") != "window":
            continue
        wrapper_marks = list(node.get("marks") or [])
        window_marks = list(window.get("marks") or [])
        if wrapper_marks or window_marks:
            window["marks"] = list(dict.fromkeys(wrapper_marks + window_marks))
        if node.get("border") and not window.get("border"):
            window["border"] = node["border"]
        scratchpad = node.get("scratchpad_state")
        if scratchpad not in (None, "", "none") and not window.get("scratchpad_state"):
            window["scratchpad_state"] = scratchpad
        windows.append(window)

    if windows:
        log.warning(
            "Floating group of %d windows saved as separate windows", len(windows)
        )
    return windows


def clean_tree(node: dict, cache: ProcCache) -> dict | None:
    """Convert one tree node into the portable session format.

    Return a window, container, or workspace dict, or None when the node has
    no restorable content or is a transient terminal.
    """
    ntype = node.get("type")
    source = _window_source(node)

    if source is not None and ntype in ("con", "floating_con"):
        cls = node_class(source)
        app_id = node_app_id(source)
        handler = handler_for(app_id)
        if handler.classify is not None:
            classified = handler.classify(source, app_id)
            if classified is None:
                return None
            app_id = classified
            # The refined identity may have its own handler, as when a kitty
            # window turns out to be the dropdown terminal.
            handler = handler_for(app_id)

        pid = node_pid(source)
        try:
            info = handler.capture(source, pid, cache) if handler.capture else {}
        except Exception:
            # One app's capture failure must not discard the whole session.
            log.exception("Failed to capture %s; saving the window without it", app_id)
            info = {}

        percent = source.get("percent")
        window: dict[str, object] = {
            "type": "window",
            "app_id": app_id,
            "class": cls,
            "name": source.get("name", ""),
            "pid": pid,
            "fullscreen_mode": source.get("fullscreen_mode", 0),
            "rect": source.get("rect", {}),
            "marks": source.get("marks", []),
            "border": source.get("border", ""),
            "scratchpad_state": source.get("scratchpad_state", "none"),
            **info,
        }
        if isinstance(percent, (int, float)):
            window["percent"] = float(percent)
        return window

    if ntype in ("con", "workspace"):
        children = [c for n in node.get("nodes", []) if (c := clean_tree(n, cache))]
        floating: list[dict] = []
        for n in node.get("floating_nodes", []):
            floating.extend(clean_floating(n, cache))
        if ntype == "con" and not children and not floating:
            return None
        result: dict[str, object] = {
            "type": ntype,
            "layout": node.get("layout"),
            "nodes": children,
            "floating_nodes": floating,
        }
        percent = node.get("percent")
        if isinstance(percent, (int, float)):
            result["percent"] = float(percent)
        return result
    return None
