"""One boundary for per-application capture, launch, and identity.

Save, restore, and the launchers each need app-specific knowledge: which
fields a window saves, how the app is started, and how a live window is
recognised as the saved one. This table is the single place that holds it,
so none of those callers grows its own app-id chain.
"""

from __future__ import annotations

from collections.abc import Callable, MutableSet
from dataclasses import dataclass

from ..proc_cache import ProcCache

# Returns the app-specific fields to merge into a saved window.
Capture = Callable[[dict, int, ProcCache], dict[str, object]]
# Launches the app and returns the window it opened.
Launch = Callable[[dict, MutableSet[int]], int | None]
# Refines a live window's app id, or returns None to drop the window.
Classify = Callable[[dict, str], str | None]


@dataclass(frozen=True)
class AppHandler:
    """Everything save and restore need to know about one application.

    Attributes:
        launch: Starts the app and returns its window, or None when the app
            is restored as a group instead of one window at a time.
        capture: Adds the app's own fields to a saved window.
        classify: Turns a live window's app id into the saved one, or None
            when the window should not be saved.
        pid_matched: The launch waits for the window owned by the process it
            started, so two launches of the same app cannot claim each
            other's window.
        grouped: One launch brings back every saved window of this app.
        on_demand: The window is spawned by its own keybinding, so a missing
            one is not reported as lost.
    """

    launch: Launch | None = None
    capture: Capture | None = None
    classify: Classify | None = None
    pid_matched: bool = False
    grouped: bool = False
    on_demand: bool = False

    @property
    def adoptable(self) -> bool:
        """Whether a live window may stand in for a saved one.

        A grouped app matches its own windows by title, so it is left alone.
        """
        return not self.grouped


def _handlers() -> tuple[dict[str, AppHandler], AppHandler]:
    """Return the handlers by canonical app id, and the fallback handler.

    The app modules are imported here rather than at module import time so
    they can keep importing the generic launcher lazily.
    """
    from .calibre import capture_calibre, launch_ebook_viewer
    from .generic import launch_generic
    from .kitty import capture_kitty, classify_kitty, launch_kitty, launch_kitty_drop
    from .mpv import capture_mpv, launch_mpv
    from .thunar import capture_thunar, launch_thunar
    from .zathura import capture_zathura, launch_zathura

    return (
        {
            "kitty": AppHandler(
                launch=launch_kitty,
                capture=capture_kitty,
                classify=classify_kitty,
                pid_matched=True,
            ),
            "kitty_drop": AppHandler(
                launch=launch_kitty_drop,
                capture=capture_kitty,
                pid_matched=True,
                on_demand=True,
            ),
            "org.pwmt.zathura": AppHandler(
                launch=launch_zathura,
                capture=capture_zathura,
                pid_matched=True,
            ),
            "calibre-ebook-viewer": AppHandler(
                launch=launch_ebook_viewer,
                capture=capture_calibre,
            ),
            "thunar": AppHandler(
                launch=launch_thunar,
                capture=capture_thunar,
                pid_matched=True,
            ),
            "mpv": AppHandler(
                launch=launch_mpv,
                capture=capture_mpv,
                pid_matched=True,
            ),
            "helium": AppHandler(grouped=True),
        },
        AppHandler(launch=launch_generic),
    )


def handler_for(app_id: str) -> AppHandler:
    """Return the handler for a canonical app id.

    Any other app is started from its command or desktop entry, so it gets
    the generic handler.
    """
    table, generic = _handlers()
    return table.get(app_id, generic)
