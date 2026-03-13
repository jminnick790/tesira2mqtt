"""tesira2mqtt entry point."""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from .bridge.coordinator import Coordinator
from .bridge.discovery import all_discovery_payloads
from .bridge.mqtt import MqttBridge
from .config import load_config
from .tesira.client import TesiraClient

logger = logging.getLogger(__name__)


async def run() -> None:
    cfg = load_config()

    tesira = TesiraClient(
        host=cfg.tesira.host,
        port=cfg.tesira.port,
        username=cfg.tesira.username,
        password=cfg.tesira.password,
        reconnect_interval_s=cfg.tesira.reconnect_interval_s,
    )
    mqtt = MqttBridge(cfg.mqtt)

    # Coordinator registers itself as a TesiraClient on_connect hook,
    # so subscriptions and state sync run automatically after every (re)connect.
    _coordinator = Coordinator(cfg, tesira, mqtt)

    async with mqtt.connect():
        # Publish HA Discovery payloads (retained) so entities appear in HA
        # immediately, even before the Tesira connection is established.
        logger.info("Publishing HA MQTT Discovery payloads")
        for topic, payload in all_discovery_payloads(cfg):
            await mqtt.publish_retained(topic, payload)

        # Subscribe to all MQTT command topics
        command_topics = (
            [f"tesira/zone/{z.id}/level/set" for z in cfg.zones]
            + [f"tesira/zone/{z.id}/mute/set" for z in cfg.zones]
            + [f"tesira/routing/{r.id}/set" for r in cfg.routing]
        )
        await mqtt.subscribe_commands(command_topics)

        # Run TTP client and MQTT listener concurrently.
        # TesiraClient.run_forever() reconnects indefinitely;
        # MqttBridge.listen() processes incoming commands.
        logger.info("Starting TTP client and MQTT listener")
        await asyncio.gather(
            tesira.run_forever(),
            mqtt.listen(),
        )


def main() -> None:
    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        stream=sys.stdout,
    )

    logger.info("tesira2mqtt starting")
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logger.info("Shutting down")
        sys.exit(0)
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        sys.exit(1)
    except Exception as exc:
        logger.exception("Fatal error: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
