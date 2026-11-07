"""Tests for the interprocess operation lock."""

import os
import tempfile
import threading
import unittest
from pathlib import Path

from session_manager.config import LOCK_FILE_NAME
from session_manager.locking import operation_lock


class TestOperationLock(unittest.TestCase):
    def test_creates_the_state_directory_and_lock_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            with operation_lock(state):
                self.assertTrue((state / LOCK_FILE_NAME).exists())

    def test_lock_can_be_taken_again_after_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            with operation_lock(state):
                pass
            with operation_lock(state):
                pass

    def test_another_process_blocks_until_the_lock_is_released(self):
        """The lock has to hold across processes, not just within one."""
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            ready_r, ready_w = os.pipe()
            release_r, release_w = os.pipe()

            pid = os.fork()
            if pid == 0:
                # Child: hold the lock until the parent says to let go.
                os.close(ready_r)
                os.close(release_w)
                try:
                    with operation_lock(state):
                        os.write(ready_w, b"1")
                        os.read(release_r, 1)
                finally:
                    os.close(ready_w)
                    os.close(release_r)
                os._exit(0)

            os.close(ready_w)
            os.close(release_r)
            try:
                self.assertEqual(
                    os.read(ready_r, 1),
                    b"1",
                    "the child never took the lock",
                )

                acquired = threading.Event()

                def take() -> None:
                    with operation_lock(state):
                        acquired.set()

                taker = threading.Thread(target=take, daemon=True)
                taker.start()
                self.assertFalse(
                    acquired.wait(0.5),
                    "the second holder was not blocked",
                )

                os.write(release_w, b"1")
                self.assertTrue(
                    acquired.wait(10),
                    "the lock was never released",
                )
                taker.join(10)
            finally:
                os.close(ready_r)
                os.close(release_w)
                os.waitpid(pid, 0)


if __name__ == "__main__":
    unittest.main()
