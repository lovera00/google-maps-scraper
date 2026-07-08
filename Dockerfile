# Imagen autocontenida del scraper: cualquier maquina con Docker puede
# correr un shard sin instalar Python, Playwright, navegadores ni nada mas.
FROM python:3.12-slim

WORKDIR /app

# Dependencias del sistema minimas (curl para debug/healthchecks opcionales)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Dependencias de Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Navegador Chromium + TODAS sus dependencias de SO (fuentes, libs graficas, etc.)
# --with-deps hace apt-get de todo lo necesario automaticamente.
RUN playwright install --with-deps chromium

# Codigo fuente y config (sin datos pesados: ver .dockerignore)
COPY src/ ./src/
COPY scripts/ ./scripts/
COPY run.py config.yaml config.example.yaml ./
COPY mocks/ ./mocks/
COPY data/paraguay_cities.json data/paraguay_boundary.geojson ./data/

# Directorio para datos montados en runtime (tasks.jsonl del shard, DB de salida, .env, proxies.txt)
RUN mkdir -p /app/data /app/logs
VOLUME ["/app/data", "/app/logs"]

ENTRYPOINT ["python", "run.py"]
CMD ["--help"]
