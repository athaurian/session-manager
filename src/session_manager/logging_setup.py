"""Logging and desktop notification helpers."""

from __future__ import annotations

import logging
import subprocess

log = logging.getLogger("session_manager")

_last_notify_id: str | None = None
_notify_send_missing = False

# A notification is a courtesy, not a result. `notify-send` must never
# hold up a save or a restore, so it gets a deadline of its own.
_NOTIFY_TIMEOUT = 5.0


def notify(msg: str, *, timeout_ms: int = 3000, urgency: str = "low") -> None:
    """Show a desktop notification, replacing the previous one if possible."""
    global _last_notify_id, _notify_send_missing
    if _notify_send_missing:
        return
    args = [
        "notify-send",
        "-u",
        urgency,
        "-t",
        str(timeout_ms),
        "-p",
        "󰁯 session manager",
        msg,
    ]
    if _last_notify_id is not None:
        args.extend(["-r", _last_notify_id])
    try:
        result = subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
            timeout=_NOTIFY_TIMEOUT,
        )
    except FileNotFoundError:
        _notify_send_missing = True
        log.info("notify-send not found; desktop notifications disabled")
        return
    except subprocess.TimeoutExpired:
        log.debug("notify-send timed out")
        return
    except OSError as exc:
        log.debug("notify-send failed: %s", exc)
        return
    nid = result.stdout.strip()
    _last_notify_id = nid if (nid and nid.isdigit()) else None
