from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from cache_utils import ProviderCooldown, async_ttl_cache
from http_client import get_async_client
from logging_utils import elapsed_ms, start_timer, url_path

logger = logging.getLogger(__name__)


BASE_URL = "https://api.geckoterminal.com/api/v2"
TIMEOUT_SECONDS = 15
RATE_LIMIT_COOLDOWN_SECONDS = 30
REQUEST_ERROR_COOLDOWN_SECONDS = 20
RETRY_ATTEMPTS = 2
RETRY_DELAY_SECONDS = 0.5
REQUEST_SEMAPHORE = asyncio.Semaphore(3)
COOLDOWN = ProviderCooldown("geckoterminal")


class GeckoTerminalError(Exception):
    pass


class GeckoTerminalNotFoundError(GeckoTerminalError):
    pass


class GeckoTerminalTransientError(GeckoTerminalError):
    pass


@dataclass(frozen=True)
class PoolPrice:
    name: str
    address: str
    network: str | None
    price_usd: str | None
    price_change_24h: str | None
    volume_24h_usd: str | None
    reserve_usd: str | None


async def async_search_pool_price(query: str) -> PoolPrice:
    return (await async_search_pool_prices(query, limit=1))[0]


async def async_search_pool_prices(query: str, limit: int = 10) -> list[PoolPrice]:
    return await _async_search_pool_prices(query.strip().lower(), limit)


@async_ttl_cache(
    ttl_seconds=60,
    maxsize=256,
    exception_ttl_seconds=60,
    cache_exceptions=(GeckoTerminalNotFoundError,),
)
async def _async_search_pool_prices(query: str, limit: int = 10) -> list[PoolPrice]:
    if not query:
        raise GeckoTerminalNotFoundError("Symbol atau kata pencarian tidak boleh kosong.")

    payload = await _get_json(f"{BASE_URL}/search/pools", {"query": query})
    data = payload.get("data")
    if not isinstance(data, list) or not data:
        raise GeckoTerminalNotFoundError("Pool tidak ditemukan dari symbol tersebut.")

    results: list[PoolPrice] = []
    for pool in data[:limit]:
        if not isinstance(pool, dict):
            continue

        attributes = pool.get("attributes")
        if not isinstance(attributes, dict):
            continue

        results.append(_parse_pool_price(pool, attributes))

    if not results:
        raise GeckoTerminalNotFoundError("Format hasil pencarian GeckoTerminal tidak sesuai.")

    return results


async def async_get_pool_price(network: str, pool_address: str) -> PoolPrice:
    network = network.strip().lower()
    pool_address = pool_address.strip()

    if not network:
        raise GeckoTerminalError("Network tidak boleh kosong.")
    if not pool_address:
        raise GeckoTerminalError("Pool address tidak boleh kosong.")

    payload = await _get_json(f"{BASE_URL}/networks/{network}/pools/{pool_address}")

    data = payload.get("data")
    if not isinstance(data, dict):
        raise GeckoTerminalError("Format response GeckoTerminal tidak sesuai.")

    attributes = data.get("attributes")
    if not isinstance(attributes, dict):
        raise GeckoTerminalError("Data pool tidak memiliki attributes.")

    return _parse_pool_price(data, attributes)


async def _get_json(url: str, params: dict[str, str] | None = None) -> dict[str, Any]:
    endpoint = url_path(url)
    if COOLDOWN.is_active():
        raise GeckoTerminalTransientError("GeckoTerminal sedang cooldown karena rate limit. Coba lagi beberapa saat.")

    started = start_timer()
    logger.info("request_start provider=geckoterminal endpoint=%s", endpoint)
    async with REQUEST_SEMAPHORE:
        client = get_async_client("geckoterminal", TIMEOUT_SECONDS)
        response = await _request_with_retry(client, url, params, endpoint, started)

    logger.info(
        "request_done provider=geckoterminal endpoint=%s status=%s duration_ms=%s",
        endpoint,
        response.status_code,
        elapsed_ms(started),
    )
    if response.status_code == 429:
        COOLDOWN.activate(RATE_LIMIT_COOLDOWN_SECONDS)
        raise GeckoTerminalTransientError("Rate limit GeckoTerminal tercapai. Coba lagi beberapa saat.")
    if response.status_code == 404:
        raise GeckoTerminalNotFoundError("Data tidak ditemukan di GeckoTerminal.")
    if not response.is_success:
        raise GeckoTerminalTransientError(
            f"GeckoTerminal mengembalikan error HTTP {response.status_code}."
        )

    try:
        payload = response.json()
    except ValueError as exc:
        logger.warning(
            "json_error provider=geckoterminal endpoint=%s duration_ms=%s",
            endpoint,
            elapsed_ms(started),
        )
        raise GeckoTerminalTransientError("Response GeckoTerminal bukan JSON valid.") from exc

    if not isinstance(payload, dict):
        raise GeckoTerminalTransientError("Format response GeckoTerminal tidak sesuai.")

    return payload


async def _request_with_retry(
    client: httpx.AsyncClient,
    url: str,
    params: dict[str, str] | None,
    endpoint: str,
    started: float,
) -> httpx.Response:
    last_exc: httpx.RequestError | None = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            return await client.get(url, params=params)
        except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ConnectError) as exc:
            last_exc = exc
            logger.warning(
                "request_retry provider=geckoterminal endpoint=%s attempt=%s duration_ms=%s error=%s",
                endpoint,
                attempt,
                elapsed_ms(started),
                exc.__class__.__name__,
            )
            if attempt < RETRY_ATTEMPTS:
                await asyncio.sleep(RETRY_DELAY_SECONDS)
        except httpx.RequestError as exc:
            COOLDOWN.activate(REQUEST_ERROR_COOLDOWN_SECONDS)
            logger.warning(
                "request_error provider=geckoterminal endpoint=%s duration_ms=%s error=%s",
                endpoint,
                elapsed_ms(started),
                exc.__class__.__name__,
            )
            raise GeckoTerminalTransientError(f"Gagal menghubungi GeckoTerminal: {exc}") from exc

    COOLDOWN.activate(REQUEST_ERROR_COOLDOWN_SECONDS)
    logger.warning(
        "request_error provider=geckoterminal endpoint=%s duration_ms=%s error=%s",
        endpoint,
        elapsed_ms(started),
        last_exc.__class__.__name__ if last_exc else "RequestError",
    )
    raise GeckoTerminalTransientError("Gagal menghubungi GeckoTerminal setelah retry.") from last_exc


def _parse_pool_price(data: dict[str, Any], attributes: dict[str, Any]) -> PoolPrice:
    price_change = attributes.get("price_change_percentage")
    volume = attributes.get("volume_usd")
    pool_id = str(data.get("id") or "-")

    return PoolPrice(
        name=str(attributes.get("name") or "Unknown pool"),
        address=pool_id,
        network=_network_from_pool_id(pool_id),
        price_usd=_string_or_none(attributes.get("base_token_price_usd")),
        price_change_24h=_nested_string_or_none(price_change, "h24"),
        volume_24h_usd=_nested_string_or_none(volume, "h24"),
        reserve_usd=_string_or_none(attributes.get("reserve_in_usd")),
    )


def _network_from_pool_id(pool_id: str) -> str | None:
    if "_" not in pool_id:
        return None
    network, _ = pool_id.split("_", 1)
    return network or None


def _nested_string_or_none(value: Any, key: str) -> str | None:
    if not isinstance(value, dict):
        return None
    return _string_or_none(value.get(key))


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)
