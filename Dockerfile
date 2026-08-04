# Usamos la imagen oficial de Playwright para Python que ya trae Chromium y dependencias instaladas
FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

# Directorio de trabajo dentro del contenedor
WORKDIR /app

# Copiamos los archivos del proyecto al contenedor
COPY . /app

# Instalamos las dependencias de Python
RUN pip install --no-cache-dir -r requirements.txt

# Puerto por defecto que expondrá el contenedor
EXPOSE 10000

# Comando para arrancar la aplicación usando Gunicorn con un tiempo de espera alto (timeout)
# para evitar cortes mientras el scraper busca los coches.
#
# --workers 1 --worker-class gthread --threads 4: usamos 1 solo PROCESO (no
# duplicamos la memoria base de Python+Playwright) pero con varios HILOS
# dentro de ese proceso, para que Gunicorn pueda seguir respondiendo al
# health check de Render mientras el hilo principal está ocupado scrapeando.
#
# Historial de por qué NO usamos las alternativas obvias:
#   - "--workers 1" a secas (sync, sin threads): el health check se moría de
#     hambre mientras el único worker atendía una búsqueda larga -> Render
#     mandaba SIGTERM pensando que estaba colgado.
#   - "--workers 2" (2 procesos): arregló el health check, pero cada proceso
#     duplica la memoria base + puede abrir sus propios navegadores Chromium
#     -> OOM y SIGKILL en bucle. La instancia no tiene RAM para 2 procesos
#     completos de Playwright a la vez.
# "gthread" resuelve ambos a la vez: memoria de un solo proceso, pero
# concurrencia real de peticiones vía hilos.
CMD ["gunicorn", "app:app", "--bind", "0.0.0.0:10000", "--timeout", "120", "--workers", "1", "--worker-class", "gthread", "--threads", "4"]
