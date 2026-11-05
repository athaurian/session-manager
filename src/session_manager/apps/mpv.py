"""mpv media player: state inspection and restore."""

from __future__ import annotations

import subprocess
from collections.abc import MutableSet
from pathlib import Path

from ..config import APP_WAIT_TIMEOUT
from ..ipc import WindowEventWatcher
from ..proc_cache import ProcCache
from ._common import read_cmdline, reap_if_needed


def get_mpv_info(pid: int, cwd: str = "") -> str:
    """Return the media path from an mpv command line.

    A relative path is resolved against the mpv process's own working
    directory, because that is the directory mpv opened the file from.
    The manager's own working directory is a different one.
    """
    for arg in reversed(read_cmdline(pid)[1:]):
        if not arg or arg.startswith("-"):
            continue
        path = Path(arg)
        if not path.is_absolute() and cwd:
            path = Path(cwd) / path
        if path.is_file():
            return str(path)
    return ""


def capture_mpv(_source: dict, pid: int, cache: ProcCache) -> dict[str, object]:
    """Return the saved media file for an mpv window."""
    entry = cache.get(pid)
    cwd = entry.cwd if entry else ""
    return {"media_path": get_mpv_info(pid, cwd)}


def launch_mpv(node: dict, claimed_ids: MutableSet[int]) -> int | None:
    """Launch mpv and return its window id."""
    from .generic import wait_for_window_by_pid

    media_path = node.get("media_path", "")
    if not media_path or not Path(media_path).is_file():
        return None

    cmd = ["mpv", "--force-window", media_path]

    proc = None
    with WindowEventWatcher() as watcher:
        proc = subprocess.Popen(
            cmd,
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            win_id = wait_for_window_by_pid(
                watcher, proc.pid, "mpv", timeout=APP_WAIT_TIMEOUT
            )
        finally:
            reap_if_needed(proc)

    if not win_id:
        return None
    claimed_ids.add(win_id)
    return win_id
