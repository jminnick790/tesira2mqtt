"""MQTT client wrapper — publishes state and subscribes to command topics."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager
from typing import Any

import aiomqtt

from ..config import MqttConfig

logger = logging.getLogger(__name__)

CommandHandler = Callable[[str, str], Coroutine[Any, Any, None]]


class MqttBridge:
    """Thin async wrapper around aiomqtt.

    - Publishes state updates and HA Discovery payloads (retained)
    - Subscribes to command topics and dispatches to registered handlers
    - Publishes LWT (Last Will) so HA marks entities unavailable on disconnect
    """

    def __init__(self, cfg: MqttConfig) -> None:
        self._cfg = cfg
        self._client: aiomqtt.Client | None = None
        self._handlers: dict[str, CommandHandler] = {}

    async def publish(self, topic: str, payload: str, retain: bool = False) -> None:
        assert self._client is not None
        await self._client.publish(topic, payload=payload, retain=retain)

    async def publish_retained(self, topic: str, payload: str) -> None:
        await self.publish(topic, payload, retain=True)

    def register_command_handler(self, topic: str, handler: CommandHandler) -> None:
        """Register a handler coroutine for an MQTT command topic."""
        self._handlers[topic] = handler

    @asynccontextmanager
    async def connect(self) -> AsyncIterator["MqttBridge"]:
        """Async context manager — connects, publishes online, yields, then cleans up."""
        will = aiomqtt.Will(
            topic="tesira/bridge/status",
            payload="offline",
            retain=True,
        )
        async with aiomqtt.Client(
            hostname=self._cfg.host,
            port=self._cfg.port,
            username=self._cfg.username or None,
            password=self._cfg.password or None,
            will=will,
        ) as client:
            self._client = client
            await self.publish_retained("tesira/bridge/status", "online")
            logger.info("MQTT connected to %s:%s", self._cfg.host, self._cfg.port)
            yield self
            await self.publish_retained("tesira/bridge/status", "offline")

    async def subscribe_commands(self, topics: list[str]) -> None:
        """Subscribe to all command topics."""
        assert self._client is not None
        for topic in topics:
            await self._client.subscribe(topic)
            logger.debug("Subscribed to %s", topic)

    async def listen(self) -> None:
        """Process incoming MQTT messages indefinitely."""
        assert self._client is not None
        async for message in self._client.messages:
            topic = str(message.topic)
            payload = message.payload.decode() if isinstance(message.payload, bytes) else str(message.payload)
            logger.debug("MQTT rx %s = %s", topic, payload)
            handler = self._handlers.get(topic)
            if handler:
                try:
                    await handler(topic, payload)
                except Exception as exc:
                    # Log and continue — a single bad command must not kill the listener
                    logger.error("Handler error for topic %s (payload=%r): %s", topic, payload, exc)
            else:
                logger.debug("No handler for topic %s", topic)
