import asyncio
import random
import time


class RateLimiter:
    """Delay dinamico entre requests.

    En vez de un intervalo fijo (predecible y facil de detectar), espera un
    tiempo aleatorio dentro de [min_delay, max_delay] antes de cada request.

    Compatibilidad: si se construye con un solo valor (delay_seconds), se usa
    como centro de un rango +-40% para mantener el comportamiento anterior
    pero con jitter.
    """

    def __init__(self, delay_seconds: float = 3.0,
                 min_delay: float = None, max_delay: float = None):
        if min_delay is not None and max_delay is not None:
            self.min_delay = float(min_delay)
            self.max_delay = float(max_delay)
        else:
            # Derivar un rango con jitter alrededor del valor unico
            self.min_delay = max(0.0, delay_seconds * 0.6)
            self.max_delay = delay_seconds * 1.4
        if self.max_delay < self.min_delay:
            self.min_delay, self.max_delay = self.max_delay, self.min_delay
        self._last_request = 0.0

    async def wait(self):
        target = random.uniform(self.min_delay, self.max_delay)
        now = time.monotonic()
        elapsed = now - self._last_request
        if elapsed < target:
            await asyncio.sleep(target - elapsed)
        self._last_request = time.monotonic()
