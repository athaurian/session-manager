#!/usr/bin/env python3
"""Compares a saved session payload with a live i3/Sway tree.

The integration scripts save a controlled set of windows, kill them, restore
them, and then run this comparison. It checks that every restored window came
back on the same workspace with the same geometry.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

# Bars are managed by the compositor and are intentionally never saved.
IGNORED_APPS = frozenset({"i3bar", "swaybar"})


def _app_id(node: dict) -> str:
    props = node.get("window_properties") or {}
    return str(
        node.get("app_id") or props.get("class") or props.get("instance") or ""
    ).lower()


def _rect_for(workspace: str, rect: dict) -> dict:
    """Hidden scratchpad geometry is not user-visible, so do not compare it."""
    return {} if workspace == "__i3_scratch" else rect


def _saved_windows(payload: dict) -> list[tuple[str, str, dict]]:
    windows: list[tuple[str, str, dict]] = []

    def walk(node: dict, workspace: str) -> None:
        if node.get("type") == "window":
            windows.append(
                (workspace, _app_id(node), _rect_for(workspace, node.get("rect", {})))
            )
        for child in node.get("nodes", []) + node.get("floating_nodes", []):
            walk(child, workspace)

    for workspace in payload.get("workspaces", []):
        if not isinstance(workspace, dict):
            continue
        name = str(workspace.get("name", ""))
        for child in workspace.get("nodes", []) + workspace.get("floating_nodes", []):
            walk(child, name)
    for node in payload.get("hidden_scratchpad", []):
        walk(node, "__i3_scratch")
    return windows


def _live_windows(tree: dict) -> list[tuple[str, str, dict]]:
    windows: list[tuple[str, str, dict]] = []

    def walk(node: dict, workspace: str) -> None:
        if node.get("type") == "workspace":
            workspace = str(node.get("name", ""))
        if node.get("type") in ("con", "floating_con"):
            app_id = _app_id(node)
            if app_id and app_id not in IGNORED_APPS:
                windows.append(
                    (
                        workspace,
                        app_id,
                        _rect_for(workspace, node.get("rect", {})),
                    )
                )
        for child in node.get("nodes", []) + node.get("floating_nodes", []):
            walk(child, workspace)

    walk(tree, "")
    return windows


def _key(window: tuple[str, str, dict]) -> str:
    return json.dumps(window, sort_keys=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--saved", required=True, help="saved session JSON")
    parser.add_argument("--tree", required=True, help="live tree JSON")
    args = parser.parse_args()

    payload = json.loads(pathlib.Path(args.saved).read_text(encoding="utf-8"))
    tree = json.loads(pathlib.Path(args.tree).read_text(encoding="utf-8"))
    expected = sorted(_saved_windows(payload), key=_key)
    actual = sorted(_live_windows(tree), key=_key)

    if expected != actual:
        print("saved windows:", file=sys.stderr)
        for window in expected:
            print(" ", window, file=sys.stderr)
        print("live windows:", file=sys.stderr)
        for window in actual:
            print(" ", window, file=sys.stderr)
        return 1

    print(f"checked {len(actual)} windows: {actual}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
