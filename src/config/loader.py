import os
import shutil
import yaml
from pathlib import Path
from .settings import Settings


def _load_dotenv(env_path: Path = Path(".env")):
    """Load .env file into os.environ. No external deps needed."""
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if value and (value.startswith('"') or value.startswith("'")):
            value = value[1:-1]
        os.environ.setdefault(key, value)


def load_config(config_path: str = "config.yaml") -> Settings:
    _load_dotenv()

    target = Path(config_path)
    if not target.exists():
        example = Path("config.example.yaml")
        if example.exists():
            shutil.copy(example, target)
            print(f"[setup] config.example.yaml copiado a {config_path}")

    raw = yaml.safe_load(target.read_text(encoding="utf-8"))
    return Settings(**raw)
