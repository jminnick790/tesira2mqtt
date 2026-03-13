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

    _RESPONSE_TIMEOUT = 5.0  # seconds to wait for a TTP response before giving up

    async def send(self, command: str) -> str:
        """Send a TTP command and await its response line.

        Returns the raw response string (e.g. '+OK', '+OK "value":-20.0').
        Raises RuntimeError if the Tesira returns a '-ERR' response or if no
        response arrives within _RESPONSE_TIMEOUT seconds.
        """
        if self._writer is None:
            raise RuntimeError("Not connected")
        self._writer.write((command + "\n").encode())
        await self._writer.drain()
        try:
            response = await asyncio.wait_for(
                self._response_queue.get(), timeout=self._RESPONSE_TIMEOUT
            )
        except asyncio.TimeoutError:
            raise RuntimeError(
                f"Timeout waiting for response to '{command}' "
                f"(>{self._RESPONSE_TIMEOUT:.0f}s)"
            )
        if response.startswith("-ERR"):
            raise RuntimeError(f"TTP error for '{command}': {response}")
        return response

    # ── Subscriptions ─────────────────────────────────────────────────────────

    def register_callback(self, publish_token: str, callback: NotificationCallback) -> None:
        """Register a coroutine callback for a subscription publish token."""
        self._callbacks[publish_token] = callback

    def unregister_callback(self, publish_token: str) -> None:
        self._callbacks.pop(publish_token, None)

    # ── Telnet IAC negotiation ────────────────────────────────────────────────

    # Telnet command bytes
    _IAC  = 0xFF
    _DONT = 0xFE
    _DO   = 0xFD
    _WONT = 0xFC
    _WILL = 0xFB
    _SB   = 0xFA  # subnegotiation begin
    _SE   = 0xF0  # subnegotiation end

    async def _strip_iac(self, data: bytes) -> str:
        """Strip Telnet IAC sequences from raw bytes, responding WONT/DONT to all
        DO/WILL option requests so the Tesira proceeds past negotiation to the
        login prompt.

        Returns the printable text content with all IAC sequences removed.
        """
        i = 0
        text = bytearray()
        response = bytearray()

        while i < len(data):
            b = data[i]
            if b != self._IAC:
                text.append(b)
                i += 1
                continue

            # Need at least one more byte for the command
            if i + 1 >= len(data):
                break

            cmd = data[i + 1]

            if cmd == self._IAC:
                # Escaped 0xFF — literal byte in stream
                text.append(self._IAC)
                i += 2

            elif cmd in (self._DO, self._WILL):
                # Server asking us to DO or announcing it WILL do something —
                # refuse both; we don't need any Telnet options.
                if i + 2 < len(data):
                    opt = data[i + 2]
                    reply = self._WONT if cmd == self._DO else self._DONT
                    response.extend([self._IAC, reply, opt])
                    logger.debug("IAC %s %d → replying %s",
                                 "DO" if cmd == self._DO else "WILL", opt,
                                 "WONT" if reply == self._WONT else "DONT")
                    i += 3
                else:
                    break  # incomplete sequence — stop here

            elif cmd in (self._DONT, self._WONT):
                # Server acknowledging our refusal — no reply needed
                i += 3

            elif cmd == self._SB:
                # Subnegotiation block — skip everything until SE
                i += 2
                while i < len(data) and data[i] != self._SE:
                    i += 1
                i += 1  # skip SE itself

            else:
                # Unknown 2-byte command
                i += 2

        if response and self._writer:
            self._writer.write(bytes(response))
            await self._writer.drain()

        return text.decode(errors="replace")

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _authenticate(self) -> None:
        """Drain the Tesira banner and optionally exchange credentials.

        After IAC negotiation the Tesira either:
        - Shows a 'login:' prompt  → credentials required (security enabled)
        - Shows the welcome banner → no auth configured, session ready immediately

        Both cases are handled by waiting for whichever arrives first.
        """
        assert self._reader is not None

        buf = await self._read_until_any(["login", "welcome", "server"], timeout=15.0)

        if "login" in buf.lower():
            logger.debug("> %s", self.username)
            self._writer.write((self.username + "\n").encode())
            await self._writer.drain()

            await self._read_until("password", timeout=10.0)
            logger.debug("> ****")
            self._writer.write((self.password + "\n").encode())
            await self._writer.drain()

            await self._read_until_any(["welcome", "server", "+OK"], timeout=10.0)
            logger.debug("Authenticated with credentials")
        else:
            logger.debug("No login prompt — Tesira has no auth configured, session ready")

    async def _read_until(self, expected: str, timeout: float = 10.0) -> str:
        """Read chunks until the accumulated text buffer contains `expected`."""
        return await self._read_until_any([expected], timeout=timeout)

    async def _read_until_any(self, candidates: list[str], timeout: float = 10.0) -> str:
        """Read raw chunks, strip IAC sequences, accumulate text until any
        candidate string appears (case-insensitive)."""
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
                raw = await asyncio.wait_for(
                    self._reader.read(4096), timeout=min(remaining, 1.0)
                )
            except asyncio.TimeoutError:
                continue
            if not raw:
                raise ConnectionError("Connection closed during authentication")
            text = await self._strip_iac(raw)
            if text:
                logger.debug("< %r", text)
            buf += text
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
                elif line.startswith(("+", "-")):
                    await self._response_queue.put(line)
                else:
                    # Echoed command or unsolicited banner text — discard.
                    # All legitimate TTP responses begin with '+' or '-'.
                    logger.debug("Discarding non-response line: %s", line)
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
