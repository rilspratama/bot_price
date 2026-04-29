from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from dataclasses import dataclass
from functools import wraps
from time import monotonic
from typing import Any, Awaitable, Callable, TypeVar, cast

F = TypeVar("F", bound=Callable[..., Any])

logger = logging.getLogger(__name__)


@dataclass
class _CachedException:
    exception: Exception


class ProviderCooldown:
    def __init__(self, provider: str) -> None:
        self.provider = provider
        self._active_until = 0.0

    def is_active(self) -> bool:
        now = monotonic()
        if now < self._active_until:
            logger.info(
                "provider_cooldown_active provider=%s remaining_ms=%s",
                self.provider,
                round((self._active_until - now) * 1000),
            )
            return True
        return False

    def activate(self, seconds: float) -> None:
        self._active_until = max(self._active_until, monotonic() + seconds)
        logger.warning(
            "provider_cooldown_set provider=%s duration_ms=%s",
            self.provider,
            round(seconds * 1000),
        )


def ttl_cache(ttl_seconds: float, maxsize: int = 128) -> Callable[[F], F]:
    def decorator(func: F) -> F:
        cache: OrderedDict[tuple[Any, ...], tuple[float, Any]] = OrderedDict()

        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            key = _cache_key(args, kwargs)
            now = monotonic()
            cached = cache.get(key)
            if cached is not None:
                timestamp, value = cached
                if now - timestamp < ttl_seconds:
                    cache.move_to_end(key)
                    return value
                cache.pop(key, None)

            value = func(*args, **kwargs)
            _store_cache_value(cache, key, now, value, maxsize)
            return value

        return cast(F, wrapper)

    return decorator


def async_ttl_cache(
    ttl_seconds: float,
    maxsize: int = 128,
    exception_ttl_seconds: float | None = None,
    cache_exceptions: tuple[type[Exception], ...] = (),
) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
    def decorator(func: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        cache: OrderedDict[tuple[Any, ...], tuple[float, Any]] = OrderedDict()
        inflight: dict[tuple[Any, ...], asyncio.Task[Any]] = {}

        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            key = _cache_key(args, kwargs)
            cached = _get_cached_value(cache, key, ttl_seconds)
            if cached is not None:
                if isinstance(cached, _CachedException):
                    logger.info("negative_cache_hit function=%s", func.__name__)
                    raise cached.exception
                logger.debug("cache_hit function=%s", func.__name__)
                return cached

            task = inflight.get(key)
            if task is not None:
                logger.info("cache_inflight_join function=%s", func.__name__)
                return await task

            logger.debug("cache_miss function=%s", func.__name__)
            task = asyncio.create_task(func(*args, **kwargs))
            inflight[key] = task
            try:
                value = await task
            except Exception as exc:
                if exception_ttl_seconds is not None and isinstance(exc, cache_exceptions):
                    timestamp = monotonic() - ttl_seconds + exception_ttl_seconds
                    _store_cache_value(cache, key, timestamp, _CachedException(exc), maxsize)
                raise
            else:
                _store_cache_value(cache, key, monotonic(), value, maxsize)
                return value
            finally:
                inflight.pop(key, None)

        return wrapper

    return decorator


def _cache_key(args: tuple[Any, ...], kwargs: dict[str, Any]) -> tuple[Any, ...]:
    return args + tuple(sorted(kwargs.items()))


def _get_cached_value(
    cache: OrderedDict[tuple[Any, ...], tuple[float, Any]],
    key: tuple[Any, ...],
    ttl_seconds: float,
) -> Any | None:
    now = monotonic()
    cached = cache.get(key)
    if cached is None:
        return None

    timestamp, value = cached
    if now - timestamp < ttl_seconds:
        cache.move_to_end(key)
        return value

    logger.debug("cache_expired")
    cache.pop(key, None)
    return None


def _store_cache_value(
    cache: OrderedDict[tuple[Any, ...], tuple[float, Any]],
    key: tuple[Any, ...],
    timestamp: float,
    value: Any,
    maxsize: int,
) -> None:
    cache[key] = (timestamp, value)
    cache.move_to_end(key)
    while len(cache) > maxsize:
        cache.popitem(last=False)
