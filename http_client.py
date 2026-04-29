from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

_clients: dict[tuple[str, float], httpx.AsyncClient] = {}


def get_async_client(provider: str, timeout: float) -> httpx.AsyncClient:
    key = (provider, timeout)
    client = _clients.get(key)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(timeout=timeout)
        _clients[key] = client
        logger.debug("http_client_created provider=%s", provider)
    else:
        logger.debug("http_client_reuse provider=%s", provider)
    return client


async def close_async_clients() -> None:
    clients = list(_clients.values())
    _clients.clear()
    for client in clients:
        if not client.is_closed:
            await client.aclose()
