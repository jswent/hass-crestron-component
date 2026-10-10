import asyncio
import struct
import logging

_LOGGER = logging.getLogger(__name__)
_CLOSE_TIMEOUT = 5


class CrestronXsig:
    def __init__(self):
        """Initialize CrestronXsig object"""
        self._digital = {}
        self._analog = {}
        self._serial = {}
        self._writer = None
        self._connection_task = None
        self._connections = {}
        self._stopping = False
        self._callbacks = set()
        self._server = None
        self._available = False
        self._sync_all_joins_callback = None

    async def listen(self, port):
        """Start TCP XSIG server listening on configured port"""
        self._stopping = False
        self._server = await asyncio.start_server(
            self.handle_connection, "0.0.0.0", port
        )
        addr = self._server.sockets[0].getsockname()
        _LOGGER.info(f"Listening on {addr}:{port}")

    async def stop(self):
        """Close the listener and all accepted connections before returning."""
        self._stopping = True
        self._available = False
        self._writer = None
        self._connection_task = None
        server = self._server
        if server is not None:
            server.close()

        # Close clients before Server.wait_closed(), which waits for them too.
        connections = tuple(self._connections.items())
        current_task = asyncio.current_task()
        for task, writer in connections:
            writer.close()
            if task is not current_task:
                task.cancel()
            else:
                # stop() can be called from a connection's own callback.
                await self._wait_writer_closed(writer)
        await asyncio.gather(
            *(task for task, _ in connections if task is not current_task),
            return_exceptions=True,
        )
        if server is not None:
            await server.wait_closed()
            self._server = None
        await self._notify_callbacks("available", "False")
        _LOGGER.info("TCP server and connections closed")

    async def _wait_writer_closed(self, writer):
        """Bound graceful closure when a peer stops draining queued writes."""
        try:
            await asyncio.wait_for(writer.wait_closed(), _CLOSE_TIMEOUT)
        except asyncio.TimeoutError:
            writer.transport.abort()
        except asyncio.CancelledError:
            writer.transport.abort()
            raise
        except (ConnectionError, OSError):
            _LOGGER.debug("Control system connection closed with an error", exc_info=True)

    async def _notify_callbacks(self, cbtype, value):
        """A failed or removed subscriber must not prevent other notifications."""
        for callback in tuple(self._callbacks):
            try:
                await callback(cbtype, value)
            except Exception:
                _LOGGER.exception("Error in Crestron callback for %s", cbtype)

    def register_sync_all_joins_callback(self, callback):
        """Allow callback to be registred for when control system requests an update to all joins"""
        _LOGGER.debug("Sync-all-joins callback registered")
        self._sync_all_joins_callback = callback

    def register_callback(self, callback):
        """Allow callbacks to be registered for when dict entries change"""
        self._callbacks.add(callback)

    def remove_callback(self, callback):
        """Allow callbacks to be de-registered"""
        self._callbacks.discard(callback)

    async def handle_connection(self, reader, writer):
        """Use the newest connection and always release its resources on exit."""
        task = asyncio.current_task()
        self._connections[task] = writer
        try:
            # A connection may have been accepted just before stop() closed the listener.
            if self._stopping:
                return

            previous_writer = self._writer
            previous_task = self._connection_task
            self._writer = writer
            self._connection_task = task
            if previous_writer is not None:
                previous_writer.close()
            if previous_task is not None:
                previous_task.cancel()

            peer = writer.get_extra_info("peername")
            _LOGGER.info("Control system connection from %s", peer)
            _LOGGER.debug("Sending update request")
            writer.write(b"\xfd")
            self._available = True
            await self._notify_callbacks("available", "True")
            await self._read_messages(reader, writer)
        except asyncio.IncompleteReadError:
            _LOGGER.debug("Control system disconnected during an XSIG message")
        except (ConnectionError, OSError):
            _LOGGER.debug("Control system connection lost", exc_info=True)
        except (asyncio.LimitOverrunError, UnicodeError):
            _LOGGER.warning("Invalid XSIG serial message", exc_info=True)
        except Exception:
            _LOGGER.exception("Error handling control system connection")
        finally:
            writer.close()
            try:
                # A retired handler must not clear a newer connection's state.
                if self._writer is writer:
                    self._writer = None
                    self._available = False
                    await self._notify_callbacks("available", "False")
            finally:
                try:
                    await self._wait_writer_closed(writer)
                finally:
                    if self._connection_task is task:
                        self._connection_task = None
                    self._connections.pop(task, None)

    async def _read_messages(self, reader, writer):
        """Read XSIG messages while this connection owns the hub."""
        while self._writer is writer and not self._stopping:
            data = await reader.read(1)
            if data:
                # Sync all joins request
                if data[0] == 0xFB:
                    _LOGGER.debug("Got update all joins request")
                    if self._sync_all_joins_callback is not None:
                        await self._sync_all_joins_callback()
                        _LOGGER.debug("Calling sync-all-joins callback")
                else:
                    data += await reader.readexactly(1)
                    # Digital Join
                    if (
                        data[0] & 0b11000000 == 0b10000000
                        and data[1] & 0b10000000 == 0b00000000
                    ):
                        header = struct.unpack("BB", data)
                        join = ((header[0] & 0b00011111) << 7 | header[1]) + 1
                        value = ~header[0] >> 5 & 0b1
                        self._digital[join] = True if value == 1 else False
                        _LOGGER.debug(f"Got Digital: {join} = {value}")
                        await self._notify_callbacks(f"d{join}", str(value))
                    # Analog Join
                    elif (
                        data[0] & 0b11001000 == 0b11000000
                        and data[1] & 0b10000000 == 0b00000000
                    ):
                        data += await reader.readexactly(2)
                        header = struct.unpack("BBBB", data)
                        join = ((header[0] & 0b00000111) << 7 | header[1]) + 1
                        value = (
                            (header[0] & 0b00110000) << 10 | header[2] << 7 | header[3]
                        )
                        self._analog[join] = value
                        _LOGGER.debug(f"Got Analog: {join} = {value}")
                        await self._notify_callbacks(f"a{join}", str(value))
                    # Serial Join
                    elif (
                        data[0] & 0b11111000 == 0b11001000
                        and data[1] & 0b10000000 == 0b00000000
                    ):
                        data += await reader.readuntil(b"\xff")
                        header = struct.unpack("BB", data[:2])
                        join = ((header[0] & 0b00000111) << 7 | header[1]) + 1
                        string = data[2:-1].decode("utf-8")
                        self._serial[join] = string
                        _LOGGER.debug(f"Got String: {join} = {string}")
                        await self._notify_callbacks(f"s{join}", string)
                    else:
                        _LOGGER.debug(f"Unknown Packet: {data.hex()}")
            else:
                _LOGGER.info("Control system disconnected")
                break

    def is_available(self):
        """Returns True if control system is connected"""
        return self._available

    def get_analog(self, join):
        """Return analog value for join"""
        return self._analog.get(join, 0)

    def get_digital(self, join):
        """Return digital value for join"""
        return self._digital.get(join, False)

    def get_serial(self, join):
        """Return serial value for join"""
        return self._serial.get(join, "")

    def set_analog(self, join, value):
        """Send Analog Join to Crestron XSIG symbol"""
        if self._writer and not self._writer.is_closing():
            data = struct.pack(
                ">BBBB",
                0b11000000 | (value >> 10 & 0b00110000) | (join - 1) >> 7,
                (join - 1) & 0b01111111,
                value >> 7 & 0b01111111,
                value & 0b01111111,
            )
            self._writer.write(data)
            _LOGGER.debug(f"Sending Analog: {join}, {value}")
        else:
            _LOGGER.info("Could not send.  No connection to hub")

    def set_digital(self, join, value):
        """Send Digital Join to Crestron XSIG symbol"""
        if self._writer and not self._writer.is_closing():
            data = struct.pack(
                ">BB",
                0b10000000 | (~value << 5 & 0b00100000) | (join - 1) >> 7,
                (join - 1) & 0b01111111,
            )
            self._writer.write(data)
            _LOGGER.debug(f"Sending Digital: {join}, {value}")
        else:
            _LOGGER.info("Could not send.  No connection to hub")

    def set_serial(self, join, string):
        """Send String Join to Crestron XSIG symbol"""
        if len(string) > 252:
            _LOGGER.info(f"Could not send. String too long ({len(string)}>252)")
            return
        elif self._writer and not self._writer.is_closing():
            data = struct.pack(
                ">BB", 0b11001000 | ((join - 1) >> 7), (join - 1) & 0b01111111
            )
            data += string.encode()
            data += b"\xff"
            self._writer.write(data)
            _LOGGER.debug(f"Sending Serial: {join}, {string}")
        else:
            _LOGGER.info("Could not send.  No connection to hub")
