class ProxyManager:
    def __init__(self, config):
        self.enabled = config.proxies.enabled
        self.proxies = []
        self.index = 0
        self.failures = {}
        self.max_failures = config.proxies.max_failures
        self.cooldown_seconds = config.proxies.cooldown_minutes * 60

    def get_proxy(self) -> dict | None:
        if not self.enabled or not self.proxies:
            return None
        return {"server": self.proxies[self.index % len(self.proxies)]}

    def rotate(self):
        if self.proxies:
            self.index = (self.index + 1) % len(self.proxies)

    def mark_failure(self, proxy: str):
        self.failures[proxy] = self.failures.get(proxy, 0) + 1

    def load_from_file(self, path: str):
        from pathlib import Path
        p = Path(path)
        if p.exists():
            self.proxies = [line.strip() for line in p.read_text().splitlines() if line.strip()]
