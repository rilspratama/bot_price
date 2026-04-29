from __future__ import annotations

import ast
import asyncio
import html
import logging
import operator
import os
import re
from dataclasses import dataclass
from time import monotonic

from dotenv import load_dotenv
from pyrogram import Client, enums, filters
from pyrogram.handlers import CallbackQueryHandler, MessageHandler
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from coingecko import (
    CoinGeckoError,
    CoinPrice,
    CoinSearchResult,
    async_search_coin_price,
    async_search_coins,
)
from currency import CurrencyError, async_convert_currency, is_priority_fiat_currency
from gas_tracker import CHAINS as GAS_CHAINS
from gas_tracker import GasPrice, GasTrackerError, async_get_gas_prices
from geckoterminal import GeckoTerminalError, PoolPrice, async_search_pool_prices
from http_client import close_async_clients
from logging_utils import configure_logging, safe_query
from rpc_balance import NativeBalance, RpcBalanceError, async_get_balances, async_get_solana_balance


logger = logging.getLogger(__name__)
app: Client | None = None


DELETE_CALLBACK = "delete_message"
LIST_CALLBACK_PREFIX = "list:"
DEX_CALLBACK_PREFIX = "dex:"
GAS_CALLBACK_PREFIX = "gas:"
TEXT_RATE_LIMIT_SECONDS = 2.5
FINAL_RESPONSE_CACHE_TTL_SECONDS = 20
MAX_MATH_EXPRESSION_LENGTH = 80
MAX_MATH_RESULT_ABS = 10**18
MATH_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
last_text_request_at: dict[int, float] = {}


@dataclass(frozen=True)
class FinalResponse:
    text: str
    reply_type: str
    keyboard_value: str | None = None


@dataclass(frozen=True)
class UsdPriceSource:
    query: str
    symbol: str
    name: str
    price_usd: float
    provider: str


final_response_cache: dict[str, tuple[float, FinalResponse]] = {}


def h(value: object) -> str:
    return html.escape(str(value), quote=False)


def bold(value: object) -> str:
    return f"<b>{h(value)}</b>"


def italic(value: object) -> str:
    return f"<i>{h(value)}</i>"


def underline(value: object) -> str:
    return f"<u>{h(value)}</u>"


def code(value: object) -> str:
    return f"<code>{h(value)}</code>"


def format_error(exc: Exception) -> str:
    return f"{bold('Error')}: {h(exc)}"


async def reply_with_delete(message: Message, text: str) -> None:
    await message.reply_text(text, reply_markup=delete_keyboard(), parse_mode=enums.ParseMode.HTML)


async def reply_price(message: Message, text: str, query: str) -> None:
    await message.reply_text(text, reply_markup=price_keyboard(query), parse_mode=enums.ParseMode.HTML)


async def reply_contract(message: Message, text: str, contract_address: str) -> None:
    await message.reply_text(text, reply_markup=contract_keyboard(contract_address), parse_mode=enums.ParseMode.HTML)


async def reply_final_response(message: Message, response: FinalResponse) -> None:
    if response.reply_type == "price":
        await reply_price(message, response.text, response.keyboard_value or "")
        return
    if response.reply_type == "contract":
        await reply_contract(message, response.text, response.keyboard_value or "")
        return
    await reply_with_delete(message, response.text)


def delete_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("Delete", callback_data=DELETE_CALLBACK)]])


def price_keyboard(query: str) -> InlineKeyboardMarkup:
    query = query[:32]
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("List", callback_data=f"{LIST_CALLBACK_PREFIX}{query}"),
                InlineKeyboardButton("Dex", callback_data=f"{DEX_CALLBACK_PREFIX}{query}"),
            ],
            [InlineKeyboardButton("Delete", callback_data=DELETE_CALLBACK)],
        ]
    )


def contract_keyboard(contract_address: str) -> InlineKeyboardMarkup:
    contract_address = contract_address[:42]
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Dex", callback_data=f"{DEX_CALLBACK_PREFIX}{contract_address}")],
            [InlineKeyboardButton("Delete", callback_data=DELETE_CALLBACK)],
        ]
    )


def gas_keyboard() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for index in range(0, len(GAS_CHAINS), 2):
        rows.append(
            [
                InlineKeyboardButton(
                    chain.chain_name,
                    callback_data=f"{GAS_CALLBACK_PREFIX}{chain.chain_name.lower()}",
                )
                for chain in GAS_CHAINS[index : index + 2]
            ]
        )
    rows.append([InlineKeyboardButton("Delete", callback_data=DELETE_CALLBACK)])
    return InlineKeyboardMarkup(rows)


async def delete_callback(_: Client, callback_query: CallbackQuery) -> None:
    await callback_query.answer()
    if callback_query.message:
        await callback_query.message.delete()


async def list_callback(_: Client, callback_query: CallbackQuery) -> None:
    query = (callback_query.data or "").removeprefix(LIST_CALLBACK_PREFIX).strip()
    if not query:
        await callback_query.answer("List query is empty.", show_alert=True)
        return

    try:
        results = await async_search_coins(query, limit=5)
    except CoinGeckoError as exc:
        await callback_query.answer(str(exc), show_alert=True)
        return

    await callback_query.answer()
    if callback_query.message:
        await callback_query.message.edit_text(
            format_coin_results(results), reply_markup=delete_keyboard(), parse_mode=enums.ParseMode.HTML
        )


async def dex_callback(_: Client, callback_query: CallbackQuery) -> None:
    query = (callback_query.data or "").removeprefix(DEX_CALLBACK_PREFIX).strip()
    if not query:
        await callback_query.answer("DEX query is empty.", show_alert=True)
        return

    try:
        pools = await async_search_pool_prices(query, limit=5)
    except GeckoTerminalError as exc:
        await callback_query.answer(str(exc), show_alert=True)
        return

    await callback_query.answer()
    if callback_query.message:
        await callback_query.message.edit_text(
            format_dex_pools(pools), reply_markup=delete_keyboard(), parse_mode=enums.ParseMode.HTML
        )


async def gas_callback(_: Client, callback_query: CallbackQuery) -> None:
    query = (callback_query.data or "").removeprefix(GAS_CALLBACK_PREFIX).strip()
    if not query:
        await callback_query.answer("Gas chain is empty.", show_alert=True)
        return

    try:
        prices = await async_get_gas_prices(query)
    except GasTrackerError as exc:
        await callback_query.answer(str(exc), show_alert=True)
        return

    await callback_query.answer()
    if callback_query.message:
        await callback_query.message.edit_text(
            format_gas_prices(prices), reply_markup=gas_keyboard(), parse_mode=enums.ParseMode.HTML
        )


async def start(_: Client, message: Message) -> None:
    text = (
        f"{bold('Hello!')} Send a coin amount, conversion, address, or math expression.\n\n"
        f"{underline('Examples')}\n"
        f"├ {code('/price BTC')}\n"
        f"├ {code('1 BTC')}\n"
        f"├ {code('1 BTC ETH')}\n"
        f"├ {code('10000 IDR ETH')}\n"
        f"├ {code('2 * 2')}\n"
        f"├ {code('/list MON')}\n"
        f"├ {code('/gas')}\n"
        f"└ {code('/about')}"
    )
    await reply_with_delete(message, text)


async def about_command(_: Client, message: Message) -> None:
    await reply_with_delete(message, format_about())


async def price_command(_: Client, message: Message) -> None:
    query = _command_argument(message.text)
    if not query:
        await reply_with_delete(message, f"Use format: {code('/price BTC')}")
        return

    await send_price(message, query)


async def list_command(_: Client, message: Message) -> None:
    query = _command_argument(message.text)
    if not query:
        await reply_with_delete(message, f"Use format: {code('/list MON')}")
        return

    try:
        results = await async_search_coins(query, limit=10)
    except CoinGeckoError as exc:
        await reply_with_delete(message, format_error(exc))
        return

    await reply_with_delete(message, format_coin_results(results))


async def gas_command(_: Client, message: Message) -> None:
    query = _command_argument(message.text)
    if not query:
        await message.reply_text(
            format_gas_chain_picker(), reply_markup=gas_keyboard(), parse_mode=enums.ParseMode.HTML
        )
        return

    await send_cached_response(
        message,
        "gas",
        final_cache_key("gas", query.lower()),
        lambda: build_gas_response(query),
    )


async def text_message(_: Client, message: Message) -> None:
    query = (message.text or "").strip()
    if not query:
        return

    contract_address = parse_contract_address(query)
    solana_address = parse_solana_address(query)
    conversion_query = parse_conversion_query(query)
    amount_query = parse_amount_query(query)
    math_expression = parse_math_expression(query)
    if (
        not contract_address
        and not solana_address
        and not conversion_query
        and not amount_query
        and math_expression is None
    ):
        return

    if contract_address:
        logger.info("route=address query=%s", safe_query(contract_address))
        await send_cached_response(
            message,
            "address",
            final_cache_key("address", contract_address.lower()),
            lambda: build_contract_address_response(contract_address),
        )
        return

    if solana_address:
        logger.info("route=solana_address query=%s", safe_query(solana_address))
        await send_cached_response(
            message,
            "solana_address",
            final_cache_key("solana", solana_address),
            lambda: build_solana_address_response(solana_address),
        )
        return

    if conversion_query:
        amount, from_symbol, to_symbol = conversion_query
        logger.info(
            "route=conversion amount=%s from=%s to=%s",
            _format_amount(amount),
            safe_query(from_symbol),
            safe_query(to_symbol),
        )
        await send_cached_response(
            message,
            "conversion",
            final_cache_key(
                "convert",
                _format_amount(amount),
                from_symbol.lower(),
                to_symbol.lower(),
            ),
            lambda: build_conversion_response(amount, from_symbol, to_symbol),
        )
        return

    if amount_query:
        amount, symbol = amount_query
        logger.info("route=amount amount=%s symbol=%s", _format_amount(amount), safe_query(symbol))
        await send_cached_response(
            message,
            "amount",
            final_cache_key("amount", _format_amount(amount), symbol.lower()),
            lambda: build_amount_response(amount, symbol),
        )
        return

    if math_expression is not None:
        logger.info("route=math expression=%s", safe_query(math_expression))
        await send_cached_response(
            message,
            "math",
            final_cache_key("math", math_expression),
            lambda: build_math_response(math_expression),
        )
        return


async def build_math_response(expression: str) -> FinalResponse:
    result = evaluate_math_expression(expression)
    return FinalResponse(format_math_result(expression, result), "delete")


async def build_gas_response(query: str) -> FinalResponse:
    prices = await async_get_gas_prices(query)
    return FinalResponse(format_gas_prices(prices), "delete")


async def send_price(message: Message, query: str) -> None:
    await send_cached_response(
        message,
        "price",
        final_cache_key("price", query.lower()),
        lambda: build_price_response(query),
    )


async def send_amount_price(message: Message, amount: float, query: str) -> None:
    await send_cached_response(
        message,
        "amount",
        final_cache_key("amount", _format_amount(amount), query.lower()),
        lambda: build_amount_response(amount, query),
    )


async def build_price_response(query: str) -> FinalResponse:
    try:
        price = await async_search_coin_price(query)
    except CoinGeckoError as exc:
        logger.warning("fallback=geckoterminal reason=coingecko_error query=%s error=%s", safe_query(query), exc)
        return await build_geckoterminal_response(query)

    return FinalResponse(format_coin_price(price), "price", query)


async def build_amount_response(amount: float, query: str) -> FinalResponse:
    try:
        price = await async_search_coin_price(query)
    except CoinGeckoError as exc:
        logger.warning("fallback=geckoterminal_amount reason=coingecko_error query=%s error=%s", safe_query(query), exc)
        return await build_geckoterminal_amount_response(amount, query)

    return FinalResponse(format_amount_price(amount, price), "price", query)


async def send_geckoterminal_price(message: Message, query: str) -> None:
    try:
        response = await build_geckoterminal_response(query)
    except GeckoTerminalError as exc:
        await reply_with_delete(message, format_error(exc))
        return

    await reply_final_response(message, response)


async def build_geckoterminal_response(query: str) -> FinalResponse:
    try:
        pool = (await async_search_pool_prices(query, limit=1))[0]
    except GeckoTerminalError as exc:
        logger.error("provider=geckoterminal failed query=%s error=%s", safe_query(query), exc)
        raise

    return FinalResponse(format_pool_price(pool), "price", query)


async def send_geckoterminal_amount_price(message: Message, amount: float, query: str) -> None:
    try:
        response = await build_geckoterminal_amount_response(amount, query)
    except GeckoTerminalError as exc:
        await reply_with_delete(message, format_error(exc))
        return

    await reply_final_response(message, response)


async def build_geckoterminal_amount_response(amount: float, query: str) -> FinalResponse:
    try:
        pool = (await async_search_pool_prices(query, limit=1))[0]
    except GeckoTerminalError as exc:
        logger.error("provider=geckoterminal failed amount=%s query=%s error=%s", _format_amount(amount), safe_query(query), exc)
        raise

    return FinalResponse(format_pool_amount_price(amount, pool), "price", query)


async def send_contract_address_price(message: Message, contract_address: str) -> None:
    await send_cached_response(
        message,
        "address",
        final_cache_key("address", contract_address.lower()),
        lambda: build_contract_address_response(contract_address),
    )


async def build_contract_address_response(contract_address: str) -> FinalResponse:
    try:
        pool = (await async_search_pool_prices(contract_address, limit=1))[0]
    except GeckoTerminalError as exc:
        logger.warning("fallback=rpc_balance reason=geckoterminal_error address=%s error=%s", safe_query(contract_address), exc)
        return await build_wallet_balance_response(contract_address)

    return FinalResponse(format_contract_pool_price(contract_address, pool), "contract", contract_address)


async def send_wallet_balance(message: Message, address: str) -> None:
    try:
        response = await build_wallet_balance_response(address)
    except RpcBalanceError as exc:
        await reply_with_delete(message, format_error(exc))
        return

    await reply_final_response(message, response)


async def build_wallet_balance_response(address: str) -> FinalResponse:
    try:
        balances = await async_get_balances(address)
    except RpcBalanceError as exc:
        logger.error("provider=rpc_balance failed address=%s error=%s", safe_query(address), exc)
        raise

    return FinalResponse(format_wallet_balances(address, balances), "delete")


async def send_solana_address_price(message: Message, solana_address: str) -> None:
    await send_cached_response(
        message,
        "solana_address",
        final_cache_key("solana", solana_address),
        lambda: build_solana_address_response(solana_address),
    )


async def build_solana_address_response(solana_address: str) -> FinalResponse:
    try:
        pool = (await async_search_pool_prices(solana_address, limit=1))[0]
    except GeckoTerminalError as exc:
        logger.warning("fallback=solana_rpc_balance reason=geckoterminal_error address=%s error=%s", safe_query(solana_address), exc)
        return await build_solana_wallet_balance_response(solana_address)

    return FinalResponse(format_contract_pool_price(solana_address, pool), "contract", solana_address)


async def send_solana_wallet_balance(message: Message, address: str) -> None:
    try:
        response = await build_solana_wallet_balance_response(address)
    except RpcBalanceError as exc:
        await reply_with_delete(message, format_error(exc))
        return

    await reply_final_response(message, response)


async def build_solana_wallet_balance_response(address: str) -> FinalResponse:
    try:
        balance = await async_get_solana_balance(address)
    except RpcBalanceError as exc:
        logger.error("provider=solana_rpc_balance failed address=%s error=%s", safe_query(address), exc)
        raise

    return FinalResponse(format_wallet_balances(address, [balance]), "delete")


async def send_conversion_price(
    message: Message, amount: float, from_query: str, to_query: str
) -> None:
    await send_cached_response(
        message,
        "conversion",
        final_cache_key(
            "convert",
            _format_amount(amount),
            from_query.lower(),
            to_query.lower(),
        ),
        lambda: build_conversion_response(amount, from_query, to_query),
    )


async def build_conversion_response(
    amount: float, from_query: str, to_query: str
) -> FinalResponse:
    if is_priority_fiat_currency(from_query):
        return await build_from_fiat_conversion_response(amount, from_query, to_query)

    from_price = await async_get_usd_price_source(from_query)

    if is_priority_fiat_currency(to_query):
        return await build_to_fiat_conversion_response(amount, from_price, to_query)

    to_price = await async_get_usd_price_source(to_query)
    return FinalResponse(format_usd_source_conversion_price(amount, from_price, to_price), "delete")


async def send_to_fiat_conversion_price(
    message: Message, amount: float, from_price: CoinPrice, to_currency: str
) -> None:
    try:
        response = await build_to_fiat_conversion_response(
            amount, usd_price_source_from_coin_price(from_price), to_currency
        )
    except (CoinGeckoError, GeckoTerminalError, CurrencyError) as exc:
        await reply_with_delete(message, format_error(exc))
        return

    await reply_final_response(message, response)


async def build_to_fiat_conversion_response(
    amount: float, from_price: UsdPriceSource, to_currency: str
) -> FinalResponse:
    total_usd = amount * from_price.price_usd
    try:
        converted_amount = await async_convert_currency(total_usd, "usd", to_currency)
    except CurrencyError as exc:
        logger.error("provider=currency to_fiat_failed currency=%s error=%s", safe_query(to_currency), exc)
        raise

    return FinalResponse(
        format_to_fiat_source_conversion_price(
            amount, from_price, to_currency, total_usd, converted_amount
        ),
        "delete",
    )


async def send_from_fiat_conversion_price(
    message: Message, amount: float, from_currency: str, to_query: str
) -> None:
    try:
        response = await build_from_fiat_conversion_response(amount, from_currency, to_query)
    except (CoinGeckoError, CurrencyError) as exc:
        await reply_with_delete(message, format_error(exc))
        return

    await reply_final_response(message, response)


async def build_from_fiat_conversion_response(
    amount: float, from_currency: str, to_query: str
) -> FinalResponse:
    to_price = await async_get_usd_price_source(to_query)

    try:
        total_usd = await async_convert_currency(amount, from_currency, "usd")
    except CurrencyError as exc:
        logger.error("provider=currency from_fiat_failed currency=%s error=%s", safe_query(from_currency), exc)
        raise

    converted_amount = total_usd / to_price.price_usd
    return FinalResponse(
        format_from_fiat_source_conversion_price(
            amount, from_currency, to_price, total_usd, converted_amount
        ),
        "delete",
    )


async def send_cached_response(
    message: Message,
    route: str,
    cache_key: str,
    builder: object,
) -> None:
    cached = get_final_response_cache(cache_key)
    if cached is not None:
        logger.info("final_cache_hit route=%s", route)
        await reply_final_response(message, cached)
        return

    logger.info("final_cache_miss route=%s", route)
    try:
        response = await builder()
    except (CoinGeckoError, GeckoTerminalError, RpcBalanceError, CurrencyError, GasTrackerError) as exc:
        await reply_with_delete(message, format_error(exc))
        return

    set_final_response_cache(cache_key, response)
    await reply_final_response(message, response)


def final_cache_key(route: str, *parts: str) -> str:
    normalized = ":".join(part.strip() for part in parts)
    return f"{route}:{normalized}"


def get_final_response_cache(cache_key: str) -> FinalResponse | None:
    cached = final_response_cache.get(cache_key)
    if cached is None:
        return None

    timestamp, response = cached
    if monotonic() - timestamp < FINAL_RESPONSE_CACHE_TTL_SECONDS:
        return response

    final_response_cache.pop(cache_key, None)
    return None


def set_final_response_cache(cache_key: str, response: FinalResponse) -> None:
    final_response_cache[cache_key] = (monotonic(), response)
    if len(final_response_cache) > 512:
        oldest_key = min(final_response_cache, key=lambda key: final_response_cache[key][0])
        final_response_cache.pop(oldest_key, None)


async def async_get_usd_price_source(query: str) -> UsdPriceSource:
    try:
        price = await async_search_coin_price(query)
    except CoinGeckoError as exc:
        logger.warning(
            "fallback=geckoterminal_conversion_price reason=coingecko_error query=%s error=%s",
            safe_query(query),
            exc,
        )
        return await async_get_geckoterminal_usd_price_source(query)

    return usd_price_source_from_coin_price(price)


async def async_get_geckoterminal_usd_price_source(query: str) -> UsdPriceSource:
    pool = (await async_search_pool_prices(query, limit=1))[0]
    price_usd = _float_from_text(pool.price_usd)
    if price_usd is None:
        raise GeckoTerminalError("GeckoTerminal USD price is not available for conversion.")

    return UsdPriceSource(
        query=query,
        symbol=query.strip().upper(),
        name=pool.name,
        price_usd=price_usd,
        provider="GeckoTerminal",
    )


def usd_price_source_from_coin_price(price: CoinPrice) -> UsdPriceSource:
    if price.price_usd is None:
        raise CoinGeckoError("Coin price is not available on CoinGecko.")

    return UsdPriceSource(
        query=price.coin_id,
        symbol=price.symbol,
        name=price.name,
        price_usd=price.price_usd,
        provider="CoinGecko",
    )


def format_about() -> str:
    return "\n".join(
        [
            f"🤖 {bold('Crypto Price Telegram Bot')}",
            f"├ {underline('What it does')}",
            f"├ Tracks cryptocurrency prices and market data.",
            f"├ Converts crypto-to-crypto and crypto-to-fiat values.",
            f"├ Looks up DEX pools through GeckoTerminal.",
            f"├ Falls back to public RPC wallet balances for EVM and Solana addresses.",
            f"├ Shows multi-chain gas estimates with USD cost estimates.",
            f"├ Supports simple math expressions like {code('2 * 2')}.",
            f"├ {underline('Main commands')}",
            f"├ {code('/price BTC')} · {code('/list MON')} · {code('/gas')}",
            f"└ {italic('Powered by CoinGecko, GeckoTerminal, public RPC, and Pyrogram')}",
        ]
    )


def format_coin_price(price: CoinPrice) -> str:
    return "\n".join(
        [
            f"💰 {bold(f'{price.name} ({price.symbol})')}",
            f"├ ID: {code(price.coin_id)}",
            f"├ {underline('Market')}",
            f"├ Price: {bold(_format_money(price.price_usd))}",
            f"├ 24h Change: {bold(_format_percentage(price.price_change_24h))}",
            f"├ 24h Range: {h(_format_money(price.low_24h_usd))} → {h(_format_money(price.high_24h_usd))}",
            f"├ Volume 24h: {h(_format_money(price.volume_24h_usd))}",
            f"├ Market Cap: {h(_format_money(price.market_cap_usd))}",
            f"├ MC Change 24h: {h(_format_money(price.market_cap_change_24h))}",
            f"├ Supply: {h(_format_number(price.circulating_supply))} / {h(_format_number(price.total_supply))}",
            f"├ Max Supply: {h(_format_number(price.max_supply))}",
            f"├ ATH / ATL: {h(_format_money(price.ath_usd))} / {h(_format_money(price.atl_usd))}",
            f"└ {italic('Source: CoinGecko')}",
        ]
    )


def format_amount_price(amount: float, price: CoinPrice) -> str:
    if price.price_usd is None:
        return format_coin_price(price)

    total = amount * price.price_usd
    return "\n".join(
        [
            f"🧮 {bold(f'{_format_amount(amount)} {price.symbol} · {price.name}')}",
            f"├ ID: {code(price.coin_id)}",
            f"├ Rank: {h(_format_rank(price.market_cap_rank))}",
            f"├ Price / {h(price.symbol)}: {bold(_format_money(price.price_usd))}",
            f"├ Total: {bold(_format_money(total))}",
            f"├ 24h Change: {bold(_format_percentage(price.price_change_24h))}",
            f"├ 24h Range: {h(_format_money(price.low_24h_usd))} → {h(_format_money(price.high_24h_usd))}",
            f"├ Volume 24h: {h(_format_money(price.volume_24h_usd))}",
            f"├ Market Cap: {h(_format_money(price.market_cap_usd))}",
            f"├ MC Change 24h: {h(_format_money(price.market_cap_change_24h))}",
            f"├ Supply: {h(_format_number(price.circulating_supply))} / {h(_format_number(price.total_supply))}",
            f"├ Max Supply: {h(_format_number(price.max_supply))}",
            f"├ ATH / ATL: {h(_format_money(price.ath_usd))} / {h(_format_money(price.atl_usd))}",
            f"└ {italic('Source: CoinGecko')}",
        ]
    )


def format_conversion_price(amount: float, from_price: CoinPrice, to_price: CoinPrice) -> str:
    return format_usd_source_conversion_price(
        amount, usd_price_source_from_coin_price(from_price), usd_price_source_from_coin_price(to_price)
    )


def format_usd_source_conversion_price(
    amount: float, from_price: UsdPriceSource, to_price: UsdPriceSource
) -> str:
    from_total_usd = amount * from_price.price_usd
    converted_amount = from_total_usd / to_price.price_usd
    return "\n".join(
        [
            f"🔁 {bold(f'Convert {_format_amount(amount)} {from_price.symbol} → {to_price.symbol}')}",
            f"├ Result: {bold(f'{_format_amount(converted_amount)} {to_price.symbol}')}",
            f"├ USD Value: {h(_format_money(from_total_usd))}",
            f"├ 1 {h(from_price.symbol)}: {h(_format_money(from_price.price_usd))} · {italic(from_price.provider)}",
            f"└ 1 {h(to_price.symbol)}: {h(_format_money(to_price.price_usd))} · {italic(to_price.provider)}",
        ]
    )


def format_to_fiat_conversion_price(
    amount: float,
    from_price: CoinPrice,
    to_currency: str,
    total_usd: float,
    converted_amount: float,
) -> str:
    return format_to_fiat_source_conversion_price(
        amount, usd_price_source_from_coin_price(from_price), to_currency, total_usd, converted_amount
    )


def format_to_fiat_source_conversion_price(
    amount: float,
    from_price: UsdPriceSource,
    to_currency: str,
    total_usd: float,
    converted_amount: float,
) -> str:
    currency = to_currency.upper()
    return "\n".join(
        [
            f"🔁 {bold(f'Convert {_format_amount(amount)} {from_price.symbol} → {currency}')}",
            f"├ Result: {bold(f'{_format_currency_amount(converted_amount)} {currency}')}",
            f"├ USD Value: {h(_format_money(total_usd))}",
            f"├ 1 {h(from_price.symbol)}: {h(_format_money(from_price.price_usd))}",
            f"├ Source: {italic(from_price.provider)}",
            f"└ USD/{h(currency)}: {h(_format_currency_amount(converted_amount / total_usd))}",
        ]
    )


def format_from_fiat_conversion_price(
    amount: float,
    from_currency: str,
    to_price: CoinPrice,
    total_usd: float,
    converted_amount: float,
) -> str:
    return format_from_fiat_source_conversion_price(
        amount, from_currency, usd_price_source_from_coin_price(to_price), total_usd, converted_amount
    )


def format_from_fiat_source_conversion_price(
    amount: float,
    from_currency: str,
    to_price: UsdPriceSource,
    total_usd: float,
    converted_amount: float,
) -> str:
    currency = from_currency.upper()
    return "\n".join(
        [
            f"🔁 {bold(f'Convert {_format_currency_amount(amount)} {currency} → {to_price.symbol}')}",
            f"├ Result: {bold(f'{_format_amount(converted_amount)} {to_price.symbol}')}",
            f"├ USD Value: {h(_format_money(total_usd))}",
            f"├ 1 {h(to_price.symbol)}: {h(_format_money(to_price.price_usd))}",
            f"├ Source: {italic(to_price.provider)}",
            f"└ {h(currency)}/USD: {h(_format_currency_amount(total_usd / amount))}",
        ]
    )


def format_contract_pool_price(contract_address: str, pool: PoolPrice) -> str:
    return "\n".join(
        [
            f"🧾 {bold('GeckoTerminal Contract Lookup')}",
            f"├ Contract: {code(contract_address)}",
            f"├ Pool: {bold(pool.name)}",
            f"├ Network: {code(pool.network or '-')}",
            f"├ Pool ID: {code(pool.address)}",
            f"├ Price: {bold(_format_text_money(pool.price_usd))}",
            f"├ 24h Change: {bold(_format_text_percentage(pool.price_change_24h))}",
            f"├ Volume 24h: {h(_format_text_money(pool.volume_24h_usd))}",
            f"├ Liquidity: {h(_format_text_money(pool.reserve_usd))}",
            f"└ {italic('Source: GeckoTerminal')}",
        ]
    )


def format_wallet_balances(address: str, balances: list[NativeBalance]) -> str:
    positive_balances = [balance for balance in balances if balance.balance > 0]
    visible_balances = positive_balances or balances

    lines = [f"👛 {bold('RPC Wallet Balance')}", f"├ Address: {code(address)}"]
    if not positive_balances:
        lines.append(f"├ Status: {italic('No native balance > 0 found on checked chains')}")

    for index, balance in enumerate(visible_balances[:10]):
        branch = "└" if index == min(len(visible_balances), 10) - 1 else "├"
        lines.append(
            f"{branch} {h(balance.chain_name)}: {bold(_format_decimal_amount(balance.balance))} {h(balance.symbol)}"
        )

    return "\n".join(lines)


def format_pool_price(pool: PoolPrice) -> str:
    return "\n".join(
        [
            f"🦎 {bold('GeckoTerminal DEX Fallback')}",
            f"├ Pool: {bold(pool.name)}",
            f"├ Network: {code(pool.network or '-')}",
            f"├ Pool ID: {code(pool.address)}",
            f"├ Price: {bold(_format_text_money(pool.price_usd))}",
            f"├ 24h Change: {bold(_format_text_percentage(pool.price_change_24h))}",
            f"├ Volume 24h: {h(_format_text_money(pool.volume_24h_usd))}",
            f"├ Liquidity: {h(_format_text_money(pool.reserve_usd))}",
            f"└ {italic('Source: GeckoTerminal')}",
        ]
    )


def format_pool_amount_price(amount: float, pool: PoolPrice) -> str:
    price_usd = _float_from_text(pool.price_usd)
    if price_usd is None:
        return format_pool_price(pool)

    total = amount * price_usd
    return "\n".join(
        [
            f"🧮 {bold(f'{_format_amount(amount)} token · GeckoTerminal')}",
            f"├ Pool: {bold(pool.name)}",
            f"├ Network: {code(pool.network or '-')}",
            f"├ Pool ID: {code(pool.address)}",
            f"├ Price / token: {bold(_format_money(price_usd))}",
            f"├ Total: {bold(_format_money(total))}",
            f"├ 24h Change: {bold(_format_text_percentage(pool.price_change_24h))}",
            f"├ Volume 24h: {h(_format_text_money(pool.volume_24h_usd))}",
            f"├ Liquidity: {h(_format_text_money(pool.reserve_usd))}",
            f"└ {italic('Source: GeckoTerminal')}",
        ]
    )


def format_coin_results(results: list[CoinSearchResult]) -> str:
    lines = [f"📋 {bold('CoinGecko Search Results')}"]
    for index, result in enumerate(results, start=1):
        branch = "└" if index == len(results) else "├"
        rank = _format_rank(result.market_cap_rank)
        lines.append(
            f"{branch} {index}. {bold(f'{result.name} ({result.symbol})')} · {code(result.coin_id)} · {h(rank)}"
        )
    return "\n".join(lines)


def format_dex_pools(pools: list[PoolPrice]) -> str:
    lines = [f"🦎 {bold('Top DEX Pools · GeckoTerminal')}"]
    for index, pool in enumerate(pools, start=1):
        branch = "└" if index == len(pools) else "├"
        lines.extend(
            [
                f"{branch} {index}. {bold(pool.name)}",
                f"   ├ Network: {code(pool.network or '-')}",
                f"   ├ Pool ID: {code(pool.address)}",
                f"   ├ Price: {bold(_format_text_money(pool.price_usd))}",
                f"   ├ 24h Change: {bold(_format_text_percentage(pool.price_change_24h))}",
                f"   ├ Volume 24h: {h(_format_text_money(pool.volume_24h_usd))}",
                f"   └ Liquidity: {h(_format_text_money(pool.reserve_usd))}",
            ]
        )
    return "\n".join(lines)


def format_gas_chain_picker() -> str:
    return "\n".join(
        [
            f"⛽ {bold('Gas Tracker')}",
            f"├ {underline('Select a chain from the buttons below')}",
            f"├ {code('/gas eth')} to open Ethereum directly",
            f"└ {italic('Source: Public RPC')}",
        ]
    )


def format_gas_prices(prices: list[GasPrice]) -> str:
    lines = [f"⛽ {bold('Gas Tracker')}"]
    for price in prices:
        lines.append(f"├ Chain: {bold(price.chain_name)}")
        lines.append(f"├ Avg: {bold(_format_gwei_with_usd(price.average_gwei, price.native_price_usd))}")
        lines.append(f"├ Fast: {bold(_format_gwei_with_usd(price.fast_gwei, price.native_price_usd))}")
        if price.base_fee_gwei is not None:
            lines.append(f"├ Base: {h(_format_gwei_with_usd(price.base_fee_gwei, price.native_price_usd))}")
            lines.append(f"├ Priority: {h(_format_gwei_with_usd(price.priority_fee_gwei, price.native_price_usd))}")
        else:
            lines.append(f"├ Gas Price: {h(_format_gwei_with_usd(price.gas_price_gwei, price.native_price_usd))}")

    lines.append(f"├ Est: {italic('standard 21,000 gas transfer')}")
    lines.append(f"└ {italic('Source: Public RPC + CoinGecko/GeckoTerminal')}")
    return "\n".join(lines)


def _user_id(message: Message) -> int | None:
    return message.from_user.id if message.from_user else None


def is_rate_limited(message: Message) -> bool:
    identifier = _user_id(message) or message.chat.id
    now = monotonic()
    previous = last_text_request_at.get(identifier)
    if previous is not None and now - previous < TEXT_RATE_LIMIT_SECONDS:
        return True

    last_text_request_at[identifier] = now
    return False


def parse_math_expression(text: str) -> str | None:
    value = text.strip()
    if len(value) > MAX_MATH_EXPRESSION_LENGTH:
        return None
    if not re.search(r"[+\-*/%^]", value):
        return None
    if not re.fullmatch(r"[0-9\s+\-*/%.^(),]+", value):
        return None
    return value.replace("^", "**")


def parse_contract_address(text: str) -> str | None:
    value = text.strip()
    if re.fullmatch(r"0x[a-fA-F0-9]{40}", value):
        return value
    return None


def parse_solana_address(text: str) -> str | None:
    value = text.strip()
    if re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", value):
        return value
    return None


def parse_conversion_query(text: str) -> tuple[float, str, str] | None:
    match = re.fullmatch(
        r"([0-9]+(?:[.,][0-9]+)?)\s+([A-Za-z][A-Za-z0-9-]*)\s+([A-Za-z][A-Za-z0-9-]*)",
        text.strip(),
    )
    if not match:
        return None

    amount = float(match.group(1).replace(",", "."))
    from_symbol = match.group(2).strip()
    to_symbol = match.group(3).strip()
    return amount, from_symbol, to_symbol


def parse_amount_query(text: str) -> tuple[float, str] | None:
    match = re.fullmatch(r"([0-9]+(?:[.,][0-9]+)?)\s+([A-Za-z][A-Za-z0-9-]*)", text.strip())
    if not match:
        return None

    amount = float(match.group(1).replace(",", "."))
    symbol = match.group(2).strip()
    return amount, symbol


def evaluate_math_expression(expression: str) -> float | int:
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ValueError("Invalid math format.") from exc

    return evaluate_math_node(tree.body)


def evaluate_math_node(node: ast.AST) -> float | int:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = evaluate_math_node(node.operand)
        return value if isinstance(node.op, ast.UAdd) else -value
    if isinstance(node, ast.BinOp):
        operator_func = MATH_OPERATORS.get(type(node.op))
        if operator_func is None:
            raise ValueError("Unsupported math operator.")
        left = evaluate_math_node(node.left)
        right = evaluate_math_node(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 10:
            raise ValueError("Exponent is too large.")
        result = operator_func(left, right)
        if abs(result) > MAX_MATH_RESULT_ABS:
            raise ValueError("Math result is too large.")
        return result
    raise ValueError("Invalid math format.")


def format_math_result(expression: str, result: float | int) -> str:
    return f"🧮 {code(expression)} = {bold(_format_math_number(result))}"


def _format_math_number(value: float | int) -> str:
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return f"{value:,.10f}".rstrip("0").rstrip(".")
    return f"{value:,}"


def _format_gwei(value: object) -> str:
    if value is None:
        return "-"
    number = float(value)
    if number < 1:
        return f"{number:.4f}".rstrip("0").rstrip(".") + " gwei"
    return f"{number:,.2f}".rstrip("0").rstrip(".") + " gwei"


def _format_gwei_with_usd(value: object, native_price_usd: object) -> str:
    gwei = _format_gwei(value)
    if value is None or native_price_usd is None:
        return gwei

    estimated_usd = float(value) * 21_000 / 1_000_000_000 * float(native_price_usd)
    return f"{gwei} ({_format_gas_usd(estimated_usd)})"


def _format_gas_usd(value: float) -> str:
    if 0 < value < 0.000001:
        return "<$0.000001"
    if value < 0.01:
        return f"${value:.6f}".rstrip("0").rstrip(".")
    return _format_money(value)


def _command_argument(text: str | None) -> str:
    if not text:
        return ""

    parts = text.split(maxsplit=1)
    if len(parts) == 1:
        return ""
    return parts[1].strip()


def _format_amount(value: float) -> str:
    return f"{value:,.8f}".rstrip("0").rstrip(".")


def _format_decimal_amount(value: object) -> str:
    normalized = str(value.normalize()) if hasattr(value, "normalize") else str(value)
    if "E" in normalized or "e" in normalized:
        normalized = f"{value:.18f}"
    return normalized.rstrip("0").rstrip(".") or "0"


def _format_currency_amount(value: float) -> str:
    if abs(value) < 1:
        return f"{value:,.8f}".rstrip("0").rstrip(".")
    return f"{value:,.2f}".rstrip("0").rstrip(".")


def _format_rank(value: int | None) -> str:
    if value is None:
        return "-"
    return f"#{value}"


def _float_from_text(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _format_text_money(value: str | None) -> str:
    number = _float_from_text(value)
    if number is None:
        return value or "-"
    return _format_money(number)


def _format_text_percentage(value: str | None) -> str:
    if value is None:
        return "-"
    try:
        return _format_percentage(float(value))
    except ValueError:
        return f"{value}%"


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


def create_app() -> Client:
    load_dotenv()

    api_id = os.getenv("TELEGRAM_API_ID")
    api_hash = os.getenv("TELEGRAM_API_HASH")
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")

    missing = [
        name
        for name, value in {
            "TELEGRAM_API_ID": api_id,
            "TELEGRAM_API_HASH": api_hash,
            "TELEGRAM_BOT_TOKEN": bot_token,
        }.items()
        if not value
    ]
    if missing:
        raise RuntimeError(
            f"Missing environment variables: {', '.join(missing)}. Create a .env file from .env.example."
        )

    workdir = os.getenv("PYROGRAM_WORKDIR", ".")
    client = Client(
        "crypto_price_bot",
        api_id=int(api_id),
        api_hash=api_hash,
        bot_token=bot_token,
        workdir=workdir,
    )
    client.add_handler(CallbackQueryHandler(delete_callback, filters.regex(f"^{DELETE_CALLBACK}$")))
    client.add_handler(CallbackQueryHandler(list_callback, filters.regex(f"^{LIST_CALLBACK_PREFIX}")))
    client.add_handler(CallbackQueryHandler(dex_callback, filters.regex(f"^{DEX_CALLBACK_PREFIX}")))
    client.add_handler(CallbackQueryHandler(gas_callback, filters.regex(f"^{GAS_CALLBACK_PREFIX}")))
    client.add_handler(MessageHandler(start, filters.command("start")))
    client.add_handler(MessageHandler(about_command, filters.command("about")))
    client.add_handler(MessageHandler(price_command, filters.command("price")))
    client.add_handler(MessageHandler(list_command, filters.command("list")))
    client.add_handler(MessageHandler(gas_command, filters.command("gas")))
    client.add_handler(
        MessageHandler(text_message, filters.text & ~filters.command(["start", "about", "price", "list", "gas"]))
    )
    return client


def main() -> int:
    configure_logging()
    try:
        bot = create_app()
    except (RuntimeError, ValueError) as exc:
        print(f"Error: {exc}")
        return 1

    print("Pyrogram Telegram bot is running. Press Ctrl+C to stop.")
    try:
        bot.run()
    finally:
        asyncio.run(close_async_clients())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
