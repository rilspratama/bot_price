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


BASE_URL = "https://api.coingecko.com/api/v3"
TIMEOUT_SECONDS = 15
RATE_LIMIT_COOLDOWN_SECONDS = 60
REQUEST_ERROR_COOLDOWN_SECONDS = 45
RETRY_ATTEMPTS = 2
RETRY_DELAY_SECONDS = 0.7
REQUEST_SEMAPHORE = asyncio.Semaphore(2)
COOLDOWN = ProviderCooldown("coingecko")


class CoinGeckoError(Exception):
    pass


class CoinGeckoNotFoundError(CoinGeckoError):
    pass


class CoinGeckoTransientError(CoinGeckoError):
    pass


@dataclass(frozen=True)
class CoinPrice:
    coin_id: str
    symbol: str
    name: str
    price_usd: float | None
    market_cap_usd: float | None
    market_cap_rank: int | None
    market_cap_change_24h: float | None
    volume_24h_usd: float | None
    high_24h_usd: float | None
    low_24h_usd: float | None
    price_change_24h: float | None
    circulating_supply: float | None
    total_supply: float | None
    max_supply: float | None
    ath_usd: float | None
    atl_usd: float | None


@dataclass(frozen=True)
class CoinSearchResult:
    coin_id: str
    symbol: str
    name: str
    market_cap_rank: int | None


async def async_search_coin_price(query: str) -> CoinPrice:
    results = await async_search_coins(query, limit=25)
    exact_symbol = _find_exact_symbol_match(query, results)
    result = exact_symbol or results[0]
    return await async_get_coin_price(result.coin_id)


async def async_search_coins(query: str, limit: int = 10) -> list[CoinSearchResult]:
    return await _async_search_coins(query.strip().lower(), limit)


@async_ttl_cache(
    ttl_seconds=300,
    maxsize=256,
    exception_ttl_seconds=120,
    cache_exceptions=(CoinGeckoNotFoundError,),
)
async def _async_search_coins(query: str, limit: int = 10) -> list[CoinSearchResult]:
    if not query:
        raise CoinGeckoNotFoundError("Symbol atau nama coin tidak boleh kosong.")

    payload = await _get_json(f"{BASE_URL}/search", {"query": query})
    coins = payload.get("coins")
    if not isinstance(coins, list) or not coins:
        raise CoinGeckoNotFoundError("Coin tidak ditemukan di CoinGecko.")

    results: list[CoinSearchResult] = []
    for coin in coins[:limit]:
        if not isinstance(coin, dict):
            continue

        coin_id = coin.get("id")
        symbol = coin.get("symbol")
        name = coin.get("name")
        if not coin_id or not symbol or not name:
            continue

        results.append(
            CoinSearchResult(
                coin_id=str(coin_id),
                symbol=str(symbol).upper(),
                name=str(name),
                market_cap_rank=_int_or_none(coin.get("market_cap_rank")),
            )
        )

    if not results:
        raise CoinGeckoNotFoundError("Format hasil pencarian CoinGecko tidak sesuai.")

    return results


def _find_exact_symbol_match(
    query: str, results: list[CoinSearchResult]
) -> CoinSearchResult | None:
    query_symbol = query.strip().upper()
    matches = [result for result in results if result.symbol == query_symbol]
    if not matches:
        return None

    return min(matches, key=lambda result: result.market_cap_rank or 10**9)


@async_ttl_cache(ttl_seconds=45, maxsize=256)
async def async_get_coin_price(coin_id: str) -> CoinPrice:
    coin_id = coin_id.strip().lower()
    if not coin_id:
        raise CoinGeckoError("Coin ID tidak boleh kosong.")

    payload = await _get_json(
        f"{BASE_URL}/coins/markets",
        {
            "vs_currency": "usd",
            "ids": coin_id,
            "price_change_percentage": "24h",
        },
    )

    if not isinstance(payload, list) or not payload:
        raise CoinGeckoError("Harga coin tidak ditemukan di CoinGecko.")

    coin = payload[0]
    if not isinstance(coin, dict):
        raise CoinGeckoError("Format harga CoinGecko tidak sesuai.")

    return CoinPrice(
        coin_id=str(coin.get("id") or coin_id),
        symbol=str(coin.get("symbol") or "-").upper(),
        name=str(coin.get("name") or "Unknown coin"),
        price_usd=_float_or_none(coin.get("current_price")),
        market_cap_usd=_float_or_none(coin.get("market_cap")),
        market_cap_rank=_int_or_none(coin.get("market_cap_rank")),
        market_cap_change_24h=_float_or_none(coin.get("market_cap_change_24h")),
        volume_24h_usd=_float_or_none(coin.get("total_volume")),
        high_24h_usd=_float_or_none(coin.get("high_24h")),
        low_24h_usd=_float_or_none(coin.get("low_24h")),
        price_change_24h=_float_or_none(coin.get("price_change_percentage_24h")),
        circulating_supply=_float_or_none(coin.get("circulating_supply")),
        total_supply=_float_or_none(coin.get("total_supply")),
        max_supply=_float_or_none(coin.get("max_supply")),
        ath_usd=_float_or_none(coin.get("ath")),
        atl_usd=_float_or_none(coin.get("atl")),
    )


async def _get_json(url: str, params: dict[str, str] | None = None) -> Any:
    endpoint = url_path(url)
    if COOLDOWN.is_active():
        raise CoinGeckoTransientError("CoinGecko sedang cooldown karena rate limit. Coba lagi beberapa saat.")

    started = start_timer()
    logger.info("request_start provider=coingecko endpoint=%s", endpoint)
    async with REQUEST_SEMAPHORE:
        client = get_async_client("coingecko", TIMEOUT_SECONDS)
        response = await _request_with_retry(client, url, params, endpoint, started)

    logger.info(
        "request_done provider=coingecko endpoint=%s status=%s duration_ms=%s",
        endpoint,
        response.status_code,
        elapsed_ms(started),
    )
    if response.status_code == 429:
        COOLDOWN.activate(RATE_LIMIT_COOLDOWN_SECONDS)
        raise CoinGeckoTransientError("Rate limit CoinGecko tercapai. Coba lagi beberapa saat.")
    if response.status_code == 404:
        raise CoinGeckoNotFoundError("Data tidak ditemukan di CoinGecko.")
    if not response.is_success:
        raise CoinGeckoTransientError(f"CoinGecko mengembalikan error HTTP {response.status_code}.")

    try:
        return response.json()
    except ValueError as exc:
        logger.warning(
            "json_error provider=coingecko endpoint=%s duration_ms=%s",
            endpoint,
            elapsed_ms(started),
        )
        raise CoinGeckoTransientError("Response CoinGecko bukan JSON valid.") from exc


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
                "request_retry provider=coingecko endpoint=%s attempt=%s duration_ms=%s error=%s",
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
                "request_error provider=coingecko endpoint=%s duration_ms=%s error=%s",
                endpoint,
                elapsed_ms(started),
                exc.__class__.__name__,
            )
            raise CoinGeckoTransientError(f"Gagal menghubungi CoinGecko: {exc}") from exc

    COOLDOWN.activate(REQUEST_ERROR_COOLDOWN_SECONDS)
    logger.warning(
        "request_error provider=coingecko endpoint=%s duration_ms=%s error=%s",
        endpoint,
        elapsed_ms(started),
        last_exc.__class__.__name__ if last_exc else "RequestError",
    )
    raise CoinGeckoTransientError("Gagal menghubungi CoinGecko setelah retry.") from last_exc


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
