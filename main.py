from __future__ import annotations

import argparse
import asyncio

from http_client import close_async_clients
from logging_utils import configure_logging
from coingecko import (
    CoinGeckoError,
    CoinPrice,
    CoinSearchResult,
    async_get_coin_price,
    async_search_coin_price,
    async_search_coins,
)


async def async_main() -> int:
    parser = argparse.ArgumentParser(
        description="Check cryptocurrency prices from CoinGecko by symbol, name, or coin ID."
    )
    parser.add_argument(
        "query",
        help="Coin symbol/name, for example: BTC, ETH, XLM. Can also be a coin ID when using --id.",
    )
    parser.add_argument(
        "--id",
        action="store_true",
        help="Treat query as a direct CoinGecko coin ID, for example: bitcoin, ethereum, stellar.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="Show multiple coin candidates instead of using the first result directly.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Number of candidates to show when using --list. Default: 10.",
    )

    args = parser.parse_args()

    try:
        if args.id:
            price = await async_get_coin_price(args.query)
        elif args.list:
            results = await async_search_coins(args.query, limit=max(args.limit, 1))
            print_coin_results(results)
            return 0
        else:
            price = await async_search_coin_price(args.query)
    except CoinGeckoError as exc:
        print(f"Error: {exc}")
        return 1

    print_coin_price(price)
    return 0


async def _run() -> int:
    try:
        return await async_main()
    finally:
        await close_async_clients()


def main() -> int:
    configure_logging()
    return asyncio.run(_run())


def print_coin_price(price: CoinPrice) -> None:
    print(f"Coin: {price.name} ({price.symbol})")
    print(f"ID: {price.coin_id}")
    print(f"Rank: {_format_rank(price.market_cap_rank)}")
    print(f"USD Price: {_format_money(price.price_usd)}")
    print(f"24h Change: {_format_percentage(price.price_change_24h)}")
    print(f"24h High USD: {_format_money(price.high_24h_usd)}")
    print(f"24h Low USD: {_format_money(price.low_24h_usd)}")
    print(f"24h Volume USD: {_format_money(price.volume_24h_usd)}")
    print(f"Market Cap USD: {_format_money(price.market_cap_usd)}")
    print(f"Market Cap 24h Change: {_format_money(price.market_cap_change_24h)}")
    print(f"Circulating Supply: {_format_number(price.circulating_supply)}")
    print(f"Total Supply: {_format_number(price.total_supply)}")
    print(f"Max Supply: {_format_number(price.max_supply)}")
    print(f"ATH USD: {_format_money(price.ath_usd)}")
    print(f"ATL USD: {_format_money(price.atl_usd)}")


def print_coin_results(results: list[CoinSearchResult]) -> None:
    for index, result in enumerate(results, start=1):
        rank = result.market_cap_rank if result.market_cap_rank is not None else "-"
        print(f"{index}. {result.name} ({result.symbol})")
        print(f"   ID: {result.coin_id}")
        print(f"   Market Cap Rank: {rank}")


def _format_rank(value: int | None) -> str:
    if value is None:
        return "-"
    return f"#{value}"


def _format_number(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:,.2f}".rstrip("0").rstrip(".")


def _format_money(value: float | None) -> str:
    if value is None:
        return "-"

    if abs(value) < 1:
        return f"${value:.10f}".rstrip("0").rstrip(".")
    return f"${value:,.2f}"


def _format_percentage(value: float | None) -> str:
    if value is None:
        return "-"

    return f"{value:.2f}%"


if __name__ == "__main__":
    raise SystemExit(main())
