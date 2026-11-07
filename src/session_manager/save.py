"""Session save orchestration."""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from pathlib import Path

from .background import detect_background_apps
from .config import (
    BACKGROUND_APPS_FILE,
    NVIM_SESSION_DIR,
    SCRATCHPAD_WORKSPACE,
    STAGING_WORKSPACES,
    STATE_DIR,
    STATE_FILE,
)
from .ipc import IpcError, close_connection, get_tree, walk_tree
from .locking import operation_lock
from .logging_setup import notify
from .model import Snapshot
from .proc_cache import ProcCache
from .sway_tree import clean_floating, clean_tree

log = logging.getLogger("session_manager")


def _focused_workspace_name(tree: dict) -> str | None:
    """Return the name of the focused non-scratchpad workspace, if any."""

    def _walk(node: dict, ws_name: str | None) -> str | None:
        """Walk a subtree, tracking the enclosing workspace."""
        if node.get("type") == "workspace":
            ws_name = node.get("name")
        if node.get("focused"):
            if not ws_name:
                return None
            if ws_name == SCRATCHPAD_WORKSPACE or ws_name in STAGING_WORKSPACES:
                return None
            return ws_name
        for child in node.get("nodes", []) + node.get("floating_nodes", []):
            found = _walk(child, ws_name)
            if found:
                return found
        return None

    for output in tree.get("nodes", []):
        found = _walk(output, None)
        if found:
            return found
    return None


def _flatten_windows(node: dict) -> list[dict]:
    """Return the windows inside a saved node, flattening containers."""
    if node.get("type") == "window":
        return [node]
    windows: list[dict] = []
    for child in node.get("nodes", []) + node.get("floating_nodes", []):
        windows.extend(_flatten_windows(child))
    return windows


def _write_temp(path: Path, content: str) -> Path:
    """Write content beside `path` and return the temporary file."""
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(path.parent),
        prefix=f".{path.name}.tmp",
        delete=False,
    ) as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
        return Path(f.name)


def _fsync_dir(path: Path) -> None:
    """Flush a directory entry so a replace survives a crash."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write(path: Path, content: str) -> None:
    """Write content to a temporary file, then replace the target atomically."""
    tmp_path = _write_temp(path, content)
    try:
        os.replace(tmp_path, path)
    except OSError:
        tmp_path.unlink(missing_ok=True)
        raise
    _fsync_dir(path.parent)


def _copy_atomic(source: Path, destination: Path) -> None:
    """Copy a file through a temporary path, then replace the destination."""
    with tempfile.NamedTemporaryFile(
        dir=str(destination.parent),
        prefix=f".{destination.name}.tmp",
        delete=False,
    ) as f:
        tmp_path = Path(f.name)
    try:
        shutil.copy2(source, tmp_path)
        os.replace(tmp_path, destination)
        _fsync_dir(destination.parent)
    except OSError:
        tmp_path.unlink(missing_ok=True)
        raise


def _rotate_backups() -> str:
    """Rotate numbered backups without removing the live state file.

    The live file is copied to `.1` rather than renamed there, so a failed
    write still leaves the previous session at `STATE_FILE`.
    """
    if not STATE_FILE.exists():
        return ""
    try:
        for i in range(3, 1, -1):
            src = STATE_FILE.with_name(f"{STATE_FILE.name}.{i - 1}")
            dst = STATE_FILE.with_name(f"{STATE_FILE.name}.{i}")
            if src.exists():
                # `replace` overwrites the destination atomically, so the old
                # backup is never missing for longer than the rename itself.
                src.replace(dst)
        first = STATE_FILE.with_name(f"{STATE_FILE.name}.1")
        # Copy through a temporary file so the new backup appears atomically
        # and a failed copy cannot leave a truncated destination.
        _copy_atomic(STATE_FILE, first)
        log.info("Rotated backups: %s -> %s.1/.2/.3", STATE_FILE, STATE_FILE)
        return " (backup .1 created)"
    except OSError as exc:
        log.warning("Backup rotation failed for %s: %s", STATE_FILE, exc)
        return ""


def save_session(*, notify_user: bool = True) -> bool:
    """Save the current Sway/i3 session to disk.

    Serialize every workspace, the hidden scratchpad, background apps, and
    the focused workspace, then rotate the previous state file to a numbered
    backup before writing the new one. Report an unreachable compositor or a
    failed write without raising.

    Args:
        notify_user: Whether to show desktop notifications.

    Returns:
        True when a snapshot was written, False when the compositor was
        unreachable or the file could not be written.
    """
    backup_msg = ""
    try:
        with operation_lock(STATE_DIR):
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            NVIM_SESSION_DIR.mkdir(parents=True, exist_ok=True)
            (STATE_DIR / "nvim" / "manager").mkdir(parents=True, exist_ok=True)

            tree = get_tree()
            cache = ProcCache.snapshot()

            workspaces = []
            hidden_scratchpad = []

            for ws in walk_tree(tree):
                if ws.get("type") != "workspace":
                    continue
                if ws.get("name") == SCRATCHPAD_WORKSPACE:
                    for child in ws.get("nodes", []):
                        cleaned = clean_tree(child, cache)
                        if cleaned:
                            hidden_scratchpad.extend(_flatten_windows(cleaned))
                    for child in ws.get("floating_nodes", []):
                        for cleaned in clean_floating(child, cache):
                            hidden_scratchpad.extend(_flatten_windows(cleaned))
                    continue
                if ws.get("name") in STAGING_WORKSPACES:
                    # A restore staging workspace is an implementation
                    # detail, never part of a user's saved session.
                    continue
                cleaned = clean_tree(ws, cache)
                if cleaned and (cleaned.get("nodes") or cleaned.get("floating_nodes")):
                    cleaned["name"] = ws.get("name")
                    workspaces.append(cleaned)

            background_apps = detect_background_apps(cache, tree=tree)

            payload = Snapshot(
                workspaces=workspaces,
                hidden_scratchpad=hidden_scratchpad,
                background_apps=background_apps,
                focused_workspace=_focused_workspace_name(tree),
            ).to_payload()

            # Authoritative single commit. Restore reads background_apps from
            # this file when present, preventing mixed-generation restore.
            # Write the new payload before touching the live file, so a failed
            # write cannot leave the user without a restorable session.
            content = json.dumps(payload, indent=2)
            tmp_path = _write_temp(STATE_FILE, content)
            try:
                backup_msg = _rotate_backups()
                os.replace(tmp_path, STATE_FILE)
                _fsync_dir(STATE_DIR)
            except OSError:
                tmp_path.unlink(missing_ok=True)
                raise

            # Sidecar for tools that read the background apps file
            # directly. It must not fail the main save.
            try:
                _atomic_write(
                    BACKGROUND_APPS_FILE, json.dumps(background_apps, indent=2)
                )
                log.info("Background apps saved to %s", BACKGROUND_APPS_FILE)
            except OSError as exc:
                log.warning(
                    "Could not write compatibility background apps file %s: %s",
                    BACKGROUND_APPS_FILE,
                    exc,
                )

            log.info("Session saved to %s%s", STATE_FILE, backup_msg)
    except IpcError as exc:
        # The compositor is unreachable, so there is nothing to save.
        # Report it plainly instead of raising a traceback at the user.
        log.warning("Cannot reach the window manager: %s", exc)
        if notify_user:
            notify("session save failed: cannot reach the window manager")
        return False
    except OSError as exc:
        log.warning("Could not write session file %s: %s", STATE_FILE, exc)
        if notify_user:
            notify("session save failed: could not write the session file")
        return False
    finally:
        close_connection()

    if notify_user:
        notify(f"session saved successfully{backup_msg}")
    return True
