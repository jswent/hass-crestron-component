"""TCP protocol regressions, runnable without Home Assistant installed."""

import asyncio
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "crestron_xsig",
    Path(__file__).resolve().parents[1] / "custom_components/crestron/crestron.py",
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
CrestronXsig = MODULE.CrestronXsig


class FakeWriter:
    """Record writes and closure without opening a network socket."""

    def __init__(self):
        self.writes = []
        self.closed = False
        self.waited_closed = False
        self.transport = self
        self.aborted = False

    def get_extra_info(self, name):
        return ("127.0.0.1", 12345)

    def write(self, data):
        if self.closed:
            raise ConnectionError("Writer is closed")
        self.writes.append(data)

    def is_closing(self):
        return self.closed

    def close(self):
        self.closed = True

    async def wait_closed(self):
        self.waited_closed = True

    def abort(self):
        self.aborted = True
        self.closed = True


class FramingTests(unittest.IsolatedAsyncioTestCase):
    async def receive(self, chunks):
        hub = CrestronXsig()
        reader = asyncio.StreamReader()
        writer = FakeWriter()
        events = []

        async def record(*event):
            events.append(event)

        async def sync():
            events.append(("sync",))

        hub.register_callback(record)
        hub.register_sync_all_joins_callback(sync)
        task = asyncio.create_task(hub.handle_connection(reader, writer))
        try:
            for chunk in chunks:
                reader.feed_data(chunk)
                # Let the handler consume each fragment before the next arrives.
                await asyncio.sleep(0)
            reader.feed_eof()
            await asyncio.wait_for(task, 1)
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(writer.writes, [b"\xfd"])
        return hub, events

    async def test_each_frame_split_at_every_byte_boundary(self):
        frames = (
            (b"\x80\x00", ("d1", "1")),
            (b"\xa0\x00", ("d1", "0")),
            (b"\xc0\x00\x01\x02", ("a1", "130")),
            (b"\xf7\x7f\x7f\x7f", ("a1024", "65535")),
            (b"\xc8\x00hello\xff", ("s1", "hello")),
            (b"\xc8\x00\xc3\xa9\xff", ("s1", "é")),
        )
        for frame, event in frames:
            for split in range(1, len(frame)):
                with self.subTest(frame=frame, split=split):
                    _, events = await self.receive([frame[:split], frame[split:]])
                    self.assertEqual(
                        events, [("available", "True"), event, ("available", "False")]
                    )

    async def test_coalesced_frames_and_sync_request(self):
        hub, events = await self.receive(
            [b"\xc0\x00\x01\x02\xfb\x80\x00\xc8\x00hello\xff"]
        )
        self.assertEqual(hub.get_analog(1), 130)
        self.assertTrue(hub.get_digital(1))
        self.assertEqual(hub.get_serial(1), "hello")
        self.assertEqual(
            events,
            [
                ("available", "True"),
                ("a1", "130"),
                ("sync",),
                ("d1", "1"),
                ("s1", "hello"),
                ("available", "False"),
            ],
        )

    async def test_multiple_frames_arriving_one_byte_at_a_time(self):
        _, events = await self.receive(
            [bytes([byte]) for byte in b"\x80\x00\xc0\x00\x01\x02\xc8\x00ok\xff"]
        )
        self.assertEqual(
            events[1:-1], [("d1", "1"), ("a1", "130"), ("s1", "ok")]
        )


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.hub = CrestronXsig()
        self.events = []
        self.hub.register_callback(self.record)

    async def record(self, *event):
        self.events.append(event)

    async def connect(self, reader=None, writer=None):
        reader = reader if reader is not None else asyncio.StreamReader()
        writer = writer if writer is not None else FakeWriter()
        task = asyncio.create_task(self.hub.handle_connection(reader, writer))

        async def cleanup():
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        self.addAsyncCleanup(cleanup)
        await asyncio.sleep(0)
        return reader, writer, task

    def assert_disconnected(self, writer):
        self.assertFalse(self.hub.is_available())
        self.assertIsNone(self.hub._writer)
        self.assertIsNone(self.hub._connection_task)
        self.assertEqual(self.hub._connections, {})
        self.assertTrue(writer.closed)
        self.assertTrue(writer.waited_closed)
        writes = list(writer.writes)
        self.hub.set_digital(1, True)
        self.hub.set_analog(1, 130)
        self.hub.set_serial(1, "ignored")
        self.assertEqual(writer.writes, writes)

    async def test_clean_eof_closes_and_forgets_writer(self):
        reader, writer, task = await self.connect()
        reader.feed_eof()
        await asyncio.wait_for(task, 1)
        self.assert_disconnected(writer)
        self.assertEqual(self.events, [("available", "True"), ("available", "False")])

    async def test_eof_at_every_incomplete_frame_boundary(self):
        for frame in (b"\x80\x00", b"\xc0\x00\x01\x02", b"\xc8\x00ok\xff"):
            for split in range(1, len(frame)):
                with self.subTest(frame=frame, split=split):
                    reader, writer, task = await self.connect()
                    reader.feed_data(frame[:split])
                    reader.feed_eof()
                    await asyncio.wait_for(task, 1)
                    self.assert_disconnected(writer)
        self.assertEqual(self.hub._digital, {})
        self.assertEqual(self.hub._analog, {})
        self.assertEqual(self.hub._serial, {})
        self.assertTrue(all(event[0] == "available" for event in self.events))

    async def test_connection_reset_cleans_up(self):
        reader, writer, task = await self.connect()
        reader.set_exception(ConnectionResetError("peer reset"))
        await asyncio.wait_for(task, 1)
        self.assert_disconnected(writer)

    async def test_cancellation_cleans_up_and_propagates(self):
        _, writer, task = await self.connect()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assert_disconnected(writer)

    async def test_invalid_serial_closes_connection(self):
        for data in (b"\xc8\x00\x80\xff", b"\xc8\x00unterminated"):
            with self.subTest(data=data):
                reader, writer, task = await self.connect(asyncio.StreamReader(limit=8))
                with self.assertLogs(MODULE.__name__, level="WARNING"):
                    reader.feed_data(data)
                    await asyncio.wait_for(task, 1)
                self.assert_disconnected(writer)

    async def test_callback_failures_do_not_break_receiving_or_cleanup(self):
        async def broken_callback(*event):
            raise RuntimeError("subscriber failed")

        self.hub.register_callback(broken_callback)
        with self.assertLogs(MODULE.__name__, level="ERROR"):
            reader, writer, task = await self.connect()
            reader.feed_data(b"\x80\x00\xc0\x00\x01\x02")
            reader.feed_eof()
            await asyncio.wait_for(task, 1)
        self.assertTrue(self.hub.get_digital(1))
        self.assertEqual(self.hub.get_analog(1), 130)
        self.assertIn(("d1", "1"), self.events)
        self.assertIn(("a1", "130"), self.events)
        self.assertEqual(self.events[-1], ("available", "False"))
        self.assert_disconnected(writer)

    async def test_callback_can_unregister_during_notification(self):
        async def remove_self(*event):
            self.hub.remove_callback(remove_self)
            await asyncio.sleep(0)

        self.hub.register_callback(remove_self)
        reader, writer, task = await self.connect()
        reader.feed_data(b"\x80\x00")
        reader.feed_eof()
        await asyncio.wait_for(task, 1)
        self.assertIn(("d1", "1"), self.events)
        self.assert_disconnected(writer)

    async def test_sync_callback_failure_cleans_up(self):
        async def broken_sync():
            raise RuntimeError("sync failed")

        self.hub.register_sync_all_joins_callback(broken_sync)
        reader, writer, task = await self.connect()
        with self.assertLogs(MODULE.__name__, level="ERROR"):
            reader.feed_data(b"\xfb")
            await asyncio.wait_for(task, 1)
        self.assert_disconnected(writer)

    async def test_replacement_retires_old_handler_without_marking_new_unavailable(self):
        callback_started = asyncio.Event()

        async def block_old_callback(cbtype, value):
            if cbtype == "d1":
                callback_started.set()
                await asyncio.Event().wait()

        self.hub.register_callback(block_old_callback)
        old_reader, old_writer, old_task = await self.connect()
        old_reader.feed_data(b"\x80\x00")
        await asyncio.wait_for(callback_started.wait(), 1)
        reader, writer, task = await self.connect()
        with self.assertRaises(asyncio.CancelledError):
            await old_task
        old_reader.feed_data(b"\xc0\x00\x7f\x7f")
        old_reader.feed_eof()
        await asyncio.sleep(0)
        self.assertTrue(old_writer.closed)
        self.assertTrue(old_writer.waited_closed)
        self.assertTrue(self.hub.is_available())
        self.assertIs(self.hub._writer, writer)
        self.assertNotIn(("available", "False"), self.events)
        self.assertEqual(self.hub.get_analog(1), 0)
        self.hub.set_digital(2, True)
        self.assertEqual(writer.writes, [b"\xfd", b"\x80\x01"])
        self.assertEqual(old_writer.writes, [b"\xfd"])
        reader.feed_data(b"\xc0\x00\x01\x02")
        reader.feed_eof()
        await asyncio.wait_for(task, 1)
        self.assertEqual(self.hub.get_analog(1), 130)
        self.assert_disconnected(writer)

    async def test_replacement_cancels_pending_disconnect_notification(self):
        disconnect_started = asyncio.Event()
        release_disconnect = asyncio.Event()
        notifications = []

        async def slow_subscriber(cbtype, value):
            if cbtype == "available":
                if value == "False":
                    disconnect_started.set()
                    await release_disconnect.wait()
                notifications.append(value)

        self.hub.register_callback(slow_subscriber)
        old_reader, old_writer, old_task = await self.connect()
        old_reader.feed_eof()
        await asyncio.wait_for(disconnect_started.wait(), 1)
        reader, writer, task = await self.connect()
        release_disconnect.set()
        await asyncio.gather(old_task, return_exceptions=True)
        self.assertEqual(notifications, ["True", "True"])
        self.assertTrue(old_writer.closed)
        self.assertTrue(self.hub.is_available())
        reader.feed_eof()
        await asyncio.wait_for(task, 1)
        self.assert_disconnected(writer)

    async def test_shutdown_aborts_a_writer_that_cannot_flush(self):
        class BlockedWriter(FakeWriter):
            async def wait_closed(self):
                self.waited_closed = True
                await asyncio.Event().wait()

        _, writer, _ = await self.connect(writer=BlockedWriter())
        with patch.object(MODULE, "_CLOSE_TIMEOUT", 0.01, create=True):
            await asyncio.wait_for(self.hub.stop(), 1)
        self.assertTrue(writer.aborted)
        self.assert_disconnected(writer)

    async def test_stop_closes_clients_before_waiting_for_server(self):
        _, writer, task = await self.connect()
        hub = self.hub

        class Server:
            closed = False
            waited = False

            def close(self):
                self.closed = True

            async def wait_closed(self):
                # Modern asyncio servers wait for accepted connections too.
                if not writer.closed or not task.done() or hub._connections:
                    raise AssertionError("Server was awaited before client cleanup")
                self.waited = True

        server = self.hub._server = Server()

        async def broken_shutdown_callback(cbtype, value):
            if value == "False":
                raise RuntimeError("shutdown subscriber failed")

        self.hub.register_callback(broken_shutdown_callback)
        with self.assertLogs(MODULE.__name__, level="ERROR"):
            await asyncio.wait_for(self.hub.stop(), 1)
        self.assertTrue(server.closed)
        self.assertTrue(server.waited)
        self.assertIsNone(self.hub._server)
        self.assert_disconnected(writer)
        self.hub.remove_callback(broken_shutdown_callback)
        await asyncio.wait_for(self.hub.stop(), 1)

    async def test_handler_starting_after_stop_is_rejected(self):
        await self.hub.stop()
        _, writer, task = await self.connect()
        await asyncio.wait_for(task, 1)
        self.assertEqual(writer.writes, [])
        self.assertNotIn(("available", "True"), self.events)
        self.assert_disconnected(writer)

    async def test_stop_from_connection_callback_does_not_await_itself(self):
        async def stop_on_join(cbtype, value):
            if cbtype == "d1":
                await self.hub.stop()

        self.hub.register_callback(stop_on_join)
        reader, writer, task = await self.connect()
        reader.feed_data(b"\x80\x00")
        await asyncio.wait_for(task, 1)
        self.assert_disconnected(writer)


class SocketLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_shutdown_disconnects_real_client_and_allows_restart(self):
        hub = CrestronXsig()
        self.addAsyncCleanup(hub.stop)
        # Bind only loopback for this test instead of the integration's LAN listener.
        hub._server = await asyncio.start_server(hub.handle_connection, "127.0.0.1", 0)
        port = hub._server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)

        async def close_client():
            writer.close()
            await writer.wait_closed()

        self.addAsyncCleanup(close_client)
        self.assertEqual(await asyncio.wait_for(reader.readexactly(1), 1), b"\xfd")
        await asyncio.wait_for(hub.stop(), 1)
        self.assertEqual(await asyncio.wait_for(reader.read(), 1), b"")
        self.assertFalse(hub.is_available())
        self.assertIsNone(hub._writer)
        self.assertEqual(hub._connections, {})
        # The listen path must reset the stop flag when starting again.
        start_server = asyncio.start_server

        async def start_on_loopback(callback, host, requested_port):
            return await start_server(callback, "127.0.0.1", requested_port)

        with patch.object(MODULE.asyncio, "start_server", start_on_loopback):
            await hub.listen(port)
        restarted_reader, restarted_writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            self.assertEqual(
                await asyncio.wait_for(restarted_reader.readexactly(1), 1), b"\xfd"
            )
            self.assertTrue(hub.is_available())
        finally:
            restarted_writer.close()
            await restarted_writer.wait_closed()


if __name__ == "__main__":
    unittest.main()
