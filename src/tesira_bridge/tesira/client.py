"""Async TCP client for the Biamp Tesira Text Protocol (TTP).

Responsibilities:
- Maintain a persistent Telnet (raw TCP) connection to the Tesira
- Authenticate on connect (and reconnect)
- Send commands and correlate +OK / value responses via a FIFO queue
- Dispatch subscription notifications (lines starting with '!') to registered callbacks
- Reconnect automatically with exponential backoff on disconnect
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Any

from .protocol import parse_notification

logger = logging.getLogger(__name__)

# Type alias for subscription callbacks
NotificationCallback = Callable[[str, str], Coroutine[Any, Any, None]]
# Type alias for on-connect hooks (called after each successful auth)
ConnectHook = Callable[[], Coroutine[Any, Any, None]]


class TesiraClient:
    """Persistent async TTP client.

    Usage::

        client = TesiraClient(host="192.168.1.100", username="default", password="default")
        async with client:
            response = await client.send("LivingRoomFader get level 0")
            client.subscribe("lr_level", callback)
    """

    def __init__(
        self,
        host: str,
        port: int = 23,
        username: str = "default",
        password: str = "default",
        reconnect_interval_s: float = 5.0,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.reconnect_interval_s = reconnect_interval_s

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._response_queue: asyncio.Queue[str] = asyncio.Queue()
        self._callbacks: dict[str, NotificationCallback] = {}
        self._on_connect_hooks: list[ConnectHook] = []
        self._running = False
        self._recv_task: asyncio.Task | None = None
        self.connected = False

    # ── Connection lifecycle ──────────────────────────────────────────────────

    def add_connect_hook(self, hook: ConnectHook) -> None:
        """Register a coroutine to call after each successful authentication.

        Use this to re-subscribe to attributes after a reconnect.
        """
        self._on_connect_hooks.append(hook)

    async def connect(self) -> None:
        """Open connection, authenticate, and fire on-connect hooks."""
        logger.info("Connecting to Tesira at %s:%s", self.host, self.port)
        self._reader, self._writer = await asyncio.open_connection(self.host, self.port)
        await self._authenticate()
        self._recv_task = asyncio.create_task(self._recv_loop(), name="tesira-recv")
        self.connected = True
        logger.info("Connected and authenticated")
        for hook in self._on_connect_hooks:
            await hook()

    async def disconnect(self) -> None:
        """Gracefully close the connection."""
        self.connected = False
        self._running = False
        if self._recv_task:
            self._recv_task.cancel()
            try:
                await self._recv_task
            except asyncio.CancelledError:
                pass
        if self._writer:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:
                pass

    async def run_forever(self) -> None:
        """Connect and reconnect indefinitely. Call this as a long-running task."""
        self._running = True
        backoff = self.reconnect_interval_s
        while self._running:
            try:
                await self.connect()
                backoff = self.reconnect_interval_s  # reset on successful connect
                await self._recv_task  # blocks until disconnect
            except (OSError, asyncio.IncompleteReadError) as exc:
                logger.warning("Tesira connection lost: %s — retrying in %.0fs", exc, backoff)
            except Exception as exc:
                logger.error("Unexpected error: %s — retrying in %.0fs", exc, backoff)
            finally:
                await self.disconnect()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)  # exponential backoff, cap at 60s

    async def __aenter__(self) -> "TesiraClient":
        await self.connect()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.disconnect()

    # ── Command / response ────────────────────────────────────────────────────

    async def send(self, command: str) -> str:
        """Send a TTP command and await its response line.

        Returns the raw response string (e.g. '+OK', '+OK "value":-20.0').
        Raises RuntimeError if the Tesira returns a '-ERR' response.
        """
        if self._writer is None:
            raise RuntimeError("Not connected")
        self._writer.write((command + "\n").encode())
        await self._writer.drain()
        response = await self._response_queue.get()
        if response.startswith("-ERR"):
            raise RuntimeError(f"TTP error for '{command}': {response}")
        return response

    # ── Subscriptions ─────────────────────────────────────────────────────────

    def register_callback(self, publish_token: str, callback: NotificationCallback) -> None:
        """Register a coroutine callback for a subscription publish token."""
        self._callbacks[publish_token] = callback

    def unregister_callback(self, publish_token: str) -> None:
        self._callbacks.pop(publish_token, None)

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _authenticate(self) -> None:
        """Drain the Tesira banner and exchange credentials.

        Uses read() with a timeout rather than readline() because Telnet
        prompts like 'login: ' and 'Password: ' don't end with a newline,
        which would cause readline() to block indefinitely.
        """
        assert self._reader is not None

        await self._read_until("login", timeout=10.0)
        logger.debug("> %s", self.username)
        self._writer.write((self.username + "\n").encode())
        await self._writer.drain()

        await self._read_until("password", timeout=10.0)
        logger.debug("> ****")
        self._writer.write((self.password + "\n").encode())
        await self._writer.drain()

        # Tesira confirms successful login with a welcome message or a bare
        # '+OK'. Either signals the session is ready.
        await self._read_until_any(["welcome", "+OK"], timeout=10.0)

    async def _read_until(self, expected: str, timeout: float = 10.0) -> str:
        """Read chunks until the accumulated buffer contains `expected` (case-insensitive)."""
        return await self._read_until_any([expected], timeout=timeout)

    async def _read_until_any(self, candidates: list[str], timeout: float = 10.0) -> str:
        """Read chunks until the buffer contains any of the candidate strings."""
        assert self._reader is not None
        buf = ""
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise asyncio.TimeoutError(
                    f"Timeout waiting for {candidates!r} during auth. "
                    f"Received so far: {buf!r}"
                )
            try:
                chunk = await asyncio.wait_for(
                    self._reader.read(4096), timeout=min(remaining, 1.0)
                )
            except asyncio.TimeoutError:
                continue
            if not chunk:
                raise ConnectionError("Connection closed during authentication")
            decoded = chunk.decode(errors="replace")
            logger.debug("< %r", decoded)
            buf += decoded
            if any(c.lower() in buf.lower() for c in candidates):
                return buf

    async def _recv_loop(self) -> None:
        """Read lines from Tesira and route to response queue or subscription callbacks."""
        assert self._reader is not None
        try:
            while True:
                raw = await self._reader.readline()
                if not raw:
                    logger.warning("Tesira closed the connection")
                    return
                line = raw.decode(errors="replace").strip()
                if not line:
                    continue
                logger.debug("< %s", line)

                if line.startswith("!"):
                    await self._dispatch_notification(line)
                else:
                    await self._response_queue.put(line)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Recv loop error: %s", exc)

    async def _dispatch_notification(self, line: str) -> None:
        """Parse a subscription notification and invoke the registered callback."""
        notification = parse_notification(line)
        if notification is None:
            logger.warning("Failed to parse notification: %s", line)
            return
        callback = self._callbacks.get(notification.publish_token)
        if callback:
            await callback(notification.publish_token, notification.value)
        else:
            logger.debug("No callback for publishToken '%s'", notification.publish_token)
