from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx

from cache_utils import async_ttl_cache
from http_client import get_async_client
from logging_utils import elapsed_ms, start_timer

logger = logging.getLogger(__name__)


TIMEOUT_SECONDS = 12
WEI_PER_NATIVE = Decimal(10) ** 18
LAMPORTS_PER_SOL = Decimal(10) ** 9
SOLANA_RPC_URL = "https://solana-rpc.publicnode.com"
RPC_RETRY_ATTEMPTS = 2
RPC_RETRY_DELAY_SECONDS = 0.5


@dataclass(frozen=True)
class ChainRpc:
    chain_name: str
    symbol: str
    rpc_url: str


@dataclass(frozen=True)
class NativeBalance:
    chain_name: str
    symbol: str
    balance: Decimal
    rpc_url: str


CHAINS = [
    ChainRpc("Ethereum", "ETH", "https://ethereum-rpc.publicnode.com"),
    ChainRpc("BNB Smart Chain", "BNB", "https://bsc-rpc.publicnode.com"),
    ChainRpc("Polygon", "POL", "https://polygon-bor-rpc.publicnode.com"),
    ChainRpc("Arbitrum One", "ETH", "https://arbitrum-one-rpc.publicnode.com"),
    ChainRpc("Optimism", "ETH", "https://optimism-rpc.publicnode.com"),
    ChainRpc("Base", "ETH", "https://base-rpc.publicnode.com"),
    ChainRpc("Avalanche C-Chain", "AVAX", "https://avalanche-c-chain-rpc.publicnode.com"),
]


class RpcBalanceError(Exception):
    pass


@async_ttl_cache(ttl_seconds=20, maxsize=256)
async def async_get_balances(address: str) -> list[NativeBalance]:
    started = start_timer()
    address = address.strip()
    balances_by_chain: dict[str, NativeBalance] = {}
    errors: list[str] = []

    chains = filtered_chains()
    client = get_async_client("rpc_balance", TIMEOUT_SECONDS)
    results = await asyncio.gather(
        *(async_get_native_balance(address, chain, client) for chain in chains),
        return_exceptions=True,
    )

    for chain, result in zip(chains, results):
        if isinstance(result, NativeBalance):
            balances_by_chain[chain.chain_name] = result
        elif isinstance(result, RpcBalanceError):
            errors.append(f"{chain.chain_name}: {result}")
        else:
            errors.append(f"{chain.chain_name}: {result}")

    balances = [balance for chain in chains if (balance := balances_by_chain.get(chain.chain_name))]
    logger.info(
        "rpc_balance_summary success=%s failed=%s duration_ms=%s",
        len(balances),
        len(errors),
        elapsed_ms(started),
    )
    if not balances and errors:
        logger.error("rpc_balance_failed duration_ms=%s", elapsed_ms(started))
        raise RpcBalanceError("Semua RPC gagal dihubungi.")

    return balances


@async_ttl_cache(ttl_seconds=20, maxsize=256)
async def async_get_solana_balance(address: str) -> NativeBalance:
    started = start_timer()
    address = address.strip()
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getBalance",
        "params": [address, {"commitment": "finalized"}],
    }

    client = get_async_client("rpc_balance", TIMEOUT_SECONDS)
    try:
        response = await post_rpc_with_retry(client, SOLANA_RPC_URL, payload, "Solana", started)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning(
            "solana_rpc_request_error duration_ms=%s error=%s",
            elapsed_ms(started),
            exc.__class__.__name__,
        )
        raise RpcBalanceError(str(exc)) from exc

    logger.info(
        "solana_rpc_request_done status=%s duration_ms=%s",
        response.status_code,
        elapsed_ms(started),
    )
    try:
        data = response.json()
    except ValueError as exc:
        logger.warning("solana_rpc_json_error duration_ms=%s", elapsed_ms(started))
        raise RpcBalanceError("Response RPC Solana bukan JSON valid.") from exc

    if not isinstance(data, dict):
        raise RpcBalanceError("Format response RPC Solana tidak sesuai.")

    error = data.get("error")
    if error:
        raise RpcBalanceError(str(error))

    result = data.get("result")
    if not isinstance(result, dict):
        raise RpcBalanceError("Result balance RPC Solana tidak valid.")

    lamports = result.get("value")
    if not isinstance(lamports, int):
        raise RpcBalanceError("Value balance RPC Solana tidak valid.")

    balance = Decimal(lamports) / LAMPORTS_PER_SOL
    return NativeBalance("Solana", "SOL", balance, SOLANA_RPC_URL)


def filtered_chains() -> list[ChainRpc]:
    configured = os.getenv("RPC_BALANCE_CHAINS", "").strip()
    if not configured:
        return CHAINS

    requested = {item.strip().lower() for item in configured.split(",") if item.strip()}
    if not requested:
        return CHAINS

    chains = [
        chain
        for chain in CHAINS
        if chain.chain_name.lower() in requested or chain.symbol.lower() in requested
    ]
    if not chains:
        logger.warning("rpc_balance_chain_filter_empty configured=%s", configured)
        return CHAINS

    logger.info("rpc_balance_chain_filter count=%s configured=%s", len(chains), configured)
    return chains


async def post_rpc_with_retry(
    client: httpx.AsyncClient,
    url: str,
    payload: dict[str, Any],
    chain_name: str,
    started: float,
) -> httpx.Response:
    last_exc: httpx.RequestError | None = None
    for attempt in range(1, RPC_RETRY_ATTEMPTS + 1):
        try:
            return await client.post(url, json=payload)
        except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ConnectError) as exc:
            last_exc = exc
            logger.warning(
                "rpc_request_retry chain=%s attempt=%s duration_ms=%s error=%s",
                chain_name,
                attempt,
                elapsed_ms(started),
                exc.__class__.__name__,
            )
            if attempt < RPC_RETRY_ATTEMPTS:
                await asyncio.sleep(RPC_RETRY_DELAY_SECONDS)
        except httpx.RequestError as exc:
            raise RpcBalanceError(str(exc)) from exc

    raise RpcBalanceError("RPC gagal setelah retry.") from last_exc


async def async_get_native_balance(
    address: str, chain: ChainRpc, client: httpx.AsyncClient
) -> NativeBalance:
    started = start_timer()
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_getBalance",
        "params": [address, "latest"],
    }

    try:
        response = await post_rpc_with_retry(client, chain.rpc_url, payload, chain.chain_name, started)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning(
            "rpc_request_error chain=%s duration_ms=%s error=%s",
            chain.chain_name,
            elapsed_ms(started),
            exc.__class__.__name__,
        )
        raise RpcBalanceError(str(exc)) from exc

    logger.info(
        "rpc_request_done chain=%s status=%s duration_ms=%s",
        chain.chain_name,
        response.status_code,
        elapsed_ms(started),
    )
    try:
        data = response.json()
    except ValueError as exc:
        logger.warning(
            "rpc_json_error chain=%s duration_ms=%s",
            chain.chain_name,
            elapsed_ms(started),
        )
        raise RpcBalanceError("Response RPC bukan JSON valid.") from exc

    if not isinstance(data, dict):
        raise RpcBalanceError("Format response RPC tidak sesuai.")

    error = data.get("error")
    if error:
        raise RpcBalanceError(str(error))

    result = data.get("result")
    if not isinstance(result, str) or not result.startswith("0x"):
        raise RpcBalanceError("Result balance RPC tidak valid.")

    balance_wei = int(result, 16)
    balance = Decimal(balance_wei) / WEI_PER_NATIVE
    return NativeBalance(chain.chain_name, chain.symbol, balance, chain.rpc_url)
