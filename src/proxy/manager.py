"""ProxyManager (E3): rotacion round-robin de proxies con health-check pasivo.

Cada scrape del DataCollector adquiere un proxy via acquire() y reporta el
resultado (report_success / report_failure). Un proxy que acumula
`max_failures` fallos entra en cooldown y se lo saltea hasta que expire. Si no
hay ningun proxy sano disponible, acquire() devuelve None y el collector cae a
conexion directa (IP real) para no frenar el run.

Formato de cada linea del archivo de proxies (esquema opcional, default http):
    host:port
    http://host:port
    https://host:port
    socks5://host:port
    http://user:pass@host:port
"""
import logging
import time
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


def _parse_proxy_line(line: str) -> dict | None:
    """Convierte 'scheme://user:pass@host:port' en el dict de Playwright
    ({server, username?, password?}). Devuelve None si no es parseable."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if "://" not in line:
        line = "http://" + line  # esquema por defecto
    try:
        u = urlparse(line)
        if not u.hostname or not u.port:
            return None
        pw = {"server": f"{u.scheme}://{u.hostname}:{u.port}"}
        if u.username:
            pw["username"] = u.username
        if u.password:
            pw["password"] = u.password
        return pw
    except Exception:
        return None


class ProxyEntry:
    __slots__ = ("raw", "pw", "consecutive_failures", "cooldown_until",
                 "total_success", "total_failure")

    def __init__(self, raw: str, pw: dict):
        self.raw = raw
        self.pw = pw                     # dict para new_context(proxy=...)
        self.consecutive_failures = 0
        self.cooldown_until = 0.0        # time.monotonic() hasta que sale de cooldown
        self.total_success = 0
        self.total_failure = 0

    def available(self, now: float) -> bool:
        return now >= self.cooldown_until


class ProxyManager:
    def __init__(self, config):
        self.enabled = config.proxies.enabled
        self.max_failures = max(1, config.proxies.max_failures)
        self.cooldown_seconds = config.proxies.cooldown_minutes * 60
        self.entries: list[ProxyEntry] = []
        self._index = 0
        self._all_cooling_warned = False
        if self.enabled:
            self.load_from_file(config.proxies.file_path)

    def load_from_file(self, path: str):
        p = Path(path)
        if not p.exists():
            logger.warning(f"Proxies habilitados pero no existe {path}; "
                           f"el scraper correra con IP directa.")
            self.enabled = False
            return
        entries = []
        for line in p.read_text(encoding="utf-8").splitlines():
            pw = _parse_proxy_line(line)
            if pw:
                entries.append(ProxyEntry(line.strip(), pw))
        self.entries = entries
        if not entries:
            logger.warning(f"{path} sin proxies validos; el scraper correra con IP directa.")
            self.enabled = False
        else:
            logger.info(f"ProxyManager: {len(entries)} proxies cargados de {path}")

    def acquire(self) -> ProxyEntry | None:
        """Proximo proxy sano (round-robin, salteando cooldown), o None si no
        hay proxies o todos estan en cooldown (-> IP directa)."""
        if not self.enabled or not self.entries:
            return None
        now = time.monotonic()
        n = len(self.entries)
        for _ in range(n):
            entry = self.entries[self._index % n]
            self._index = (self._index + 1) % n
            if entry.available(now):
                return entry
        if not self._all_cooling_warned:
            logger.warning("Todos los proxies en cooldown; usando IP directa temporalmente.")
            self._all_cooling_warned = True
        return None

    def report_success(self, entry: "ProxyEntry | None"):
        if entry is None:
            return
        entry.consecutive_failures = 0
        entry.total_success += 1
        self._all_cooling_warned = False

    def report_failure(self, entry: "ProxyEntry | None"):
        if entry is None:
            return
        entry.consecutive_failures += 1
        entry.total_failure += 1
        if entry.consecutive_failures >= self.max_failures:
            entry.cooldown_until = time.monotonic() + self.cooldown_seconds
            entry.consecutive_failures = 0
            logger.info(
                f"Proxy {entry.raw} -> cooldown {self.cooldown_seconds // 60}min "
                f"(ok={entry.total_success}, fail={entry.total_failure})"
            )

    def stats(self) -> str:
        if not self.entries:
            return "sin proxies"
        now = time.monotonic()
        active = sum(1 for e in self.entries if e.available(now))
        return f"{active}/{len(self.entries)} proxies activos"
