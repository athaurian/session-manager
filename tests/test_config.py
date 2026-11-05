"""Tests for path and environment configuration helpers."""

import os
import unittest
import unittest.mock
from pathlib import Path

import session_manager.config as cfg_mod


class TestEscapeCwd(unittest.TestCase):
    def test_replaces_separators(self):
        self.assertEqual(cfg_mod.escape_cwd("/home/user/proj"), "%home%user%proj")

    def test_collapses_repeated_separators(self):
        self.assertEqual(cfg_mod.escape_cwd("C:\\Windows\\a:b/c"), "C%Windows%a%b%c")

    def test_escapes_percent_before_separators(self):
        self.assertEqual(cfg_mod.escape_cwd("/a%b/c"), "%a%25b%c")

    def test_paths_with_percent_do_not_collide(self):
        self.assertNotEqual(cfg_mod.escape_cwd("/a/b/c"), cfg_mod.escape_cwd("/a%b/c"))


class TestXdgDir(unittest.TestCase):
    def test_uses_environment_value(self):
        with unittest.mock.patch.dict(os.environ, {"XDG_STATE_HOME": "/tmp/xdg_state"}):
            self.assertEqual(
                cfg_mod._xdg_dir("XDG_STATE_HOME", Path("/fallback")),
                Path("/tmp/xdg_state"),
            )

    def test_falls_back_when_unset(self):
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                cfg_mod._xdg_dir("XDG_STATE_HOME", Path("/fallback")),
                Path("/fallback"),
            )


class TestFloatEnv(unittest.TestCase):
    def test_reads_a_valid_value(self):
        with unittest.mock.patch.dict(os.environ, {"SM_TEST_TIMEOUT": "2.5"}):
            self.assertEqual(cfg_mod._float_env("SM_TEST_TIMEOUT", 1.0), 2.5)

    def test_falls_back_on_invalid_input(self):
        with unittest.mock.patch.dict(os.environ, {"SM_TEST_TIMEOUT": "soon"}):
            self.assertEqual(cfg_mod._float_env("SM_TEST_TIMEOUT", 1.0), 1.0)

    def test_rejects_a_value_that_is_not_finite(self):
        for value in ("nan", "inf", "-inf"):
            with (
                self.subTest(value=value),
                unittest.mock.patch.dict(os.environ, {"SM_TEST_TIMEOUT": value}),
            ):
                self.assertEqual(cfg_mod._float_env("SM_TEST_TIMEOUT", 1.0), 1.0)

    def test_rejects_a_negative_value(self):
        """A negative deadline would fire immediately."""
        with unittest.mock.patch.dict(os.environ, {"SM_TEST_TIMEOUT": "-1"}):
            self.assertEqual(cfg_mod._float_env("SM_TEST_TIMEOUT", 1.0), 1.0)


if __name__ == "__main__":
    unittest.main()
