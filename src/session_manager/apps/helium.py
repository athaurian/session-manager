"""Helium browser group save and restore with title matching."""

from __future__ import annotations

import json
import logging
import subprocess
import time
from collections.abc import Collection
from itertools import islice

from ..config import (
    APP_PROFILES,
    DEFAULT_APP_PROFILE,
    HELIUM_ASSIGNMENT_TIMEOUT,
    HELIUM_CONFIG_DIR,
    HELIUM_TITLE_GRACE_PERIOD,
    HELIUM_WINDOW_STALL_TIMEOUT,
    WINDOW_SETTLE_POLL_INTERVAL,
    AppProfile,
)
from ..ipc import WindowEventWatcher, get_tree, node_app_id, walk_tree
from ..proc_cache import ProcCache
from ..state import RestoreContext
from ._common import normalize_title, reap_if_needed

log = logging.getLogger("session_manager")


def _assign_by_title(
    remaining_saved: list[int],
    unassigned: list[int],
    titles: dict[int, str],
    ctx: RestoreContext,
) -> bool:
    """Match saved Helium windows to live windows by title.

    Try an exact match first, then a normalized (lowercased, collapsed
    whitespace) match. Return True if at least one new match was made.

    Args:
        remaining_saved: Indexes of saved windows still needing a
            live window.
        unassigned: Live window ids available for assignment.
        titles: Mapping of live window id to current title.
        ctx: Restore context holding assignment results.

    Returns:
        True when at least one new match was made.
    """
    matched_any = False
    for idx in list(remaining_saved):
        if ctx.helium_restored_ids[idx] is not None:
            remaining_saved.remove(idx)
            continue
        saved_title = ctx.helium_saved_nodes[idx].get("name", "").strip()
        if not saved_title:
            continue
        match = next(
            (cid for cid in unassigned if titles.get(cid) == saved_title),
            None,
        )
        if match is None:
            norm_saved = normalize_title(saved_title)
            match = next(
                (
                    cid
                    for cid in unassigned
                    if norm_saved and normalize_title(titles.get(cid, "")) == norm_saved
                ),
                None,
            )
        if match is not None:
            ctx.helium_restored_ids[idx] = match
            unassigned.remove(match)
            remaining_saved.remove(idx)
            matched_any = True
    return matched_any


def _live_helium_titles(
    only: Collection[int] | None = None,
    skip: Collection[int] = (),
) -> dict[int, str]:
    """Return the titles of live Helium windows, by window id.

    `only` restricts the result to those ids and `skip` excludes them, so
    the baseline, the new-window, and the unassigned views of the same walk
    are all one filter over one tree read.
    """
    result: dict[int, str] = {}
    for n in walk_tree(get_tree()):
        cid = n.get("id")
        if (
            cid is None
            or n.get("type") not in ("con", "floating_con")
            or node_app_id(n) != "helium"
        ):
            continue
        if only is not None and cid not in only:
            continue
        if cid in skip:
            continue
        result[cid] = n.get("name") or ""
    return result


def collect_helium_nodes(node: dict, ctx: RestoreContext) -> None:
    """Collect saved Helium nodes into the restore context."""
    if node.get("type") == "window" and node_app_id(node) == "helium":
        idx = len(ctx.helium_saved_nodes)
        ctx.helium_saved_nodes.append(node)
        ctx.helium_saved_index[id(node)] = idx
        ctx.helium_restored_ids.append(None)
    for child in node.get("nodes", []) + node.get("floating_nodes", []):
        collect_helium_nodes(child, ctx)


def _suppress_helium_crash_prompt(cache: ProcCache) -> None:
    """Clear Helium's crash markers before launch."""
    if cache.process_named_running("helium"):
        log.info("Helium already running; skipping crash-marker patch.")
        return

    prefs_path = HELIUM_CONFIG_DIR / "Default" / "Preferences"
    if not prefs_path.exists():
        return
    try:
        data = json.loads(prefs_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        log.warning("Could not parse Helium Preferences")
        return

    profile = data.get("profile")
    if not isinstance(profile, dict):
        return

    changed = False
    if profile.get("exit_type") != "Normal":
        profile["exit_type"] = "Normal"
        changed = True
    if profile.get("exited_cleanly") is False:
        profile["exited_cleanly"] = True
        changed = True

    if changed:
        try:
            prefs_path.write_text(json.dumps(data), encoding="utf-8")
        except OSError:
            log.warning("Could not rewrite Helium Preferences")


def _live_new_helium_windows(baseline_ids: set[int]) -> dict[int, str]:
    """Return live Helium windows that appeared after the baseline."""
    return _live_helium_titles(skip=baseline_ids)


def _wait_for_new_window(
    watcher: WindowEventWatcher,
    baseline_ids: set[int],
    known: set[int],
    deadline: float,
) -> int | None:
    """Wait for one new browser window to appear.

    The window event stream is the primary signal; the tree is scanned as
    a fallback for an event that was missed. Returns the new window id, or
    None when the deadline passes.
    """
    next_scan = time.monotonic()
    while time.monotonic() < deadline:
        now = time.monotonic()
        wait = min(
            WINDOW_SETTLE_POLL_INTERVAL,
            max(0.01, deadline - now),
            max(0.01, next_scan - now),
        )
        ev = watcher.get(timeout=wait)
        if ev is not None:
            cid = ev.container.get("id")
            if (
                ev.change == "new"
                and cid is not None
                and node_app_id(ev.container) == "helium"
                and cid not in baseline_ids
                and cid not in known
            ):
                return cid
        now = time.monotonic()
        if now >= next_scan:
            for cid in _live_new_helium_windows(baseline_ids):
                if cid not in known:
                    return cid
            next_scan = now + WINDOW_SETTLE_POLL_INTERVAL
    return None


def _restore_scoped_helium(
    ctx: RestoreContext, count: int, baseline_ids: set[int], profile: AppProfile
) -> None:
    """Restore the browser windows of a scoped restore.

    A restore that covers a single workspace cannot replay the browser's
    own session, because that session holds the windows saved on every
    other workspace. Each window is opened on its own instead, and the
    browser does not expose its URLs to the compositor, so a window that
    was not already live comes back blank.

    A live window whose title matches a saved one is reused, so a browser
    that is already running keeps its tabs instead of growing blank ones.
    """
    remaining_saved = list(range(count))
    unassigned = list(baseline_ids)
    live_titles = _live_helium_titles(only=baseline_ids)
    # Only a title match reuses a live window here. A scoped restore must
    # not claim a window that belongs to another workspace's browser
    # session, so an unmatched saved window gets a blank one instead.
    _assign_by_title(remaining_saved, unassigned, live_titles, ctx)

    missing = [idx for idx in range(count) if ctx.helium_restored_ids[idx] is None]
    if not missing:
        return

    deadline = time.monotonic() + profile["timeout"]
    opened: set[int] = set()
    with WindowEventWatcher() as watcher:
        for idx in missing:
            try:
                proc = subprocess.Popen(
                    ["helium-browser", "--new-window", "about:blank"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            except OSError:
                log.warning("Could not launch helium window")
                break
            try:
                win_id = _wait_for_new_window(watcher, baseline_ids, opened, deadline)
            finally:
                reap_if_needed(proc)
            if win_id is None:
                log.warning("Helium window %d did not appear", idx + 1)
                break
            opened.add(win_id)
            ctx.helium_restored_ids[idx] = win_id


def restore_helium_group(ctx: RestoreContext) -> None:
    """Restore the saved Helium window group."""
    if ctx.helium_restore_started:
        return
    ctx.helium_restore_started = True
    if not ctx.helium_saved_nodes:
        return

    count = len(ctx.helium_saved_nodes)
    profile = APP_PROFILES.get("helium", DEFAULT_APP_PROFILE)
    baseline_ids = set(_live_helium_titles())
    cache = ProcCache.snapshot()
    _suppress_helium_crash_prompt(cache)

    if not ctx.helium_restore_last_session:
        _restore_scoped_helium(ctx, count, baseline_ids, profile)
        return

    # If Helium is already running with enough windows, reuse them directly
    # instead of launching a new instance (which would only signal the
    # existing one and exit, but still cost time).
    if cache.process_named_running("helium") and len(baseline_ids) >= count:
        existing_windows = dict(
            islice(_live_helium_titles(only=baseline_ids).items(), count)
        )
        if existing_windows:
            remaining_saved = list(range(count))
            unassigned = list(existing_windows.keys())

            _assign_by_title(remaining_saved, unassigned, dict(existing_windows), ctx)
            for idx in list(remaining_saved):
                if ctx.helium_restored_ids[idx] is None and unassigned:
                    ctx.helium_restored_ids[idx] = unassigned.pop(0)
                elif ctx.helium_restored_ids[idx] is None:
                    break
            return

    with WindowEventWatcher() as watcher:
        proc = subprocess.Popen(
            ["helium-browser", "--restore-last-session"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

        windows: dict[int, str] = {}
        deadline = time.monotonic() + profile["timeout"]
        next_tree_scan = time.monotonic() + 0.25
        last_new = time.monotonic()

        try:
            while len(windows) < count and time.monotonic() < deadline:
                # If the launcher exited quickly (Helium was already running
                # and just signaled the existing instance), avoid burning
                # the full timeout waiting for windows that will not appear.
                if proc.poll() is not None:
                    before = len(windows)
                    windows.update(_live_new_helium_windows(baseline_ids))
                    if not windows:
                        break
                    if len(windows) > before:
                        last_new = time.monotonic()
                    # Have some windows but not all; give a short grace
                    # for titles.
                    if deadline - time.monotonic() > HELIUM_TITLE_GRACE_PERIOD:
                        deadline = time.monotonic() + HELIUM_TITLE_GRACE_PERIOD
                if (
                    windows
                    and time.monotonic() - last_new > HELIUM_WINDOW_STALL_TIMEOUT
                ):
                    break
                now = time.monotonic()
                wait = min(
                    WINDOW_SETTLE_POLL_INTERVAL,
                    max(0.01, deadline - now),
                    max(0.01, next_tree_scan - now),
                )
                ev = watcher.get(timeout=wait)
                if ev is None:
                    pass
                else:
                    cont = ev.container
                    cid = cont.get("id")
                    if (
                        cid is not None
                        and node_app_id(cont) == "helium"
                        and cid not in baseline_ids
                    ):
                        if ev.change == "new" or (
                            ev.change == "title" and cid in windows
                        ):
                            is_new = cid not in windows
                            windows[cid] = cont.get("name") or ""
                            if is_new:
                                last_new = time.monotonic()
                        elif ev.change == "close":
                            windows.pop(cid, None)

                if len(windows) >= count:
                    break

                now = time.monotonic()
                if now >= next_tree_scan:
                    before = len(windows)
                    windows.update(_live_new_helium_windows(baseline_ids))
                    if len(windows) > before:
                        last_new = time.monotonic()
                    next_tree_scan = now + 0.25
        finally:
            reap_if_needed(proc)

        if len(windows) < count:
            windows.update(_live_new_helium_windows(baseline_ids))
            if len(windows) < count:
                missing = count - len(windows)
                log.info(
                    "Helium missing %d windows, launching via --new-window", missing
                )
                for idx in range(missing):
                    try:
                        p = subprocess.Popen(
                            ["helium-browser", "--new-window", "about:blank"],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            start_new_session=True,
                        )
                        time.sleep(0.6)
                        new_windows = _live_new_helium_windows(baseline_ids)
                        for cid, title in new_windows.items():
                            if cid not in windows:
                                windows[cid] = title
                        reap_if_needed(p)
                    except OSError:
                        log.warning("Failed to launch extra helium window")
                    if len(windows) >= count:
                        break
                windows.update(_live_new_helium_windows(baseline_ids))

        if not windows:
            log.warning("Helium started, but no new windows found.")
            return
        if len(windows) < count:
            log.warning("Helium restored only %d/%d windows.", len(windows), count)

        remaining_saved = list(range(count))
        unassigned = list(windows.keys())

        def _match_remaining(titles: dict[int, str]) -> None:
            """Assign every window that the current titles can match."""
            while _assign_by_title(remaining_saved, unassigned, titles, ctx):
                pass

        assignment_deadline = time.monotonic() + min(
            HELIUM_ASSIGNMENT_TIMEOUT, profile["timeout"]
        )
        _match_remaining(dict(windows))
        next_title_scan = time.monotonic() + 1.0
        while remaining_saved and unassigned and time.monotonic() < assignment_deadline:
            now = time.monotonic()
            wait = min(0.5, max(0.01, assignment_deadline - now))
            ev = watcher.get(timeout=wait)
            if ev is not None:
                cont = ev.container
                cid = cont.get("id")
                if (
                    cid is not None
                    and node_app_id(cont) == "helium"
                    and cid in unassigned
                ):
                    if ev.change == "title":
                        windows[cid] = cont.get("name") or ""
                        _match_remaining(dict(windows))
                    elif ev.change == "close":
                        windows.pop(cid, None)
                        unassigned.remove(cid)
                        _match_remaining(dict(windows))

            now = time.monotonic()
            if now >= next_title_scan:
                live_titles = _live_helium_titles(only=unassigned)
                if live_titles:
                    windows.update(live_titles)
                    _match_remaining(dict(windows))
                next_title_scan = now + 1.0

        for idx in list(remaining_saved):
            if ctx.helium_restored_ids[idx] is None and unassigned:
                ctx.helium_restored_ids[idx] = unassigned.pop(0)
            elif ctx.helium_restored_ids[idx] is None:
                break


def get_helium_restored_id(node: dict, ctx: RestoreContext) -> int | None:
    """Return the live window id assigned to a saved Helium node.

    The group is normally restored through materialization. A node that
    bypassed it, such as one launched on demand, restores the group here.
    """
    if not ctx.helium_restore_started:
        restore_helium_group(ctx)
    idx = ctx.helium_saved_index.get(id(node))
    if idx is None:
        return None
    return ctx.helium_restored_ids[idx]
