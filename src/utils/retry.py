import asyncio
import functools
import logging

logger = logging.getLogger(__name__)


def async_retry(max_retries: int = 3, base_delay: float = 2.0):
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    last_exc = e
                    if attempt < max_retries:
                        delay = base_delay ** (attempt + 1)
                        logger.warning(f"Retry {attempt + 1}/{max_retries} para "
                                       f"{func.__name__}: {e}. Esperando {delay:.1f}s")
                        await asyncio.sleep(delay)
            raise last_exc
        return wrapper
    return decorator
