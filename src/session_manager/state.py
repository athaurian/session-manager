"""Mutable restore context threaded through the restore call chain."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from .registry import ClaimedWindows, WindowRegistry


@dataclass
class RestoreContext:
    """Mutable state for a single restore run."""

    claimed_ids: ClaimedWindows = field(default_factory=ClaimedWindows)
    registry: WindowRegistry = field(default_factory=WindowRegistry)
    current_workspace: str | None = None

    # Windows that could not be restored. One failing app must not stop
    # the rest of the session, so failures are collected here. Launches run
    # in worker threads, so writes are serialized.
    failures: list[str] = field(default_factory=list)

    # Windows that were saved but never came back, such as an app that is
    # no longer installed.
    missing: list[str] = field(default_factory=list)

    _lock: threading.Lock = field(
        default_factory=threading.Lock, repr=False, compare=False
    )

    def add_failure(self, message: str) -> None:
        """Record a failure from any thread."""
        with self._lock:
            self.failures.append(message)

    def add_missing(self, message: str) -> None:
        """Record a window that did not come back."""
        with self._lock:
            self.missing.append(message)

    # Helium group restore state
    helium_saved_nodes: list[dict] = field(default_factory=list)
    helium_saved_index: dict[int, int] = field(default_factory=dict)
    helium_restored_ids: list[int | None] = field(default_factory=list)
    helium_restore_started: bool = False
    helium_materialized: bool = False
    helium_thread: threading.Thread | None = None
