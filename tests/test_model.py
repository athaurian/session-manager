"""Tests for the versioned session snapshot contract."""

import unittest

from session_manager.model import (
    CURRENT_VERSION,
    Snapshot,
    StateValidationError,
)
from tests.helpers import make_con, make_window, make_workspace


class TestSnapshotParsing(unittest.TestCase):
    def test_legacy_list_is_version_one(self):
        ws = make_workspace("1")
        snapshot = Snapshot.from_payload([ws])
        self.assertEqual(snapshot.version, 1)
        self.assertEqual(snapshot.workspaces, [ws])

    def test_version_two_is_accepted_and_written_as_current(self):
        ws = make_workspace("1")
        snapshot = Snapshot.from_payload({"version": 2, "workspaces": [ws]})
        self.assertEqual(snapshot.version, 2)
        self.assertEqual(snapshot.to_payload()["version"], CURRENT_VERSION)

    def test_unsupported_version_is_refused(self):
        with self.assertRaises(StateValidationError):
            Snapshot.from_payload({"version": 99, "workspaces": []})

    def test_non_object_payload_is_refused(self):
        with self.assertRaises(StateValidationError):
            Snapshot.from_payload("not a payload")

    def test_round_trip_preserves_the_session(self):
        win = make_window("kitty", cwd="/tmp")
        ws = make_workspace("1", nodes=[win])
        original = Snapshot(
            workspaces=[ws],
            hidden_scratchpad=[],
            background_apps=[{"app_id": "vesktop"}],
            focused_workspace="1",
        )
        parsed = Snapshot.from_payload(original.to_payload())
        self.assertEqual(parsed.workspaces, [ws])
        self.assertEqual(parsed.background_apps, [{"app_id": "vesktop"}])
        self.assertEqual(parsed.focused_workspace, "1")


class TestSnapshotRepair(unittest.TestCase):
    def test_bad_node_is_dropped_but_good_node_survives(self):
        good = make_workspace("1", nodes=[make_window("kitty")])
        bad = {"type": "workspace", "name": ""}
        snapshot = Snapshot.from_payload({"workspaces": [bad, good]})
        self.assertEqual([ws["name"] for ws in snapshot.workspaces], ["1"])
        self.assertTrue(snapshot.issues)

    def test_hidden_scratchpad_container_is_accepted(self):
        group = make_con(nodes=[make_window("kitty"), make_window("kitty")])
        snapshot = Snapshot.from_payload(
            {"workspaces": [], "hidden_scratchpad": [group]}
        )
        self.assertEqual(len(snapshot.hidden_scratchpad), 1)
        self.assertEqual(snapshot.hidden_scratchpad[0]["type"], "con")

    def test_invalid_rect_member_is_dropped(self):
        win = make_window(
            "kitty", rect={"x": 0, "y": 0, "width": "800; exec id", "height": 600}
        )
        ws = make_workspace("1", nodes=[win])
        snapshot = Snapshot.from_payload({"workspaces": [ws]})
        rect = snapshot.workspaces[0]["nodes"][0]["rect"]
        self.assertNotIn("width", rect)
        self.assertEqual(rect["height"], 600)

    def test_missing_layout_defaults_to_splith(self):
        ws = make_workspace("1")
        del ws["layout"]
        snapshot = Snapshot.from_payload({"workspaces": [ws]})
        self.assertEqual(snapshot.workspaces[0]["layout"], "splith")

    def test_invalid_background_app_is_dropped(self):
        snapshot = Snapshot.from_payload(
            {
                "workspaces": [],
                "background_apps": [{"app_id": "vesktop"}, {"app_id": 42}, 7],
            }
        )
        self.assertEqual(snapshot.background_apps, [{"app_id": "vesktop"}])
        self.assertTrue(snapshot.issues)

    def test_invalid_focused_workspace_is_dropped(self):
        snapshot = Snapshot.from_payload({"workspaces": [], "focused_workspace": 42})
        self.assertIsNone(snapshot.focused_workspace)
        self.assertTrue(snapshot.issues)


if __name__ == "__main__":
    unittest.main()
