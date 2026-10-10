"""TCP protocol regressions, runnable without Home Assistant installed."""

import asyncio
import importlib.util
from pathlib import Path
import unittest


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


if __name__ == "__main__":
    unittest.main()
