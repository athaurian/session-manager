"""Versioned session snapshot model and per-node validation.

The saved session is a contract between save and restore. This module owns
that contract: the current version, the shape of the payload, and the rules
for repairing a damaged node. A bad node is dropped or stripped instead of
rejecting the whole session, so one unsupported window cannot make every
other window unrestorable.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

CURRENT_VERSION = 3
SUPPORTED_VERSIONS = frozenset({1, 2, 3})

_NODE_TYPES = frozenset({"workspace", "con", "window"})


class StateValidationError(ValueError):
    """Raised when a payload cannot be read as a session at all."""


def _is_number(value: Any) -> bool:
    """Report whether a value is a real number, not a bool."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_finite(value: Any) -> bool:
    """Report whether a value is a finite number."""
    return _is_number(value) and math.isfinite(float(value))


def _clean_rect(rect: Any, path: str, issues: list[str]) -> dict[str, Any] | None:
    """Return a rect with only finite numeric members."""
    if not isinstance(rect, dict):
        issues.append(f"{path} must be an object; dropped")
        return None
    cleaned: dict[str, Any] = {}
    for key in ("x", "y", "width", "height"):
        if key not in rect:
            continue
        value = rect[key]
        if not _is_finite(value):
            issues.append(f"{path}.{key} must be a finite number; dropped")
            continue
        cleaned[key] = value
    return cleaned


def _clean_marks(marks: Any, path: str, issues: list[str]) -> list[str]:
    """Return only the string marks, dropping anything else."""
    if not isinstance(marks, list):
        issues.append(f"{path} must be a list; dropped")
        return []
    cleaned: list[str] = []
    for index, mark in enumerate(marks):
        if not isinstance(mark, str):
            issues.append(f"{path}[{index}] must be a string; dropped")
            continue
        cleaned.append(mark)
    return cleaned


def _clean_node(node: Any, path: str, issues: list[str]) -> dict[str, Any] | None:
    """Return a repaired node, or None when it cannot be used at all."""
    if not isinstance(node, dict):
        issues.append(f"{path} must be an object; dropped")
        return None

    ntype = node.get("type")
    if ntype not in _NODE_TYPES:
        issues.append(f"{path}.type is invalid or missing; dropped")
        return None

    cleaned = dict(node)

    if ntype == "window":
        if "rect" in cleaned:
            rect = _clean_rect(cleaned["rect"], f"{path}.rect", issues)
            if rect is None:
                cleaned.pop("rect", None)
            else:
                cleaned["rect"] = rect
        if "marks" in cleaned:
            cleaned["marks"] = _clean_marks(cleaned["marks"], f"{path}.marks", issues)
        if "fullscreen_mode" in cleaned and (
            not isinstance(cleaned["fullscreen_mode"], int)
            or isinstance(cleaned["fullscreen_mode"], bool)
        ):
            issues.append(f"{path}.fullscreen_mode must be an integer; dropped")
            cleaned.pop("fullscreen_mode", None)
        if "percent" in cleaned and not _is_finite(cleaned["percent"]):
            issues.append(f"{path}.percent must be a finite number; dropped")
            cleaned.pop("percent", None)
        if "name" in cleaned and not isinstance(cleaned["name"], str):
            issues.append(f"{path}.name must be a string; dropped")
            cleaned.pop("name", None)
        if "app_id" in cleaned and not isinstance(cleaned["app_id"], str):
            issues.append(f"{path}.app_id must be a string; dropped")
            cleaned.pop("app_id", None)
        return cleaned

    if ntype == "workspace":
        name = cleaned.get("name")
        if not isinstance(name, str) or not name:
            issues.append(f"{path}.name must be a non-empty string; dropped")
            return None
        if "output" in cleaned and not isinstance(cleaned["output"], str):
            issues.append(f"{path}.output must be a string; dropped")
            cleaned.pop("output", None)

    if not isinstance(cleaned.get("layout"), str):
        issues.append(f"{path}.layout must be a string; using splith")
        cleaned["layout"] = "splith"

    if "percent" in cleaned and not _is_finite(cleaned["percent"]):
        issues.append(f"{path}.percent must be a finite number; dropped")
        cleaned.pop("percent", None)

    for key in ("nodes", "floating_nodes"):
        children = cleaned.get(key, [])
        if not isinstance(children, list):
            issues.append(f"{path}.{key} must be a list; dropped")
            cleaned[key] = []
            continue
        repaired: list[dict[str, Any]] = []
        for index, child in enumerate(children):
            child_path = f"{path}.{key}[{index}]"
            result = _clean_node(child, child_path, issues)
            if result is not None:
                repaired.append(result)
        cleaned[key] = repaired

    return cleaned


@dataclass
class Snapshot:
    """A parsed session payload with its source version and repairs."""

    workspaces: list[dict[str, Any]] = field(default_factory=list)
    hidden_scratchpad: list[dict[str, Any]] = field(default_factory=list)
    background_apps: list[dict[str, str]] | None = None
    focused_workspace: str | None = None
    version: int = CURRENT_VERSION
    issues: list[str] = field(default_factory=list)

    @classmethod
    def from_payload(cls, payload: Any) -> Snapshot:
        """Parse a saved payload, repairing what can be repaired.

        Raises:
            StateValidationError: The payload is not a session at all, or
                it declares a version this build cannot read.
        """
        if isinstance(payload, list):
            version = 1
            raw_workspaces: Any = payload
            raw_hidden: Any = []
            raw_background: Any = None
            raw_focused: Any = None
        elif isinstance(payload, dict):
            raw_version = payload.get("version", 2)
            if not isinstance(raw_version, int) or isinstance(raw_version, bool):
                raise StateValidationError("version must be an integer")
            if raw_version not in SUPPORTED_VERSIONS:
                raise StateValidationError(f"unsupported session version {raw_version}")
            version = raw_version
            raw_workspaces = payload.get("workspaces", [])
            raw_hidden = payload.get("hidden_scratchpad", [])
            raw_background = payload.get("background_apps")
            raw_focused = payload.get("focused_workspace")
        else:
            raise StateValidationError(
                "session payload must be an object or legacy list"
            )

        if not isinstance(raw_workspaces, list):
            raise StateValidationError("workspaces must be a list")
        if not isinstance(raw_hidden, list):
            raise StateValidationError("hidden_scratchpad must be a list")

        issues: list[str] = []

        workspaces: list[dict[str, Any]] = []
        for index, node in enumerate(raw_workspaces):
            result = _clean_node(node, f"workspaces[{index}]", issues)
            if result is None:
                continue
            if result.get("type") != "workspace":
                issues.append(f"workspaces[{index}] must be a workspace; dropped")
                continue
            workspaces.append(result)

        hidden_scratchpad: list[dict[str, Any]] = []
        for index, node in enumerate(raw_hidden):
            result = _clean_node(node, f"hidden_scratchpad[{index}]", issues)
            if result is None:
                continue
            if result.get("type") not in ("window", "con"):
                issues.append(
                    f"hidden_scratchpad[{index}] must be a window or container; dropped"
                )
                continue
            hidden_scratchpad.append(result)

        background_apps: list[dict[str, str]] | None = None
        if raw_background is not None:
            if not isinstance(raw_background, list):
                issues.append("background_apps must be a list; dropped")
            else:
                background_apps = []
                for index, app in enumerate(raw_background):
                    if not isinstance(app, dict):
                        issues.append(
                            f"background_apps[{index}] must be an object; dropped"
                        )
                        continue
                    if "app_id" in app and not isinstance(app["app_id"], str):
                        issues.append(
                            f"background_apps[{index}].app_id must be a string; dropped"
                        )
                        continue
                    background_apps.append(app)

        focused_workspace: str | None = None
        if raw_focused is not None:
            if not isinstance(raw_focused, str) or not raw_focused:
                issues.append("focused_workspace must be a non-empty string; dropped")
            else:
                focused_workspace = raw_focused

        return cls(
            workspaces=workspaces,
            hidden_scratchpad=hidden_scratchpad,
            background_apps=background_apps,
            focused_workspace=focused_workspace,
            version=version,
            issues=issues,
        )

    def to_payload(self) -> dict[str, Any]:
        """Serialize to the current on-disk version."""
        return {
            "version": CURRENT_VERSION,
            "workspaces": self.workspaces,
            "hidden_scratchpad": self.hidden_scratchpad,
            "background_apps": self.background_apps or [],
            "focused_workspace": self.focused_workspace,
        }
