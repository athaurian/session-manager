"""Tests for restore orchestration: payload normalisation, focused-workspace,
and floating-window restore branches."""

import unittest
import unittest.mock

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

    def test_non_list_payload_rejected(self):
        with self.assertRaises(self.restore_mod.StateValidationError):
            self._norm("not a payload")

    def test_workspace_type_enforced(self):
        payload = {"workspaces": [make_window("kitty")]}
        workspaces, _, _, _ = self._norm(payload)
        self.assertEqual(workspaces, [])

    def test_missing_layout_defaults_to_splith(self):
        ws = make_workspace("1")
        del ws["layout"]
        workspaces, _, _, _ = self._norm({"workspaces": [ws]})
        self.assertEqual(workspaces[0]["layout"], "splith")

    def test_invalid_mark_types_dropped(self):
        win = make_window("kitty", marks=[42])
        ws = make_workspace("1", nodes=[win])
        workspaces, _, _, _ = self._norm({"workspaces": [ws]})
        self.assertEqual(workspaces[0]["nodes"][0]["marks"], [])

    def test_hidden_scratchpad_container_is_accepted(self):
        group = make_con(nodes=[make_window("kitty"), make_window("kitty")])
        payload = {"workspaces": [], "hidden_scratchpad": [group]}
        _, scratchpad, _, _ = self._norm(payload)
        self.assertEqual(len(scratchpad), 1)
        self.assertEqual(scratchpad[0]["type"], "con")

    def test_bad_workspace_does_not_drop_the_good_one(self):
        good = make_workspace("1", nodes=[make_window("kitty")])
        bad = {"type": "workspace", "name": ""}
        workspaces, _, _, _ = self._norm({"workspaces": [bad, good]})
        self.assertEqual([ws["name"] for ws in workspaces], ["1"])

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
    def _run_locked(self, payload, focused=None):
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

    def test_rect_members_must_be_numbers(self):
        win = make_window(
            "kitty",
            rect={"x": 0, "y": 0, "width": "800 px; exec id", "height": 600},
        )
        ws = make_workspace("1", nodes=[win])
        workspaces, _, _, _ = self._norm({"workspaces": [ws]})
        rect = workspaces[0]["nodes"][0]["rect"]
        self.assertNotIn("width", rect)
        self.assertEqual(rect["height"], 600)

    def test_rect_members_must_be_finite(self):
        win = make_window(
            "kitty", rect={"x": 0, "y": 0, "width": float("inf"), "height": 600}
        )
        ws = make_workspace("1", nodes=[win])
        workspaces, _, _, _ = self._norm({"workspaces": [ws]})
        self.assertNotIn("width", workspaces[0]["nodes"][0]["rect"])

    def test_percent_must_be_finite(self):
        win = make_window("kitty", percent=float("nan"))
        ws = make_workspace("1", nodes=[win])
        workspaces, _, _, _ = self._norm({"workspaces": [ws]})
        self.assertNotIn("percent", workspaces[0]["nodes"][0])

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

    def _reconcile(self, ctx, before, after, workspaces=(), hidden=()):
        with unittest.mock.patch.object(
            self.restore_mod,
            "_read_window_placements",
            side_effect=[before, after],
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
            self.restore_mod, "_read_window_placements", return_value=None
        ):
            self.restore_mod._reconcile_placements([ws], [], ctx)
        self.assertEqual(ctx.missing, [])
        self.assertEqual(self.calls, [])


class TestRecoverStagedWindows(RestoreHarnessTestCase):
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
            recovered = self.restore_mod._recover_staged_windows([ws], [], ctx)

        self.assertEqual(recovered, 1)
        self.assertEqual(ctx.missing, [])
        self.assertEqual(ctx.registry.resolve(win), 801)
        self.assertCommand("[con_id=801] move to workspace 7")

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
            recovered = self.restore_mod._recover_staged_windows([], [win], ctx)

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
            recovered = self.restore_mod._recover_staged_windows([ws], [], ctx)

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
            recovered = self.restore_mod._recover_staged_windows([ws], [], ctx)

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
            recovered = self.restore_mod._recover_staged_windows([ws], [], ctx)

        self.assertEqual(recovered, 1)
        self.assertEqual(ctx.registry.resolve(saved), 805)

    def test_finish_staging_sweeps_a_workspace_that_survived(self):
        ctx = self._ctx()
        with (
            unittest.mock.patch.object(
                self.restore_mod,
                "_staging_workspace_names",
                side_effect=[["_session_restore"], []],
            ),
            unittest.mock.patch.object(
                self.restore_mod, "_recover_staged_windows", return_value=1
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

    def test_finish_staging_sweeps_before_switching_away(self):
        """A window that maps during the sweep lands on staging, not the target."""
        ctx = self._ctx()
        calls_during_sweep: list[list[str]] = []
        with (
            unittest.mock.patch.object(
                self.restore_mod,
                "_staging_workspace_names",
                side_effect=[["_session_restore"], []],
            ),
            unittest.mock.patch.object(
                self.restore_mod,
                "_recover_staged_windows",
                side_effect=lambda *_a, **_kw: calls_during_sweep.append(
                    list(self.calls)
                ),
            ),
            unittest.mock.patch.object(self.restore_mod, "_reconcile_placements"),
        ):
            self.restore_mod._finish_staging([], [], ctx, "2")

        self.assertEqual(calls_during_sweep, [[]])
        self.assertEqual(self.calls, ["workspace 2"])

    def test_finish_staging_reports_a_workspace_that_will_not_leave(self):
        ctx = self._ctx()
        with (
            unittest.mock.patch.object(
                self.restore_mod,
                "_staging_workspace_names",
                side_effect=[["_session_restore"], ["_session_restore"]],
            ),
            unittest.mock.patch.object(
                self.restore_mod, "_recover_staged_windows", return_value=0
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


if __name__ == "__main__":
    unittest.main()
