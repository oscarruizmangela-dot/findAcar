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
# --workers 2 es importante: Render detecta 1 sola CPU en la instancia y por
# defecto fija WEB_CONCURRENCY=1 (un único worker de proceso). Con 1 solo
# worker, mientras está ocupado atendiendo una búsqueda no puede responder
# al health check de Render en paralelo, y Render acaba matando el proceso
# con SIGTERM pensando que está colgado (aunque solo estaba ocupado).
# Con 2 workers, uno puede seguir respondiendo mientras el otro scrapea.
#
# Ojo con la memoria: cada worker puede llegar a abrir hasta 2 navegadores
# Chromium a la vez (ver app.py, pool de scrapers "pesados"). Con 2 workers,
# el peor caso teórico es 4 Chromium simultáneos si llegan 2 peticiones a
# la vez. Para uso en solitario/testing no debería ser problema, pero si
# vuelve a haber SIGKILL por memoria, hay que revisar el plan de Render
# (más RAM) antes de subir --workers más.
CMD ["gunicorn", "app:app", "--bind", "0.0.0.0:10000", "--timeout", "120", "--workers", "2"]
