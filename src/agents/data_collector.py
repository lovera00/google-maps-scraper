"""Agente 2: Data Collector.

Usa Playwright (modo live) o archivos HTML locales (modo mock/test).
Extrae datos crudos de la pagina de resultados de Google Maps.
"""
import asyncio
import logging
import random
import re
from pathlib import Path
from typing import List, Optional

from bs4 import BeautifulSoup

from ..models.query_task import QueryTask
from ..utils.fingerprints import random_fingerprint
from ..utils.rate_limiter import RateLimiter
from ..utils.selectors import SELECTORS
from ..utils.urls import extract_google_place_id

logger = logging.getLogger(__name__)


class GoogleMapsBlockedError(Exception):
    """Google Maps respondio con un status HTTP distinto de 200.

    Senal de que probablemente nos esta bloqueando/rate-limitando; no tiene
    sentido reintentar la tarea individual, hay que frenar todo el pipeline.
    """


class DataCollector:
    def __init__(self, config):
        self.config = config
        self.test_mode = config.test_mode
        self.headless = config.headless
        self.mock_dir = Path(config.mock.html_directory)
        self.simulate_delay = config.mock.simulate_delay
        self.mock_delay = config.mock.mock_delay_seconds
        self.max_scrolls = config.rate_limit.max_scroll_iterations
        self.rate_limiter = RateLimiter(
            config.rate_limit.request_delay_seconds,
            min_delay=config.rate_limit.request_delay_min_seconds,
            max_delay=config.rate_limit.request_delay_max_seconds,
        )
        self.browser = None
        self.context = None
        self.page = None

    async def setup(self):
        if self.test_mode or self.config.test_mode:
            self.test_mode = True
            logger.info("DataCollector iniciado en modo TEST (mocks HTML)")
            return
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            logger.error("Playwright no instalado. Ejecuta: pip install playwright && playwright install")
            raise
        self._playwright = await async_playwright().start()
        self.browser = await self._playwright.chromium.launch(
            headless=self.headless,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        logger.info("DataCollector: navegador Playwright iniciado")

    async def teardown(self):
        """Cierra navegador y playwright de forma robusta (tolera browser ya muerto)."""
        if self.browser:
            try:
                await self.browser.close()
            except Exception:
                pass  # browser ya estaba desconectado
        self.browser = None
        if hasattr(self, "_playwright") and self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass
        self._playwright = None
        logger.info("DataCollector: navegador cerrado")

    async def scrape(self, task: QueryTask) -> List[dict]:
        if self.test_mode:
            results = await self._scrape_mock(task)
        else:
            max_retries = 3
            results = []
            for attempt in range(max_retries + 1):
                try:
                    results = await self._scrape_live(task)
                    break
                except Exception as e:
                    msg = str(e)
                    is_browser_crash = any(kw in msg for kw in (
                        "Connection closed",
                        "Target page, context or browser has been closed",
                        "Browser has been closed",
                        "Protocol error",
                    ))
                    if is_browser_crash and attempt < max_retries:
                        delay = 2 ** attempt  # 1s, 2s, 4s
                        logger.warning(
                            f"Navegador desconectado (intento {attempt + 1}/{max_retries}), "
                            f"reiniciando en {delay}s... ({e})"
                        )
                        await asyncio.sleep(delay)
                        await self.teardown()
                        await self.setup()
                    elif is_browser_crash:
                        logger.error(
                            f"Navegador desconectado tras {max_retries} reintentos. "
                            f"Abortando tarea: {task.category} @ {task.center_lat:.4f},{task.center_lng:.4f}"
                        )
                        results = []
                        break
                    else:
                        raise
        # Inyectar la categoria de busqueda en cada resultado
        for r in results:
            r["search_category"] = task.category
        return results

    # ── Mock mode ──────────────────────────────────────────────

    async def _scrape_mock(self, task: QueryTask) -> List[dict]:
        if self.simulate_delay:
            await asyncio.sleep(self.mock_delay + random.uniform(0, 0.3))

        html = self._load_mock_html(task)
        if not html:
            return []

        soup = BeautifulSoup(html, "lxml")

        # Overflow detection
        overflow_div = soup.select_one("div.overflow-simulated")
        if overflow_div:
            try:
                count = int(overflow_div.text.strip())
            except ValueError:
                count = 120
            # Generar resultados sinteticos para simular overflow
            return self._generate_overflow_results(task, count)

        return self._parse_results(soup)

    def _load_mock_html(self, task: QueryTask) -> Optional[str]:
        resolve_order = [
            f"search_asuncion_1.html",
            f"search_asuncion_2.html",
            f"search_encarnacion.html",
            f"search_empty.html",
        ]

        # Matching por categoria
        category_keywords = {
            "restaurantes": ["asuncion_1", "asuncion_2", "encarnacion"],
            "farmacias": ["asuncion_1", "asuncion_2", "encarnacion"],
            "supermercados": ["asuncion_1", "asuncion_2", "encarnacion"],
            "hoteles": ["asuncion_1", "encarnacion"],
            "hospitales": ["asuncion_1"],
            "clinicas": ["encarnacion"],
            "colegios": ["asuncion_2"],
            "universidades": ["asuncion_2"],
            "bancos": ["asuncion_1"],
            "mecanicos": ["asuncion_2"],
            "tiendas de ropa": ["asuncion_2"],
            "ferreterias": ["asuncion_1"],
            "veterinarias": ["asuncion_2"],
            "gimnasios": ["asuncion_1", "asuncion_2", "encarnacion"],
            "peluquerias": ["asuncion_2"],
            "panaderias": ["asuncion_1"],
            "carnicerias": ["asuncion_1"],
            "verdulerias": ["asuncion_2"],
            "librerias": ["asuncion_1"],
            "estaciones de servicio": ["asuncion_2"],
        }

        candidates = category_keywords.get(task.category.lower(), ["asuncion_1"])
        for suffix in candidates:
            path = self.mock_dir / f"search_{suffix}.html"
            if path.exists():
                logger.debug(f"Mock: usando {path.name} para categoria '{task.category}'")
                return path.read_text(encoding="utf-8")

        default = self.mock_dir / "search_empty.html"
        if default.exists():
            return default.read_text(encoding="utf-8")
        return None

    def _generate_overflow_results(self, task: QueryTask, count: int) -> List[dict]:
        results = []
        base_lat = task.center_lat
        base_lng = task.center_lng
        for i in range(count):
            results.append({
                "name": f"Negocio Overflow #{i+1}",
                "lat": round(base_lat + random.uniform(-0.005, 0.005), 6),
                "lng": round(base_lng + random.uniform(-0.005, 0.005), 6),
                "category": task.category,
                "source_url": f"https://www.google.com/maps/place/Overflow+{i+1}/"
                              f"@{base_lat},{base_lng},17z",
            })
        return results

    # ── Live mode (Playwright) ─────────────────────────────────

    async def _scrape_live(self, task: QueryTask) -> List[dict]:
        await self.rate_limiter.wait()
        url = task.to_maps_url()
        logger.info(f"Scraping: {url}")

        fp = random_fingerprint()
        context = await self.browser.new_context(
            viewport=fp["viewport"],
            user_agent=fp["user_agent"],
            locale="es-PY",
            timezone_id="America/Asuncion",
        )
        page = await context.new_page()

        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            if response is not None and response.status != 200:
                raise GoogleMapsBlockedError(
                    f"Google Maps respondio HTTP {response.status} (esperado 200) para {url}"
                )
            await asyncio.sleep(random.uniform(2, 4))

            # Manejar popup de consentimiento
            await self._dismiss_consent(page)

            # Scroll para cargar todos los resultados
            await self._scroll_results(page)

            html = await page.content()

            # Diagnostico: guardar HTML para inspeccionar estructura
            self._save_debug_html(html, task)

            soup = BeautifulSoup(html, "lxml")
            return self._parse_results(soup)
        except GoogleMapsBlockedError:
            raise
        except Exception as e:
            msg = str(e)
            # Errores de crash de navegador: re-lanzar para que scrape() reconecte
            if any(kw in msg for kw in (
                "Connection closed",
                "Target page, context or browser has been closed",
                "Browser has been closed",
                "Protocol error",
            )):
                raise
            logger.error(f"Error en scrape live: {e}")
            return []
        finally:
            await context.close()

    async def _dismiss_consent(self, page):
        for sel in SELECTORS["consent_button"]:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=2000):
                    await btn.click()
                    await asyncio.sleep(1)
                    return
            except Exception:
                pass

    async def _scroll_results(self, page):
        for i in range(self.max_scrolls):
            prev_count = await page.locator(SELECTORS["result_cards"][0]).count()
            try:
                feed = page.locator(SELECTORS["results_container"][0])
                if await feed.is_visible():
                    await feed.evaluate("el => el.scrollTop = el.scrollHeight")
            except Exception:
                pass
            await asyncio.sleep(random.uniform(1.5, 2.5))
            curr_count = await page.locator(SELECTORS["result_cards"][0]).count()
            if curr_count == prev_count:
                break

    # ── Diagnostic ─────────────────────────────────────────────

    def _save_debug_html(self, html: str, task: QueryTask):
        """Guarda el HTML crudo de un scrape live para diagnostico de selectores."""
        debug_dir = Path("data/debug_html")
        try:
            debug_dir.mkdir(parents=True, exist_ok=True)
            cat_slug = task.category.replace(" ", "_")[:30]
            filename = f"live_{cat_slug}_{task.center_lat:.4f}_{task.center_lng:.4f}.html"
            (debug_dir / filename).write_text(html, encoding="utf-8")
            logger.debug(f"HTML debug guardado: {filename}")
        except Exception:
            pass  # No queremos que falle el scrape por el debug

    # ── Parsing ────────────────────────────────────────────────

    # Palabras clave que jamas son una categoria
    _NON_CATEGORY_PATTERNS = [
        "reseña", "estrellas", "estrellas", "cerrado", "abierto",
        "km", "mts", "metros", "dirección", "como llegar", "llamar",
        "sitio web", "compartir", "guardar", "gratis",
    ]

    def _extract_category(self, card) -> str:
        """Extrae la categoria de una card de resultado usando multiples estrategias."""

        # --- Estrategia 1: Selectores de clase conocidos ---
        known_selectors = [
            "[class*='W4Efsd'] span",
            "[class*='W4Efsd']",
            "div[class*='fontBodyMedium'] span",
            "div[class*='fontBodyMedium']",
            "span[class*='fontBodyMedium']",
            "[class*='category']",
            "div[role='heading'] + div span",
        ]
        for sel in known_selectors:
            el = card.select_one(sel)
            text = (el.get_text(strip=True) if el else "")
            if self._looks_like_category(text):
                return text

        # --- Estrategia 2: Todos los spans/divs, buscar el que parece categoria ---
        name = card.get("aria-label", "")
        all_elements = card.select("span, div")
        for el in all_elements:
            text = el.get_text(strip=True)
            if self._looks_like_category(text) and text not in name:
                return text

        # --- Estrategia 3: Buscar keywords conocidas de rubros en texto largo ---
        full_text = card.get_text(separator=" ", strip=True)
        known_categories = [
            "restaurante", "farmacia", "supermercado", "hotel", "hospital",
            "clinica", "colegio", "universidad", "banco", "mecanico",
            "tienda de ropa", "ferreteria", "veterinaria", "gimnasio",
            "peluqueria", "panaderia", "carniceria", "verduleria",
            "libreria", "estacion de servicio", "bar", "cafeteria",
            "iglesia", "comisaria", "municipalidad", "correo",
            "farmacia veterinaria", "centro comercial", "shopping",
            "estadio", "plaza", "parque", "museo", "teatro", "cine",
            "heladeria", "pizzeria", "confiteria", "rotiseria",
            "despensa", "almacen", "kiosco", "fotocopiadora",
            "lavadero", "taller", "gomeria", "estetica", "day spa",
            "dentista", "odontologo", "abogado", "contador",
            "arquitecto", "ingeniero", "electricista", "plomero",
            "gasista", "pintor", "albañil", "carpintero",
            "cerrajero", "tapicero", "vidriero", "herrero",
            "imprenta", "grafica", "publicidad", "seguros",
            "inmobiliaria", "constructora", "transporte",
            "remis", "taxi", "alquiler", "venta", "compra",
            "distribuidora", "mayorista", "minorista",
            "importadora", "exportadora", "fabrica", "industria",
            "laboratorio", "consultorio", "estudio juridico",
            "escribania", "notaria", "registro", "juzgado",
            "casa de cambio", "financiera", "cooperativa",
            "asociacion", "fundacion", "ong", "club deportivo",
            "complejo deportivo", "polideportivo", "natatorio",
            "academia", "instituto", "facultad", "escuela",
            "jardin", "guarderia", "hogar de ancianos",
            "funeraria", "velatorio", "tanatorio",
        ]
        full_lower = full_text.lower()
        for cat in known_categories:
            if cat in full_lower:
                return cat

        return ""

    def _looks_like_category(self, text: str) -> bool:
        """Heuristica: ¿este texto parece una categoria de negocio?"""
        if not text or len(text) < 2 or len(text) > 60:
            return False
        lower = text.lower()
        # Descartar ratings, numeros puros, direcciones
        if lower[0].isdigit():
            return False
        if any(p in lower for p in self._NON_CATEGORY_PATTERNS):
            return False
        if lower in ("paraguay", "asuncion", "asuncion", "encarnacion",
                      "ciudad del este", "paraguay", "py", "españa", "argentina"):
            return False
        return True

    def _parse_results(self, soup: BeautifulSoup) -> List[dict]:
        results = []
        seen = set()

        # Selector estable: role="feed" es atributo ARIA (Google lo necesita para
        # lectores de pantalla, no lo ofusca). /maps/place/ en href es estable hace años.
        for card in soup.select('div[role="feed"] a[href*="/maps/place/"]'):
            href = card.get("href", "")
            if not href or "/maps/place/" not in href:
                continue

            name = card.get("aria-label", "")
            if not name or name in seen:
                continue
            seen.add(name)

            lat, lng = self._extract_coords_from_url(href)

            # Buscar el contenedor del resultado (padre comun del card y la metadata)
            container = card.find_parent('div', class_='Nv2PK')
            category, address, rating, review_count = self._extract_metadata(container, card)

            results.append({
                "name": name.strip(),
                "lat": lat if lat is not None else 0.0,
                "lng": lng if lng is not None else 0.0,
                "category": category,
                "address": address,
                "source_url": f"https://www.google.com{href}" if href.startswith("/") else href,
                "google_place_id": extract_google_place_id(href),
                "rating": rating,
                "review_count": review_count,
            })

        return results

    def _extract_metadata(self, container, card) -> tuple:
        """Extrae categoria, direccion, rating y review_count del contenedor."""
        category = ""
        address = ""
        rating = None
        review_count = None

        if container is None:
            return category, address, rating, review_count

        # --- Rating: span.ZkP5Je con aria-label "X.X estrellas" ---
        rating_el = container.select_one('span.ZkP5Je')
        if rating_el:
            rating_text = rating_el.get("aria-label", "") or rating_el.get_text(strip=True)
            match = re.search(r'(\d+[\.,]?\d*)', rating_text)
            if match:
                rating = float(match.group(1).replace(",", "."))

        # --- Review count: extraer de texto con parentesis como "(320)" ---
        review_el = container.select_one('span[aria-label*="reseñas"], span[aria-label*="opiniones"]')
        if not review_el:
            # Intentar extraer de cualquier texto con formato "(N)"
            all_text = container.get_text(separator=" ", strip=True)
            match = re.search(r'\((\d[\d,\.]*)\s*(?:reseñas?|opiniones?|reviews?)?\)', all_text, re.IGNORECASE)
            if match:
                review_count = int(match.group(1).replace(",", "").replace(".", ""))

        # --- Categoria + direccion ---
        # Buscar cualquier texto con · que no sea precio (₲, PYG) ni rating
        found = False
        for el in container.select("*"):
            text = el.get_text(separator=" ", strip=True)
            if "·" not in text:
                continue
            # Descartar lineas de precio: contienen ₲, PYG, o numeros grandes con .
            if "₲" in text or "PYG" in text:
                continue
            # Descartar si es solo un numero (rating)
            clean_num = text.replace(".", "").replace(",", "").replace("·", "").replace(" ", "")
            if clean_num.isdigit():
                continue
            # Descartar si tiene rating con parentesis tipo "4.5 (320)"
            if re.search(r'\(\d+', text):
                continue

            parts = [p.strip() for p in text.split("·")]
            if parts and len(parts[0]) > 1:
                category = parts[0]
                if len(parts) >= 2:
                    second = parts[1].strip()
                    if len(second) <= 2:
                        address = parts[2].strip() if len(parts) > 2 else ""
                    else:
                        address = second
                found = True
                break

        # Fallback sin · (hoteles, posadas, hospitales): buscar texto corto
        if not found:
            card_name = card.get("aria-label", "") if card is not None else ""
            for el in container.select("div, span"):
                text = el.get_text(separator=" ", strip=True)
                if not text or len(text) > 60:
                    continue
                clean = text.replace(".", "").replace(",", "").replace(" ", "")
                if clean.isdigit():
                    continue
                if any(ord(c) > 0xE000 for c in text):
                    continue
                if "₲" in text or "PYG" in text:
                    continue
                # Descartar textos que son el nombre del negocio + rating + ruido
                if "No hay opiniones" in text or "No hay reseñas" in text:
                    continue
                # Si empieza con el nombre del negocio, recortar
                if card_name and text.startswith(card_name):
                    remaining = text[len(card_name):].strip()
                    if remaining:
                        text = remaining
                    else:
                        continue
                # Si contiene rating numerico (ej "4.5"), probablemente no es solo categoria
                if re.search(r'\d[\.,]\d\s*(estrellas|opiniones|reseñas)?', text):
                    continue
                if len(text) >= 2:
                    category = text
                    break

        # Limpiar direccion: remover estado abierto/cerrado
        if address:
            for suffix in ["Abierto", "Cerrado", "Cierra", "Abre"]:
                idx = address.find(suffix)
                if idx > 0:
                    address = address[:idx].strip()
                    break

        return category, address, rating, review_count

    @staticmethod
    def _extract_coords_from_url(url: str) -> tuple:
        match = re.search(r'/@(-?\d+\.\d+),(-?\d+\.\d+),\d+z', url)
        if match:
            return float(match.group(1)), float(match.group(2))
        match = re.search(r'!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)', url)
        if match:
            return float(match.group(1)), float(match.group(2))
        return None, None
