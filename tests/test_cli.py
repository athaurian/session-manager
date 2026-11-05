"""Tests for the CLI entry point.

The CLI is the boundary the user sees, so an unreachable compositor has to
come out as a plain message and a non-zero exit code, not a traceback.
"""

import io
import json
import tempfile
import unittest
import unittest.mock
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import session_manager.restore as restore_mod
import session_manager.save as save_mod
from session_manager.__main__ import main
from session_manager.ipc import IpcError
from session_manager.restore import restore_session


class TestUnreachableCompositor(unittest.TestCase):
    def _run(self, action):
        stderr = io.StringIO()
        with (
            unittest.mock.patch("sys.argv", ["session_manager", action]),
            # The tests must never rewrite the developer's environment.
            unittest.mock.patch.object(restore_mod, "apply_compositor_env"),
            redirect_stderr(stderr),
            self.assertRaises(SystemExit) as raised,
        ):
            main()
        return raised.exception.code, stderr.getvalue()

    def test_save_reports_the_failure(self):
        with unittest.mock.patch(
            "session_manager.save.save_session",
            side_effect=IpcError("cannot connect to /run/i3.sock: no such file"),
        ):
            code, stderr = self._run("save")
        self.assertEqual(code, 1)
        self.assertIn("Cannot reach the window manager", stderr)

    def test_restore_reports_the_failure(self):
        with unittest.mock.patch(
            "session_manager.restore.restore_session",
            side_effect=IpcError("cannot connect to /run/i3.sock: no such file"),
        ):
            code, stderr = self._run("restore")
        self.assertEqual(code, 1)
        self.assertIn("Cannot reach the window manager", stderr)

    def test_diff_reports_the_failure(self):
        with unittest.mock.patch(
            "session_manager.restore.diff_sessions",
            side_effect=IpcError("cannot connect to /run/i3.sock: no such file"),
        ):
            code, stderr = self._run("diff")
        self.assertEqual(code, 1)
        self.assertIn("Cannot reach the window manager", stderr)

    def test_other_errors_still_propagate(self):
        """Only the unreachable-compositor case is swallowed."""
        with (
            unittest.mock.patch(
                "session_manager.save.save_session",
                side_effect=ValueError("something else went wrong"),
            ),
            self.assertRaises(ValueError),
        ):
            self._run("save")


class TestExitCodes(unittest.TestCase):
    """The exit code follows the result of the real command functions."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_dir = Path(self.tmp.name) / "state"
        self.state_dir.mkdir()
        self.state_file = self.state_dir / "session.json"

    def _run(self, action):
        """Run the CLI and return (exit code, stdout, stderr)."""
        stderr = io.StringIO()
        stdout = io.StringIO()
        code = 0
        with (
            unittest.mock.patch("sys.argv", ["session_manager", action]),
            unittest.mock.patch.object(restore_mod, "apply_compositor_env"),
            # A test run must never pop a desktop notification.
            unittest.mock.patch.object(save_mod, "notify"),
            unittest.mock.patch.object(restore_mod, "notify"),
            redirect_stderr(stderr),
            redirect_stdout(stdout),
        ):
            try:
                main()
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
        return code, stdout.getvalue(), stderr.getvalue()

    def _save_patches(self, *extra):
        """Patch the save path so it only touches a temporary state dir."""
        return [
            unittest.mock.patch.object(save_mod, "STATE_DIR", self.state_dir),
            unittest.mock.patch.object(save_mod, "STATE_FILE", self.state_file),
            unittest.mock.patch.object(
                save_mod, "NVIM_SESSION_DIR", self.state_dir / "nvim"
            ),
            unittest.mock.patch.object(
                save_mod,
                "BACKGROUND_APPS_FILE",
                self.state_dir / "session_background_apps.json",
            ),
            *extra,
        ]

    def test_save_exits_one_when_the_compositor_is_unreachable(self):
        patches = self._save_patches(
            unittest.mock.patch.object(
                save_mod,
                "get_tree",
                side_effect=IpcError("cannot connect to /run/i3.sock: no such file"),
            )
        )
        for p in patches:
            p.start()
        try:
            code, _stdout, stderr = self._run("save")
        finally:
            for p in patches:
                p.stop()
        self.assertEqual(code, 1)
        self.assertNotIn("Traceback", stderr)

    def test_save_exits_zero_when_the_snapshot_is_written(self):
        patches = self._save_patches(
            unittest.mock.patch.object(
                save_mod, "get_tree", return_value={"nodes": [], "floating_nodes": []}
            ),
            unittest.mock.patch.object(
                save_mod, "detect_background_apps", return_value=[]
            ),
        )
        for p in patches:
            p.start()
        try:
            code, _stdout, _stderr = self._run("save")
        finally:
            for p in patches:
                p.stop()
        self.assertEqual(code, 0)
        self.assertTrue(self.state_file.exists())

    def test_diff_exits_one_when_the_live_tree_is_unreadable(self):
        self.state_file.write_text(json.dumps({"workspaces": []}), encoding="utf-8")
        with (
            unittest.mock.patch.object(restore_mod, "STATE_FILE", self.state_file),
            unittest.mock.patch.object(
                restore_mod,
                "get_tree",
                side_effect=IpcError("cannot connect to /run/i3.sock: no such file"),
            ),
        ):
            code, stdout, _stderr = self._run("diff")
        self.assertEqual(code, 1)
        self.assertIn("Cannot read live tree", stdout)

    def test_diff_exits_one_when_the_saved_session_is_unreadable(self):
        self.state_file.write_text("{ not valid json", encoding="utf-8")
        with (
            unittest.mock.patch.object(restore_mod, "STATE_FILE", self.state_file),
            unittest.mock.patch.object(
                restore_mod,
                "get_tree",
                return_value={"nodes": [], "floating_nodes": []},
            ),
        ):
            code, stdout, _stderr = self._run("diff")
        self.assertEqual(code, 1)
        self.assertIn("Saved session unreadable", stdout)

    def test_diff_exits_zero_when_both_sides_are_readable(self):
        self.state_file.write_text(
            json.dumps(
                {
                    "workspaces": [
                        {
                            "type": "workspace",
                            "name": "1",
                            "layout": "splith",
                            "nodes": [],
                            "floating_nodes": [],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        with (
            unittest.mock.patch.object(restore_mod, "STATE_FILE", self.state_file),
            unittest.mock.patch.object(
                restore_mod,
                "get_tree",
                return_value={"nodes": [], "floating_nodes": []},
            ),
        ):
            code, stdout, _stderr = self._run("diff")
        self.assertEqual(code, 0)
        self.assertIn("Live vs Saved diff", stdout)

    def test_a_timeout_does_not_produce_a_traceback(self):
        with (
            unittest.mock.patch.object(restore_mod, "apply_compositor_env"),
            unittest.mock.patch.object(restore_mod, "notify"),
            unittest.mock.patch.object(
                restore_mod, "get_tree", side_effect=TimeoutError("IPC read timeout")
            ),
        ):
            code, _stdout, stderr = self._run("restore")
        self.assertEqual(code, 1)
        self.assertIn("Cannot reach the window manager", stderr)
        self.assertNotIn("Traceback", stderr)


class TestRestoreFailsFast(unittest.TestCase):
    """A dead compositor must be detected before any window is touched."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        state_file = Path(self.tmp.name) / "session.json"
        state_file.write_text('{"workspaces": []}', encoding="utf-8")
        patch = unittest.mock.patch.object(restore_mod, "STATE_FILE", state_file)
        patch.start()
        self.addCleanup(patch.stop)

    def test_no_window_is_restored_when_the_compositor_is_unreachable(self):
        with (
            unittest.mock.patch(
                "session_manager.restore.get_tree",
                side_effect=IpcError("cannot connect to /run/i3.sock: no such file"),
            ),
            unittest.mock.patch(
                "session_manager.restore._restore_session_locked"
            ) as locked,
            self.assertRaises(IpcError),
        ):
            restore_session(notify_user=False)
        locked.assert_not_called()

    def test_the_user_is_told_the_compositor_is_unreachable(self):
        with (
            unittest.mock.patch(
                "session_manager.restore.get_tree",
                side_effect=IpcError("cannot connect to /run/i3.sock: no such file"),
            ),
            unittest.mock.patch("session_manager.restore.notify") as notify,
            self.assertRaises(IpcError),
        ):
            restore_session(notify_user=True)
        notify.assert_called_once()
        self.assertIn("cannot reach the window manager", notify.call_args.args[0])


class TestArgumentWiring(unittest.TestCase):
    def test_has_session_exits_zero_when_content_exists(self):
        with (
            unittest.mock.patch("sys.argv", ["session_manager", "has-session"]),
            unittest.mock.patch(
                "session_manager.restore.has_restorable_content", return_value=True
            ),
            self.assertRaises(SystemExit) as raised,
        ):
            main()
        self.assertEqual(raised.exception.code, 0)

    def test_has_session_exits_one_when_empty(self):
        with (
            unittest.mock.patch("sys.argv", ["session_manager", "has-session"]),
            unittest.mock.patch(
                "session_manager.restore.has_restorable_content", return_value=False
            ),
            self.assertRaises(SystemExit) as raised,
        ):
            main()
        self.assertEqual(raised.exception.code, 1)

    def test_restore_passes_the_workspace_filter(self):
        with (
            unittest.mock.patch(
                "sys.argv",
                ["session_manager", "restore", "--quiet", "--workspace", "3"],
            ),
            unittest.mock.patch("session_manager.restore.restore_session") as restore,
        ):
            main()
        restore.assert_called_once_with(notify_user=False, workspace_filter="3")

    def test_save_quiet_disables_notifications(self):
        with (
            unittest.mock.patch("sys.argv", ["session_manager", "save", "--quiet"]),
            unittest.mock.patch("session_manager.save.save_session") as save,
        ):
            main()
        save.assert_called_once_with(notify_user=False)


if __name__ == "__main__":
    unittest.main()
