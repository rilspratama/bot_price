from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx

from cache_utils import ProviderCooldown, async_ttl_cache
from coingecko import CoinGeckoError, async_search_coin_price
from geckoterminal import GeckoTerminalError, async_search_pool_prices
from http_client import get_async_client
from logging_utils import elapsed_ms, start_timer

logger = logging.getLogger(__name__)


TIMEOUT_SECONDS = 12
GWEI_PER_WEI = Decimal(10) ** 9
RPC_RETRY_ATTEMPTS = 2
RPC_RETRY_DELAY_SECONDS = 0.5
RATE_LIMIT_COOLDOWN_SECONDS = 20
REQUEST_SEMAPHORE = asyncio.Semaphore(4)
COOLDOWN = ProviderCooldown("gas_tracker")


@dataclass(frozen=True)
class GasChain:
    chain_name: str
    symbol: str
    rpc_url: str
    supports_eip1559: bool


@dataclass(frozen=True)
class GasPrice:
    chain_name: str
    symbol: str
    gas_price_gwei: Decimal
    low_gwei: Decimal
    average_gwei: Decimal
    fast_gwei: Decimal
    base_fee_gwei: Decimal | None
    priority_fee_gwei: Decimal | None
    native_price_usd: Decimal | None
    rpc_url: str


class GasTrackerError(Exception):
    pass


CHAINS = [
    GasChain("Ethereum", "ETH", "https://ethereum-rpc.publicnode.com", True),
    GasChain("BNB Smart Chain", "BNB", "https://bsc-rpc.publicnode.com", False),
    GasChain("Polygon", "POL", "https://polygon-bor-rpc.publicnode.com", True),
    GasChain("Arbitrum One", "ETH", "https://arbitrum-one-rpc.publicnode.com", True),
    GasChain("Optimism", "ETH", "https://optimism-rpc.publicnode.com", True),
    GasChain("Base", "ETH", "https://base-rpc.publicnode.com", True),
    GasChain("Avalanche C-Chain", "AVAX", "https://avalanche-c-chain-rpc.publicnode.com", True),
]

NATIVE_PRICE_QUERIES = {
    "Ethereum": "ethereum",
    "BNB Smart Chain": "binancecoin",
    "Polygon": "polygon-ecosystem-token",
    "Arbitrum One": "ethereum",
    "Optimism": "ethereum",
    "Base": "ethereum",
    "Avalanche C-Chain": "avalanche-2",
}

CHAIN_ALIASES = {
    "eth": "ethereum",
    "ethereum": "ethereum",
    "bnb": "bnb smart chain",
    "bsc": "bnb smart chain",
    "binance": "bnb smart chain",
    "polygon": "polygon",
    "pol": "polygon",
    "matic": "polygon",
    "arb": "arbitrum one",
    "arbitrum": "arbitrum one",
    "arbitrum one": "arbitrum one",
    "op": "optimism",
    "optimism": "optimism",
    "base": "base",
    "avax": "avalanche c-chain",
    "avalanche": "avalanche c-chain",
    "avalanche c-chain": "avalanche c-chain",
}


@async_ttl_cache(ttl_seconds=20, maxsize=64)
async def async_get_gas_prices(chain_query: str = "") -> list[GasPrice]:
    if COOLDOWN.is_active():
        raise GasTrackerError("Gas tracker is cooling down. Try again in a moment.")

    started = start_timer()
    chains = filtered_chains(chain_query)
    client = get_async_client("gas_tracker", TIMEOUT_SECONDS)
    async with REQUEST_SEMAPHORE:
        results = await asyncio.gather(
            *(async_get_chain_gas_price(chain, client) for chain in chains),
            return_exceptions=True,
        )

    gas_prices: list[GasPrice] = []
    errors: list[str] = []
    for chain, result in zip(chains, results):
        if isinstance(result, GasPrice):
            gas_prices.append(result)
        elif isinstance(result, GasTrackerError):
            errors.append(f"{chain.chain_name}: {result}")
        else:
            errors.append(f"{chain.chain_name}: {result}")

    logger.info(
        "gas_tracker_summary success=%s failed=%s duration_ms=%s",
        len(gas_prices),
        len(errors),
        elapsed_ms(started),
    )
    if not gas_prices and errors:
        COOLDOWN.activate(RATE_LIMIT_COOLDOWN_SECONDS)
        raise GasTrackerError("All gas RPC endpoints failed.")

    return gas_prices


def filtered_chains(chain_query: str) -> list[GasChain]:
    query = chain_query.strip().lower()
    if not query:
        return CHAINS

    normalized = CHAIN_ALIASES.get(query, query)
    chains = [
        chain
        for chain in CHAINS
        if chain.chain_name.lower() == normalized
        or chain.symbol.lower() == normalized
        or normalized in chain.chain_name.lower()
    ]
    if not chains:
        raise GasTrackerError(
            "Gas chain is not supported. Examples: /gas eth, /gas base, /gas bnb, /gas polygon."
        )

    return chains


async def async_get_chain_gas_price(chain: GasChain, client: httpx.AsyncClient) -> GasPrice:
    started = start_timer()
    gas_price_wei = await get_gas_price_wei(client, chain, started)
    native_price_usd = await get_native_price_usd(chain)

    base_fee_wei: int | None = None
    priority_fee_wei: int | None = None
    if chain.supports_eip1559:
        try:
            base_fee_wei, priority_fee_wei = await get_fee_history_estimate(client, chain, started)
        except GasTrackerError as exc:
            logger.warning("gas_fee_history_fallback chain=%s error=%s", chain.chain_name, exc)

    gas_price_gwei = wei_to_gwei(gas_price_wei)
    if base_fee_wei is None:
        low_gwei = gas_price_gwei * Decimal("0.9")
        average_gwei = gas_price_gwei
        fast_gwei = gas_price_gwei * Decimal("1.15")
        return GasPrice(
            chain.chain_name,
            chain.symbol,
            gas_price_gwei,
            low_gwei,
            average_gwei,
            fast_gwei,
            None,
            None,
            native_price_usd,
            chain.rpc_url,
        )

    base_fee_gwei = wei_to_gwei(base_fee_wei)
    priority_fee_gwei = wei_to_gwei(priority_fee_wei or 0)
    low_gwei = base_fee_gwei + priority_fee_gwei
    average_gwei = max(gas_price_gwei, low_gwei)
    fast_gwei = average_gwei + priority_fee_gwei
    return GasPrice(
        chain.chain_name,
        chain.symbol,
        gas_price_gwei,
        low_gwei,
        average_gwei,
        fast_gwei,
        base_fee_gwei,
        priority_fee_gwei,
        native_price_usd,
        chain.rpc_url,
    )


async def get_gas_price_wei(client: httpx.AsyncClient, chain: GasChain, started: float) -> int:
    payload = {"jsonrpc": "2.0", "id": 1, "method": "eth_gasPrice", "params": []}
    data = await post_rpc_json_with_retry(client, chain, payload, started)
    result = data.get("result")
    if not isinstance(result, str) or not result.startswith("0x"):
        raise GasTrackerError("eth_gasPrice result is invalid.")
    return int(result, 16)


@async_ttl_cache(ttl_seconds=45, maxsize=32)
async def get_native_price_usd(chain: GasChain) -> Decimal | None:
    query = NATIVE_PRICE_QUERIES.get(chain.chain_name, chain.symbol)
    try:
        coin_price = await async_search_coin_price(query)
        if coin_price.price_usd is not None:
            return Decimal(str(coin_price.price_usd))
    except CoinGeckoError as exc:
        logger.warning("gas_native_price_coingecko_failed chain=%s error=%s", chain.chain_name, exc)

    try:
        pool = (await async_search_pool_prices(chain.symbol, limit=1))[0]
        if pool.price_usd is not None:
            return Decimal(pool.price_usd)
    except (GeckoTerminalError, ValueError) as exc:
        logger.warning("gas_native_price_geckoterminal_failed chain=%s error=%s", chain.chain_name, exc)

    return None


async def get_fee_history_estimate(
    client: httpx.AsyncClient, chain: GasChain, started: float
) -> tuple[int | None, int | None]:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_feeHistory",
        "params": ["0x5", "latest", [10, 50, 90]],
    }
    data = await post_rpc_json_with_retry(client, chain, payload, started)
    result = data.get("result")
    if not isinstance(result, dict):
        raise GasTrackerError("eth_feeHistory result is invalid.")

    base_fees = result.get("baseFeePerGas")
    rewards = result.get("reward")
    base_fee_wei = hex_list_last_int(base_fees)
    priority_fee_wei = reward_average_wei(rewards)
    return base_fee_wei, priority_fee_wei


async def post_rpc_json_with_retry(
    client: httpx.AsyncClient,
    chain: GasChain,
    payload: dict[str, Any],
    started: float,
) -> dict[str, Any]:
    last_exc: httpx.RequestError | None = None
    for attempt in range(1, RPC_RETRY_ATTEMPTS + 1):
        try:
            response = await client.post(chain.rpc_url, json=payload)
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise GasTrackerError("Gas RPC response format is invalid.")
            error = data.get("error")
            if error:
                raise GasTrackerError(str(error))
            logger.info(
                "gas_rpc_request_done chain=%s method=%s status=%s duration_ms=%s",
                chain.chain_name,
                payload.get("method"),
                response.status_code,
                elapsed_ms(started),
            )
            return data
        except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ConnectError) as exc:
            last_exc = exc
            logger.warning(
                "gas_rpc_request_retry chain=%s method=%s attempt=%s duration_ms=%s error=%s",
                chain.chain_name,
                payload.get("method"),
                attempt,
                elapsed_ms(started),
                exc.__class__.__name__,
            )
            if attempt < RPC_RETRY_ATTEMPTS:
                await asyncio.sleep(RPC_RETRY_DELAY_SECONDS)
        except httpx.HTTPError as exc:
            raise GasTrackerError(str(exc)) from exc
        except ValueError as exc:
            raise GasTrackerError("Gas RPC response is not valid JSON.") from exc

    raise GasTrackerError("Gas RPC failed after retries.") from last_exc


def hex_list_last_int(value: Any) -> int | None:
    if not isinstance(value, list) or not value:
        return None
    last = value[-1]
    if not isinstance(last, str) or not last.startswith("0x"):
        return None
    return int(last, 16)


def reward_average_wei(value: Any) -> int | None:
    if not isinstance(value, list):
        return None

    rewards: list[int] = []
    for block_rewards in value:
        if not isinstance(block_rewards, list) or len(block_rewards) < 2:
            continue
        reward = block_rewards[1]
        if isinstance(reward, str) and reward.startswith("0x"):
            rewards.append(int(reward, 16))

    if not rewards:
        return None
    return round(sum(rewards) / len(rewards))


def wei_to_gwei(value: int) -> Decimal:
    return Decimal(value) / GWEI_PER_WEI
