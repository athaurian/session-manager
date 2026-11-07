"""Tests for Thunar state capture and restore."""

import tempfile
import unittest
import unittest.mock
from pathlib import Path

import session_manager.apps.generic as gen_mod
import session_manager.apps.thunar as thunar_mod
from tests.helpers import FakeWatcher, RecordingPopen


class TestGetThunarInfo(unittest.TestCase):
    def test_returns_an_existing_directory_argument(self):
        with (
            tempfile.TemporaryDirectory() as first,
            tempfile.TemporaryDirectory() as second,
            unittest.mock.patch.object(
                thunar_mod, "read_cmdline", return_value=["thunar", first, second]
            ),
        ):
            self.assertEqual(thunar_mod.get_thunar_info(10), second)

    def test_ignores_flags_and_missing_directories(self):
        with (
            unittest.mock.patch.object(
                thunar_mod,
                "read_cmdline",
                return_value=["thunar", "--flag", "/no/such/dir"],
            ),
            unittest.mock.patch.object(Path, "readlink", side_effect=OSError),
        ):
            self.assertEqual(thunar_mod.get_thunar_info(10), "")

    def test_falls_back_to_the_process_working_directory(self):
        with (
            tempfile.TemporaryDirectory() as folder,
            unittest.mock.patch.object(
                thunar_mod, "read_cmdline", return_value=["thunar"]
            ),
            unittest.mock.patch.object(Path, "readlink", return_value=Path(folder)),
        ):
            self.assertEqual(thunar_mod.get_thunar_info(10), folder)


class TestLaunchThunar(unittest.TestCase):
    def _launch(self, node, win_id=55, baseline=()):
        popen = RecordingPopen()
        claimed = set()
        with (
            unittest.mock.patch.object(thunar_mod.subprocess, "Popen", popen),
            unittest.mock.patch.object(thunar_mod, "WindowEventWatcher", FakeWatcher),
            unittest.mock.patch.object(
                thunar_mod, "matching_window_ids", return_value=list(baseline)
            ),
            unittest.mock.patch.object(
                gen_mod, "wait_for_app_window", return_value=win_id
            ),
        ):
            result = thunar_mod.launch_thunar(node, claimed)
        return result, claimed, popen

    def test_launches_with_the_saved_folder(self):
        with tempfile.TemporaryDirectory() as folder:
            result, claimed, popen = self._launch({"folder_path": folder})
        self.assertEqual(result, 55)
        self.assertEqual(claimed, {55})
        self.assertEqual(popen.commands, [["thunar", folder]])

    def test_falls_back_to_the_home_directory(self):
        with (
            tempfile.TemporaryDirectory() as home,
            unittest.mock.patch.object(Path, "home", return_value=Path(home)),
        ):
            result, claimed, popen = self._launch({"folder_path": "/no/such/dir"})
        self.assertEqual(result, 55)
        self.assertEqual(claimed, {55})
        self.assertEqual(popen.commands, [["thunar", home]])

    def test_returns_none_when_no_window_appears(self):
        result, claimed, _ = self._launch({}, win_id=None)
        self.assertIsNone(result)
        self.assertEqual(claimed, set())

    def test_matches_by_app_identity_not_by_pid(self):
        """Thunar is single-instance: the window belongs to the daemon."""
        with (
            unittest.mock.patch.object(
                gen_mod, "wait_for_app_window", return_value=55
            ) as wait,
            unittest.mock.patch.object(
                thunar_mod.subprocess, "Popen", RecordingPopen()
            ),
            unittest.mock.patch.object(thunar_mod, "WindowEventWatcher", FakeWatcher),
            unittest.mock.patch.object(
                thunar_mod, "matching_window_ids", return_value=[]
            ),
        ):
            thunar_mod.launch_thunar({"folder_path": "/tmp"}, set())
        self.assertEqual(wait.call_args.args[0], "thunar")
        self.assertFalse(wait.call_args.kwargs["fail_fast_on_exit"])

    def test_existing_windows_are_the_baseline(self):
        with (
            unittest.mock.patch.object(
                gen_mod, "wait_for_app_window", return_value=55
            ) as wait,
            unittest.mock.patch.object(
                thunar_mod.subprocess, "Popen", RecordingPopen()
            ),
            unittest.mock.patch.object(thunar_mod, "WindowEventWatcher", FakeWatcher),
            unittest.mock.patch.object(
                thunar_mod, "matching_window_ids", return_value=[11, 12]
            ),
        ):
            thunar_mod.launch_thunar({"folder_path": "/tmp"}, set())
        self.assertEqual(wait.call_args.kwargs["baseline_ids"], {11, 12})


if __name__ == "__main__":
    unittest.main()
