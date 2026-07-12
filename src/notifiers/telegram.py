import asyncio
import logging
import time
from datetime import datetime
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)


class TelegramNotifier:
    RATE_LIMIT_WINDOW = 60.0
    RATE_LIMIT_MAX = 20
    _SEND_TIMEOUT = 10.0

    def __init__(self, bot_token: str = "", chat_id: str = "",
                 min_level: str = "warning",
                 notify_on_start: bool = True,
                 notify_on_complete: bool = True):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self._level_map = {"debug": 0, "info": 1, "warning": 2, "error": 3, "critical": 4}
        self._min_priority = self._level_map.get(min_level.lower(), 2)
        self.notify_on_start = notify_on_start
        self.notify_on_complete = notify_on_complete
        self._session: Optional[aiohttp.ClientSession] = None
        self._msg_timestamps: list[float] = []
        self._base_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self.enabled = bool(bot_token and chat_id)

    async def _ensure_session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()

    async def _send(self, message: str, level: str = "info"):
        if not self.enabled:
            return
        priority = self._level_map.get(level.lower(), 1)
        if priority < self._min_priority:
            return
        await self._ensure_session()
        now = time.monotonic()
        self._msg_timestamps = [t for t in self._msg_timestamps if now - t < self.RATE_LIMIT_WINDOW]
        if len(self._msg_timestamps) >= self.RATE_LIMIT_MAX:
            logger.debug("Telegram rate limit reached, skipping message")
            return
        self._msg_timestamps.append(now)
        try:
            async with self._session.post(
                self._base_url,
                data={"chat_id": self.chat_id, "text": message, "parse_mode": "HTML"},
                timeout=aiohttp.ClientTimeout(total=self._SEND_TIMEOUT),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning(f"Telegram API error (HTTP {resp.status}): {body[:200]}")
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.warning(f"Fallo al enviar notificacion Telegram: {e}")

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    def _fmt_time(self) -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _fmt_task(self, task) -> str:
        return (
            f"📋 Categoria: {task.category}\n"
            f"📍 {task.center_lat:.4f}, {task.center_lng:.4f}"
        )

    async def notify_block(self, consecutive: int, max_consecutive: int,
                           delay_seconds: float, task=None):
        mins = int(delay_seconds // 60)
        secs = int(delay_seconds % 60)
        msg = (
            f"\U0001f6ab <b>GOOGLE MAPS BLOQUEO</b> (#{consecutive}/{max_consecutive})\n"
            f"\u23f1 Pausa: {mins}m {secs}s\n"
        )
        if task:
            msg += f"{self._fmt_task(task)}\n"
        msg += f"\U0001f550 {self._fmt_time()}"
        await self._send(msg, level="critical")

    async def notify_task_failed(self, task, error: str,
                                 retry_count: int, max_retries: int):
        msg = (
            f"\u274c <b>TAREA FALLIDA</b> (retries {retry_count}/{max_retries})\n"
            f"{self._fmt_task(task)}\n"
            f"\U0001f534 Error: {error[:200]}\n"
            f"\U0001f550 {self._fmt_time()}"
        )
        await self._send(msg, level="error")

    async def notify_abort(self, reason: str):
        msg = (
            f"\U0001f6a8 <b>PIPELINE ABORTADO</b>\n"
            f"\u26a0\ufe0f {reason[:300]}\n"
            f"\U0001f550 {self._fmt_time()}"
        )
        await self._send(msg, level="critical")

    async def notify_browser_crash(self, attempt: int, max_retries: int,
                                   task=None, recovered: bool = True):
        status = "\U0001f504 Recovery exitoso" if recovered else "\U0001f4a5 Sin recovery"
        msg = (
            f"\U0001f4a5 <b>CRASH NAVEGADOR</b>\n"
            f"{status} (intento {attempt}/{max_retries})\n"
        )
        if task:
            msg += f"{self._fmt_task(task)}\n"
        msg += f"\U0001f550 {self._fmt_time()}"
        level = "warning" if recovered else "error"
        await self._send(msg, level=level)

    async def notify_pipeline_start(self, config_summary: str):
        if not self.notify_on_start:
            return
        msg = (
            f"\u2705 <b>PIPELINE INICIADO</b>\n"
            f"\U0001f680 {config_summary}\n"
            f"\U0001f550 {self._fmt_time()}"
        )
        await self._send(msg, level="info")

    async def notify_pipeline_end(self, stats_summary: str):
        if not self.notify_on_complete:
            return
        msg = (
            f"\U0001f3c1 <b>PIPELINE COMPLETADO</b>\n"
            f"\U0001f4ca {stats_summary}\n"
            f"\U0001f550 {self._fmt_time()}"
        )
        await self._send(msg, level="info")

    async def notify_fatal_error(self, error: str):
        msg = (
            f"\U0001f525 <b>ERROR FATAL EN PIPELINE</b>\n"
            f"\U0001f534 {error[:300]}\n"
            f"\U0001f550 {self._fmt_time()}"
        )
        await self._send(msg, level="critical")

    async def notify_overflow_max_depth(self, task):
        msg = (
            f"\u26a0\ufe0f <b>OVERFLOW PROF. MAXIMA</b>\n"
            f"Celda dividida pero ya en depth maxima\n"
            f"{self._fmt_task(task)}\n"
            f"\U0001f550 {self._fmt_time()}"
        )
        await self._send(msg, level="warning")

    async def notify_transient_retry(self, task, retry_count: int,
                                     max_retries: int, error: str):
        msg = (
            f"\u26a0\ufe0f <b>FALLO TRANSITORIO</b> (retry {retry_count}/{max_retries})\n"
            f"{self._fmt_task(task)}\n"
            f"\U0001f538 Error: {error[:200]}\n"
            f"\U0001f550 {self._fmt_time()}"
        )
        await self._send(msg, level="warning")
