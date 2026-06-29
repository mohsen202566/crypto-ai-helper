from __future__ import annotations

import logging


def setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    for name in ("httpx", "telegram", "apscheduler"):
        logging.getLogger(name).setLevel(logging.WARNING)
