"""Tests for concurrent materialization and the window identity registry."""

import json
import tempfile
import threading
import unittest
import unittest.mock
from contextlib import nullcontext
from pathlib import Path

import session_manager.restore as restore_mod
from session_manager.registry import ClaimedWindows, WindowRegistry
from session_manager.state import RestoreContext
from tests.helpers import RestoreHarnessTestCase, make_window, make_workspace


class RegistryTestCase(unittest.TestCase):
    def make_context(self):
        ctx = RestoreContext()
        self.addCleanup(ctx.registry.close)
        return ctx


class MaterializeHarness(RestoreHarnessTestCase):
    def make_context(self):
        ctx = RestoreContext()
        self.addCleanup(ctx.registry.close)
        return ctx


class TestClaimedWindows(RegistryTestCase):
    def test_claims_are_shared_across_threads(self):
        claims = ClaimedWindows()

        def claim(start):
            for value in range(start, start + 100):
                claims.add(value)

        threads = [threading.Thread(target=claim, args=(base,)) for base in (0, 100)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len(claims), 200)
        self.assertIn(150, claims)
        self.assertNotIn(250, claims)

    def test_discard_releases_a_claim(self):
        claims = ClaimedWindows()
        claims.add(7)
        claims.discard(7)
        self.assertNotIn(7, claims)
        self.assertEqual(len(claims), 0)


class TestLaunchUnits(RegistryTestCase):
    def test_pid_matched_windows_get_their_own_unit(self):
        units = restore_mod._launch_units([make_window("kitty"), make_window("kitty")])
        self.assertEqual(len(units), 2)

    def test_app_identity_windows_share_one_unit(self):
        units = restore_mod._launch_units(
            [
                make_window("calibre-ebook-viewer"),
                make_window("calibre-ebook-viewer"),
            ]
        )
        self.assertEqual(len(units), 1)
        self.assertEqual(len(units[0]), 2)

    def test_different_apps_get_different_units(self):
        units = restore_mod._launch_units(
            [make_window("calibre-ebook-viewer"), make_window("vesktop")]
        )
        self.assertEqual(len(units), 2)

    def test_helium_is_left_to_the_group_restore(self):
        self.assertEqual(restore_mod._launch_units([make_window("helium")]), [])


class TestMaterialize(MaterializeHarness):
    def test_independent_windows_launch_concurrently(self):
        ctx = self.make_context()
        win_a = make_window("kitty", cwd="/a")
        win_b = make_window("kitty", cwd="/b")
        barrier = threading.Barrier(2, timeout=5)
        lock = threading.Lock()
        ids = iter((501, 502))

        def launch(node, claimed_ids):
            # Both launches must be in flight before either may return.
            barrier.wait()
            with lock:
                win_id = next(ids)
            claimed_ids.add(win_id)
            return win_id

        with unittest.mock.patch.object(restore_mod, "launch_and_get_id", launch):
            restore_mod._materialize([win_a, win_b], ctx)

        self.assertEqual(
            {ctx.registry.resolve(win_a), ctx.registry.resolve(win_b)}, {501, 502}
        )

    def test_app_identity_launches_do_not_overlap(self):
        ctx = self.make_context()
        nodes = [
            make_window("calibre-ebook-viewer", name="a"),
            make_window("calibre-ebook-viewer", name="b"),
        ]
        active = 0
        peak = 0
        lock = threading.Lock()
        ids = iter((601, 602))

        def launch(node, claimed_ids):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                win_id = next(ids)
                claimed_ids.add(win_id)
                return win_id
            finally:
                with lock:
                    active -= 1

        with unittest.mock.patch.object(restore_mod, "launch_and_get_id", launch):
            restore_mod._materialize(nodes, ctx)

        self.assertEqual(peak, 1)
        self.assertEqual([ctx.registry.resolve(node) for node in nodes], [601, 602])

    def test_a_failed_launch_does_not_stop_the_others(self):
        ctx = self.make_context()
        failing = make_window("mpv")
        good = make_window("kitty")

        def launch(node, claimed_ids):
            if node is failing:
                raise FileNotFoundError("mpv: command not found")
            claimed_ids.add(702)
            return 702

        with unittest.mock.patch.object(restore_mod, "launch_and_get_id", launch):
            restore_mod._materialize([failing, good], ctx)

        self.assertIsNone(ctx.registry.resolve(failing))
        self.assertEqual(ctx.registry.resolve(good), 702)
        self.assertEqual(len(ctx.failures), 1)
        self.assertIn("mpv", ctx.failures[0])

    def test_walk_reuses_the_materialized_window(self):
        ctx = self.make_context()
        win = make_window("kitty", cwd="/a")
        ws = make_workspace("1", nodes=[win])

        def launch(node, claimed_ids):
            claimed_ids.add(4321)
            return 4321

        with unittest.mock.patch.object(restore_mod, "launch_and_get_id", launch):
            restore_mod._materialize([win], ctx)

        with unittest.mock.patch.object(
            restore_mod,
            "launch_and_get_id",
            side_effect=AssertionError("the walk must not launch again"),
        ):
            restore_mod._restore_node(ws, ctx)

        self.assertIn("[con_id=4321] focus", self.calls)


class TestHeliumGroup(MaterializeHarness):
    def test_group_records_every_saved_window(self):
        ctx = self.make_context()
        nodes = [make_window("helium", name="a"), make_window("helium", name="b")]
        ctx.helium_saved_nodes = list(nodes)
        ctx.helium_restored_ids = [None, None]

        def fake_group(context):
            context.helium_restore_started = True
            context.helium_restored_ids[:] = [11, 12]

        with unittest.mock.patch.object(
            restore_mod, "restore_helium_group", fake_group
        ):
            restore_mod._start_helium_materialization(ctx)
            self.assertEqual(ctx.registry.resolve(nodes[0]), 11)

        self.assertEqual([ctx.registry.resolve(node) for node in nodes], [11, 12])

    def test_group_starts_only_once(self):
        ctx = self.make_context()
        node = make_window("helium", name="a")
        ctx.helium_saved_nodes = [node]
        ctx.helium_restored_ids = [None]
        calls = []

        def fake_group(context):
            calls.append(1)
            context.helium_restored_ids[0] = 21

        with unittest.mock.patch.object(
            restore_mod, "restore_helium_group", fake_group
        ):
            restore_mod._start_helium_materialization(ctx)
            restore_mod._start_helium_materialization(ctx)
            self.assertEqual(ctx.registry.resolve(node), 21)

        self.assertEqual(len(calls), 1)


class TestMaterializeBeforeArrange(MaterializeHarness):
    """No placement command may be issued while windows are still launching."""

    def _run_locked(self, payload):
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
                unittest.mock.patch.object(
                    self.restore_mod, "_start_helium_materialization"
                ),
            ]
            for patch in extra:
                patch.start()
            try:
                self.restore_mod._restore_session_locked(notify_user=False)
            finally:
                for patch in extra:
                    patch.stop()

    def test_placement_waits_for_every_launch(self):
        placements: list[str] = []

        def launch(node, claimed_ids):
            # Materialization runs before the walk, so no window has been
            # focused, moved, or split yet. The staging workspace is the only
            # command issued beforehand.
            self.assertEqual(
                [c for c in self.calls if c != "workspace _session_restore"], []
            )
            placements.append(node["name"])
            win_id = next(self._id_iter)
            claimed_ids.add(win_id)
            return win_id

        win_a = make_window("kitty", cwd="/a")
        win_b = make_window("kitty", cwd="/b")
        ws = make_workspace("1", nodes=[win_a, win_b])
        with unittest.mock.patch.object(self.restore_mod, "launch_and_get_id", launch):
            self._run_locked(
                {"workspaces": [ws], "hidden_scratchpad": [], "background_apps": []}
            )

        self.assertEqual(len(placements), 2)
        self.assertTrue(any(c.endswith("] focus") for c in self.calls))


class TestRegistryFallback(MaterializeHarness):
    def test_unmaterialized_node_falls_back_to_launching(self):
        ctx = self.make_context()
        win = make_window("kitty", cwd="/a")
        with unittest.mock.patch.object(
            restore_mod, "launch_and_get_id", lambda node, claimed: 99
        ):
            self.assertEqual(restore_mod._resolve_window(win, ctx), 99)

    def test_registry_reports_unsubmitted_nodes(self):
        registry = WindowRegistry()
        self.addCleanup(registry.close)
        self.assertFalse(registry.has(make_window("kitty")))
        with self.assertRaises(KeyError):
            registry.resolve(make_window("kitty"))


if __name__ == "__main__":
    unittest.main()
