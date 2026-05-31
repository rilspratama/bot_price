from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from cache_utils import async_ttl_cache
from http_client import get_async_client
from logging_utils import elapsed_ms, start_timer, url_path

logger = logging.getLogger(__name__)

_CURRENCY_CODE_RE = re.compile(r"^[a-z]{2,5}$")


PRIMARY_URL = "https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@latest/v1/currencies"
FALLBACK_URL = "https://latest.currency-api.pages.dev/v1/currencies"
TIMEOUT_SECONDS = 15


class CurrencyError(Exception):
    pass


FIAT_PRIORITY_CODES = {
    "aed",
    "aud",
    "brl",
    "cad",
    "chf",
    "cny",
    "eur",
    "gbp",
    "hkd",
    "idr",
    "inr",
    "jpy",
    "krw",
    "myr",
    "php",
    "sgd",
    "thb",
    "try",
    "usd",
    "vnd",
}


def is_priority_fiat_currency(currency: str) -> bool:
    currency = currency.strip().lower()
    return currency in FIAT_PRIORITY_CODES


def _validate_currency_code(code: str, label: str) -> str:
    code = code.strip().lower()
    if not code:
        raise CurrencyError(f"{label} currency cannot be empty.")
    if not _CURRENCY_CODE_RE.match(code):
        raise CurrencyError(
            f"Invalid currency code: {code.upper()}. "
            "Currency codes should be 2–5 letters (e.g. USD, EUR)."
        )
    return code


async def async_convert_currency(amount: float, from_currency: str, to_currency: str) -> float:
    from_currency = _validate_currency_code(from_currency, "Source")
    to_currency = _validate_currency_code(to_currency, "Target")

    rates = await async_get_exchange_rates(from_currency)
    rate = rates.get(to_currency)
    if rate is None:
        raise CurrencyError(
            f"Currency not found: {to_currency.upper()}. "
            "Please check the currency code and try again."
        )

    return amount * rate


@async_ttl_cache(ttl_seconds=21600, maxsize=64)
async def async_get_exchange_rates(base_currency: str) -> dict[str, float]:
    base_currency = _validate_currency_code(base_currency, "Base")

    payload = await _get_json(
        f"{PRIMARY_URL}/{base_currency}.json", f"{FALLBACK_URL}/{base_currency}.json"
    )
    rates = payload.get(base_currency)
    if not isinstance(rates, dict):
        raise CurrencyError(f"Currency rate format for {base_currency.upper()} is invalid.")

    parsed_rates: dict[str, float] = {}
    for code, rate in rates.items():
        try:
            parsed_rates[str(code).lower()] = float(rate)
        except (TypeError, ValueError):
            continue

    return parsed_rates


def _friendly_network_error(exc: httpx.HTTPError) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "Currency service timed out. Please try again later."
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
        return "Could not connect to currency service. Please try again later."
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status == 404:
            return (
                "Currency not found. "
                "Please check the currency code and try again."
            )
        if 500 <= status < 600:
            return "Currency service is temporarily unavailable. Please try again later."
        return f"Currency service returned an error (HTTP {status}). Please try again later."
    return "Could not retrieve currency data. Please try again later."


async def _get_json(primary_url: str, fallback_url: str) -> dict[str, Any]:
    started = start_timer()
    primary_endpoint = url_path(primary_url)
    fallback_endpoint = url_path(fallback_url)
    client = get_async_client("currency", TIMEOUT_SECONDS)
    try:
        logger.info("request_start provider=currency source=primary endpoint=%s", primary_endpoint)
        response = await client.get(primary_url)
        response.raise_for_status()
        logger.info(
            "request_done provider=currency source=primary endpoint=%s status=%s duration_ms=%s",
            primary_endpoint,
            response.status_code,
            elapsed_ms(started),
        )
    except httpx.HTTPError as primary_exc:
        logger.warning(
            "request_error provider=currency source=primary endpoint=%s duration_ms=%s error=%s",
            primary_endpoint,
            elapsed_ms(started),
            primary_exc.__class__.__name__,
        )
        try:
            fallback_started = start_timer()
            logger.info("request_start provider=currency source=fallback endpoint=%s", fallback_endpoint)
            response = await client.get(fallback_url)
            response.raise_for_status()
            logger.info(
                "request_done provider=currency source=fallback endpoint=%s status=%s duration_ms=%s",
                fallback_endpoint,
                response.status_code,
                elapsed_ms(fallback_started),
            )
        except httpx.HTTPError as exc:
            logger.warning(
                "request_error provider=currency source=fallback endpoint=%s duration_ms=%s error=%s",
                fallback_endpoint,
                elapsed_ms(started),
                exc.__class__.__name__,
            )
            raise CurrencyError(_friendly_network_error(exc)) from exc

    try:
        payload = response.json()
    except ValueError as exc:
        logger.warning("json_error provider=currency duration_ms=%s", elapsed_ms(started))
        raise CurrencyError("Currency API returned an invalid response. Please try again later.") from exc

    if not isinstance(payload, dict):
        raise CurrencyError("Currency API returned an unexpected response format. Please try again later.")

    return payload
