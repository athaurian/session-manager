"""Tests for the IPC connection's failure handling.

A broken connection must never leave a half-read reply behind: the next
command would then read the previous answer.
"""

import json
import os
import socket
import tempfile
import threading
import unittest
import unittest.mock

from session_manager import ipc as ipc_mod
from session_manager.ipc import (
    EVENT_BIT,
    EVENT_WINDOW,
    HEADER,
    MAGIC,
    MSG_GET_TREE,
    MSG_RUN_COMMAND,
    MSG_SUBSCRIBE,
    IpcConnection,
    IpcError,
    WindowEventWatcher,
)


class FakeCompositor:
    """A unix socket that accepts a connection and can reply or stay quiet."""

    def __init__(self, tmpdir):
        self.path = os.path.join(tmpdir, "ipc.sock")
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(self.path)
        self.sock.listen(1)
        self.conn: socket.socket | None = None

    def accept(self):
        self.conn, _ = self.sock.accept()

    def send(self, payload: bytes):
        assert self.conn is not None
        self.conn.sendall(payload)

    def send_message(self, msg_type: int, text: str):
        body = text.encode()
        self.send(HEADER.pack(MAGIC, len(body), msg_type) + body)

    def close(self):
        if self.conn is not None:
            self.conn.close()
        self.sock.close()


class TestConnectionFailures(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.server = FakeCompositor(self.tmp.name)
        self.addCleanup(self.server.close)
        shared = unittest.mock.patch.object(ipc_mod, "_shared", None)
        shared.start()
        self.addCleanup(shared.stop)

    def _connect(self):
        conn = IpcConnection(sock_path=self.server.path).connect()
        self.addCleanup(conn.close)
        return conn

    def test_command_timeout_closes_the_connection(self):
        conn = self._connect()
        self.server.accept()
        with self.assertRaises(IpcError):
            conn.command("get_tree", timeout=0.2)
        # The socket is dropped so the next command starts fresh.
        self.assertIsNone(conn._sock)

    def test_body_timeout_is_fatal_for_the_stream(self):
        conn = self._connect()
        self.server.accept()
        # The header promises a body that never arrives.
        self.server.send(HEADER.pack(MAGIC, 100, MSG_GET_TREE))
        with self.assertRaises(IpcError):
            conn.command("get_tree", timeout=0.05)
        self.assertIsNone(conn._sock)

    def test_partial_reply_is_a_fatal_error(self):
        conn = self._connect()
        self.server.accept()
        # Half a header, then silence: the stream is out of step.
        self.server.send(HEADER.pack(MAGIC, 32, 0)[:6])
        with self.assertRaises(IpcError):
            conn.command("get_tree", timeout=0.2)
        self.assertIsNone(conn._sock)

    def test_connecting_to_a_missing_socket_raises_ipc_error(self):
        with self.assertRaises(IpcError):
            IpcConnection(sock_path=os.path.join(self.tmp.name, "gone")).connect()

    def test_ipc_error_is_an_oserror(self):
        """Callers guard IPC calls with `except OSError`, so it must be one.

        An unreachable socket is an I/O failure, and the guards around
        get_tree() and run_command() rely on that.
        """
        self.assertTrue(issubclass(IpcError, OSError))
        with self.assertRaises(OSError):
            IpcConnection(sock_path=os.path.join(self.tmp.name, "gone")).connect()

    def test_shared_connection_is_rebuilt_after_a_failure(self):
        with unittest.mock.patch.object(
            ipc_mod, "resolve_socket_path", return_value=self.server.path
        ):
            first = ipc_mod.connection()
            self.server.accept()
            with self.assertRaises(IpcError):
                first.command("first", timeout=0.05)
            self.assertIsNone(first._sock)

            # The next caller must get a fresh connection that reads the
            # reply belonging to its own command.
            second = ipc_mod.connection()
            self.assertIsNot(second, first)
            self.server.accept()
            self.server.send_message(MSG_RUN_COMMAND, json.dumps([{"success": True}]))
            self.assertEqual(second.command("second"), [{"success": True}])
        ipc_mod.close_connection()

    def test_command_after_a_timeout_reads_the_new_reply(self):
        """The connection is rebuilt, so no stale reply can be returned."""
        conn = self._connect()
        self.server.accept()
        with self.assertRaises(IpcError):
            conn.command("first", timeout=0.2)
        self.assertIsNone(conn._sock)

        conn.connect()
        self.server.accept()
        self.server.send_message(MSG_RUN_COMMAND, json.dumps([{"success": True}]))
        self.assertEqual(conn.command("second"), [{"success": True}])


class TestRequests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.server = FakeCompositor(self.tmp.name)
        self.addCleanup(self.server.close)

    def _connect(self):
        conn = IpcConnection(sock_path=self.server.path).connect()
        self.addCleanup(conn.close)
        self.server.accept()
        return conn

    def test_get_tree_returns_the_payload(self):
        conn = self._connect()
        payload = {"nodes": []}
        self.server.send_message(MSG_GET_TREE, json.dumps(payload))
        self.assertEqual(conn.get_tree(), payload)

    def test_get_tree_rejects_a_command_reply(self):
        conn = self._connect()
        self.server.send_message(0, "[]")
        with self.assertRaises(IpcError):
            conn.get_tree()

    def test_a_non_list_reply_is_refused(self):
        """An empty list would look like a command that did nothing."""
        conn = self._connect()
        self.server.send_message(0, json.dumps({"success": True}))
        with self.assertRaises(IpcError):
            conn.command("split h")

    def test_a_malformed_reply_is_an_ipc_error(self):
        conn = self._connect()
        self.server.send_message(0, "{not json")
        with self.assertRaises(IpcError):
            conn.command("split h")

    def test_a_reply_that_is_not_utf8_is_an_ipc_error(self):
        conn = self._connect()
        body = b"\xff\xfe"
        self.server.send(HEADER.pack(MAGIC, len(body), 0) + body)
        with self.assertRaises(IpcError):
            conn.command("split h")

    def test_a_malformed_tree_reply_is_an_ipc_error(self):
        conn = self._connect()
        self.server.send_message(MSG_GET_TREE, "[1, 2]")
        with self.assertRaises(IpcError):
            conn.get_tree()


class FakeSocket:
    """A socket stand-in whose writes can stall, fail, or stop early."""

    def __init__(self, sent_per_call=None, error=None):
        self.sent_per_call = sent_per_call
        self.error = error
        self.written = b""
        self.calls = 0

    def send(self, data):
        self.calls += 1
        if self.error is not None:
            raise self.error
        if self.sent_per_call is None:
            # The peer is gone, so nothing is accepted.
            return 0
        count = min(self.sent_per_call, len(data))
        self.written += data[:count]
        return count

    def shutdown(self, _how):
        return None

    def close(self):
        return None


class TestWriteDeadlines(unittest.TestCase):
    """A write must not block forever when the compositor stopped reading.

    A read may be told to wait forever, but a write never may: a compositor
    that stopped reading must not block the manager.
    """

    def _connection(self, sock, request_timeout=0.05):
        conn = IpcConnection(
            sock_path="/tmp/unused.sock", request_timeout=request_timeout
        )
        conn._sock = sock
        self.addCleanup(conn.close)
        return conn

    def _writable(self, sock):
        """Pretend the socket can be written to."""
        return unittest.mock.patch.object(
            ipc_mod.select, "select", lambda *_args: ([], [sock], [])
        )

    def test_a_write_that_cannot_proceed_times_out(self):
        sock = FakeSocket()
        conn = self._connection(sock)
        with (
            unittest.mock.patch.object(
                ipc_mod.select, "select", return_value=([], [], [])
            ),
            self.assertRaises(IpcError) as raised,
        ):
            conn._send(MSG_GET_TREE)
        self.assertIn("timed out", str(raised.exception))
        self.assertEqual(sock.calls, 0)

    def test_the_write_deadline_expires_even_on_a_writable_socket(self):
        """A peer that accepts a byte at a time must still hit the deadline."""
        sock = FakeSocket(sent_per_call=1)
        conn = self._connection(sock, request_timeout=0.05)
        clock = [0.0]

        def fake_monotonic():
            clock[0] += 0.03
            return clock[0]

        with (
            self._writable(sock),
            unittest.mock.patch.object(ipc_mod.time, "monotonic", fake_monotonic),
            self.assertRaises(IpcError) as raised,
        ):
            conn._send(MSG_RUN_COMMAND, "x" * 100)
        self.assertIn("timed out", str(raised.exception))
        self.assertGreater(sock.calls, 0)

    def test_a_partial_write_is_resumed(self):
        """A short write must not truncate the message."""
        sock = FakeSocket(sent_per_call=3)
        conn = self._connection(sock)
        with self._writable(sock):
            conn._send(MSG_RUN_COMMAND, "hello")
        body = b"hello"
        self.assertEqual(
            sock.written, HEADER.pack(MAGIC, len(body), MSG_RUN_COMMAND) + body
        )

    def test_a_peer_that_accepts_nothing_times_out(self):
        """A socket that accepts no bytes must not spin past the deadline.

        A blocking socket either sends something or raises, so the zero-byte
        case is only reachable through a fake; the write deadline bounds it.
        """
        sock = FakeSocket()
        conn = self._connection(sock)
        with self._writable(sock), self.assertRaises(IpcError) as raised:
            conn._send(MSG_GET_TREE)
        self.assertIn("timed out", str(raised.exception))
        self.assertGreater(sock.calls, 0)

    def test_a_failed_send_is_an_ipc_error(self):
        sock = FakeSocket(error=OSError("broken pipe"))
        conn = self._connection(sock)
        with self._writable(sock), self.assertRaises(IpcError) as raised:
            conn._send(MSG_GET_TREE)
        self.assertIn("closed", str(raised.exception))


class TestWindowEventWatcher(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.server = FakeCompositor(self.tmp.name)
        self.addCleanup(self.server.close)

    def _serve(self, subscribe_reply, event=None):
        def serve():
            self.server.accept()
            self.server.send_message(MSG_SUBSCRIBE, subscribe_reply)
            if event is not None:
                self.server.send_message(EVENT_BIT | EVENT_WINDOW, json.dumps(event))

        thread = threading.Thread(target=serve)
        thread.start()
        return thread

    def test_receives_a_window_event(self):
        thread = self._serve(
            '{"success": true}',
            {"change": "new", "container": {"id": 7, "app_id": "kitty"}},
        )
        watcher = WindowEventWatcher(self.server.path)
        self.addCleanup(watcher.close)
        event = watcher.get(timeout=1.0)
        thread.join(timeout=1.0)
        self.assertIsNotNone(event)
        self.assertEqual(event.change, "new")
        self.assertEqual(event.container["id"], 7)

    def test_rejected_subscription_raises(self):
        thread = self._serve('{"success": false}')
        with self.assertRaises(IpcError):
            WindowEventWatcher(self.server.path)
        thread.join(timeout=1.0)


if __name__ == "__main__":
    unittest.main()
