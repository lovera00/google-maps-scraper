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
    """Google Maps nos esta bloqueando (status != 200, o captcha / pagina
    /sorry servida con 200).

    Ya NO aborta el pipeline de inmediato: el orquestador dispara una pausa
    global con backoff exponencial (ver DataCollector.handle_block) y reintenta
    la tarea. Solo se aborta si el bloqueo persiste tras `max_consecutive_blocks`
    pausas.
    """


class ScrapeTransientError(Exception):
    """Fallo transitorio de un scrape (timeout de navegacion, error de red).

    No es un bloqueo de Google ni un crash del navegador: la tarea se re-encola
    con retry_count++ hasta `max_task_retries` antes de marcarse failed, en vez
    de perderse como 'completed con 0 resultados'.
    """


class DataCollector:
    # Cap de resultados de Google Maps por busqueda. Al alcanzarlo no tiene
    # sentido seguir scrolleando (el orquestador lo usa como umbral de overflow).
    RESULTS_CAP = 120
    # Bloqueo de recursos pesados: se hace a NIVEL NAVEGADOR via flags de launch
    # (ver setup), NO con context.route(). Interceptar cada request desde Python
    # fuga memoria (Playwright retiene Tasks/tracebacks por request; solo se
    # liberan con playwright.stop()) y fue la causa del OOM de ~73GB en 7h.
    _IMAGE_BLOCK_ARGS = [
        "--blink-settings=imagesEnabled=false",
        "--disable-remote-fonts",
    ]
    # Mensajes que indican que el navegador se cayo (no es bloqueo de Google).
    _BROWSER_CRASH_KEYWORDS = (
        "Connection closed",
        "Target page, context or browser has been closed",
        "Browser has been closed",
        "Protocol error",
    )
    # Señales de bloqueo servidas con HTTP 200 (captcha / trafico inusual).
    _BLOCK_CONTENT_SIGNALS = (
        "unusual traffic", "trafico inusual", "tráfico inusual",
        "not a robot", "no soy un robot", "recaptcha",
    )

    @classmethod
    def _is_browser_crash(cls, msg: str) -> bool:
        return any(kw in msg for kw in cls._BROWSER_CRASH_KEYWORDS)

    def __init__(self, config):
        self.config = config
        self.test_mode = config.test_mode
        self.headless = config.headless
        self.mock_dir = Path(config.mock.html_directory)
        self.simulate_delay = config.mock.simulate_delay
        self.mock_delay = config.mock.mock_delay_seconds
        self.max_scrolls = config.rate_limit.max_scroll_iterations
        self.block_resources = getattr(config, "block_resources", True)
        self.save_debug_html = getattr(config, "save_debug_html", False)
        self.crash_retries = config.rate_limit.max_retries
        self.max_consecutive_blocks = getattr(
            config.rate_limit, "max_consecutive_blocks", 5)
        self.block_backoff_base = getattr(
            config.rate_limit, "block_backoff_base_seconds", 300.0)
        self.block_backoff_max = getattr(
            config.rate_limit, "block_backoff_max_seconds", 3600.0)
        # Un RateLimiter POR worker: cada context se comporta a ritmo humano
        # (2-6 s entre SUS requests) de forma independiente. Un limiter global
        # con lock serializaria todo y anularia el paralelismo de E1; la
        # velocidad sale de tener N contexts, no de apurar cada uno.
        self.num_workers = max(1, getattr(config, "workers", 1))
        self.rate_limiters = [
            RateLimiter(
                config.rate_limit.request_delay_seconds,
                min_delay=config.rate_limit.request_delay_min_seconds,
                max_delay=config.rate_limit.request_delay_max_seconds,
            )
            for _ in range(self.num_workers)
        ]
        self.rate_limiter = self.rate_limiters[0]  # alias compat
        self.browser = None
        self.context = None
        self.page = None
        # Compuerta de pausa global ante bloqueo de Google. set() = abierta
        # (los workers pueden scrapear); clear() = pausada. Compartida por todos
        # los workers.
        self._block_gate = asyncio.Event()
        self._block_gate.set()
        self._block_lock = asyncio.Lock()
        self._consecutive_blocks = 0
        # Recovery del browser COMPARTIDO ante crash: solo un worker relanza a
        # la vez; los demas detectan el cambio de generacion y no re-relanzan.
        self._browser_lock = asyncio.Lock()
        self._browser_generation = 0
        # Reciclado periodico del browser para liberar la memoria que Playwright
        # retiene del lado Python. _in_flight = scrapes con context abierto ahora;
        # el reciclado espera a que llegue a 0 (compuerta) antes de cerrar el
        # browser compartido, para no cerrarlo bajo otros workers.
        self.browser_recycle_interval = getattr(config, "browser_recycle_interval", 500)
        self._scrapes_since_recycle = 0
        self._in_flight = 0
        self._recycle_gate = asyncio.Event()
        self._recycle_gate.set()

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
        launch_args = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
        if self.block_resources:
            # Bloquear imagenes/fonts a nivel navegador (barato, sin fuga) en
            # vez de interceptar cada request con context.route (que fugaba RAM).
            launch_args += self._IMAGE_BLOCK_ARGS
        self._playwright = await async_playwright().start()
        self.browser = await self._playwright.chromium.launch(
            headless=self.headless,
            args=launch_args,
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

    async def scrape(self, task: QueryTask, worker_id: int = 0) -> List[dict]:
        if self.test_mode:
            results = await self._scrape_mock(task)
        else:
            max_retries = self.crash_retries
            results = []
            self._scrapes_since_recycle += 1
            for attempt in range(max_retries + 1):
                gen = self._browser_generation
                try:
                    results = await self._scrape_live(task, worker_id)
                    self.note_success()
                    break
                except (GoogleMapsBlockedError, ScrapeTransientError):
                    # Bloqueo o fallo transitorio: los maneja el orquestador
                    # (pausa con backoff / re-encolado). No reintentar aca.
                    raise
                except Exception as e:
                    msg = str(e)
                    is_browser_crash = self._is_browser_crash(msg)
                    if is_browser_crash and attempt < max_retries:
                        delay = 2 ** attempt  # 1s, 2s, 4s
                        logger.warning(
                            f"Navegador desconectado (intento {attempt + 1}/{max_retries}), "
                            f"reiniciando en {delay}s... ({e})"
                        )
                        await asyncio.sleep(delay)
                        # Relanzar el browser compartido de forma coordinada:
                        # si otro worker ya lo relanzo, este no lo vuelve a hacer.
                        await self._recover_browser(gen)
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

    async def _recover_browser(self, gen_at_failure: int):
        """Relanza el browser compartido tras un crash, coordinado entre workers.

        Solo relanza si nadie mas lo hizo desde que este worker vio el fallo
        (comparando la generacion). Evita que N workers cierren y relancen el
        browser en cascada, tumbandoselo unos a otros.
        """
        async with self._browser_lock:
            if self._browser_generation != gen_at_failure:
                return  # otro worker ya relanzo el browser
            await self.teardown()
            await self.setup()
            self._browser_generation += 1

    # ── Resiliencia ante bloqueo (E2) ──────────────────────────

    async def wait_if_blocked(self):
        """Bloquea si hay una pausa global en curso (Google nos esta bloqueando)."""
        await self._block_gate.wait()

    def note_success(self):
        """Un scrape exitoso resetea el contador de bloqueos consecutivos."""
        self._consecutive_blocks = 0

    async def handle_block(self) -> bool:
        """Gestiona un bloqueo de Google con pausa global escalante.

        Devuelve True si conviene reintentar (tras la pausa), False si el
        bloqueo persiste tras `max_consecutive_blocks` y hay que abortar.

        Solo el primer worker que detecta el bloqueo hace la pausa; los demas
        se suman a la misma pausa (esperan la compuerta) sin apilar backoff.
        """
        # Si ya hay una pausa en curso, sumarse a ella sin escalar el backoff.
        if not self._block_gate.is_set():
            await self._block_gate.wait()
            return True

        async with self._block_lock:
            if not self._block_gate.is_set():
                await self._block_gate.wait()
                return True
            self._consecutive_blocks += 1
            if self._consecutive_blocks > self.max_consecutive_blocks:
                return False
            n = self._consecutive_blocks
            delay = min(self.block_backoff_base * (2 ** (n - 1)),
                        self.block_backoff_max)
            # Cerrar la compuerta dentro del lock para que ningun otro worker
            # inicie una segunda pausa en paralelo.
            self._block_gate.clear()

        logger.warning(
            f"Google Maps nos esta bloqueando (bloqueo consecutivo #{n}/"
            f"{self.max_consecutive_blocks}); pausando {delay:.0f}s antes de reintentar"
        )
        try:
            await asyncio.sleep(delay)
        finally:
            self._block_gate.set()
        return True

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

    async def _scrape_live(self, task: QueryTask, worker_id: int = 0) -> List[dict]:
        # Reciclar el browser periodicamente (libera memoria de Playwright) y
        # respetar una pausa global si Google nos esta bloqueando.
        await self._maybe_recycle_browser()
        await self.wait_if_blocked()
        # Rate limiter propio de este worker (ritmo humano independiente).
        await self.rate_limiters[worker_id % len(self.rate_limiters)].wait()
        url = task.to_maps_url()
        logger.info(f"[w{worker_id}] Scraping: {url}")

        # Tomar la referencia al browser compartido bajo lock y contar este
        # scrape como en vuelo, de forma atomica respecto al reciclado: mientras
        # este worker sostiene el lock, el reciclador no puede cerrar el browser.
        async with self._browser_lock:
            browser = self.browser
            self._in_flight += 1
        fp = random_fingerprint()
        context = None
        try:
            context = await browser.new_context(
                viewport=fp["viewport"],
                user_agent=fp["user_agent"],
                locale="es-PY",
                timezone_id="America/Asuncion",
            )
            page = await context.new_page()

            try:
                response = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                if self._is_browser_crash(str(e)):
                    raise
                # Timeout u otro fallo al navegar: transitorio -> reintentar la
                # tarea (no marcarla 'completed con 0').
                raise ScrapeTransientError(f"Fallo al navegar a {url}: {e}")

            if response is not None and response.status != 200:
                raise GoogleMapsBlockedError(
                    f"Google Maps respondio HTTP {response.status} (esperado 200) para {url}"
                )
            if await self._detect_block(page):
                raise GoogleMapsBlockedError(
                    f"Pagina de bloqueo/captcha detectada (HTTP 200) en {page.url}"
                )

            # Manejar popup de consentimiento (best-effort, corto)
            await self._dismiss_consent(page)

            # Esperar a que aparezca el feed en vez de dormir un tiempo fijo.
            # Si no aparece (busqueda sin resultados o pagina de lugar unico),
            # seguimos igual: _parse_results devolvera [].
            try:
                await page.wait_for_selector(
                    SELECTORS["results_container"][0], timeout=8000
                )
            except Exception:
                pass
            # Jitter humano corto (no un sleep fijo de 2-4 s)
            await asyncio.sleep(random.uniform(0.4, 1.0))

            # Scroll para cargar todos los resultados
            await self._scroll_results(page)

            html = await page.content()

            # Diagnostico opcional: guardar HTML para inspeccionar selectores
            if self.save_debug_html:
                self._save_debug_html(html, task)

            # Parsear en un thread: BeautifulSoup/lxml es CPU-bound (~1-3 s en
            # paginas densas) y bloquearia el event loop, serializando a los N
            # workers. run_in_executor libera el loop para los otros scrapes.
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, self._parse_html, html)
        except (GoogleMapsBlockedError, ScrapeTransientError):
            raise
        except Exception as e:
            # Errores de crash de navegador: re-lanzar para que scrape() reconecte
            if self._is_browser_crash(str(e)):
                raise
            logger.error(f"Error en scrape live: {e}")
            return []
        finally:
            self._in_flight -= 1
            if context is not None:
                try:
                    await context.close()
                except Exception:
                    pass  # el browser pudo cerrarse (recycle/crash); no enmascarar

    async def _maybe_recycle_browser(self):
        """Recicla el browser+conexion Playwright cada browser_recycle_interval
        tareas para liberar la memoria que Playwright retiene del lado Python
        (solo se libera con playwright.stop(), no con browser.close()).

        Seguro para el browser COMPARTIDO: cierra una compuerta para que no
        arranquen scrapes nuevos, espera a que los en vuelo terminen (in_flight
        == 0) y recien ahi hace teardown()+setup(). Asi nunca cierra el browser
        con contexts abiertos de otros workers (evita crash-storm)."""
        if self.browser_recycle_interval <= 0:
            return
        # Si hay un reciclado en curso, esperar a que termine.
        await self._recycle_gate.wait()
        if self._scrapes_since_recycle < self.browser_recycle_interval:
            return
        async with self._browser_lock:
            # Doble chequeo: otro worker pudo haber tomado el reciclado.
            if (self._scrapes_since_recycle < self.browser_recycle_interval
                    or not self._recycle_gate.is_set()):
                return
            self._recycle_gate.clear()  # bloquea scrapes nuevos en la compuerta

        logger.info(
            f"Reciclando navegador tras ~{self._scrapes_since_recycle} tareas "
            f"(libera memoria retenida por Playwright)"
        )
        # Esperar a que drenen los scrapes en vuelo y reciclar bajo lock. Como
        # los scrapes nuevos quedan bloqueados en la compuerta antes de tocar
        # _in_flight, este contador solo puede bajar: converge a 0.
        while True:
            await asyncio.sleep(0.1)
            async with self._browser_lock:
                if self._in_flight == 0:
                    await self.teardown()
                    await self.setup()
                    self._browser_generation += 1
                    self._scrapes_since_recycle = 0
                    break
        self._recycle_gate.set()

    async def _detect_block(self, page) -> bool:
        """Detecta paginas de bloqueo/captcha servidas con HTTP 200.

        Google a veces responde 200 con una pagina /sorry o de 'trafico inusual'
        en vez de un status de error. Chequeo barato: URL final + titulo.
        """
        try:
            final_url = (page.url or "").lower()
        except Exception:
            final_url = ""
        if "/sorry/" in final_url or "sorry/index" in final_url:
            return True
        try:
            title = (await page.title() or "").lower()
        except Exception:
            title = ""
        haystack = f"{title} {final_url}"
        return any(sig in haystack for sig in self._BLOCK_CONTENT_SIGNALS)

    async def _dismiss_consent(self, page):
        for sel in SELECTORS["consent_button"]:
            try:
                btn = page.locator(sel).first
                # Timeout corto: en es-PY el popup casi nunca aparece; no quemar
                # 2 s por selector esperandolo.
                if await btn.is_visible(timeout=800):
                    await btn.click()
                    await asyncio.sleep(random.uniform(0.4, 0.8))
                    return
            except Exception:
                pass

    async def _scroll_results(self, page):
        cards_sel = SELECTORS["result_cards"][0]
        for i in range(self.max_scrolls):
            prev_count = await page.locator(cards_sel).count()
            # Al alcanzar el cap de Google no hay mas resultados que cargar.
            if prev_count >= self.RESULTS_CAP:
                break
            try:
                feed = page.locator(SELECTORS["results_container"][0])
                if await feed.is_visible():
                    await feed.evaluate("el => el.scrollTop = el.scrollHeight")
            except Exception:
                pass
            # Espera corta con jitter en vez del sleep fijo de 1.5-2.5 s.
            await asyncio.sleep(random.uniform(0.6, 1.1))
            curr_count = await page.locator(cards_sel).count()
            if curr_count == prev_count:
                # Confirmar con un segundo chequeo corto: al acortar la espera,
                # un lazy-load lento podria no haber cargado todavia y no
                # queremos cortar antes de tiempo (evita perder resultados).
                await asyncio.sleep(random.uniform(0.6, 1.0))
                curr_count = await page.locator(cards_sel).count()
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

    def _parse_html(self, html: str) -> List[dict]:
        """Construye el soup y parsea. Sincrono, pensado para run_in_executor."""
        soup = BeautifulSoup(html, "lxml")
        return self._parse_results(soup)

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
