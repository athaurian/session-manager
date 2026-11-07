"""Tests for restore orchestration: payload normalisation, focused-workspace,
and floating-window restore branches."""

import itertools
import unittest
import unittest.mock
from typing import ClassVar

import session_manager.restore as restore_mod
from session_manager.state import RestoreContext
from tests.helpers import (
    RestoreHarnessTestCase,
    make_con,
    make_two_output_tree,
    make_window,
    make_workspace,
)


class TestNormalisePayload(RestoreHarnessTestCase):
    def _norm(self, payload):
        return self.restore_mod._normalise_restore_payload(payload)

    def test_legacy_list_payload(self):
        ws = make_workspace("1")
        workspaces, scratchpad, bg, focused = self._norm([ws])
        self.assertEqual(workspaces, [ws])
        self.assertEqual(scratchpad, [])
        self.assertIsNone(bg)
        self.assertIsNone(focused)

    def test_dict_payload_roundtrip(self):
        ws = make_workspace("1")
        payload = {
            "workspaces": [ws],
            "hidden_scratchpad": [],
            "background_apps": [{"app_id": "vesktop"}],
            "focused_workspace": "3",
        }
        workspaces, scratchpad, bg, focused = self._norm(payload)
        self.assertEqual(workspaces, [ws])
        self.assertEqual(scratchpad, [])
        self.assertEqual(bg, [{"app_id": "vesktop"}])
        self.assertEqual(focused, "3")

    def test_focused_workspace_absent_is_none(self):
        payload = {"workspaces": [make_workspace("1")]}
        _, _, _, focused = self._norm(payload)
        self.assertIsNone(focused)

    def test_invalid_focused_workspace_is_ignored(self):
        for bad in ("", 42, ["1"]):
            payload = {"workspaces": [], "focused_workspace": bad}
            _, _, _, focused = self._norm(payload)
            self.assertIsNone(focused)

    def test_workspace_type_enforced(self):
        payload = {"workspaces": [make_window("kitty")]}
        workspaces, _, _, _ = self._norm(payload)
        self.assertEqual(workspaces, [])

    def test_restore_staging_workspace_is_dropped(self):
        payload = {
            "workspaces": [
                make_workspace("_session_restore", nodes=[make_window("kitty")]),
                make_workspace("1", nodes=[make_window("kitty")]),
            ],
            "focused_workspace": "_session_restore",
        }
        workspaces, _, _, focused = self._norm(payload)
        self.assertEqual([ws["name"] for ws in workspaces], ["1"])
        self.assertIsNone(focused)


class TestRestoreNode(RestoreHarnessTestCase):
    def test_workspace_restores_tiled_and_floating_children(self):
        win = make_window("kitty", cwd="/tmp")
        ws = make_workspace(
            "1", nodes=[win], floating=[make_window("kitty", cwd="/tmp")]
        )
        self.restore_workspaces({"workspaces": [ws]})
        self.assertCommand("workspace 1")
        self.assertCommand("[con_id=1000] focus")
        self.assertCommand("[con_id=1001] floating enable")

    def test_con_with_only_floating_children(self):
        con = make_con(nodes=[], floating=[make_window("kitty", cwd="/tmp")])
        ws = make_workspace("1", nodes=[con])
        ctx = self.restore_workspaces({"workspaces": [ws]})
        self.assertCommand("floating enable")
        self.assertCommand("resize set width 800 px")
        self.assertEqual(len(ctx.claimed_ids), 1)

    def test_empty_con_is_pruned(self):
        con = make_con(nodes=[], floating=[])
        ws = make_workspace("1", nodes=[con])
        ctx = self.restore_workspaces({"workspaces": [ws]})
        self.assertEqual(len(ctx.claimed_ids), 0)
        self.assertEqual([c for c in self.calls if "con_id=" in c], [])

    def test_con_tabbed_uses_split_h(self):
        win_a = make_window("kitty", cwd="/a")
        win_b = make_window("kitty", cwd="/b")
        con = make_con(nodes=[win_a, win_b], layout="tabbed")
        ws = make_workspace("1", nodes=[con])
        self.restore_workspaces({"workspaces": [ws]})
        self.assertIn("split h", self.calls)
        self.assertIn("layout tabbed", self.calls)
        focuses = [c for c in self.calls if c.endswith("] focus")]
        self.assertEqual(len(focuses), 2)

    def test_con_splitv_uses_split_v(self):
        win_a = make_window("kitty", cwd="/a")
        win_b = make_window("kitty", cwd="/b")
        con = make_con(nodes=[win_a, win_b], layout="splitv")
        ws = make_workspace("1", nodes=[con])
        self.restore_workspaces({"workspaces": [ws]})
        self.assertIn("split v", self.calls)
        self.assertIn("layout splitv", self.calls)

    def test_marks_applied_to_restored_window(self):
        win = make_window("kitty", marks=["foo", "bar"])
        ws = make_workspace("1", nodes=[win])
        self.restore_workspaces({"workspaces": [ws]})
        self.assertCommand("mark foo")
        self.assertCommand("mark bar")

    def test_arrangement_reads_the_tree_once_per_child_plus_the_root(self):
        """One tree read answers both focus depth and placement."""
        ws = make_workspace("1", nodes=[make_window("kitty"), make_window("kitty")])
        tree = self._fake_get_tree()
        reads = 0

        def counted_tree():
            nonlocal reads
            reads += 1
            return tree

        ctx = self.restore_mod.RestoreContext()
        self.addCleanup(ctx.registry.close)
        with unittest.mock.patch.object(self.restore_mod, "get_tree", counted_tree):
            self.restore_mod._restore_node(ws, ctx)
        # The workspace root is read once, then each child gets one fresh
        # read. Placement reuses that read instead of asking for another.
        self.assertEqual(reads, 3)


class TestRestoreFloating(RestoreHarnessTestCase):
    def _floating(self, node):
        ws = make_workspace("1", floating=[node])
        return self.restore_workspaces({"workspaces": [ws]})

    def test_kitty_drop_only_geometry(self):
        self._floating(make_window("kitty_drop"))
        self.assertCommand("move position 0 0")
        self.assertCommand("resize set width 800 px")
        joined = " ".join(self.calls)
        self.assertNotIn("floating enable", joined)
        self.assertNotIn("border normal", joined)
        self.assertNotIn("scratchpad", joined)

    def test_scratchpad_window_moved_to_scratchpad(self):
        self._floating(make_window("kitty", scratchpad_state="shown"))
        self.assertCommand("floating enable")
        self.assertCommand("border normal")
        self.assertCommand("move scratchpad")
        self.assertCommand("scratchpad show")

    def test_plain_floating_window(self):
        self._floating(make_window("kitty"))
        self.assertCommand("floating enable")
        self.assertCommand("move position 0 0")
        self.assertNotIn("scratchpad", " ".join(self.calls))

    def test_helium_floating_moved_to_current_workspace(self):
        self._floating(make_window("helium", name="hel tab"))
        self.assertCommand("move to workspace 1")
        self.assertCommand("floating enable")

    def test_fullscreen_floating_window(self):
        self._floating(make_window("kitty", fullscreen_mode=1))
        self.assertCommand("fullscreen enable")

    def test_global_fullscreen_floating_window(self):
        self._floating(make_window("kitty", fullscreen_mode=2))
        self.assertCommand("fullscreen enable global")

    def test_non_fullscreen_floating_window(self):
        self._floating(make_window("kitty", fullscreen_mode=0))
        self.assertCommand("fullscreen disable")

    def test_fullscreen_rerun_asserts_the_same_state(self):
        """A rerun must not toggle an already-fullscreen window."""
        self.restore_mod._apply_fullscreen(1000, 1)
        self.restore_mod._apply_fullscreen(1000, 1)
        self.assertEqual(
            [call for call in self.calls if "fullscreen" in call],
            [
                "[con_id=1000] fullscreen enable",
                "[con_id=1000] fullscreen enable",
            ],
        )

        self.calls.clear()
        self.restore_mod._apply_fullscreen(1000, 0)
        self.restore_mod._apply_fullscreen(1000, 0)
        self.assertEqual(
            [call for call in self.calls if "fullscreen" in call],
            [
                "[con_id=1000] fullscreen disable",
                "[con_id=1000] fullscreen disable",
            ],
        )

    def test_marks_applied_to_kitty_drop_floating(self):
        self._floating(make_window("kitty_drop", marks=["d"]))
        self.assertCommand("mark d")

    def test_marks_applied_to_scratchpad_floating(self):
        self._floating(make_window("kitty", scratchpad_state="shown", marks=["s"]))
        self.assertCommand("mark s")


class TestApplyGeometry(RestoreHarnessTestCase):
    """Saved rects are output-absolute. `move position` is workspace-relative
    and anchors on the titlebar, so _apply_geometry must convert both."""

    def _tree(self, ws_origin=(4, 28), deco_height=0, win_id=1000):
        tree = self._fake_get_tree()
        tree["nodes"].append(
            {
                "type": "output",
                "name": "eDP-1",
                "active": True,
                "rect": {"x": 0, "y": 0, "width": 1920, "height": 1080},
                "nodes": [
                    {
                        "type": "workspace",
                        "name": "1",
                        "rect": {
                            "x": ws_origin[0],
                            "y": ws_origin[1],
                            "width": 1912,
                            "height": 1048,
                        },
                        "nodes": [],
                        "floating_nodes": [
                            {
                                "id": win_id,
                                "deco_rect": {
                                    "x": 0,
                                    "y": 0,
                                    "width": 100,
                                    "height": deco_height,
                                },
                                "nodes": [],
                                "floating_nodes": [],
                            }
                        ],
                    }
                ],
                "floating_nodes": [],
            }
        )
        return tree

    def _apply(self, rect, ws_origin=(4, 28), deco_height=0):
        import unittest.mock

        tree = self._tree(ws_origin, deco_height)
        with unittest.mock.patch.object(self.restore_mod, "get_tree", lambda: tree):
            self.restore_mod._apply_geometry(1000, rect, "1")

    def test_converts_output_to_workspace_coords(self):
        self._apply({"x": 243, "y": 197, "width": 800, "height": 600})
        self.assertCommand("move position 239 169")
        self.assertCommand("resize set width 800 px height 600 px")

    def test_titlebar_added_to_height_and_subtracted_from_y(self):
        self._apply(
            {"x": 243, "y": 197, "width": 1434, "height": 709},
            deco_height=26,
        )
        self.assertCommand("move position 239 143")
        self.assertCommand("resize set width 1434 px height 735 px")

    def test_resize_precedes_move(self):
        self._apply({"x": 243, "y": 197, "width": 800, "height": 600})
        moves = [i for i, c in enumerate(self.calls) if "move position" in c]
        resizes = [i for i, c in enumerate(self.calls) if "resize set" in c]
        self.assertEqual(len(moves), 1)
        self.assertEqual(len(resizes), 1)
        self.assertLess(resizes[0], moves[0])

    def test_missing_workspace_falls_back_to_saved_coords(self):
        import unittest.mock

        with unittest.mock.patch.object(
            self.restore_mod, "get_tree", self._fake_get_tree
        ):
            self.restore_mod._apply_geometry(
                1000, {"x": 10, "y": 20, "width": 800, "height": 600}, "1"
            )
        self.assertCommand("move position 10 20")


class TestHeliumScratchpad(RestoreHarnessTestCase):
    def test_helium_in_hidden_scratchpad_is_restored(self):
        hel = make_window("helium", name="scratch hel tab")
        payload = {
            "workspaces": [],
            "hidden_scratchpad": [hel],
            "background_apps": [],
        }
        ctx = self.restore_workspaces(payload)
        self.assertEqual(len(ctx.helium_saved_nodes), 1)
        self.assertIsNotNone(ctx.helium_restored_ids[0])
        self.assertCommand("floating enable")
        self.assertCommand("move scratchpad")

    def test_helium_scratchpad_plus_workspace_collected_once(self):
        hel_ws = make_window("helium", name="ws hel tab")
        hel_sp = make_window("helium", name="sp hel tab")
        ws = make_workspace("1", nodes=[hel_ws])
        payload = {
            "workspaces": [ws],
            "hidden_scratchpad": [hel_sp],
            "background_apps": [],
        }
        ctx = self.restore_workspaces(payload)
        self.assertEqual(len(ctx.helium_saved_nodes), 2)
        self.assertEqual(len(ctx.helium_restored_ids), 2)

    def test_non_helium_scratchpad_restored_plain(self):
        win = make_window("kitty", cwd="/tmp")
        payload = {"workspaces": [], "hidden_scratchpad": [win], "background_apps": []}
        self.restore_workspaces(payload)
        self.assertCommand("floating enable")
        self.assertCommand("border normal")
        self.assertCommand("move scratchpad")


class TestFocusedWorkspace(RestoreHarnessTestCase):
    def _run_locked(self, payload, focused=None, extra_patches=()):
        """Runs _restore_session_locked against a temp state file."""
        import json
        import tempfile
        import unittest.mock
        from contextlib import nullcontext
        from pathlib import Path

        if focused is not None:
            payload["focused_workspace"] = focused
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "sway_session.json"
            state_file.write_text(json.dumps(payload), encoding="utf-8")
            extra = [
                unittest.mock.patch.object(self.restore_mod, "STATE_FILE", state_file),
                unittest.mock.patch.object(
                    self.restore_mod, "operation_lock", lambda _dir: nullcontext()
                ),
                unittest.mock.patch.object(self.restore_mod, "restore_background_apps"),
                unittest.mock.patch.object(self.restore_mod, "close_connection"),
                unittest.mock.patch.object(self.restore_mod, "ProcCache"),
                # The browser would launch here; the ordering test only
                # needs the workspace walk.
                unittest.mock.patch.object(
                    self.restore_mod, "_start_helium_materialization"
                ),
                *extra_patches,
            ]
            for p in extra:
                p.start()
            try:
                self.restore_mod._restore_session_locked(notify_user=False)
            finally:
                for p in extra:
                    p.stop()

    def test_restore_ends_on_focused_workspace(self):
        ws1 = make_workspace("1", nodes=[make_window("kitty", cwd="/a")])
        ws3 = make_workspace("3", nodes=[make_window("kitty", cwd="/b")])
        payload = {
            "workspaces": [ws1, ws3],
            "hidden_scratchpad": [],
            "background_apps": [],
        }
        self._run_locked(payload, focused="3")
        switches = [c for c in self.calls if c.startswith("workspace ")]
        self.assertEqual(switches[-1], "workspace 3")

    def test_restore_falls_back_to_workspace_1(self):
        ws2 = make_workspace("2", nodes=[make_window("kitty", cwd="/a")])
        payload = {"workspaces": [ws2], "hidden_scratchpad": [], "background_apps": []}
        self._run_locked(payload, focused=None)
        switches = [c for c in self.calls if c.startswith("workspace ")]
        self.assertEqual(switches[-1], "workspace 1")

    def test_workspaces_restore_in_saved_order(self):
        hel = make_window("helium", name="tab a")
        ws1 = make_workspace("1", nodes=[hel])
        ws2 = make_workspace("2", nodes=[make_window("kitty", cwd="/b")])
        payload = {
            "workspaces": [ws1, ws2],
            "hidden_scratchpad": [],
            "background_apps": [],
        }
        self._run_locked(payload, focused="1")
        switches = [
            c
            for c in self.calls
            if c.startswith("workspace ") and "_session_restore" not in c
        ]
        self.assertLess(switches.index("workspace 1"), switches.index("workspace 2"))

    def test_geometry_is_verified_before_the_final_staging_sweep(self):
        """A window that maps during the settle pass must still be recoverable."""
        events: list[str] = []
        payload = {
            "workspaces": [make_workspace("1", nodes=[make_window("kitty")])],
            "hidden_scratchpad": [],
            "background_apps": [],
        }
        self._run_locked(
            payload,
            focused="1",
            extra_patches=[
                unittest.mock.patch.object(
                    self.restore_mod,
                    "_verify_floating_geometry",
                    side_effect=lambda *_a, **_kw: events.append("verify"),
                ),
                unittest.mock.patch.object(
                    self.restore_mod,
                    "_finish_staging",
                    side_effect=lambda *_a, **_kw: events.append("finish"),
                ),
            ],
        )
        self.assertEqual(events, ["verify", "finish"])

    def test_a_geometry_failure_does_not_leave_focus_on_staging(self):
        payload = {
            "workspaces": [make_workspace("1", nodes=[make_window("kitty")])],
            "hidden_scratchpad": [],
            "background_apps": [],
        }
        with (
            unittest.mock.patch.object(
                self.restore_mod,
                "_verify_floating_geometry",
                side_effect=RuntimeError("geometry failed"),
            ),
            self.assertRaisesRegex(RuntimeError, "geometry failed"),
        ):
            self._run_locked(payload, focused="1")
        switches = [c for c in self.calls if c.startswith("workspace ")]
        self.assertEqual(switches[-1], "workspace 1")

    def test_invalid_session_file_skips_restore(self):
        import tempfile
        import unittest.mock
        from contextlib import nullcontext
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "sway_session.json"
            state_file.write_text("{ not valid json", encoding="utf-8")
            extra = [
                unittest.mock.patch.object(self.restore_mod, "STATE_FILE", state_file),
                unittest.mock.patch.object(
                    self.restore_mod, "operation_lock", lambda _dir: nullcontext()
                ),
                unittest.mock.patch.object(self.restore_mod, "close_connection"),
            ]
            for p in extra:
                p.start()
            try:
                self.restore_mod._restore_session_locked(notify_user=False)
            finally:
                for p in extra:
                    p.stop()
        self.assertEqual(self.calls, [])

    def test_a_session_file_that_is_not_utf8_skips_restore(self):
        import tempfile
        import unittest.mock
        from contextlib import nullcontext
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "sway_session.json"
            state_file.write_bytes(b'{"workspaces": [\xff]}')
            extra = [
                unittest.mock.patch.object(self.restore_mod, "STATE_FILE", state_file),
                unittest.mock.patch.object(
                    self.restore_mod, "operation_lock", lambda _dir: nullcontext()
                ),
                unittest.mock.patch.object(self.restore_mod, "close_connection"),
            ]
            for p in extra:
                p.start()
            try:
                self.restore_mod._restore_session_locked(notify_user=False)
            finally:
                for p in extra:
                    p.stop()
        self.assertEqual(self.calls, [])


class TestWorkspaceFilter(RestoreHarnessTestCase):
    """A filtered restore covers one workspace and nothing else."""

    def _run_locked(self, payload, workspace_filter):
        """Runs _restore_session_locked with a workspace filter."""
        import json
        import tempfile
        import unittest.mock
        from contextlib import nullcontext
        from pathlib import Path

        background_calls = []
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "sway_session.json"
            state_file.write_text(json.dumps(payload), encoding="utf-8")
            patches = [
                unittest.mock.patch.object(self.restore_mod, "STATE_FILE", state_file),
                unittest.mock.patch.object(
                    self.restore_mod, "operation_lock", lambda _dir: nullcontext()
                ),
                unittest.mock.patch.object(self.restore_mod, "close_connection"),
                unittest.mock.patch.object(self.restore_mod, "ProcCache"),
                unittest.mock.patch.object(
                    self.restore_mod,
                    "restore_background_apps",
                    lambda apps=None, **_kw: background_calls.append(apps),
                ),
            ]
            for p in patches:
                p.start()
            try:
                self.restore_mod._restore_session_locked(
                    notify_user=False, workspace_filter=workspace_filter
                )
            finally:
                for p in patches:
                    p.stop()
        return background_calls

    def _payload(self):
        ws1 = make_workspace("1", nodes=[make_window("kitty", cwd="/a")])
        ws3 = make_workspace("3", nodes=[make_window("kitty", cwd="/b")])
        return {
            "workspaces": [ws1, ws3],
            "hidden_scratchpad": [make_window("kitty", cwd="/c")],
            "background_apps": [{"app_id": "vesktop"}],
            "focused_workspace": "1",
        }

    def test_background_apps_are_not_restored(self):
        """A filtered restore leaves the background apps alone."""
        calls = self._run_locked(self._payload(), "3")
        self.assertEqual(calls, [[]])

    def test_only_the_filtered_workspace_is_arranged(self):
        self._run_locked(self._payload(), "3")
        switches = [c for c in self.calls if c.startswith("workspace ")]
        self.assertIn("workspace 3", switches)
        self.assertNotIn("workspace 1", switches)

    def test_scratchpad_is_not_restored(self):
        self._run_locked(self._payload(), "3")
        self.assertFalse(any("scratchpad" in c for c in self.calls))

    def test_restore_finishes_on_the_filtered_workspace(self):
        """The saved focus may name another workspace; the filter wins."""
        self._run_locked(self._payload(), "3")
        switches = [c for c in self.calls if c.startswith("workspace ")]
        self.assertEqual(switches[-1], "workspace 3")

    def test_unknown_workspace_restores_nothing(self):
        calls = self._run_locked(self._payload(), "9")
        self.assertEqual(calls, [])
        self.assertEqual(self.calls, [])


class TestRestoreReporting(RestoreHarnessTestCase):
    """The restore summary reports both kinds of loss."""

    def _run_locked(self, payload):
        """Runs _restore_session_locked and returns the notifications."""
        import json
        import tempfile
        import unittest.mock
        from contextlib import nullcontext
        from pathlib import Path

        notifications: list[str] = []

        def fake_launch(node, claimed_ids):
            if node.get("app_id") == "broken-app":
                raise OSError("cannot start")
            if node.get("app_id") == "gone-app":
                return None
            return next(self._id_iter)

        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "sway_session.json"
            state_file.write_text(json.dumps(payload), encoding="utf-8")
            patches = [
                unittest.mock.patch.object(self.restore_mod, "STATE_FILE", state_file),
                unittest.mock.patch.object(
                    self.restore_mod, "operation_lock", lambda _dir: nullcontext()
                ),
                unittest.mock.patch.object(self.restore_mod, "restore_background_apps"),
                unittest.mock.patch.object(self.restore_mod, "close_connection"),
                unittest.mock.patch.object(self.restore_mod, "ProcCache"),
                unittest.mock.patch.object(
                    self.restore_mod, "launch_and_get_id", fake_launch
                ),
                unittest.mock.patch.object(
                    self.restore_mod,
                    "notify",
                    lambda msg, **_kw: notifications.append(msg),
                ),
            ]
            for p in patches:
                p.start()
            try:
                self.restore_mod._restore_session_locked(notify_user=True)
            finally:
                for p in patches:
                    p.stop()
        return notifications

    def _payload(self):
        ws = make_workspace(
            "1",
            nodes=[
                make_window("gone-app", name="gone window"),
                make_window("broken-app", name="broken window"),
            ],
        )
        return {"workspaces": [ws], "hidden_scratchpad": [], "background_apps": []}

    def test_missing_windows_are_reported_next_to_partial_failures(self):
        """A partial failure must not hide the windows that never came back."""
        notifications = self._run_locked(self._payload())
        summary = notifications[-1]
        self.assertIn("Partial failures: 1", summary)
        self.assertIn("2 window(s) did not come back", summary)

    def test_a_clean_restore_reports_nothing(self):
        payload = {
            "workspaces": [make_workspace("1")],
            "hidden_scratchpad": [],
            "background_apps": [],
        }
        notifications = self._run_locked(payload)
        self.assertIn("you can safely move now", notifications[-1])


class TestRestoreContext(unittest.TestCase):
    def test_a_missing_window_is_only_reported_once(self):
        ctx = RestoreContext()
        self.addCleanup(ctx.registry.close)
        ctx.add_missing("alpha")
        ctx.add_missing("alpha")
        self.assertEqual(ctx.missing, ["alpha"])

    def test_a_repeated_failure_is_only_reported_once(self):
        """One root cause must not be counted once per attempt."""
        ctx = RestoreContext()
        self.addCleanup(ctx.registry.close)
        ctx.add_failure("could not focus staging workspace")
        ctx.add_failure("could not focus staging workspace")
        self.assertEqual(ctx.failures, ["could not focus staging workspace"])


class TestBackgroundRestoreOrder(RestoreHarnessTestCase):
    def test_success_notification_precedes_background_restore(self):
        """The user is told the session is ready before daemon checks run."""
        import json
        import tempfile
        import unittest.mock
        from contextlib import nullcontext
        from pathlib import Path

        events: list[tuple[str, str]] = []
        ws = make_workspace("1", nodes=[make_window("kitty", cwd="/a")])
        payload = {
            "workspaces": [ws],
            "hidden_scratchpad": [],
            "background_apps": [],
        }
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "sway_session.json"
            state_file.write_text(json.dumps(payload), encoding="utf-8")
            patches = [
                unittest.mock.patch.object(self.restore_mod, "STATE_FILE", state_file),
                unittest.mock.patch.object(
                    self.restore_mod, "operation_lock", lambda _dir: nullcontext()
                ),
                unittest.mock.patch.object(self.restore_mod, "close_connection"),
                unittest.mock.patch.object(self.restore_mod, "ProcCache"),
                unittest.mock.patch.object(
                    self.restore_mod, "_start_helium_materialization"
                ),
                unittest.mock.patch.object(
                    self.restore_mod,
                    "notify",
                    lambda msg, **_kw: events.append(("notify", msg)),
                ),
                unittest.mock.patch.object(
                    self.restore_mod,
                    "restore_background_apps",
                    lambda *_a, **_kw: events.append(("background", "")),
                ),
            ]
            for p in patches:
                p.start()
            try:
                self.restore_mod._restore_session_locked(notify_user=True)
            finally:
                for p in patches:
                    p.stop()

        kinds = [kind for kind, _ in events]
        self.assertIn("notify", kinds)
        self.assertIn("background", kinds)
        success = next(
            index
            for index, (kind, msg) in enumerate(events)
            if kind == "notify" and "session restored successfully" in msg
        )
        background = next(
            index for index, (kind, _msg) in enumerate(events) if kind == "background"
        )
        self.assertLess(success, background)


class TestHasRestorableContent(RestoreHarnessTestCase):
    def _check(self, content):
        """Point STATE_FILE at a temp file with the given text content."""
        import json
        import tempfile
        import unittest.mock
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "sway_session.json"
            if content is not None:
                if isinstance(content, (dict, list)):
                    content = json.dumps(content)
                state_file.write_text(content, encoding="utf-8")
            with unittest.mock.patch.object(self.restore_mod, "STATE_FILE", state_file):
                return self.restore_mod.has_restorable_content()

    def test_missing_file_is_empty(self):
        self.assertFalse(self._check(None))

    def test_all_empty_lists_is_empty(self):
        payload = {"workspaces": [], "hidden_scratchpad": [], "background_apps": []}
        self.assertFalse(self._check(payload))

    def test_legacy_empty_list_is_empty(self):
        self.assertFalse(self._check([]))

    def test_workspace_counts_as_content(self):
        ws = make_workspace("1", nodes=[make_window("kitty")])
        self.assertTrue(self._check({"workspaces": [ws]}))

    def test_scratchpad_window_alone_counts_as_content(self):
        win = make_window("kitty")
        payload = {"workspaces": [], "hidden_scratchpad": [win]}
        self.assertTrue(self._check(payload))

    def test_drop_terminal_alone_is_empty(self):
        drop = make_window("kitty_drop")
        payload = {"workspaces": [], "hidden_scratchpad": [drop]}
        self.assertFalse(self._check(payload))

    def test_drop_terminal_alongside_real_scratchpad_counts(self):
        drop = make_window("kitty_drop")
        win = make_window("kitty")
        payload = {"workspaces": [], "hidden_scratchpad": [drop, win]}
        self.assertTrue(self._check(payload))

    def test_background_app_alone_counts_as_content(self):
        payload = {
            "workspaces": [],
            "hidden_scratchpad": [],
            "background_apps": [{"app_id": "vesktop"}],
        }
        self.assertTrue(self._check(payload))

    def test_unreadable_json_is_not_restorable(self):
        self.assertFalse(self._check("{ not valid json"))

    def test_a_file_that_is_not_utf8_is_not_restorable(self):
        """A damaged state file must not raise out of the check."""
        import tempfile
        import unittest.mock
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "sway_session.json"
            state_file.write_bytes(b'{"workspaces": [\xff]}')
            with unittest.mock.patch.object(self.restore_mod, "STATE_FILE", state_file):
                self.assertFalse(self.restore_mod.has_restorable_content())

    def test_structurally_invalid_payload_is_not_restorable(self):
        self.assertFalse(self._check("not a payload"))


class TestPayloadValidation(RestoreHarnessTestCase):
    """A malformed payload must be refused before anything is launched."""

    def _norm(self, payload):
        return self.restore_mod._normalise_restore_payload(payload)

    def test_null_rect_member_is_dropped(self):
        win = make_window(
            "kitty", rect={"x": None, "y": 0, "width": 800, "height": 600}
        )
        ws = make_workspace("1", nodes=[win])
        workspaces, _, _, _ = self._norm({"workspaces": [ws]})
        self.assertNotIn("x", workspaces[0]["nodes"][0]["rect"])


class TestCommandSafety(RestoreHarnessTestCase):
    """Saved values must never become extra compositor commands."""

    def test_unsafe_mark_is_skipped(self):
        win = make_window("kitty", marks=["ok", "a; exec id"])
        ws = make_workspace("1", nodes=[win])
        self.restore_workspaces({"workspaces": [ws]})
        self.assertCommand("mark ok")
        self.assertNotIn("exec id", " ".join(self.calls))

    def test_unknown_layout_is_skipped(self):
        con = make_con(nodes=[make_window("kitty")], layout="totally-bogus")
        ws = make_workspace("1", nodes=[con])
        self.restore_workspaces({"workspaces": [ws]})
        self.assertNotIn("totally-bogus", " ".join(self.calls))

    def test_stacked_layout_is_sent_as_stacking(self):
        con = make_con(nodes=[make_window("kitty")], layout="stacked")
        ws = make_workspace("1", nodes=[con])
        self.restore_workspaces({"workspaces": [ws]})
        self.assertCommand("layout stacking")

    def test_geometry_uses_plain_numbers(self):
        # The payload check refuses this rect; the geometry code must also
        # survive it when it is called directly.
        self.restore_mod._apply_geometry(
            1000,
            {"x": "10; exec id", "y": 0, "width": 800, "height": 600},
            "1",
        )
        joined = " ".join(self.calls)
        self.assertNotIn("exec id", joined)
        self.assertCommand("move position 0 0")
        self.assertCommand("resize set width 800 px height 600 px")

    def test_workspace_name_is_quoted_when_it_has_separators(self):
        ws = make_workspace("2 dev; exec id")
        win = make_window("helium")
        ws["nodes"].append(win)
        self.restore_workspaces({"workspaces": [ws]})
        joined = " ".join(self.calls)
        self.assertIn('workspace "2 dev; exec id"', joined)
        self.assertIn('move to workspace "2 dev; exec id"', joined)


class TestScratchpadBorder(RestoreHarnessTestCase):
    def test_floating_scratchpad_keeps_its_border(self):
        win = make_window("kitty", scratchpad_state="shown", border="pixel")
        ws = make_workspace("1", floating=[win])
        self.restore_workspaces({"workspaces": [ws]})
        self.assertCommand("border pixel")
        self.assertNotIn("border normal", " ".join(self.calls))

    def test_scratchpad_without_a_border_falls_back_to_normal(self):
        win = make_window("kitty", scratchpad_state="shown")
        ws = make_workspace("1", floating=[win])
        self.restore_workspaces({"workspaces": [ws]})
        self.assertCommand("border normal")


class TestFailureIsolation(RestoreHarnessTestCase):
    """One app that cannot start must not cost the other windows."""

    def _flaky_launch(self):
        state = {"calls": 0}

        def launch(node, claimed_ids):
            state["calls"] += 1
            if state["calls"] == 1:
                raise FileNotFoundError("mpv: command not found")
            win_id = next(self._id_iter)
            claimed_ids.add(win_id)
            return win_id

        return launch

    def test_failing_tiled_window_does_not_stop_the_others(self):
        win_a = make_window("mpv")
        win_b = make_window("kitty")
        ws = make_workspace("1", nodes=[win_a, win_b])
        with unittest.mock.patch.object(
            self.restore_mod, "launch_and_get_id", self._flaky_launch()
        ):
            ctx = self.restore_workspaces({"workspaces": [ws]})
        self.assertEqual(len(ctx.failures), 1)
        self.assertIn("mpv", ctx.failures[0])
        self.assertEqual(len(ctx.claimed_ids), 1)
        self.assertCommand("[con_id=1000] focus")

    def test_failing_floating_window_does_not_stop_the_others(self):
        float_a = make_window("mpv")
        float_b = make_window("kitty")
        ws = make_workspace("1", floating=[float_a, float_b])
        with unittest.mock.patch.object(
            self.restore_mod, "launch_and_get_id", self._flaky_launch()
        ):
            ctx = self.restore_workspaces({"workspaces": [ws]})
        self.assertEqual(len(ctx.failures), 1)
        self.assertEqual(len(ctx.claimed_ids), 1)

    def test_window_that_does_not_come_back_is_reported(self):
        def no_launch(node, claimed_ids):
            return None

        win = make_window("definitely-not-installed")
        ws = make_workspace("1", nodes=[win])
        with unittest.mock.patch.object(
            self.restore_mod, "launch_and_get_id", no_launch
        ):
            ctx = self.restore_workspaces({"workspaces": [ws]})
        self.assertEqual(ctx.missing, ["definitely-not-installed window"])

    def test_drop_terminal_is_not_reported_as_missing(self):
        """The dropdown terminal is spawned by its keybinding on demand."""

        def no_launch(node, claimed_ids):
            return None

        win = make_window("kitty_drop")
        ws = make_workspace("1", nodes=[win])
        with unittest.mock.patch.object(
            self.restore_mod, "launch_and_get_id", no_launch
        ):
            ctx = self.restore_workspaces({"workspaces": [ws]})
        self.assertEqual(ctx.missing, [])


class TestDefaultGeometry(RestoreHarnessTestCase):
    def test_missing_rect_uses_the_default_size(self):
        win = make_window("kitty", rect={})
        ws = make_workspace("1", floating=[win])
        self.restore_workspaces({"workspaces": [ws]})
        self.assertCommand("width 800 px")
        self.assertCommand("height 600 px")


class TestMultiOutputClamp(RestoreHarnessTestCase):
    """A floating window is clamped against the output it was saved on."""

    def _apply(self, rect):
        with unittest.mock.patch.object(
            self.restore_mod, "get_tree", make_two_output_tree
        ):
            self.restore_mod._apply_geometry(1000, rect, "1")

    def test_rect_on_the_second_output_is_not_pulled_to_the_first(self):
        self._apply({"x": 2000, "y": 100, "width": 800, "height": 600})
        self.assertCommand("move position 2000 100")

    def test_rect_beyond_every_output_is_clamped_inwards(self):
        self._apply({"x": 5000, "y": 100, "width": 800, "height": 600})
        self.assertCommand("move position 1120 100")


class TestWorkspacePlacement(RestoreHarnessTestCase):
    """A window's workspace is asserted, not inherited from focus."""

    def _tree_with(self, win_id, workspace):
        """A live tree with one window on the given workspace."""
        win = {
            "type": "con",
            "id": win_id,
            "app_id": "kitty",
            "name": "kitty window",
            "pid": 12345,
            "nodes": [],
            "floating_nodes": [],
        }
        ws = make_workspace(workspace, nodes=[win])
        return {
            "nodes": [
                {
                    "type": "output",
                    "name": "HEADLESS-1",
                    "active": True,
                    "rect": {"x": 0, "y": 0, "width": 1920, "height": 1080},
                    "nodes": [ws],
                    "floating_nodes": [],
                }
            ],
            "floating_nodes": [],
        }

    def test_window_on_the_wrong_workspace_is_moved_back(self):
        ws = make_workspace("1", nodes=[make_window("kitty", cwd="/a")])
        with unittest.mock.patch.object(
            self.restore_mod, "get_tree", lambda: self._tree_with(1000, "9")
        ):
            self.restore_workspaces({"workspaces": [ws]})
        self.assertCommand("[con_id=1000] move to workspace 1")

    def test_window_already_on_its_workspace_is_not_moved(self):
        ws = make_workspace("1", nodes=[make_window("kitty", cwd="/a")])
        with unittest.mock.patch.object(
            self.restore_mod, "get_tree", lambda: self._tree_with(1000, "1")
        ):
            self.restore_workspaces({"workspaces": [ws]})
        self.assertNotIn("move to workspace 1", " ".join(self.calls))

    def test_floating_window_on_the_wrong_workspace_is_moved_back(self):
        ws = make_workspace("1", floating=[make_window("kitty", cwd="/a")])
        with unittest.mock.patch.object(
            self.restore_mod, "get_tree", lambda: self._tree_with(1000, "9")
        ):
            self.restore_workspaces({"workspaces": [ws]})
        self.assertCommand("[con_id=1000] move to workspace 1")

    def test_window_workspace_name_walks_the_tree(self):
        win = {
            "type": "con",
            "id": 4242,
            "app_id": "kitty",
            "nodes": [],
            "floating_nodes": [],
        }
        ws = make_workspace("1", nodes=[win])
        tree = {
            "nodes": [
                {
                    "type": "output",
                    "name": "HEADLESS-1",
                    "active": True,
                    "rect": {"x": 0, "y": 0, "width": 1920, "height": 1080},
                    "nodes": [ws],
                    "floating_nodes": [],
                }
            ],
            "floating_nodes": [],
        }
        with unittest.mock.patch.object(self.restore_mod, "get_tree", lambda: tree):
            self.assertEqual(self.restore_mod._window_workspace_name(4242), "1")


class TestLiveWindowPlacements(unittest.TestCase):
    """The reconcile pass reads each live window's workspace from the tree."""

    def test_windows_map_to_the_workspace_that_holds_them(self):
        tree = {
            "nodes": [
                {
                    "type": "output",
                    "name": "HEADLESS-1",
                    "active": True,
                    "nodes": [
                        {
                            "type": "workspace",
                            "name": "1",
                            "nodes": [
                                {
                                    "type": "con",
                                    "id": 20,
                                    "nodes": [
                                        {"type": "con", "id": 11, "app_id": "kitty"}
                                    ],
                                    "floating_nodes": [],
                                }
                            ],
                            "floating_nodes": [
                                {"type": "floating_con", "id": 12, "app_id": "kitty"}
                            ],
                        },
                        {
                            "type": "workspace",
                            "name": "__i3_scratch",
                            "nodes": [{"type": "con", "id": 13, "app_id": "mpv"}],
                            "floating_nodes": [],
                        },
                    ],
                    "floating_nodes": [],
                }
            ],
            "floating_nodes": [],
        }
        from session_manager.restore import _live_window_placements

        self.assertEqual(
            _live_window_placements(tree),
            {11: "1", 12: "1", 13: "__i3_scratch"},
        )


class TestReconcilePlacements(RestoreHarnessTestCase):
    """Placement drift after arrangement is repaired by con id."""

    def _ctx(self, *adopted):
        ctx = self.restore_mod.RestoreContext()
        self.addCleanup(ctx.registry.close)
        for node, win_id in adopted:
            ctx.registry.adopt(node, win_id)
        return ctx

    def _placements_tree(self, placements):
        """Build a live tree whose window ids sit on the named workspaces."""
        by_workspace: dict[str, list[int]] = {}
        for win_id, name in placements.items():
            by_workspace.setdefault(name or "unnamed", []).append(win_id)
        return {
            "nodes": [
                {
                    "type": "output",
                    "name": "HEADLESS-1",
                    "active": True,
                    "nodes": [
                        {
                            "type": "workspace",
                            "name": name,
                            "nodes": [
                                {"type": "con", "id": win_id, "app_id": "kitty"}
                                for win_id in sorted(ids)
                            ],
                            "floating_nodes": [],
                        }
                        for name, ids in by_workspace.items()
                    ],
                    "floating_nodes": [],
                }
            ],
            "floating_nodes": [],
        }

    def _reconcile(self, ctx, before, after, workspaces=(), hidden=()):
        """Reconcile with `before` as the first tree and `after` as the last.

        The windows in these tests are adopted and not launched. Thus the
        rebind pass records no pid for them and reads no tree.

        Args:
            ctx: The restore context to reconcile through.
            before: The placements that the first tree read shows.
            after: The placements that the last tree read shows.
            workspaces: The saved workspaces of this restore.
            hidden: The saved scratchpad entries of this restore.
        """
        with unittest.mock.patch.object(
            self.restore_mod,
            "get_tree",
            side_effect=[
                self._placements_tree(before),
                self._placements_tree(after),
            ],
        ):
            self.restore_mod._reconcile_placements(list(workspaces), list(hidden), ctx)

    def test_window_on_the_wrong_workspace_is_moved_back(self):
        win = make_window("kitty", name="alpha")
        ws = make_workspace("1", nodes=[win])
        ctx = self._ctx((win, 501))
        with unittest.mock.patch.object(
            self.restore_mod, "_window_workspace_name", lambda _win_id: "9"
        ):
            self._reconcile(ctx, {501: "9"}, {501: "1"}, workspaces=[ws])
        self.assertCommand("[con_id=501] move to workspace 1")
        self.assertEqual(ctx.failures, [])

    def test_a_window_that_did_not_move_is_reported(self):
        win = make_window("kitty", name="alpha")
        ws = make_workspace("1", nodes=[win])
        ctx = self._ctx((win, 502))
        with unittest.mock.patch.object(
            self.restore_mod, "_window_workspace_name", lambda _win_id: "9"
        ):
            self._reconcile(ctx, {502: "9"}, {502: "9"}, workspaces=[ws])
        self.assertTrue(
            any("still on workspace" in failure for failure in ctx.failures),
            ctx.failures,
        )

    def test_window_already_placed_is_left_alone(self):
        win = make_window("kitty", name="alpha")
        ws = make_workspace("1", nodes=[win])
        ctx = self._ctx((win, 503))
        self._reconcile(ctx, {503: "1"}, {503: "1"}, workspaces=[ws])
        self.assertEqual(self.calls, [])
        self.assertEqual(ctx.failures, [])

    def test_hidden_window_left_visible_is_parked_again(self):
        from session_manager.config import SCRATCHPAD_WORKSPACE

        win = make_window("kitty", name="alpha")
        ctx = self._ctx((win, 601))
        names = iter(["1", SCRATCHPAD_WORKSPACE])
        with unittest.mock.patch.object(
            self.restore_mod, "_window_workspace_name", lambda _win_id: next(names)
        ):
            self._reconcile(ctx, {601: "1"}, {601: "1"}, hidden=[win])
        self.assertCommand("[con_id=601] move scratchpad")
        self.assertNotIn("scratchpad show", " ".join(self.calls))
        self.assertEqual(ctx.failures, [])

    def test_hidden_window_already_parked_is_left_alone(self):
        from session_manager.config import SCRATCHPAD_WORKSPACE

        win = make_window("kitty", name="alpha")
        ctx = self._ctx((win, 602))
        self._reconcile(ctx, {602: SCRATCHPAD_WORKSPACE}, {}, hidden=[win])
        self.assertEqual(self.calls, [])

    def test_vanished_window_is_reported_missing(self):
        win = make_window("kitty", name="alpha")
        ws = make_workspace("1", nodes=[win])
        ctx = self._ctx((win, 701))
        self._reconcile(ctx, {}, {}, workspaces=[ws])
        self.assertEqual(ctx.missing, ["alpha"])

    def _pid_tree(self, win_id, pid, app_id, workspace):
        """Build a live tree that holds one window which a pid owns.

        Args:
            win_id: The id of the window.
            pid: The pid of the process that owns the window.
            app_id: The app identity of the window.
            workspace: The name of the workspace that holds the window.
        """
        return {
            "nodes": [
                {
                    "type": "output",
                    "name": "HEADLESS-1",
                    "active": True,
                    "nodes": [
                        {
                            "type": "workspace",
                            "name": workspace,
                            "nodes": [
                                {
                                    "type": "con",
                                    "id": win_id,
                                    "app_id": app_id,
                                    "name": "calibre",
                                    "pid": pid,
                                    "nodes": [],
                                    "floating_nodes": [],
                                }
                            ],
                            "floating_nodes": [],
                        }
                    ],
                    "floating_nodes": [],
                }
            ],
            "floating_nodes": [],
        }

    def _reconcile_replacement(self, ctx, trees, workspaces):
        """Reconcile through a fixed sequence of live trees.

        Args:
            ctx: The restore context to reconcile through.
            trees: The trees that the successive reads return.
            workspaces: The saved workspaces of this restore.
        """
        with (
            unittest.mock.patch.object(
                self.restore_mod, "_pid_is_alive", return_value=True
            ),
            unittest.mock.patch.object(self.restore_mod, "get_tree", side_effect=trees),
        ):
            self.restore_mod._reconcile_placements(list(workspaces), [], ctx)

    def test_a_replaced_window_is_bound_and_moved_back(self):
        """The replacement window is put on the saved workspace.

        The walk placed the placeholder window. The placeholder died after
        the placement. The pass binds the window that is in the tree and
        moves that window back. The saved window is not reported as missing
        while its replacement sits on a different workspace.
        """
        win = make_window("calibre-gui", name="calibre")
        ws = make_workspace("7", nodes=[win])
        ctx = self._ctx((win, 501))
        ctx.registry.record(win, 501, pid=777)
        self._reconcile_replacement(
            ctx,
            [
                # The pid owns no window yet: the replacement is not mapped.
                self._pid_tree(901, 888, "calibre-gui", "9"),
                # The replacement maps while the pass waits for it.
                self._pid_tree(502, 777, "calibre-gui", "1"),
                # The pass reads the placements, then verifies the move.
                self._pid_tree(502, 777, "calibre-gui", "1"),
                self._pid_tree(502, 777, "calibre-gui", "7"),
            ],
            [ws],
        )
        self.assertEqual(ctx.registry.resolve(win), 502)
        self.assertEqual(ctx.registry.pid(win), 777)
        self.assertCommand("[con_id=502] move to workspace 7")
        self.assertEqual(ctx.missing, [])
        self.assertEqual(ctx.failures, [])

    def test_a_replacement_that_maps_during_the_wait_is_awaited(self):
        """The application maps the replacement after the placeholder dies."""
        win = make_window("calibre-gui", name="calibre")
        ws = make_workspace("7", nodes=[win])
        ctx = self._ctx((win, 501))
        ctx.registry.record(win, 501, pid=777)
        self._reconcile_replacement(
            ctx,
            [
                # The bound container is gone and the pid owns no window.
                self._placements_tree({}),
                # The replacement maps while the pass waits for it.
                self._pid_tree(503, 777, "calibre-gui", "1"),
                # The pass reads the placements, then verifies the move.
                self._pid_tree(503, 777, "calibre-gui", "1"),
                self._pid_tree(503, 777, "calibre-gui", "7"),
            ],
            [ws],
        )
        self.assertEqual(ctx.registry.resolve(win), 503)
        self.assertCommand("[con_id=503] move to workspace 7")
        self.assertEqual(ctx.missing, [])
        self.assertEqual(ctx.failures, [])

    def test_no_replacement_is_awaited_once_the_process_is_gone(self):
        """An exited process does not map a replacement window."""
        win = make_window("calibre-gui", name="calibre")
        ws = make_workspace("7", nodes=[win])
        ctx = self._ctx((win, 501))
        ctx.registry.record(win, 501, pid=777)
        with unittest.mock.patch.object(
            self.restore_mod, "get_tree", return_value=self._placements_tree({})
        ):
            self.restore_mod._reconcile_placements([ws], [], ctx)
        self.assertEqual(ctx.registry.resolve(win), 501)
        self.assertEqual(ctx.missing, ["calibre"])

    def test_a_replacement_another_window_owns_is_not_stolen(self):
        win = make_window("calibre-gui", name="calibre")
        ws = make_workspace("7", nodes=[win])
        ctx = self._ctx((win, 501))
        ctx.registry.record(win, 501, pid=777)
        other = make_window("calibre-gui", name="other")
        ctx.registry.adopt(other, 902)
        ctx.claimed_ids.add(902)
        self._reconcile_replacement(
            ctx,
            [
                self._pid_tree(902, 777, "calibre-gui", "7"),
                self._pid_tree(902, 777, "calibre-gui", "7"),
                self._pid_tree(902, 777, "calibre-gui", "7"),
            ],
            [ws],
        )
        self.assertEqual(ctx.registry.resolve(win), 501)
        self.assertEqual(ctx.registry.resolve(other), 902)
        self.assertEqual(ctx.missing, ["calibre"])

    def test_unmaterialized_node_is_skipped(self):
        win = make_window("kitty", name="alpha")
        ws = make_workspace("1", nodes=[win])
        ctx = self._ctx()
        self._reconcile(ctx, {}, {}, workspaces=[ws])
        self.assertEqual(ctx.missing, [])
        self.assertEqual(self.calls, [])

    def test_unreadable_tree_skips_reconcile(self):
        win = make_window("kitty", name="alpha")
        ws = make_workspace("1", nodes=[win])
        ctx = self._ctx((win, 702))
        with unittest.mock.patch.object(
            self.restore_mod, "get_tree", side_effect=OSError("no socket")
        ):
            self.restore_mod._reconcile_placements([ws], [], ctx)
        self.assertEqual(ctx.missing, [])
        self.assertEqual(self.calls, [])


class TestAwaitStablePlacements(RestoreHarnessTestCase):
    """The last pass waits until the containers do not change."""

    def _ctx(self, *adopted):
        ctx = self.restore_mod.RestoreContext()
        self.addCleanup(ctx.registry.close)
        for node, win_id in adopted:
            ctx.registry.adopt(node, win_id)
        return ctx

    def _window(self, win_id, pid=0, app_id="calibre-gui", name="calibre"):
        return {
            "type": "con",
            "id": win_id,
            "app_id": app_id,
            "name": name,
            "pid": pid,
            "nodes": [],
            "floating_nodes": [],
        }

    def _tree(self, *windows):
        """A tree with one output that holds `windows` on workspace 1.

        Args:
            windows: The window containers that workspace 1 holds.
        """
        return {
            "nodes": [
                {
                    "type": "output",
                    "name": "HEADLESS-1",
                    "active": True,
                    "nodes": [
                        {
                            "type": "workspace",
                            "name": "1",
                            "nodes": list(windows),
                            "floating_nodes": [],
                        }
                    ],
                    "floating_nodes": [],
                }
            ],
            "floating_nodes": [],
        }

    def test_a_stable_tree_ends_the_wait_after_two_reads(self):
        win = make_window("calibre-gui", name="calibre")
        ws = make_workspace("1", nodes=[win])
        ctx = self._ctx((win, 501))
        with unittest.mock.patch.object(
            self.restore_mod, "get_tree", return_value=self._tree(self._window(501))
        ) as get_tree:
            self.restore_mod._await_stable_placements([ws], [], ctx)

        self.assertEqual(get_tree.call_count, 2)
        self.assertEqual(ctx.failures, [])

    def test_a_young_window_holds_the_wait_open(self):
        """A window that an application mapped recently can still change.

        The application can replace a window that it mapped a short time
        before. A young window holds the wait open until the window is past
        its settle time or until the deadline ends the wait.
        """
        win = make_window("calibre-gui", name="calibre")
        ws = make_workspace("1", nodes=[win])
        ctx = self._ctx((win, 501))
        ctx.registry.record(win, 501)
        with (
            unittest.mock.patch.object(
                self.restore_mod,
                "get_tree",
                return_value=self._tree(self._window(501)),
            ) as get_tree,
            unittest.mock.patch.object(self.restore_mod, "_GEOMETRY_POLL_INTERVAL", 0),
            unittest.mock.patch.object(
                self.restore_mod, "_PLACEMENT_SETTLE_TIMEOUT", 0.05
            ),
        ):
            self.restore_mod._await_stable_placements([ws], [], ctx)

        self.assertGreater(get_tree.call_count, 2)
        self.assertEqual(ctx.failures, [])

    def test_a_window_past_its_settle_ends_the_wait(self):
        win = make_window("calibre-gui", name="calibre")
        ws = make_workspace("1", nodes=[win])
        ctx = self._ctx((win, 501))
        ctx.registry.record(win, 501)
        with (
            unittest.mock.patch.object(
                self.restore_mod,
                "get_tree",
                return_value=self._tree(self._window(501)),
            ) as get_tree,
            unittest.mock.patch.object(ctx.registry, "age", return_value=5.0),
        ):
            self.restore_mod._await_stable_placements([ws], [], ctx)

        self.assertEqual(get_tree.call_count, 2)
        self.assertEqual(ctx.failures, [])

    def test_a_replaced_window_keeps_the_wait_running(self):
        win = make_window("calibre-gui", name="calibre")
        ws = make_workspace("1", nodes=[win])
        ctx = self._ctx((win, 501))
        trees = [
            self._tree(self._window(501)),
            self._tree(self._window(502)),
            self._tree(self._window(502)),
        ]
        with (
            unittest.mock.patch.object(self.restore_mod, "get_tree", side_effect=trees),
            unittest.mock.patch.object(self.restore_mod, "_GEOMETRY_POLL_INTERVAL", 0),
        ):
            self.restore_mod._await_stable_placements([ws], [], ctx)

        self.assertEqual(ctx.failures, [])

    def test_the_wait_gives_up_at_the_deadline(self):
        win = make_window("calibre-gui", name="calibre")
        ws = make_workspace("1", nodes=[win])
        ctx = self._ctx((win, 501))
        ids = itertools.count(500)
        with (
            unittest.mock.patch.object(
                self.restore_mod,
                "get_tree",
                side_effect=lambda: self._tree(self._window(next(ids))),
            ),
            unittest.mock.patch.object(self.restore_mod, "_GEOMETRY_POLL_INTERVAL", 0),
            unittest.mock.patch.object(
                self.restore_mod, "_PLACEMENT_SETTLE_TIMEOUT", 0.05
            ),
        ):
            self.restore_mod._await_stable_placements([ws], [], ctx)

        self.assertEqual(ctx.failures, [])

    def test_nothing_placed_skips_the_wait(self):
        ws = make_workspace("1", nodes=[make_window("kitty")])
        ctx = self._ctx()
        with unittest.mock.patch.object(self.restore_mod, "get_tree") as get_tree:
            self.restore_mod._await_stable_placements([ws], [], ctx)

        get_tree.assert_not_called()

    def test_an_unreadable_tree_ends_the_wait(self):
        win = make_window("calibre-gui", name="calibre")
        ws = make_workspace("1", nodes=[win])
        ctx = self._ctx((win, 501))
        with unittest.mock.patch.object(
            self.restore_mod, "get_tree", side_effect=OSError("no socket")
        ):
            self.restore_mod._await_stable_placements([ws], [], ctx)

        self.assertEqual(ctx.failures, [])


class TestRecoverLateWindows(RestoreHarnessTestCase):
    """A window that maps after its launch wait is still placed."""

    def _ctx(self, *adopted):
        ctx = self.restore_mod.RestoreContext()
        self.addCleanup(ctx.registry.close)
        for node, win_id in adopted:
            ctx.registry.adopt(node, win_id)
        return ctx

    def _staging_tree(self, *windows):
        return {
            "nodes": [
                {
                    "type": "output",
                    "name": "HEADLESS-1",
                    "active": True,
                    "nodes": [
                        {
                            "type": "workspace",
                            "name": self.restore_mod.RESTORE_STAGING_WORKSPACE,
                            "nodes": list(windows),
                            "floating_nodes": [],
                        }
                    ],
                    "floating_nodes": [],
                }
            ],
            "floating_nodes": [],
        }

    def _target_tree(self, workspace, *windows):
        return {
            "nodes": [
                {
                    "type": "output",
                    "name": "HEADLESS-1",
                    "active": True,
                    "rect": {"x": 0, "y": 0, "width": 1920, "height": 1080},
                    "nodes": [
                        {
                            "type": "workspace",
                            "name": workspace,
                            "nodes": list(windows),
                            "floating_nodes": [],
                        }
                    ],
                    "floating_nodes": [],
                }
            ],
            "floating_nodes": [],
        }

    def _live(self, win_id, app_id, name):
        return {
            "type": "con",
            "id": win_id,
            "app_id": app_id,
            "name": name,
            "pid": 4242,
            "nodes": [],
            "floating_nodes": [],
        }

    def test_late_visible_window_is_adopted_and_moved(self):
        win = make_window("calibre-gui", name="Library - calibre")
        ws = make_workspace("7", nodes=[win])
        ctx = self._ctx()
        ctx.add_missing("Library - calibre")
        tree = self._staging_tree(self._live(801, "calibre-gui", "Library - calibre"))
        with (
            unittest.mock.patch.object(self.restore_mod, "get_tree", lambda: tree),
            unittest.mock.patch.object(
                self.restore_mod,
                "_window_workspace_name",
                lambda _win_id: self.restore_mod.RESTORE_STAGING_WORKSPACE,
            ),
        ):
            recovered = self.restore_mod._recover_late_windows([ws], [], ctx)

        self.assertEqual(recovered, 1)
        self.assertEqual(ctx.missing, [])
        self.assertEqual(ctx.registry.resolve(win), 801)
        self.assertCommand("[con_id=801] move to workspace 7")

    def test_late_window_on_a_restored_workspace_is_adopted(self):
        """The geometry correction can briefly focus a restored workspace."""
        win = make_window("calibre-gui", name="Library - calibre")
        ws = make_workspace("7", nodes=[win])
        ctx = self._ctx()
        ctx.add_missing("Library - calibre")
        tree = self._target_tree(
            "7", self._live(806, "calibre-gui", "Library - calibre")
        )
        with unittest.mock.patch.object(self.restore_mod, "get_tree", lambda: tree):
            recovered = self.restore_mod._recover_late_windows([ws], [], ctx)

        self.assertEqual(recovered, 1)
        self.assertEqual(ctx.missing, [])
        self.assertEqual(ctx.registry.resolve(win), 806)

    def test_unmatched_window_on_a_restored_workspace_is_left_alone(self):
        saved = make_window("kitty", name="alpha")
        ws = make_workspace("7", nodes=[saved])
        ctx = self._ctx((saved, 900))
        tree = self._target_tree("7", self._live(807, "mpv", "video - mpv"))
        with unittest.mock.patch.object(self.restore_mod, "get_tree", lambda: tree):
            recovered = self.restore_mod._recover_late_windows([ws], [], ctx)

        self.assertEqual(recovered, 0)
        self.assertEqual(ctx.failures, [])
        self.assertFalse(any("move to workspace" in call for call in self.calls))

    def test_late_hidden_window_is_parked(self):
        from session_manager.config import SCRATCHPAD_WORKSPACE

        win = make_window("kitty", name="heavy-hidden-1")
        ctx = self._ctx()
        ctx.add_missing("heavy-hidden-1")
        tree = self._staging_tree(self._live(802, "kitty", "heavy-hidden-1"))
        with (
            unittest.mock.patch.object(self.restore_mod, "get_tree", lambda: tree),
            unittest.mock.patch.object(
                self.restore_mod,
                "_window_workspace_name",
                lambda _win_id: SCRATCHPAD_WORKSPACE,
            ),
        ):
            recovered = self.restore_mod._recover_late_windows([], [win], ctx)

        self.assertEqual(recovered, 1)
        self.assertEqual(ctx.missing, [])
        self.assertEqual(ctx.registry.resolve(win), 802)
        self.assertCommand("[con_id=802] move scratchpad")
        self.assertNotIn("scratchpad show", " ".join(self.calls))

    def test_unmatched_staged_window_is_reported_and_moved(self):
        saved = make_window("kitty", name="alpha")
        ws = make_workspace("1", nodes=[saved])
        ctx = self._ctx((saved, 900))
        tree = self._staging_tree(self._live(803, "mpv", "video - mpv"))
        with (
            unittest.mock.patch.object(self.restore_mod, "get_tree", lambda: tree),
            unittest.mock.patch.object(
                self.restore_mod,
                "_window_workspace_name",
                lambda _win_id: self.restore_mod.RESTORE_STAGING_WORKSPACE,
            ),
        ):
            recovered = self.restore_mod._recover_late_windows([ws], [], ctx)

        self.assertEqual(recovered, 0)
        self.assertTrue(
            any("did not match a saved window" in failure for failure in ctx.failures),
            ctx.failures,
        )
        self.assertCommand("[con_id=803] move to workspace 1")

    def test_a_staged_window_without_the_saved_title_is_not_adopted(self):
        """App identity alone must not let one window stand in for another."""
        saved = make_window("kitty", name="alpha")
        ws = make_workspace("7", nodes=[saved])
        ctx = self._ctx()
        tree = self._staging_tree(self._live(804, "kitty", "beta"))
        with (
            unittest.mock.patch.object(self.restore_mod, "get_tree", lambda: tree),
            unittest.mock.patch.object(
                self.restore_mod,
                "_window_workspace_name",
                lambda _win_id: self.restore_mod.RESTORE_STAGING_WORKSPACE,
            ),
        ):
            recovered = self.restore_mod._recover_late_windows([ws], [], ctx)

        self.assertEqual(recovered, 0)
        self.assertFalse(ctx.registry.has(saved))

    def test_a_staged_window_with_the_saved_pid_is_adopted(self):
        """The pid identifies the window even when the title changed."""
        saved = make_window("kitty", name="alpha", pid=4242)
        ws = make_workspace("7", nodes=[saved])
        ctx = self._ctx()
        tree = self._staging_tree(self._live(805, "kitty", "beta"))
        with (
            unittest.mock.patch.object(self.restore_mod, "get_tree", lambda: tree),
            unittest.mock.patch.object(
                self.restore_mod,
                "_window_workspace_name",
                lambda _win_id: self.restore_mod.RESTORE_STAGING_WORKSPACE,
            ),
        ):
            recovered = self.restore_mod._recover_late_windows([ws], [], ctx)

        self.assertEqual(recovered, 1)
        self.assertEqual(ctx.registry.resolve(saved), 805)

    def test_finish_staging_sweeps_a_workspace_that_survived(self):
        ctx = self._ctx()
        with (
            unittest.mock.patch.object(
                self.restore_mod,
                "_staging_workspace_names",
                side_effect=[[]],
            ),
            unittest.mock.patch.object(
                self.restore_mod, "_recover_late_windows", return_value=1
            ) as recover,
            unittest.mock.patch.object(
                self.restore_mod, "_reconcile_placements"
            ) as reconcile,
        ):
            self.restore_mod._finish_staging([], [], ctx, "2")

        self.assertEqual(self.calls, ["workspace 2"])
        recover.assert_called_once()
        reconcile.assert_called_once()
        self.assertEqual(ctx.failures, [])

    def test_finish_staging_settles_before_the_last_two_passes(self):
        """A replaced window is repaired and not asserted for a dead id."""
        ctx = self._ctx()
        order: list[str] = []
        with (
            unittest.mock.patch.object(
                self.restore_mod, "_staging_workspace_names", side_effect=[[]]
            ),
            unittest.mock.patch.object(
                self.restore_mod,
                "_await_stable_placements",
                side_effect=lambda *_a, **_kw: order.append("settle"),
            ),
            unittest.mock.patch.object(
                self.restore_mod,
                "_reconcile_placements",
                side_effect=lambda *_a, **_kw: order.append("reconcile"),
            ),
            unittest.mock.patch.object(
                self.restore_mod,
                "_recover_late_windows",
                side_effect=lambda *_a, **_kw: order.append("recover"),
            ),
        ):
            self.restore_mod._finish_staging([], [], ctx, "2")

        # The reconciliation binds a replaced window first. Thus the sweep
        # does not report it as a window that matches no saved window.
        self.assertEqual(order, ["settle", "reconcile", "recover"])
        self.assertEqual(ctx.failures, [])

    def test_finish_staging_reconciles_without_a_staging_workspace(self):
        """A staging workspace that was already swept is no reason to skip."""
        ctx = self._ctx()
        with (
            unittest.mock.patch.object(
                self.restore_mod, "_staging_workspace_names", side_effect=[[]]
            ),
            unittest.mock.patch.object(
                self.restore_mod, "_recover_late_windows", return_value=0
            ) as recover,
            unittest.mock.patch.object(
                self.restore_mod, "_reconcile_placements"
            ) as reconcile,
        ):
            self.restore_mod._finish_staging([], [], ctx, "2")

        recover.assert_called_once()
        reconcile.assert_called_once()
        self.assertEqual(ctx.failures, [])

    def test_finish_staging_sweeps_before_switching_away(self):
        """A window that maps during the sweep lands on staging, not the target."""
        ctx = self._ctx()
        ws = make_workspace("1", nodes=[make_window("kitty")])
        calls_during_sweep: list[list[str]] = []
        with (
            unittest.mock.patch.object(
                self.restore_mod,
                "_staging_workspace_names",
                side_effect=[[]],
            ),
            unittest.mock.patch.object(
                self.restore_mod,
                "_recover_late_windows",
                side_effect=lambda *_a, **_kw: calls_during_sweep.append(
                    list(self.calls)
                ),
            ),
            unittest.mock.patch.object(self.restore_mod, "_reconcile_placements"),
        ):
            self.restore_mod._finish_staging([ws], [], ctx, "2")

        self.assertEqual(calls_during_sweep, [["workspace _session_restore"]])
        self.assertEqual(self.calls, ["workspace _session_restore", "workspace 2"])

    def test_finish_staging_reports_a_workspace_that_will_not_leave(self):
        ctx = self._ctx()
        with (
            unittest.mock.patch.object(
                self.restore_mod,
                "_staging_workspace_names",
                side_effect=[["_session_restore"]],
            ),
            unittest.mock.patch.object(
                self.restore_mod, "_recover_late_windows", return_value=0
            ),
            unittest.mock.patch.object(self.restore_mod, "_reconcile_placements"),
        ):
            self.restore_mod._finish_staging([], [], ctx, "2")

        self.assertTrue(
            any("staging workspace not removed" in failure for failure in ctx.failures),
            ctx.failures,
        )


class TestHiddenScratchpad(RestoreHarnessTestCase):
    """Hidden scratchpad windows stay hidden, and groups are not dropped."""

    def test_hidden_container_is_flattened(self):
        group = make_con(
            nodes=[
                make_window("kitty", cwd="/a"),
                make_window("kitty", cwd="/b"),
            ]
        )
        ctx = self.restore_workspaces({"workspaces": [], "hidden_scratchpad": [group]})
        self.assertEqual(len(ctx.claimed_ids), 2)
        self.assertEqual(sum(1 for c in self.calls if "move scratchpad" in c), 2)

    def test_hidden_window_is_never_shown(self):
        win = make_window("kitty", cwd="/a")
        self.restore_workspaces({"workspaces": [], "hidden_scratchpad": [win]})
        self.assertNotIn("scratchpad show", " ".join(self.calls))

    def test_hidden_window_that_stays_visible_is_reported(self):
        win = make_window("kitty", cwd="/a")
        with unittest.mock.patch.object(
            self.restore_mod, "_window_workspace_name", lambda _win_id: "1"
        ):
            ctx = self.restore_workspaces(
                {"workspaces": [], "hidden_scratchpad": [win]}
            )
        self.assertTrue(
            any("still visible" in failure for failure in ctx.failures),
            ctx.failures,
        )

    def test_hidden_window_parked_in_the_scratchpad_is_not_reported(self):
        from session_manager.config import SCRATCHPAD_WORKSPACE

        win = make_window("kitty", cwd="/a")
        with unittest.mock.patch.object(
            self.restore_mod,
            "_window_workspace_name",
            lambda _win_id: SCRATCHPAD_WORKSPACE,
        ):
            ctx = self.restore_workspaces(
                {"workspaces": [], "hidden_scratchpad": [win]}
            )
        self.assertEqual(ctx.failures, [])

    def test_every_scratchpad_move_is_verified(self):
        """The last move must be checked too, not assumed to have worked."""
        from session_manager.config import SCRATCHPAD_WORKSPACE

        win = make_window("kitty", cwd="/a")
        moves = {"count": 0}

        def record(command):
            self.calls.append(command)
            if "move scratchpad" in command:
                moves["count"] += 1

        def workspace_of(_win_id):
            # The window only reaches the scratchpad once every move has
            # been tried.
            return SCRATCHPAD_WORKSPACE if moves["count"] >= 3 else "1"

        with (
            unittest.mock.patch.object(self.restore_mod, "cmd", record),
            unittest.mock.patch.object(
                self.restore_mod, "_window_workspace_name", workspace_of
            ),
        ):
            ctx = self.restore_workspaces(
                {"workspaces": [], "hidden_scratchpad": [win]}
            )
        self.assertEqual(ctx.failures, [])
        self.assertEqual(moves["count"], 3)

    def test_a_window_that_vanished_is_not_a_successful_park(self):
        win = make_window("kitty", cwd="/a")
        with unittest.mock.patch.object(
            self.restore_mod, "_window_workspace_name", lambda _win_id: None
        ):
            ctx = self.restore_workspaces(
                {"workspaces": [], "hidden_scratchpad": [win]}
            )
        self.assertTrue(
            any("vanished" in failure for failure in ctx.failures),
            ctx.failures,
        )


class TreeSource:
    """Serve a sequence of live trees, repeating the last one.

    The verification pass reads the tree more than once: twice to settle the
    target, then twice to confirm the reapplied geometry. The sequence lets
    a test say what the compositor answers at each step.
    """

    def __init__(self, trees):
        self.trees = list(trees)
        self.calls = 0

    def __call__(self):
        index = min(self.calls, len(self.trees) - 1)
        self.calls += 1
        return self.trees[index]


class TestFloatingGeometryVerification(RestoreHarnessTestCase):
    """The saved floating geometry is checked against the live tree.

    An application asks for its own size right after it maps, and that
    request can land after the restore placed the window. The verification
    pass reads the tree back once it has settled and applies the saved
    geometry again when the window no longer matches it.
    """

    SAVED_RECT: ClassVar[dict] = {"x": 100, "y": 121, "width": 600, "height": 379}
    LIVE_RECT: ClassVar[dict] = {"x": 100, "y": 121, "width": 704, "height": 502}
    WIN_ID = 4242

    def setUp(self):
        super().setUp()
        # The settle poll must not slow the suite down.
        patch = unittest.mock.patch.object(
            self.restore_mod, "_GEOMETRY_POLL_INTERVAL", 0
        )
        patch.start()
        self.addCleanup(patch.stop)
        timeout_patch = unittest.mock.patch.object(
            self.restore_mod, "_GEOMETRY_SETTLE_TIMEOUT", 0.05
        )
        timeout_patch.start()
        self.addCleanup(timeout_patch.stop)
        deco_patch = unittest.mock.patch.object(
            self.restore_mod, "_live_deco_height", return_value=0
        )
        deco_patch.start()
        self.addCleanup(deco_patch.stop)
        origin_patch = unittest.mock.patch.object(
            self.restore_mod, "_workspace_origin", return_value=(0, 0)
        )
        origin_patch.start()
        self.addCleanup(origin_patch.stop)

    def _live_tree(self, rect, workspace="2", node_type="floating_con"):
        """Return a tree with one floating window at the given rect."""
        return {
            "nodes": [
                {
                    "type": "output",
                    "name": "HEADLESS-1",
                    "active": True,
                    "rect": {"x": 0, "y": 0, "width": 1920, "height": 1080},
                    "nodes": [
                        {
                            "type": "workspace",
                            "name": workspace,
                            "rect": {"x": 0, "y": 0, "width": 1920, "height": 1080},
                            "nodes": [],
                            "floating_nodes": [
                                {
                                    "type": node_type,
                                    "id": self.WIN_ID,
                                    "app_id": "foot",
                                    "rect": dict(rect),
                                }
                            ],
                        }
                    ],
                }
            ],
            "floating_nodes": [],
        }

    def _context(self):
        ctx = RestoreContext()
        self.addCleanup(ctx.registry.close)
        return ctx

    def _verify(self, trees, node, ctx):
        """Run the verification pass against the given tree sequence."""
        source = TreeSource(trees)
        workspaces = [make_workspace("2", nodes=[], floating=[node])]
        with unittest.mock.patch.object(self.restore_mod, "get_tree", source):
            self.restore_mod._verify_floating_geometry(workspaces, ctx)
        return source

    def _saved_window(self, **extra):
        node = make_window("foot", rect=dict(self.SAVED_RECT))
        node.update(extra)
        return node

    def test_a_divergent_rect_is_reapplied(self):
        """The application overrode the size, so the saved one is applied again."""
        ctx = self._context()
        node = self._saved_window()
        ctx.registry.adopt(node, self.WIN_ID)
        self._verify(
            [
                self._live_tree(self.LIVE_RECT),
                self._live_tree(self.LIVE_RECT),
                self._live_tree(self.SAVED_RECT),
                self._live_tree(self.SAVED_RECT),
            ],
            node,
            ctx,
        )
        self.assertTrue(
            any("resize set width 600 px height 379 px" in c for c in self.calls),
            self.calls,
        )
        self.assertTrue(
            any("move position 100 121" in c for c in self.calls), self.calls
        )
        # Sway applies `move position` in the focused workspace. The saved
        # workspace is focused for the correction, then focus returns to
        # staging so a late window can still be recovered.
        workspace_index = self.calls.index("workspace 2")
        resize_index = next(
            i for i, call in enumerate(self.calls) if "resize set" in call
        )
        staging_index = (
            len(self.calls) - 1 - self.calls[::-1].index("workspace _session_restore")
        )
        self.assertLess(workspace_index, resize_index)
        self.assertGreater(staging_index, resize_index)

    def test_a_matching_rect_is_reapplied_after_the_settle(self):
        """A late application request cannot win before the final check."""
        ctx = self._context()
        node = self._saved_window()
        ctx.registry.adopt(node, self.WIN_ID)
        self._verify([self._live_tree(self.SAVED_RECT)], node, ctx)
        self.assertTrue(
            any("resize set width 600 px height 379 px" in c for c in self.calls),
            self.calls,
        )
        self.assertEqual(ctx.failures, [])

    def test_a_partial_saved_rect_uses_the_restore_defaults(self):
        """Missing width and height use the same defaults as the first apply."""
        ctx = self._context()
        node = make_window("foot", rect={"x": 100, "y": 121})
        ctx.registry.adopt(node, self.WIN_ID)
        live_rect = {"x": 100, "y": 121, "width": 800, "height": 600}
        self._verify(
            [
                self._live_tree(live_rect),
                self._live_tree(live_rect),
                self._live_tree(live_rect),
                self._live_tree(live_rect),
            ],
            node,
            ctx,
        )
        self.assertTrue(
            any("resize set width 800 px height 600 px" in c for c in self.calls),
            self.calls,
        )
        self.assertEqual(ctx.failures, [])

    def test_a_tiled_window_is_not_resized_as_floating(self):
        """A failed float must not turn into a tile resize."""
        ctx = self._context()
        node = self._saved_window()
        ctx.registry.adopt(node, self.WIN_ID)
        tree = self._live_tree(self.LIVE_RECT, node_type="con")
        self._verify([tree], node, ctx)
        self.assertFalse(any("resize set" in c for c in self.calls), self.calls)
        self.assertTrue(
            any("window is not floating" in failure for failure in ctx.failures),
            ctx.failures,
        )

    def test_a_failed_workspace_switch_skips_the_geometry_correction(self):
        ctx = self._context()
        node = self._saved_window()
        ctx.registry.adopt(node, self.WIN_ID)

        def record(command):
            self.calls.append(command)
            return command != "workspace 2"

        with unittest.mock.patch.object(self.restore_mod, "cmd", record):
            self._verify(
                [
                    self._live_tree(self.LIVE_RECT),
                    self._live_tree(self.LIVE_RECT),
                ],
                node,
                ctx,
            )
        self.assertFalse(any("resize set" in c for c in self.calls), self.calls)
        self.assertTrue(
            any("could not focus workspace" in failure for failure in ctx.failures),
            ctx.failures,
        )

    def test_a_dropped_connection_during_the_correction_is_reported(self):
        """A transport failure costs one window, not the whole restore."""
        ctx = self._context()
        node = self._saved_window()
        ctx.registry.adopt(node, self.WIN_ID)

        def failing(command):
            self.calls.append(command)
            if "resize set" in command:
                raise OSError("connection dropped")
            return True

        with unittest.mock.patch.object(self.restore_mod, "cmd", failing):
            self._verify(
                [
                    self._live_tree(self.LIVE_RECT),
                    self._live_tree(self.LIVE_RECT),
                ],
                node,
                ctx,
            )
        self.assertTrue(
            any("connection dropped" in failure for failure in ctx.failures),
            ctx.failures,
        )
        # The correction stopped for that window, but the pass still left
        # staging focused and did not abort.
        self.assertIn("workspace 2", self.calls)
        self.assertEqual(
            self.calls.count("workspace _session_restore"),
            2,
            self.calls,
        )

    def test_a_target_that_never_settles_is_reported(self):
        ctx = self._context()
        node = self._saved_window()
        ctx.registry.adopt(node, self.WIN_ID)
        reads = 0

        def changing_tree():
            nonlocal reads
            reads += 1
            rect = dict(self.LIVE_RECT)
            rect["width"] += reads
            return self._live_tree(rect)

        with unittest.mock.patch.object(self.restore_mod, "get_tree", changing_tree):
            self.restore_mod._verify_floating_geometry(
                [make_workspace("2", nodes=[], floating=[node])], ctx
            )
        self.assertTrue(
            any("did not settle" in failure for failure in ctx.failures),
            ctx.failures,
        )
        self.assertFalse(any("resize set" in c for c in self.calls), self.calls)

    def test_unrelated_layout_churn_does_not_delay_the_settle(self):
        ctx = self._context()
        node = self._saved_window()
        ctx.registry.adopt(node, self.WIN_ID)
        reads = 0

        def changing_tree():
            nonlocal reads
            reads += 1
            rect = self.LIVE_RECT if reads <= 2 else self.SAVED_RECT
            tree = self._live_tree(rect)
            tree["nodes"][0]["nodes"].append(
                {
                    "type": "con",
                    "id": 9999,
                    "rect": {"x": 0, "y": 0, "width": reads, "height": 100},
                    "nodes": [],
                    "floating_nodes": [],
                }
            )
            return tree

        with unittest.mock.patch.object(self.restore_mod, "get_tree", changing_tree):
            self.restore_mod._verify_floating_geometry(
                [make_workspace("2", nodes=[], floating=[node])], ctx
            )
        self.assertLess(reads, 10)
        self.assertFalse(any("did not settle" in f for f in ctx.failures), ctx.failures)

    def test_the_final_staging_focus_does_not_mask_a_geometry_error(self):
        ctx = self._context()
        node = self._saved_window()
        ctx.registry.adopt(node, self.WIN_ID)
        staging_calls = 0

        def record(command):
            nonlocal staging_calls
            self.calls.append(command)
            if command == "workspace _session_restore":
                staging_calls += 1
                if staging_calls > 1:
                    raise OSError("staging is gone")
            return True

        with (
            unittest.mock.patch.object(self.restore_mod, "cmd", record),
            unittest.mock.patch.object(
                self.restore_mod,
                "_apply_sway_geometry",
                side_effect=RuntimeError("original failure"),
            ),
            self.assertRaisesRegex(RuntimeError, "original failure"),
        ):
            self._verify(
                [
                    self._live_tree(self.LIVE_RECT),
                    self._live_tree(self.LIVE_RECT),
                ],
                node,
                ctx,
            )

        # The correction aborted for that window, and its `finally` still
        # left staging focused: the second staging focus is the one that
        # failed, and it is reported instead of masking the original error.
        self.assertEqual(self.calls.count("workspace _session_restore"), 2, self.calls)
        self.assertIn("workspace 2", self.calls)
        self.assertTrue(
            any("could not focus staging workspace" in f for f in ctx.failures),
            ctx.failures,
        )

    def test_the_decoration_is_included_in_the_reapplied_geometry(self):
        ctx = self._context()
        node = self._saved_window()
        ctx.registry.adopt(node, self.WIN_ID)
        with (
            unittest.mock.patch.object(
                self.restore_mod, "_live_deco_height", return_value=20
            ),
            unittest.mock.patch.object(
                self.restore_mod, "_workspace_origin", return_value=(0, 0)
            ),
        ):
            self._verify(
                [
                    self._live_tree(self.LIVE_RECT),
                    self._live_tree(self.LIVE_RECT),
                    self._live_tree(self.SAVED_RECT),
                    self._live_tree(self.SAVED_RECT),
                ],
                node,
                ctx,
            )
        self.assertTrue(
            any("resize set width 600 px height 399 px" in c for c in self.calls),
            self.calls,
        )
        self.assertTrue(
            any("move position 100 101" in c for c in self.calls), self.calls
        )

    def test_a_fullscreen_window_is_skipped(self):
        """A fullscreen window fills its output, so its rect never matches."""
        ctx = self._context()
        node = self._saved_window(fullscreen_mode=1)
        ctx.registry.adopt(node, self.WIN_ID)
        self._verify(
            [self._live_tree({"x": 0, "y": 0, "width": 1920, "height": 1080})],
            node,
            ctx,
        )
        self.assertFalse(
            any("resize set" in c for c in self.calls),
            self.calls,
        )
        self.assertEqual(ctx.failures, [])

    def test_i3_is_not_compared(self):
        """i3 reports the frame rect, which is not the size that was asked for."""
        self.backend = "i3"
        ctx = self._context()
        node = self._saved_window()
        ctx.registry.adopt(node, self.WIN_ID)
        source = self._verify([self._live_tree(self.LIVE_RECT)], node, ctx)
        self.assertEqual(source.calls, 0)
        self.assertFalse(
            any("resize set" in c for c in self.calls),
            self.calls,
        )

    def test_a_geometry_that_does_not_stick_is_reported(self):
        """A window that still does not match is reported, not passed over."""
        ctx = self._context()
        node = self._saved_window()
        ctx.registry.adopt(node, self.WIN_ID)
        self._verify([self._live_tree(self.LIVE_RECT)], node, ctx)
        self.assertTrue(
            any("geometry is" in failure for failure in ctx.failures),
            ctx.failures,
        )

    def test_a_reapplied_geometry_that_sticks_is_not_reported(self):
        ctx = self._context()
        node = self._saved_window()
        ctx.registry.adopt(node, self.WIN_ID)
        self._verify(
            [
                self._live_tree(self.LIVE_RECT),
                self._live_tree(self.LIVE_RECT),
                self._live_tree(self.SAVED_RECT),
                self._live_tree(self.SAVED_RECT),
            ],
            node,
            ctx,
        )
        self.assertEqual(ctx.failures, [])

    def test_an_intermediate_rect_is_not_reported_as_the_final_state(self):
        """Sway can expose an intermediate rect before the correction sticks."""
        ctx = self._context()
        node = self._saved_window()
        ctx.registry.adopt(node, self.WIN_ID)
        intermediate = {"x": 100, "y": 121, "width": 640, "height": 502}
        self._verify(
            [
                self._live_tree(self.LIVE_RECT),
                self._live_tree(self.LIVE_RECT),
                self._live_tree(intermediate),
                self._live_tree(self.SAVED_RECT),
                self._live_tree(self.SAVED_RECT),
            ],
            node,
            ctx,
        )
        self.assertEqual(ctx.failures, [])

    def test_an_unreadable_tree_is_reported_as_unverified(self):
        ctx = self._context()
        node = self._saved_window()
        ctx.registry.adopt(node, self.WIN_ID)
        with unittest.mock.patch.object(
            self.restore_mod, "get_tree", side_effect=OSError("cannot connect")
        ):
            self.restore_mod._verify_floating_geometry(
                [make_workspace("2", nodes=[], floating=[node])], ctx
            )
        self.assertTrue(
            any("could not be verified" in failure for failure in ctx.failures),
            ctx.failures,
        )

    def test_a_materialized_window_that_vanished_is_reported_missing(self):
        ctx = self._context()
        node = self._saved_window()
        ctx.registry.adopt(node, self.WIN_ID)
        tree = self._live_tree(self.LIVE_RECT)
        tree["nodes"][0]["nodes"][0]["floating_nodes"] = []
        self._verify([tree], node, ctx)
        self.assertEqual(ctx.missing, ["foot window"])

    def test_a_window_relocated_by_the_correction_is_reported(self):
        """The right rect on the wrong workspace is still a failed restore."""
        ctx = self._context()
        node = self._saved_window()
        ctx.registry.adopt(node, self.WIN_ID)
        self._verify(
            [
                self._live_tree(self.LIVE_RECT),
                self._live_tree(self.LIVE_RECT),
                self._live_tree(self.SAVED_RECT, workspace="1"),
            ],
            node,
            ctx,
        )
        self.assertTrue(
            any("workspace is '1'" in failure for failure in ctx.failures),
            ctx.failures,
        )

    def test_a_window_that_never_came_back_is_skipped(self):
        """A node that was never materialized has no live id to compare."""
        ctx = self._context()
        node = self._saved_window()
        self._verify([self._live_tree(self.LIVE_RECT)], node, ctx)
        self.assertFalse(
            any("resize set" in c for c in self.calls),
            self.calls,
        )
        self.assertEqual(ctx.failures, [])


class TestReadTree(unittest.TestCase):
    """The live tree is read once and shared, so a failure has to be clear."""

    def test_an_unreadable_tree_clears_the_placements(self):
        ctx = RestoreContext()
        self.addCleanup(ctx.registry.close)
        with unittest.mock.patch.object(
            restore_mod, "get_tree", side_effect=OSError("cannot connect")
        ):
            self.assertIsNone(restore_mod._read_tree(ctx))
        # The cached placements are dropped, so the next placement check asks
        # the compositor itself instead of trusting a stale answer.
        self.assertIsNone(ctx.placements)


if __name__ == "__main__":
    unittest.main()
