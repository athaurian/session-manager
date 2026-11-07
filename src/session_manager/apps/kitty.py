"""Kitty terminal launch and nvim session arguments."""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
from collections.abc import MutableSet
from pathlib import Path

from ..config import (
    DROP_TERMINAL_INSTANCE,
    DROP_TERMINAL_MARK,
    DROP_TERMINAL_TITLE,
    NVIM_SESSION_DIR,
    TERMINAL_WAIT_TIMEOUT,
    TRANSIENT_TERMINAL_TITLES,
    escape_cwd,
)
from ..ipc import WindowEventWatcher, node_instance
from ..nvim.inspect import get_terminal_nvim_state, is_dashboard_state
from ..nvim.snapshot import (
    create_manager_snapshot,
    get_nvim_snapshot_path,
    snacks_sidecar_path,
)
from ..nvim.sockets import nvim_socket_path
from ..proc_cache import ProcCache
from ._common import reap_if_needed

log = logging.getLogger("session_manager")

# The drop terminal is identified by any of its WM_CLASS instance, window name,
# or mark, because each window manager surfaces a different subset of them.
# The transient helper terminals share one set of names: a title, an instance,
# and a mark all name the same few helpers.
_TRANSIENT_KITTY_NAMES = frozenset(TRANSIENT_TERMINAL_TITLES)
_DROP_KITTY_TITLE = DROP_TERMINAL_TITLE
_DROP_KITTY_INSTANCE = DROP_TERMINAL_INSTANCE
_DROP_KITTY_MARK = DROP_TERMINAL_MARK


def _legacy_session_file(cwd: str) -> str:
    """Return a legacy per-directory session file path, or an empty string."""
    sf = NVIM_SESSION_DIR / f"{escape_cwd(cwd)}.vim"
    return str(sf) if sf.exists() else ""


def classify_kitty(node: dict, app_id: str) -> str | None:
    """Classify a live kitty window, dropping transient helper terminals."""
    if app_id != "kitty":
        return app_id

    title = node.get("name") or ""
    instance = node_instance(node)
    marks = node.get("marks") or []

    if (
        title in _TRANSIENT_KITTY_NAMES
        or instance in _TRANSIENT_KITTY_NAMES
        or any(mark in _TRANSIENT_KITTY_NAMES for mark in marks)
    ):
        return None

    if (
        title == _DROP_KITTY_TITLE
        or instance == _DROP_KITTY_INSTANCE
        or _DROP_KITTY_MARK in marks
    ):
        return "kitty_drop"
    return app_id


def capture_kitty(_source: dict, pid: int, cache: ProcCache) -> dict[str, object]:
    """Return the saved terminal state for a kitty window."""
    cwd = cache.get_terminal_cwd(pid) if pid else str(Path.home())
    zellij_sess = cache.get_zellij_session(pid) if pid else None
    is_ssh, ssh_cmd = cache.is_ssh_or_sudo(pid) if pid else (False, "")
    nvim_pid = cache.get_nvim_pid(pid) if pid else None

    info: dict[str, object] = {}
    if zellij_sess is not None:
        info["nvim_type"] = "none"
        info["zellij_session"] = zellij_sess
        info["cwd"] = cwd
        log.info("Kitty %s has zellij session %s", pid, zellij_sess)
    elif is_ssh:
        info["nvim_type"] = "none"
        info["was_ssh"] = ssh_cmd
        info["cwd"] = cwd
        log.info("Kitty %s is ssh/sudo (%s), saving cwd only", pid, ssh_cmd)
    elif nvim_pid is not None:
        nvim_server_pid, live_state = get_terminal_nvim_state(pid, cache)

        if live_state is None:
            info["nvim_type"] = "unknown"
            info["session_file"] = _legacy_session_file(cwd)
        elif is_dashboard_state(live_state):
            info["nvim_type"] = "dashboard"
        else:
            snapshot_path = get_nvim_snapshot_path(cwd, nvim_server_pid)
            snapshot_path.parent.mkdir(parents=True, exist_ok=True)
            server_path = (
                str(nvim_socket_path(nvim_server_pid))
                if nvim_server_pid is not None
                else ""
            )
            ok, err = create_manager_snapshot(server_path, snapshot_path)
            if ok and snapshot_path.exists():
                info["nvim_type"] = "session"
                info["nvim_snapshot"] = str(snapshot_path)
                sidecar = snacks_sidecar_path(snapshot_path)
                if sidecar.exists():
                    info["nvim_snacks_sidecar"] = str(sidecar)
            else:
                info["nvim_type"] = "unknown"
                info["session_file"] = _legacy_session_file(cwd)
                log.warning("Failed to snapshot nvim in %s (using file): %s", cwd, err)
        info["cwd"] = cwd
    else:
        info["nvim_type"] = "none"
        info["cwd"] = cwd

    return info


def _zellij_args(node: dict) -> list[str] | None:
    """Return the zellij launch arguments for a saved node, if any."""
    sess = node.get("zellij_session")
    if sess and isinstance(sess, str):
        # Saved as "zellij" for bare zellij without named session
        if sess == "zellij":
            return ["zellij"]
        return ["zellij", "attach", "-c", sess]
    return None


def build_kitty_nvim_args(node: dict) -> list[str]:
    """Build the shell command that restores a terminal's programs."""
    # Zellij takes precedence over nvim
    z_args = _zellij_args(node)
    if z_args is not None:
        return z_args

    was_ssh = node.get("was_ssh")
    if was_ssh and isinstance(was_ssh, str):
        # Do not auto-run ssh, because it needs a password. Leave a reconnect
        # hint for the user instead. Quote the whole message: quoting only the
        # command would leave shell metacharacters outside the echo argument.
        message = f"was: {was_ssh} — press Up to reconnect"
        return ["bash", "-ic", f"echo {shlex.quote(message)}; exec bash"]

    nvim_type = node.get("nvim_type", "none")
    snapshot_path_str = node.get("nvim_snapshot", "")
    snacks_sidecar_str = node.get("nvim_snacks_sidecar", "")
    session_path_str = node.get("session_file", "")

    if (
        nvim_type == "session"
        and snapshot_path_str
        and Path(snapshot_path_str).is_file()
    ):
        safe_snapshot = shlex.quote(snapshot_path_str)
        lua_parts = [
            (
                "local f=os.getenv('NVIM_RESTORE_FILE'); "
                "if f then vim.cmd('source ' .. vim.fn.fnameescape(f)) end"
            ),
        ]
        snacks_sidecar = Path(snacks_sidecar_str) if snacks_sidecar_str else None
        if snacks_sidecar and snacks_sidecar.exists():
            try:
                content = snacks_sidecar.read_text(
                    encoding="utf-8", errors="replace"
                ).strip()
                if content == "1":
                    lua_parts.append(
                        "vim.defer_fn(function() "
                        "pcall(require, 'snacks') "
                        "pcall(Snacks.explorer) "
                        "end, 300)"
                    )
            except OSError:
                pass
        full_lua = "; ".join(lua_parts)
        shell_cmd = (
            f"NVIM_RESTORE_SESSION=1 "
            f"NVIM_RESTORE_FILE={safe_snapshot} "
            f"nvim -c {shlex.quote('lua ' + full_lua)}; exec bash"
        )
        return ["bash", "-ic", shell_cmd]

    if nvim_type == "dashboard":
        return ["nvim"]

    if (
        nvim_type in ("unknown", "session")
        and session_path_str
        and Path(session_path_str).is_file()
    ):
        safe_session = shlex.quote(session_path_str)
        log.info("Restoring kitty nvim from legacy session file: %s", session_path_str)
        lua_cmd = (
            "local f=os.getenv('NVIM_RESTORE_FILE'); "
            "if f then vim.cmd('source ' .. vim.fn.fnameescape(f)) end"
        )
        shell_cmd = (
            f"NVIM_RESTORE_SESSION=1 NVIM_RESTORE_FILE={safe_session} "
            f"nvim -c {shlex.quote('lua ' + lua_cmd)}; exec bash"
        )
        return ["bash", "-ic", shell_cmd]

    return ["bash"]


def build_kitty_command(node: dict, *, drop: bool = False) -> list[str]:
    """Build the kitty launch command for a saved node."""
    cwd = node.get("cwd", str(Path.home()))
    if drop:
        # `--name` sets the X11 WM_CLASS instance (the class stays "kitty"),
        # which is what the window manager's `for_window` rule matches on.
        # Without it the rule never fires, so the dropdown would not pick up
        # its floating size, mark, and scratchpad placement until the next
        # toggle. `--title` keeps the window name stable across saves.
        return [
            "kitty",
            f"--name={DROP_TERMINAL_INSTANCE}",
            f"--title={DROP_TERMINAL_TITLE}",
            "--directory",
            cwd,
        ] + build_kitty_nvim_args(node)

    title = node.get("name", "kitty")
    if not title or title == "kitty":
        user = os.getenv("USER", "user")
        host = os.uname().nodename.split(".")[0]
        home = str(Path.home())
        short_cwd = (
            "~"
            if cwd == home
            else (f"~{cwd[len(home) :]}" if cwd.startswith(home + "/") else cwd)
        )
        title = f"{user}@{host}:{short_cwd}"
    return ["kitty", "--directory", cwd, "--title", title] + build_kitty_nvim_args(node)


def launch_kitty(
    node: dict, claimed_ids: MutableSet[int], *, drop: bool = False
) -> int | None:
    """Launch a kitty terminal and return its window id."""
    from .generic import wait_for_window_by_pid

    cmd = build_kitty_command(node, drop=drop)

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
                watcher, proc.pid, "kitty", timeout=TERMINAL_WAIT_TIMEOUT
            )
        finally:
            reap_if_needed(proc)

    if win_id:
        claimed_ids.add(win_id)
        return win_id
    return None


def launch_kitty_drop(node: dict, claimed_ids: MutableSet[int]) -> int | None:
    """Launch the dropdown terminal and return its window id."""
    return launch_kitty(node, claimed_ids, drop=True)
