from __future__ import annotations

import logging
import os
from time import monotonic
from urllib.parse import urlparse


def configure_logging() -> None:
    level_name = os.getenv("LOG_LEVEL", "INFO").strip().upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def start_timer() -> float:
    return monotonic()


def elapsed_ms(start: float) -> int:
    return round((monotonic() - start) * 1000)


def safe_query(value: object, max_length: int = 80) -> str:
    text = str(value).replace("\n", " ").replace("\r", " ").strip()
    if len(text) <= max_length:
        return text
    return f"{text[:max_length]}..."


def url_path(url: str) -> str:
    parsed = urlparse(url)
    return parsed.path or url
