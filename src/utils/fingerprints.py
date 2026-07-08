"""Fingerprints realistas para diversificar los contextos del navegador.

Cada fingerprint combina User-Agent + viewport + plataforma de forma coherente
(un UA de Windows va con un viewport tipico de Windows, etc.) para que la
combinacion no sea detectable como inconsistente.

La locale/timezone se mantienen en Paraguay a proposito: el objetivo es que
las busquedas parezcan de un usuario local, no rotar el pais.
"""
import random

# Combos coherentes (UA, plataforma, viewports tipicos de ese SO).
# Chrome reciente sobre Windows y macOS, resoluciones de escritorio comunes.
_PROFILES = [
    {
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "platform": "Windows",
        "viewports": [(1920, 1080), (1536, 864), (1366, 768), (1280, 720)],
    },
    {
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        "platform": "Windows",
        "viewports": [(1920, 1080), (1600, 900), (1440, 900), (1366, 768)],
    },
    {
        "ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "platform": "macOS",
        "viewports": [(1440, 900), (1512, 982), (1680, 1050), (2560, 1440)],
    },
    {
        "ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        "platform": "macOS",
        "viewports": [(1440, 900), (1280, 800), (1728, 1117)],
    },
    {
        "ua": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "platform": "Linux",
        "viewports": [(1920, 1080), (1366, 768), (1600, 900)],
    },
]


def random_fingerprint() -> dict:
    """Devuelve un fingerprint aleatorio coherente para un nuevo contexto.

    Returns dict con: user_agent, viewport {width, height}, con un pequeno
    jitter en el viewport para que dos contextos con el mismo perfil no
    queden identicos.
    """
    profile = random.choice(_PROFILES)
    base_w, base_h = random.choice(profile["viewports"])
    # Jitter chico (los navegadores reales no siempre estan maximizados)
    width = base_w - random.randint(0, 40)
    height = base_h - random.randint(0, 80)
    return {
        "user_agent": profile["ua"],
        "viewport": {"width": width, "height": height},
    }
