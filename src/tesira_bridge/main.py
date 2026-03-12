"""tesira-bridge entry point."""

import asyncio
import logging
import sys
from pathlib import Path

from .config import load_config

logger = logging.getLogger(__name__)


async def run() -> None:
    """Main async run loop — wires TTP client, subscription manager, and MQTT bridge."""
    # TODO: instantiate TTP client, MQTT bridge, and coordinator
    logger.info("tesira-bridge starting")
    raise NotImplementedError("Bridge not yet implemented")


def main() -> None:
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logger.info("Shutting down")
        sys.exit(0)


if __name__ == "__main__":
    main()
