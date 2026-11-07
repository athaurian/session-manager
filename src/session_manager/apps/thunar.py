"""Thunar file manager: state inspection and restore."""

from __future__ import annotations

import subprocess
from collections.abc import MutableSet
from pathlib import Path

from ..config import APP_PROFILES
from ..ipc import WindowEventWatcher, matching_window_ids
from ..proc_cache import ProcCache
from ._common import read_cmdline, reap_if_needed


def get_thunar_info(pid: int) -> str:
    """Return the folder path from a Thunar command line or cwd."""
    for arg in reversed(read_cmdline(pid)[1:]):
        if arg and not arg.startswith("-") and Path(arg).is_dir():
            return arg
    # No directory argument: infer it from the process working directory.
    try:
        cwd = Path(f"/proc/{pid}/cwd").readlink()
        if cwd.is_dir():
            return str(cwd)
    except OSError:
        pass
    return ""


def capture_thunar(_source: dict, pid: int, _cache: ProcCache) -> dict[str, object]:
    """Return the saved folder for a Thunar window."""
    return {"folder_path": get_thunar_info(pid)}


def launch_thunar(node: dict, claimed_ids: MutableSet[int]) -> int | None:
    """Launch Thunar and return its window id.

    Thunar is single-instance over D-Bus: the `thunar` command hands the
    folder to the running daemon and exits, so the window belongs to the
    daemon rather than to the process that was started here. The window is
    therefore found by app identity, taking the windows that were already
    open as the baseline so an old window is never claimed twice.
    """
    from .generic import wait_for_app_window

    folder = node.get("folder_path", "")
    if not folder or not Path(folder).is_dir():
        folder = str(Path.home())

    cmd = ["thunar", folder]
    app_id = "thunar"
    class_name = node.get("class", "")
    profile = APP_PROFILES[app_id]
    baseline_ids = set(matching_window_ids(app_id, class_name))

    proc = None
    with WindowEventWatcher() as watcher:
        proc = subprocess.Popen(
            cmd,
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            win_id = wait_for_app_window(
                app_id,
                class_name,
                watcher,
                settle=profile["settle"],
                reject_floating=profile["reject_floating"],
                timeout=profile["timeout"],
                baseline_ids=baseline_ids,
                claimed_ids=claimed_ids,
                process=proc,
                # The launcher exits as soon as the daemon took the request,
                # so its exit says nothing about the window.
                fail_fast_on_exit=False,
            )
        finally:
            reap_if_needed(proc)

    if not win_id:
        return None
    claimed_ids.add(win_id)
    return win_id
