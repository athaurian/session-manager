"""Background app detection and restore (for example, minimized Vesktop)."""

from __future__ import annotations

import json
import logging
import os
import subprocess
from typing import Any

from .config import BACKGROUND_APPS_FILE, detect_backend
from .ipc import matching_window_ids, matching_window_ids_in_tree
from .proc_cache import ProcCache

log = logging.getLogger("session_manager")

# The installed Vesktop launcher is a wrapper script that execs
# `electron44`, so the running process is not named "vesktop" and its
# executable is shared with other Electron apps. Only its command line
# still names the app's own directory.
_VESKTOP_CMDLINE_MARKER = "/vesktop"


def _vesktop_running(cache: ProcCache) -> bool:
    """Report whether Vesktop is running.

    The launcher execs a generic Electron binary, so the process name
    does not identify the app; the command line still does.
    """
    return cache.process_named_running("vesktop") or cache.cmdline_contains(
        _VESKTOP_CMDLINE_MARKER
    )


def detect_background_apps(
    cache: ProcCache | None = None,
    *,
    tree: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    """Return background apps that have no visible window."""
    cache = cache or ProcCache.snapshot()
    apps: list[dict[str, str]] = []
    vesktop_visible = bool(
        matching_window_ids_in_tree(tree, "vesktop", "")
        if tree is not None
        else matching_window_ids("vesktop", "")
    )
    if _vesktop_running(cache) and not vesktop_visible:
        apps.append({"app_id": "vesktop"})
    return apps


def _unit_installed(service: str) -> bool:
    """Report whether a systemd user unit for a daemon is installed.

    A daemon the system does not ship has not gone missing, so it is
    skipped instead of warned about. When the answer cannot be read the
    daemon is assumed to be there, which keeps the old behaviour.
    """
    try:
        result = subprocess.run(
            ["systemctl", "--user", "show", service, "-p", "LoadState", "--value"],
            capture_output=True,
            text=True,
            timeout=1.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired, TypeError):
        return True
    if result.returncode != 0:
        return True
    return result.stdout.strip() != "not-found"


def _ensure_background_daemons(notify_user: bool = True) -> None:
    """Perform a best-effort health check for daily-use daemons.

    Log through the session_manager logger and send a notification when a
    daemon is missing.
    """
    from .logging_setup import notify

    # Skip health checks in tests via explicit opt-out.
    if os.getenv("SWAY_SESSION_SKIP_DAEMON_CHECKS") == "1":
        return

    backend = detect_backend()
    checks: list[tuple[str, list[str], str, str | None]] = [
        ("pipewire", ["pgrep", "-x", "pipewire"], "pipewire (audio)", "pipewire"),
        (
            "pipewire-pulse",
            ["pgrep", "-x", "pipewire-pulse"],
            "pipewire-pulse (audio)",
            "pipewire-pulse",
        ),
    ]
    if backend == "i3":
        # i3 starts its screen locker from the config rather than a user unit,
        # so report a missing lock process without trying to restart it.
        checks.insert(
            0,
            (
                "xss-lock",
                ["pgrep", "-x", "xss-lock"],
                "xss-lock (screen lock)",
                None,
            ),
        )
    else:
        checks.insert(
            0,
            (
                "swayidle",
                ["pgrep", "-x", "swayidle"],
                "swayidle (screen lock)",
                "swayidle",
            ),
        )

    for name, cmd, label, service in checks:
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=1.0, check=False)
            if r.returncode != 0:
                if service is not None and not _unit_installed(service):
                    # The system does not ship this daemon, so it has
                    # not gone missing.
                    log.debug(
                        "Skipping %s: no %s user unit is installed", name, service
                    )
                    continue
                msg = f"Background daemon {label} not running, attempting restart"
                log.warning(msg)
                if notify_user:
                    notify(msg, urgency="normal", timeout_ms=4000)
                if service is None:
                    continue
                try:
                    subprocess.run(
                        ["systemctl", "--user", "start", "--no-block", service],
                        capture_output=True,
                        timeout=2.0,
                        check=False,
                    )
                except (
                    OSError,
                    subprocess.SubprocessError,
                    subprocess.TimeoutExpired,
                    TypeError,
                ):
                    pass
        except (
            OSError,
            subprocess.SubprocessError,
            subprocess.TimeoutExpired,
            TypeError,
        ):
            continue

    # Ensure the session target is active (covers polkit, portal, etc. via
    # autostart). i3 sessions use i3-session.target; Sway uses
    # sway-session.target.
    target = f"{backend}-session.target"
    try:
        r = subprocess.run(
            ["systemctl", "--user", "is-active", "--quiet", target],
            capture_output=True,
            timeout=1.0,
            check=False,
        )
        if r.returncode != 0:
            msg = f"{target} not active, starting"
            log.info(msg)
            if notify_user:
                notify(msg, urgency="low", timeout_ms=3000)
            subprocess.run(
                ["systemctl", "--user", "start", "--no-block", target],
                capture_output=True,
                timeout=2.0,
                check=False,
            )
    except (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired, TypeError):
        pass


def restore_background_apps(
    apps: list[dict[str, str]] | None = None,
    *,
    cache: ProcCache | None = None,
    notify_user: bool = True,
) -> None:
    """Restore saved background apps and check desktop daemons."""
    _ensure_background_daemons(notify_user=notify_user)

    if apps is None:
        if not BACKGROUND_APPS_FILE.exists():
            return
        try:
            loaded = json.loads(BACKGROUND_APPS_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            log.warning("Could not parse %s", BACKGROUND_APPS_FILE)
            return
        if not isinstance(loaded, list):
            log.warning("Invalid background apps payload in %s", BACKGROUND_APPS_FILE)
            return
        apps = loaded

    for app in apps:
        if not isinstance(app, dict):
            continue
        if app.get("app_id") != "vesktop":
            continue
        if matching_window_ids("vesktop", ""):
            continue
        if cache is None:
            cache = ProcCache.snapshot()
        if _vesktop_running(cache):
            continue
        try:
            subprocess.Popen(
                ["vesktop", "--start-minimized"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            log.warning("Could not start vesktop: %s", exc)
            continue
        log.info("Restored vesktop --start-minimized")
        if notify_user:
            from .logging_setup import notify

            notify("vesktop restored", urgency="low", timeout_ms=3000)
