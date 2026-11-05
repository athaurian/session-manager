"""Tests for the per-app handler boundary."""

import unittest

import session_manager.apps.generic as gen_mod
from session_manager.apps.handlers import handler_for


class TestHandlerTable(unittest.TestCase):
    """Each app's launch and identity facts live in one place."""

    def test_kitty_waits_for_the_window_of_its_own_pid(self):
        self.assertTrue(handler_for("kitty").pid_matched)

    def test_drop_terminal_is_on_demand(self):
        self.assertTrue(handler_for("kitty_drop").on_demand)

    def test_calibre_is_matched_by_app_identity(self):
        self.assertFalse(handler_for("calibre-ebook-viewer").pid_matched)

    def test_helium_is_a_group_without_a_launcher(self):
        handler = handler_for("helium")
        self.assertTrue(handler.grouped)
        self.assertIsNone(handler.launch)
        self.assertFalse(handler.adoptable)

    def test_unknown_app_uses_the_generic_launcher(self):
        handler = handler_for("no.such.app")
        self.assertIs(handler.launch, gen_mod.launch_generic)
        self.assertIsNone(handler.capture)


class TestClassify(unittest.TestCase):
    """Capture-side identity is owned by the app that needs it."""

    def test_transient_terminal_is_dropped(self):
        self.assertIsNone(
            handler_for("kitty").classify({"name": "topgrade_term"}, "kitty")
        )

    def test_drop_terminal_is_named(self):
        self.assertEqual(
            handler_for("kitty").classify({"name": "drop_term"}, "kitty"),
            "kitty_drop",
        )
