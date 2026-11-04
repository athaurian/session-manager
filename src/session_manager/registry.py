"""Thread-safe window identity for a restore run.

Restore used to launch a window, place it, and only then launch the next one.
Materialization separates launching from placing: independent windows are
launched in parallel, and the arrangement walk consumes the resulting ids.

That makes the claim set and the saved-node map shared across threads, so both
live behind a lock rather than relying on the interpreter.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterator, MutableSet
from concurrent.futures import ThreadPoolExecutor

from .config import MATERIALIZE_WORKERS

log = logging.getLogger("session_manager")


class ClaimedWindows(MutableSet[int]):
    """The live window ids already assigned to a saved window.

    A window is claimed exactly once. Two launches that map at the same time
    must not both see the same id as free, so membership and insertion are
    guarded by one lock.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ids: set[int] = set()

    def __contains__(self, value: object) -> bool:
        with self._lock:
            return value in self._ids

    def __iter__(self) -> Iterator[int]:
        with self._lock:
            snapshot = set(self._ids)
        return iter(snapshot)

    def __len__(self) -> int:
        with self._lock:
            return len(self._ids)

    def add(self, value: int) -> None:
        with self._lock:
            self._ids.add(value)

    def discard(self, value: int) -> None:
        with self._lock:
            self._ids.discard(value)


class WindowRegistry:
    """Map each saved node to the live window that represents it.

    Launch units are submitted to a bounded pool. Every node covered by a
    unit shares one event, so a caller can wait for exactly the window it
    needs instead of for the whole session. A node that was never submitted
    is reported as unmaterialized, which lets callers fall back to launching
    on demand.
    """

    def __init__(self, max_workers: int = MATERIALIZE_WORKERS) -> None:
        self._lock = threading.Lock()
        self._events: dict[int, threading.Event] = {}
        self._results: dict[int, int | None] = {}
        self._pool = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="session-materialize"
        )

    def submit(self, nodes: list[dict], task: Callable[[], None]) -> threading.Event:
        """Run `task` in the pool and associate its result with `nodes`.

        The returned event is set when the task finishes, whether or not it
        recorded ids for every node.
        """
        done = threading.Event()
        with self._lock:
            for node in nodes:
                key = id(node)
                self._events[key] = done
                self._results[key] = None

        def run() -> None:
            """Run the unit and release every node waiting on it."""
            try:
                task()
            except Exception:
                log.exception("Materialization unit failed")
            finally:
                done.set()

        self._pool.submit(run)
        return done

    def record(self, node: dict, win_id: int | None) -> None:
        """Record the live id assigned to a saved node."""
        with self._lock:
            self._results[id(node)] = win_id

    def has(self, node: dict) -> bool:
        """Report whether this node was submitted for materialization."""
        with self._lock:
            return id(node) in self._events

    def resolve(self, node: dict) -> int | None:
        """Wait for the node's launch unit and return its live window id.

        Raises:
            KeyError: The node was never submitted.
        """
        with self._lock:
            done = self._events.get(id(node))
        if done is None:
            raise KeyError(f"node was not materialized: {node.get('name')!r}")
        done.wait()
        with self._lock:
            return self._results.get(id(node))

    def close(self) -> None:
        """Wait for in-flight launches and stop the pool."""
        self._pool.shutdown(wait=True, cancel_futures=True)
