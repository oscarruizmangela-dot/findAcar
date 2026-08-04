import os
import html
import time
from flask import Flask, request, jsonify, send_file, make_response
import requests
from bs4 import BeautifulSoup
import json
import re
import asyncio
from playwright.async_api import async_playwright
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_PATH = os.path.join(BASE_DIR, 'templates', 'index.html')
RUTA_LOG_DIAGNOSTICO = os.path.join(BASE_DIR, 'diagnostico_estabilidad.jsonl')

def registrar_diagnostico(filtros, conteo_por_proveedor, urls_por_proveedor, tiempos_por_proveedor, total_filtrado):
    """Guarda un registro de cada búsqueda (filtros usados, cuántos
    resultados brutos ha traído cada proveedor, qué URLs exactas y
    cuánto ha tardado) en un archivo .jsonl -- una línea por búsqueda.

    Esto es SOLO para poder analizar después, comparando varias
    ejecuciones con los mismos filtros, si la variabilidad viene de
    verdad del scraping (proveedores que fallan o devuelven cosas
    distintas) o de otra causa. No afecta nunca a la respuesta de la
    API: si escribir el log falla, la búsqueda sigue funcionando igual."""
    try:
        registro = {
            "timestamp": time.time(),
            "fecha": time.strftime("%Y-%m-%d %H:%M:%S"),
            "filtros": filtros,
            "total_filtrado": total_filtrado,
            "conteo_por_proveedor": conteo_por_proveedor,
            "tiempos_por_proveedor": tiempos_por_proveedor,
            "urls_por_proveedor": urls_por_proveedor
        }
        with open(RUTA_LOG_DIAGNOSTICO, "a", encoding="utf-8") as f:
            f.write(json.dumps(registro, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[Diagnóstico] No se pudo escribir el log: {e}")
if not os.path.exists(TEMPLATE_PATH):
    TEMPLATE_PATH = os.path.join(BASE_DIR, 'index.html')

app = Flask(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
    "Content-Type": "application/json",
    "Origin": "https://www.flexicar.es",
    "Referer": "https://www.flexicar.es/"
}

PALABRAS_PROHIBIDAS = [
    "contacto", "vender", "compramos", "politica", "privacidad", "cookies", 
    "legal", "tel:", "www.", "flexicar", "garantia", "frecuentes", "nosotros", 
    "opiniones", "delegaciones", "concesionarios", "trabaja-con-nosotros"
]

MODELOS_VALIDOS = {
    "audi": ["a1", "a2", "a3", "a4", "a5", "a6", "a7", "a8", "q2", "q3", "q4", "q5", "q7", "q8", "tt", "r8", "e-tron", "etron", "rs"],
    "bmw": ["118", "120", "320", "330", "520", "x1", "x2", "x3", "x4", "x5", "x6", "serie"],
    "seat": ["ibiza", "leon", "arona", "ateca", "tarraco", "toledo", "altea", "mii"],
    "volkswagen": ["golf", "polo", "passat", "tiguan", "t-roc", "t-cross", "touareg", "arteon", "taigo"]
}

# Mapea el valor recibido del selector (slug) a la etiqueta exacta que
# devuelven extraer_combustible()/extraer_cambio() sobre cada vehículo.
COMBUSTIBLE_MAP = {
    "diesel": "Diésel",
    "gasolina": "Gasolina",
    "electrico": "Eléctrico",
    "hibrido-enchufable": "Híbrido enchufable",
    "hibrido-no-enchufable": "Híbrido no enchufable",
}
CAMBIO_MAP = {
    "manual": "Manual",
    "automatico": "Automático",
}

def limpiar_numero(cadena):
    if not cadena or cadena == "N/D":
        return None
    num = re.sub(r'[^\d]', '', str(cadena))
    return int(num) if num else None

def extraer_potencia(texto):
    """Busca la potencia (CV o kW) en un texto ya descargado (ficha/tarjeta o slug de URL).
    No hace peticiones nuevas: reutiliza el mismo texto que ya se usa para precio/año.
    Devuelve un string tipo '150 CV' o 'N/D' si no se encuentra ningún patrón reconocido."""
    if not texto:
        return "N/D"
    match_cv = re.search(r'(\d{2,3})\s?-?\s?cv\b', texto, re.IGNORECASE)
    if match_cv:
        return f"{match_cv.group(1)} CV"
    match_kw = re.search(r'(\d{2,3})\s?-?\s?kw\b', texto, re.IGNORECASE)
    if match_kw:
        return f"{match_kw.group(1)} kW"
    return "N/D"

def calcular_puntuaciones(resultados):
    """Puntúa cada vehículo del 0 al 100 comparándolo con el resto de
    resultados de ESTA MISMA búsqueda (no contra un baremo fijo, porque
    lo que es "buena potencia" o "buen precio" depende mucho de qué se
    esté buscando en cada momento -- no es lo mismo comparar utilitarios
    que berlinas de gama alta).

    Para cada uno de los 4 factores (año, potencia, km, precio al contado)
    se normaliza el valor del vehículo entre el mínimo y el máximo de ESE
    factor dentro del conjunto de resultados, invirtiendo km y precio
    (menos es mejor) y dejando año y potencia tal cual (más es mejor).

    Si un vehículo no tiene alguno de los 4 datos (ej. potencia 'N/D'),
    ese factor simplemente no entra en su media -- no se le penaliza por
    un dato que no se ha podido leer, igual que ya hacen los filtros.
    Si no tiene NINGÚN dato con el que puntuar, se le asigna puntuacion=None
    (se mostrará como 'N/D' en la tabla, no como un 0 injusto)."""

    valores = {"anio": [], "potencia": [], "km": [], "precio": []}
    for item in resultados:
        a = limpiar_numero(item.get("anio"))
        if a:
            valores["anio"].append(a)
        p = valor_potencia_cv(item.get("potencia"))
        if p is not None:
            valores["potencia"].append(p)
        k = limpiar_numero(item.get("kilometros"))
        if k is not None:
            valores["km"].append(k)
        c = limpiar_numero(item.get("contado"))
        if c:
            valores["precio"].append(c)

    rangos = {}
    for clave, lista in valores.items():
        if lista:
            rangos[clave] = (min(lista), max(lista))

    def normalizar(valor, minimo, maximo, invertir=False):
        if maximo == minimo:
            return 100.0  # todos empatan en este factor: no penaliza a nadie
        fraccion = (valor - minimo) / (maximo - minimo)
        if invertir:
            fraccion = 1 - fraccion
        return fraccion * 100

    for item in resultados:
        subpuntuaciones = []

        if "anio" in rangos:
            a = limpiar_numero(item.get("anio"))
            if a:
                subpuntuaciones.append(normalizar(a, *rangos["anio"]))

        if "potencia" in rangos:
            p = valor_potencia_cv(item.get("potencia"))
            if p is not None:
                subpuntuaciones.append(normalizar(p, *rangos["potencia"]))

        if "km" in rangos:
            k = limpiar_numero(item.get("kilometros"))
            if k is not None:
                subpuntuaciones.append(normalizar(k, *rangos["km"], invertir=True))

        if "precio" in rangos:
            c = limpiar_numero(item.get("contado"))
            if c:
                subpuntuaciones.append(normalizar(c, *rangos["precio"], invertir=True))

        if subpuntuaciones:
            item["puntuacion"] = round(sum(subpuntuaciones) / len(subpuntuaciones))
        else:
            item["puntuacion"] = None

    return resultados

def valor_potencia_cv(texto_potencia):
    """Convierte el texto ya mostrado en la tabla ('165 kW' o '224 CV') a un
    número en CV, solo para poder comparar contra el filtro de rango. El
    dato que ve el usuario en la tabla/Excel NO cambia, se queda tal cual
    lo devuelve cada proveedor -- esto es únicamente para el filtrado.
    Si es 'N/D' devuelve None, y ese vehículo no se descarta por potencia
    (igual que ya pasa con precio/año cuando no se puede leer el número)."""
    if not texto_potencia:
        return None
    match_cv = re.search(r'(\d+)\s*CV', texto_potencia, re.IGNORECASE)
    if match_cv:
        return int(match_cv.group(1))
    match_kw = re.search(r'(\d+)\s*kW', texto_potencia, re.IGNORECASE)
    if match_kw:
        return round(int(match_kw.group(1)) * 1.35962)
    return None

def extraer_km(texto):
    """Busca los kilómetros ('75.950 km') en un texto ya descargado, con el
    mismo patrón que extraer_potencia: sin peticiones nuevas, reutiliza el
    texto ya obtenido para precio/año. Devuelve un string tipo '75.950 km'
    o 'N/D' si no se encuentra ningún patrón reconocido."""
    if not texto:
        return "N/D"
    match = re.search(r'(\d{1,3}(?:[.\s]\d{3})*)\s*km\b', texto, re.IGNORECASE)
    if match:
        val = limpiar_numero(match.group(1))
        if val and 0 <= val <= 500000:
            return f"{val:,}".replace(",", ".") + " km"
    return "N/D"

def extraer_combustible(texto):
    """Detecta la tecnología del motor (Diésel / Gasolina / Eléctrico /
    Híbrido enchufable / Híbrido no enchufable) a partir del texto ya
    descargado -- mismo patrón que potencia/km, sin peticiones nuevas.
    El orden de las comprobaciones importa: se descartan primero el
    plug-in y el eléctrico puro (que también podrían contener la palabra
    'híbrido' o coincidir con otros patrones) antes de caer en las
    categorías más genéricas."""
    if not texto:
        return "N/D"
    t = texto.lower()

    if re.search(r'h[ií]brido\s*enchufable|plug-?in|\bphev\b', t):
        return "Híbrido enchufable"
    if re.search(r'el[eé]ctrico|\bbev\b|100\s*%\s*el[eé]ctrico', t):
        return "Eléctrico"
    if re.search(r'h[ií]brido|\bhev\b|\bmhev\b|\bhybrid\b', t):
        return "Híbrido no enchufable"
    if re.search(r'di[eé]sel|\btdi\b|\bhdi\b|\bdci\b|\bcdti\b|bluehdi|\bcrdi\b|\bmultijet\b', t):
        return "Diésel"
    if re.search(r'gasolina|\btsi\b|\btfsi\b|\bvti\b|puretech|\bmpi\b|\bpetrol\b|\btce\b', t):
        return "Gasolina"
    return "N/D"

def extraer_cambio(texto):
    """Detecta el tipo de cambio (Manual / Automático) a partir del texto
    ya descargado, mismo patrón que el resto de extractores adicionales."""
    if not texto:
        return "N/D"
    t = texto.lower()
    if re.search(r'autom[aá]tico|\bauto\b|\bdsg\b|\bdct\b|\bcvt\b|tiptronic|s\s?tronic|powershift|\bdht\b', t):
        return "Automático"
    if re.search(r'\bmanual\b', t):
        return "Manual"
    return "N/D"

def es_modelo_legitimo(marca, slug_o_texto):
    marca_clean = marca.lower().strip()
    modelos = MODELOS_VALIDOS.get(marca_clean, [])
    if not modelos:
        return True
    texto = slug_o_texto.lower()
    return marca_clean in texto or any(re.search(rf'\b{re.escape(m)}\b', texto) or f"-{m}-" in texto or texto.startswith(f"{m}-") for m in modelos)

# ================= 1. AUTOKOLECCIÓ =================
def scrape_autokoleccio(marca="audi", tipo="todas", max_pages=2):
    resultados = []
    categorias = []
    marca_clean = marca.lower().strip()
    
    if tipo in ["todas", "coches-km0"]:
        categorias.append(("KM0", f"https://www.autokoleccio.com/coches-km0/{marca_clean}/"))
    if tipo in ["todas", "segunda-mano"]:
        categorias.append(("Ocasión", f"https://www.autokoleccio.com/segunda-mano/{marca_clean}/"))
        categorias.append(("Ocasión", f"https://www.autokoleccio.com/coches-ocasion/{marca_clean}/"))

    session = requests.Session()
    enlaces_procesados = set()

    for cat_label, base_url in categorias:
        for page in range(1, max_pages + 1):
            url = f"{base_url}{page}/" if page > 1 else base_url
            try:
                res = session.get(url, headers=HEADERS, timeout=8)
                if res.status_code != 200:
                    break

                soup = BeautifulSoup(res.text, "html.parser")
                cards = soup.find_all('a', href=lambda h: h and '/coches-ocasion/' in h)

                for card in cards:
                    link = card['href']
                    if link in enlaces_procesados:
                        continue

                    slug_match = re.search(r'/coches-ocasion/([a-z0-9-]+)-\d+/', link)
                    if not slug_match:
                        continue
                    
                    full_slug = slug_match.group(1).lower()
                    if marca_clean not in full_slug:
                        continue

                    enlaces_procesados.add(link)

                    titulo = full_slug.replace('-', ' ').title()
                    titulo = re.sub(r'\b(\d)\s(\d)\b', r'\1.\2', titulo)
                    marca_real = full_slug.split('-')[0].capitalize()

                    parent = card
                    for _ in range(4):
                        if parent.parent and parent.parent.name in ['div', 'article', 'li']:
                            parent = parent.parent

                    text_content = parent.get_text(separator=" ", strip=True) if parent else card.get_text(separator=" ", strip=True)
                    
                    precios_raw = re.findall(r'(\d+[\.\d]*\s*€)', text_content)
                    if not precios_raw:
                        precios_raw = re.findall(r'\b(\d{2}[\.\s]?\d{3})\b', text_content)

                    contado = "Consultar"
                    financiado = "Consultar"

                    if precios_raw:
                        precios_validos = []
                        for p in precios_raw:
                            val = limpiar_numero(p)
                            if val and 5000 <= val <= 150000:
                                precios_validos.append(f"{val:,}".replace(",", ".") + " €")
                        
                        if precios_validos:
                            contado = precios_validos[0]
                            financiado = precios_validos[1] if len(precios_validos) > 1 else contado

                    anio_val = "2023"
                    anios = re.findall(r'\b(20[1-2][0-9])\b', text_content + " " + link)
                    if anios:
                        anio_val = anios[0]

                    url_coche = link if link.startswith('http') else f"https://www.autokoleccio.com{link}"
                    potencia = extraer_potencia(text_content)
                    kilometros = extraer_km(text_content)
                    combustible = extraer_combustible(text_content)
                    cambio = extraer_cambio(text_content)

                    resultados.append({
                        "proveedor": "Autokolecció",
                        "categoria": cat_label,
                        "marca": marca_real,
                        "modelo": titulo,
                        "anio": str(anio_val),
                        "contado": contado,
                        "financiado": financiado,
                        "potencia": potencia,
                        "kilometros": kilometros,
                        "combustible": combustible,
                        "cambio": cambio,
                        "url": url_coche
                    })

            except Exception as e:
                print(f"[Autokolecció] Error en {url}: {e}")
                break

    return resultados

# ================= 2. FLEXICAR (PLAYWRIGHT - VERSIÓN FIABLE) =================
async def scrape_flexicar_async(marca="audi", tipo="todas", max_scrolls=8):
    resultados = []
    marca_clean = marca.lower().strip()
    # URL real confirmada: https://www.flexicar.es/{marca}/segunda-mano/
    target_url = f"https://www.flexicar.es/{marca_clean}/segunda-mano/"

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
            )
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
                viewport={"width": 1920, "height": 1080},
                locale="es-ES"
            )
            page = await context.new_page()
            await page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

            print(f"[Flexicar] Cargando: {target_url}")
            try:
                await page.goto(target_url, wait_until="domcontentloaded", timeout=25000)
            except Exception as e:
                print(f"[Flexicar] Timeout/navegación: {e}")

            # Aceptar banner de cookies si aparece (evita que bloquee el render)
            for selector in ["#onetrust-accept-btn-handler", "button:has-text('Aceptar')", "button:has-text('ACEPTAR')"]:
                try:
                    btn = page.locator(selector).first
                    if await btn.is_visible(timeout=2000):
                        await btn.click()
                        break
                except Exception:
                    pass

            await page.wait_for_timeout(2500)

            # Scroll progresivo para forzar la carga de tarjetas con
            # lazy-load. Adaptativo: en vez de un número fijo de scrolls
            # con espera fija, sigue mientras la altura de la página siga
            # creciendo (contenido nuevo cargando) y para en cuanto dos
            # scrolls seguidos no cambian nada -- el diagnóstico detectó
            # una ejecución con la mitad de resultados de lo normal,
            # compatible con que esa vez la carga fuese más lenta y el
            # número fijo de scrolls se quedara corto.
            altura_anterior = 0
            scrolls_sin_cambios = 0
            for _ in range(max_scrolls):
                await page.evaluate("window.scrollBy(0, document.body.scrollHeight/4)")
                await page.wait_for_timeout(1000)
                altura_actual = await page.evaluate("document.body.scrollHeight")
                if altura_actual == altura_anterior:
                    scrolls_sin_cambios += 1
                    if scrolls_sin_cambios >= 2:
                        break
                else:
                    scrolls_sin_cambios = 0
                altura_anterior = altura_actual

            content = await page.content()
            await browser.close()

        soup = BeautifulSoup(content, "html.parser")

        # Cada ficha de coche real enlaza a /coches-ocasion/{slug}_{id-numerico}/
        # ej: /coches-ocasion/audi-a5-40-tfsi-140kw-190cv-...-marbella_903000000258934/
        car_links = soup.find_all(
            'a',
            href=lambda h: h and re.search(r'/coches-ocasion/[a-z0-9-]+_\d+/?$', h)
        )

        vistas = set()
        for a in car_links:
            href = a['href']
            if href in vistas:
                continue

            slug_completo = href.rstrip('/').split('/')[-1].lower()
            # slug_completo tiene forma "audi-a5-40-tfsi-...-marbella_903000000258934"
            slug = slug_completo.rsplit('_', 1)[0]
            if len(slug) < 4 or any(p in slug for p in PALABRAS_PROHIBIDAS):
                continue
            if marca_clean not in slug:
                continue

            vistas.add(href)

            # Sube por los padres SOLO mientras el contenedor siga teniendo
            # un único enlace de coche. En cuanto detecta un segundo enlace
            # (=ha entrado en el contenedor de la lista completa, mezclando
            # datos de coches vecinos), para justo un nivel antes.
            card = a
            nivel = 0
            while card.parent is not None and nivel < 8:
                candidato = card.parent
                enlaces_dentro = candidato.find_all(
                    'a', href=lambda h: h and re.search(r'/coches-ocasion/[a-z0-9-]+_\d+/?$', h)
                )
                if len(enlaces_dentro) > 1:
                    break
                card = candidato
                nivel += 1

            text = card.get_text(separator=" | ", strip=True)

            precio_match = re.findall(r'(\d{1,3}(?:\.\d{3})+)\s*€', text)
            precios_validos = []
            for p in precio_match:
                val = limpiar_numero(p)
                if val and 3000 <= val <= 150000:
                    precios_validos.append(val)

            if not precios_validos:
                continue

            # En Flexicar el precio "en oferta" (financiado) suele ser el más bajo
            # de los dos que aparecen, y el "al contado" el más alto
            precio_oferta = min(precios_validos)
            precio_contado = max(precios_validos)

            # El año va justo antes de los "km" en la ficha (ej: "... | 2024 | 75.950 km | ...")
            anio_match = re.search(r'\b(20[1-2][0-9])\s*\|\s*[\d.,]+\s*km', text)
            if not anio_match:
                anio_match = re.search(r'\b(20[1-2][0-9])\b', text)
            anio_val = anio_match.group(1) if anio_match else "2022"

            titulo_raw = slug.replace('-', ' ').title()
            titulo_raw = re.sub(r'\b(\d)\s(\d)\b', r'\1.\2', titulo_raw)

            url_coche = href if href.startswith('http') else f"https://www.flexicar.es{href}"
            potencia = extraer_potencia(slug + " " + text)
            kilometros = extraer_km(text)
            combustible = extraer_combustible(slug + " " + text)
            cambio = extraer_cambio(slug + " " + text)

            resultados.append({
                "proveedor": "Flexicar",
                "categoria": "Ocasión",
                "marca": marca.capitalize(),
                "modelo": titulo_raw,
                "anio": str(anio_val),
                "contado": f"{precio_contado:,}".replace(",", ".") + " €",
                "financiado": f"{precio_oferta:,}".replace(",", ".") + " €",
                "potencia": potencia,
                "kilometros": kilometros,
                "combustible": combustible,
                "cambio": cambio,
                "url": url_coche
            })

    except Exception as e:
        print(f"[Flexicar] Error durante scraping: {e}")

    print(f"[Flexicar] Vehículos extraídos: {len(resultados)}")
    return resultados


def scrape_flexicar(marca="audi", tipo="todas"):
    return asyncio.run(scrape_flexicar_async(marca=marca, tipo=tipo))

# ================= 3. COCHES.NET =================
async def scrape_cochesnet_async(marca="audi"):
    resultados = []
    marca_clean = marca.lower().strip()
    url = f"https://www.coches.net/segunda-mano/{marca_clean}/"

    TEXTOS_PROHIBIDOS = [
        "mis anuncios", "mis favoritos", "iniciar sesión", "registrarse", 
        "publicar anuncio", "alerta", "financiar", "publicidad", "comparar"
    ]

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
            context = await browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36", viewport={"width": 1366, "height": 768})
            page = await context.new_page()

            await page.route("**/*.{png,jpg,jpeg,svg,webp,woff,woff2,ttf}", lambda route: route.abort())

            # Diagnóstico detectó ~40% de ejecuciones devolviendo 0 coches,
            # con tiempos anómalamente largos en esos casos -- señal de que
            # una sola pasada con espera fija no siempre da tiempo a que
            # cargue el contenido real (bot-detection, red lenta puntual).
            # Se reintenta una vez más, con una espera más larga, SOLO si
            # la primera pasada no encuentra ningún enlace de coche -- no
            # penaliza en tiempo al caso normal, que ya funciona a la primera.
            car_links = []
            esperas = [3500, 7000]
            for intento, espera_ms in enumerate(esperas, start=1):
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=15000)
                except Exception as e:
                    print(f"[Coches.net] Intento {intento}: timeout/error de navegación ({e})")

                await page.wait_for_timeout(espera_ms)
                content = await page.content()
                soup = BeautifulSoup(content, "html.parser")

                car_links = soup.find_all('a', href=lambda h: h and ('-covo.aspx' in h or ('.aspx' in h and marca_clean in h.lower())))
                if car_links:
                    if intento > 1:
                        print(f"[Coches.net] Recuperado en el intento {intento} ({len(car_links)} enlaces)")
                    break
                print(f"[Coches.net] Intento {intento}: 0 enlaces encontrados, {'reintentando' if intento < len(esperas) else 'sin más intentos'}")

            enlaces_vistos = set()

            for a in car_links:
                href = a['href']
                if href in enlaces_vistos:
                    continue

                titulo_raw = a.get_text(strip=True)
                if any(p in titulo_raw.lower() for p in TEXTOS_PROHIBIDOS) or len(titulo_raw) < 4:
                    continue

                parent = a
                for _ in range(4):
                    if parent.parent:
                        parent = parent.parent

                text = parent.get_text(separator=" | ", strip=True)
                parts = [p.strip() for p in text.split('|') if p.strip()]

                if not parts:
                    continue

                titulo = titulo_raw
                if any(p in titulo.lower() for p in TEXTOS_PROHIBIDOS):
                    titulo = parts[0]
                    if any(p in titulo.lower() for p in TEXTOS_PROHIBIDOS):
                        continue

                if marca_clean not in titulo.lower() and marca_clean not in href.lower():
                    continue

                enlaces_vistos.add(href)
                url_coche = href if href.startswith('http') else f"https://www.coches.net{href}"

                precio_match = re.search(r'(\d+[\.\d]*\s*€)', text)
                precio_str = precio_match.group(1) if precio_match else "Consultar"

                anio_str = "2022"
                anio_match = re.search(r'\b(20[1-2][0-9])\b', text)
                if anio_match:
                    anio_str = anio_match.group(1)

                potencia = extraer_potencia(text)
                kilometros = extraer_km(text)
                combustible = extraer_combustible(text)
                cambio = extraer_cambio(text)

                resultados.append({
                    "proveedor": "Coches.net",
                    "categoria": "Ocasión",
                    "marca": marca.capitalize(),
                    "modelo": titulo,
                    "anio": str(anio_str),
                    "contado": precio_str,
                    "financiado": precio_str,
                    "potencia": potencia,
                    "kilometros": kilometros,
                    "combustible": combustible,
                    "cambio": cambio,
                    "url": url_coche
                })

            await browser.close()
    except Exception as e:
        print(f"[Coches.net] Error durante scraping: {e}")

    print(f"[Coches.net] Vehículos extraídos: {len(resultados)}")
    return resultados

def scrape_cochesnet(marca="audi"):
    return asyncio.run(scrape_cochesnet_async(marca=marca))

# ================= 4. COCHESINTERNET.NET =================
def scrape_cochesinternet(marca="audi", tipo="todas"):
    resultados = []
    marca_clean = marca.lower().strip()
    categorias = []

    if tipo in ["todas", "segunda-mano"]:
        categorias.append(("Ocasión", f"https://www.cochesinternet.net/coches-segunda-mano/{marca_clean}"))
    if tipo in ["todas", "coches-km0"]:
        categorias.append(("KM0", f"https://www.cochesinternet.net/coches-km-0/{marca_clean}"))

    session = requests.Session()
    enlaces_procesados = set()

    def extraer_precios_validos(nodo):
        """Devuelve (lista_de_precios_en_euros, texto_completo).
        Ignora explícitamente cualquier precio seguido de '/mes' (cuota),
        para no confundir la cuota mensual con un precio de coche."""
        txt = nodo.get_text(separator=" | ", strip=True)
        precios = []
        for m in re.finditer(r'(\d{1,3}(?:\.\d{3})+)\s*€', txt):
            cola = txt[m.end():m.end() + 15]
            if re.match(r'\s*/\s*mes', cola, re.IGNORECASE):
                continue
            val = limpiar_numero(m.group(1))
            if val and 4000 <= val <= 200000:
                precios.append(val)
        return precios, txt

    for cat_label, base_url in categorias:
        try:
            res = session.get(base_url, headers={"User-Agent": HEADERS["User-Agent"]}, timeout=8)
            if res.status_code != 200:
                continue

            soup = BeautifulSoup(res.text, "html.parser")
            candidates = soup.find_all(['article', 'div', 'a'], class_=lambda c: c and any(k in str(c).lower() for k in ['coche', 'item', 'card', 'ficha', 'anuncio', 'listing']))
            if not candidates:
                candidates = soup.find_all('a', href=True)

            for cand in candidates:
                link_tag = cand if cand.name == 'a' else cand.find('a', href=True)
                if not link_tag or not link_tag.get('href'):
                    continue

                link = link_tag['href']
                if any(x in link.lower() for x in ['facebook', 'twitter', 'instagram', 'legal', 'contacto', 'politica', 'javascript']):
                    continue
                if link in enlaces_procesados:
                    continue

                # Sube SIEMPRE desde el propio enlace del coche (ignorando el
                # cand original, que puede ser demasiado amplio si la clase
                # CSS coincidía con el contenedor de toda la lista), parando
                # en cuanto encuentra el par de precios (contado+financiado).
                card = link_tag
                nivel = 0
                precios_validos, text_content = extraer_precios_validos(card)
                while len(precios_validos) < 2 and card.parent is not None and nivel < 10:
                    card = card.parent
                    nivel += 1
                    precios_validos, text_content = extraer_precios_validos(card)

                if not precios_validos:
                    continue

                enlaces_procesados.add(link)

                slug_match = re.search(r'/(?:coche|segunda-mano|km-0|oferta|vehiculo)/([a-z0-9-]+)', link)
                if slug_match and len(slug_match.group(1)) > 3:
                    raw_name = slug_match.group(1).replace('-', ' ').title()
                    raw_name = re.sub(r'\b(\d)\s(\d)\b', r'\1.\2', raw_name)
                    titulo = raw_name
                else:
                    heading = card.find(['h2', 'h3', 'h4', 'strong'])
                    titulo_candidate = heading.get_text(strip=True) if heading else link_tag.get_text(strip=True)
                    if re.search(r'\b\d+\s+de\s+\d+\b', titulo_candidate, re.IGNORECASE) or len(titulo_candidate) < 4:
                        titulo = f"{marca.capitalize()} Ocasión"
                    else:
                        titulo = titulo_candidate

                # El precio financiado suele ser el más bajo de los dos que
                # aparecen en la ficha, y el "al contado" el más alto.
                contado = f"{max(precios_validos):,}".replace(",", ".") + " €"
                financiado = f"{min(precios_validos):,}".replace(",", ".") + " €"

                anio_val = "2022"
                anio_match = re.search(r'\b(20[0-2][0-9])\s*\|\s*[\d.,]+\s*km', text_content)
                if not anio_match:
                    anio_match = re.search(r'\b(20[0-2][0-9])\b', text_content)
                if anio_match:
                    anio_val = anio_match.group(1)

                url_coche = link if link.startswith('http') else f"https://www.cochesinternet.net{link}"
                potencia = extraer_potencia(text_content)
                kilometros = extraer_km(text_content)
                combustible = extraer_combustible(text_content)
                cambio = extraer_cambio(text_content)

                resultados.append({
                    "proveedor": "Cochesinternet.net",
                    "categoria": cat_label,
                    "marca": marca.capitalize(),
                    "modelo": titulo,
                    "anio": str(anio_val),
                    "contado": contado,
                    "financiado": financiado,
                    "potencia": potencia,
                    "kilometros": kilometros,
                    "combustible": combustible,
                    "cambio": cambio,
                    "url": url_coche
                })

        except Exception as e:
            print(f"[Cochesinternet.net] Error en {base_url}: {e}")

    print(f"[Cochesinternet.net] Vehículos extraídos: {len(resultados)}")
    return resultados

# ================= 5. COCHES.COM =================
def scrape_cochescom(marca="audi", tipo="todas"):
    resultados = []
    marca_clean = marca.lower().strip()
    categorias = []

    if tipo in ["todas", "coches-km0"]:
        categorias.append(("KM0", f"https://www.coches.com/km0/{marca_clean}.htm"))
    if tipo in ["todas", "segunda-mano"]:
        categorias.append(("Ocasión", f"https://www.coches.com/coches-segunda-mano/{marca_clean}.htm"))

    session = requests.Session()
    enlaces_procesados = set()

    link_pattern = re.compile(
        r'/(?:km0|coches-segunda-mano)/[a-z0-9-]+\.htm\?id=[\w-]+',
        re.IGNORECASE
    )
    slug_pattern = re.compile(
        r'/(?:km0|coches-segunda-mano)/(?:seminuevo-|ocasion-)?([a-z0-9-]+)\.htm',
        re.IGNORECASE
    )

    for cat_label, base_url in categorias:
        try:
            res = session.get(base_url, headers={"User-Agent": HEADERS["User-Agent"]}, timeout=8)
            if res.status_code != 200:
                continue

            soup = BeautifulSoup(res.text, "html.parser")
            # Cada ficha es un único <a> que envuelve título + specs + precio(s)
            car_links = soup.find_all('a', href=lambda h: h and link_pattern.search(h))

            for a in car_links:
                href = a['href']
                if href in enlaces_procesados:
                    continue
                enlaces_procesados.add(href)

                text = a.get_text(separator=" | ", strip=True)

                precios_validos = []
                for m in re.finditer(r'(\d{1,3}(?:\.\d{3})+)\s*€', text):
                    val = limpiar_numero(m.group(1))
                    if val and 3000 <= val <= 200000:
                        precios_validos.append(val)

                if not precios_validos:
                    continue

                # A veces solo se muestra el precio al contado (+ una cuota
                # mensual con coma decimal, que ya queda excluida por la
                # regex anterior). Si solo hay un precio, se usa como
                # contado y financiado por igual.
                if len(precios_validos) >= 2:
                    contado_val = max(precios_validos)
                    financiado_val = min(precios_validos)
                else:
                    contado_val = financiado_val = precios_validos[0]

                # El año a veces va pegado sin espacios a los km y la
                # provincia (ej. "km2025Madrid"), así que se usa un
                # lookahead a mayúscula en vez de \b.
                anio_match = re.search(r'(20[0-2][0-9])(?=[A-ZÁÉÍÓÚÑ])', text)
                if not anio_match:
                    anio_match = re.search(r'\b(20[0-2][0-9])\b', text)
                anio_val = anio_match.group(1) if anio_match else "2022"

                slug_match = slug_pattern.search(href)
                if not slug_match or len(slug_match.group(1)) <= 3:
                    continue

                raw_name = slug_match.group(1).replace('-', ' ').title()
                titulo = re.sub(r'\b(\d)\s(\d)\b', r'\1.\2', raw_name)

                url_coche = href if href.startswith('http') else f"https://www.coches.com{href}"
                potencia = extraer_potencia(text)
                kilometros = extraer_km(text)
                combustible = extraer_combustible(text)
                cambio = extraer_cambio(text)

                resultados.append({
                    "proveedor": "Coches.com",
                    "categoria": cat_label,
                    "marca": marca.capitalize(),
                    "modelo": titulo,
                    "anio": str(anio_val),
                    "contado": f"{contado_val:,}".replace(",", ".") + " €",
                    "financiado": f"{financiado_val:,}".replace(",", ".") + " €",
                    "potencia": potencia,
                    "kilometros": kilometros,
                    "combustible": combustible,
                    "cambio": cambio,
                    "url": url_coche
                })

        except Exception as e:
            print(f"[Coches.com] Error en {base_url}: {e}")

    print(f"[Coches.com] Vehículos extraídos: {len(resultados)}")
    return resultados

# ================= 6. OCASIONPLUS =================
def scrape_ocasionplus(marca="audi", tipo="todas"):
    resultados = []
    marca_clean = marca.lower().strip().replace(' ', '-')
    # NOTA: la sección KM0 de OcasionPlus (/coches-km-0/{marca}) NO filtra
    # por marca vía URL -- se comprobó que devuelve siempre el mismo listado
    # genérico sin filtrar (coches de marcas variadas), sea cual sea la
    # marca solicitada. El filtrado por marca ahí depende de JavaScript en
    # el navegador, no de la URL, así que se desactiva para no devolver
    # resultados de marca incorrecta. Solo se usa la sección "Ocasión"
    # (segunda mano), que sí filtra correctamente por marca.
    categorias = [
        ("Ocasión", f"https://www.ocasionplus.com/coches-segunda-mano/{marca_clean}")
    ]

    session = requests.Session()
    enlaces_procesados = set()

    # El año y los km ya vienen embebidos en la propia URL de la ficha:
    # /coches-segunda-mano/{slug}-con-{km}km-{año}-{id}
    link_pattern = re.compile(
        r'/coches-segunda-mano(?:-canarias)?/([a-z0-9-]+)-con-(\d+)km-(20\d{2})-[a-z0-9]+',
        re.IGNORECASE
    )

    MAX_PAGINAS = 3  # amplía cobertura; sube este número si necesitas más resultados

    for cat_label, base_url in categorias:
        try:
            for pagina in range(1, MAX_PAGINAS + 1):
                url_pagina = base_url if pagina == 1 else f"{base_url}?pagina={pagina}"

                res = session.get(url_pagina, headers={"User-Agent": HEADERS["User-Agent"]}, timeout=8)
                if res.status_code != 200:
                    break

                soup = BeautifulSoup(res.text, "html.parser")
                car_links = soup.find_all('a', href=lambda h: h and link_pattern.search(h))

                nuevos_en_esta_pagina = 0

                for a in car_links:
                    href = a['href']
                    if href in enlaces_procesados:
                        continue

                    m = link_pattern.search(href)
                    if not m:
                        continue
                    enlaces_procesados.add(href)
                    nuevos_en_esta_pagina += 1

                    slug_raw, km_str, anio_val = m.group(1), m.group(2), m.group(3)

                    text = a.get_text(separator=" | ", strip=True)
                    # Quita "Ahorro de X €" (fichas "¡A estrenar!") para que
                    # no se confunda con un precio real.
                    text_limpio = re.sub(r'Ahorro de\s*\d{1,3}(?:\.\d{3})*\s*€', '', text, flags=re.IGNORECASE)

                    precios_validos = []
                    for pm in re.finditer(r'(\d{1,3}(?:\.\d{3})+)\s*€', text_limpio):
                        cola = text_limpio[pm.end():pm.end() + 10]
                        if re.match(r'\s*/\s*mes', cola, re.IGNORECASE):
                            continue
                        val = limpiar_numero(pm.group(1))
                        if val and 3000 <= val <= 200000:
                            precios_validos.append(val)

                    if not precios_validos:
                        continue

                    # Si hay descuento aplicado se muestran dos precios
                    # (original y con descuento); si no, solo uno. El
                    # financiado se usa como el más bajo de los dos, igual
                    # que en el resto de proveedores.
                    if len(precios_validos) >= 2:
                        contado_val = max(precios_validos)
                        financiado_val = min(precios_validos)
                    else:
                        contado_val = financiado_val = precios_validos[0]

                    titulo = re.sub(r'\b(\d)\s(\d)\b', r'\1.\2', slug_raw.replace('-', ' ').title())

                    url_coche = href if href.startswith('http') else f"https://www.ocasionplus.com{href}"
                    potencia = extraer_potencia(text_limpio)
                    # Los km ya vienen exactos en la propia URL (con-{km}km-),
                    # no hace falta ninguna regex adicional sobre el texto.
                    kilometros = f"{int(km_str):,}".replace(",", ".") + " km"
                    combustible = extraer_combustible(text_limpio)
                    cambio = extraer_cambio(text_limpio)

                    resultados.append({
                        "proveedor": "OcasionPlus",
                        "categoria": cat_label,
                        "marca": marca.capitalize(),
                        "modelo": titulo,
                        "anio": str(anio_val),
                        "contado": f"{contado_val:,}".replace(",", ".") + " €",
                        "financiado": f"{financiado_val:,}".replace(",", ".") + " €",
                        "potencia": potencia,
                        "kilometros": kilometros,
                        "combustible": combustible,
                        "cambio": cambio,
                        "url": url_coche
                    })

                # Si una página no aporta ningún enlace nuevo, hemos llegado
                # al final del listado (o a una página vacía): paramos aquí.
                if nuevos_en_esta_pagina == 0:
                    break

        except Exception as e:
            print(f"[OcasionPlus] Error en {base_url}: {e}")

    print(f"[OcasionPlus] Vehículos extraídos: {len(resultados)}")
    return resultados

def extraer_potencia_mibec(configuracion):
    """Mibec expone la ficha técnica en formato schema.org (JSON-LD), mucho
    más fiable que raspar texto libre. La potencia en kW suele venir
    explícita (ej. '165KW'); si no aparece, se recurre al número que
    precede a las puertas (ej. '...DHT 224 5P' -> 224 CV), patrón muy
    consistente en las fichas de este proveedor."""
    potencia = extraer_potencia(configuracion)
    if potencia != "N/D":
        return potencia
    match_cv_puertas = re.search(r'\b(\d{2,3})\s+\dP\b', configuracion or "")
    if match_cv_puertas:
        return f"{match_cv_puertas.group(1)} CV"
    return "N/D"

# ================= 7. MIBEC =================
def scrape_mibec(marca="audi", tipo="todas"):
    """Grupo Mibec expone los datos de cada vehículo en bloques JSON-LD
    (schema.org/Car) incrustados en el propio HTML servido por el
    servidor -- no hace falta Playwright, con requests+regex es
    suficiente y además es un dato estructurado, mucho más fiable que
    el raspado de texto libre que usan el resto de proveedores."""
    resultados = []
    marca_clean = marca.lower().strip()
    base_url = f"https://mibec.es/coches-ocasion/{marca_clean}"

    session = requests.Session()
    enlaces_procesados = set()
    MAX_PAGINAS = 3

    ld_json_pattern = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.DOTALL)
    link_pattern = re.compile(r'href="(/vehiculos-detalle/[^"]+)"')

    for pagina in range(1, MAX_PAGINAS + 1):
        url_pagina = base_url if pagina == 1 else f"{base_url}?page={pagina}"
        try:
            res = session.get(url_pagina, headers={"User-Agent": HEADERS["User-Agent"]}, timeout=8)
            if res.status_code != 200:
                break

            coches_json = []
            for bloque in ld_json_pattern.findall(res.text):
                try:
                    d = json.loads(bloque)
                except Exception:
                    continue
                if d.get("@type") == "Car":
                    coches_json.append(d)

            if not coches_json:
                break

            # Enlaces únicos a fichas, en el mismo orden en que aparecen en
            # el HTML (mismo orden en que aparecen los bloques JSON-LD).
            enlaces_pagina = []
            vistos_pagina = set()
            for href in link_pattern.findall(res.text):
                if href not in vistos_pagina:
                    vistos_pagina.add(href)
                    enlaces_pagina.append(href)

            nuevos_en_esta_pagina = 0
            for i, coche in enumerate(coches_json):
                # Correlación por orden de aparición: el bloque JSON-LD nº i
                # corresponde al enlace nº i. Si alguna vez no coincidieran
                # en cantidad, se usa la URL del listado como respaldo para
                # no perder la ficha ni romper el resultado.
                href = enlaces_pagina[i] if i < len(enlaces_pagina) else None
                url_coche = f"https://mibec.es{href}" if href else url_pagina

                if url_coche in enlaces_procesados:
                    continue
                enlaces_procesados.add(url_coche)
                nuevos_en_esta_pagina += 1

                marca_real = coche.get("brand", {}).get("name", marca.capitalize())

                modelo = re.sub(r'\s+', ' ', coche.get("name", "")).strip().title()

                anio_val = coche.get("vehicleModelDate")
                if not anio_val:
                    anio_match = re.match(r'(20\d{2})', str(coche.get("dateVehicleFirstRegistered", "")))
                    anio_val = anio_match.group(1) if anio_match else "2022"

                precio = coche.get("offers", {}).get("price")
                precio_str = f"{int(precio):,}".replace(",", ".") + " €" if precio else "Consultar"

                potencia = extraer_potencia_mibec(coche.get("vehicleConfiguration", ""))

                km_valor = coche.get("mileageFromOdometer", {}).get("value")
                kilometros = f"{int(km_valor):,}".replace(",", ".") + " km" if km_valor is not None else "N/D"
                combustible = extraer_combustible(coche.get("vehicleConfiguration", ""))
                cambio = extraer_cambio(coche.get("vehicleConfiguration", ""))

                condicion = coche.get("itemCondition", "")
                categoria = "KM0" if "New" in condicion else "Ocasión"

                resultados.append({
                    "proveedor": "Mibec",
                    "categoria": categoria,
                    "marca": marca_real,
                    "modelo": modelo,
                    "anio": str(anio_val),
                    "contado": precio_str,
                    "financiado": precio_str,
                    "potencia": potencia,
                    "kilometros": kilometros,
                    "combustible": combustible,
                    "cambio": cambio,
                    "url": url_coche
                })

            if nuevos_en_esta_pagina == 0:
                break

        except Exception as e:
            print(f"[Mibec] Error en {url_pagina}: {e}")
            break

    print(f"[Mibec] Vehículos extraídos: {len(resultados)}")
    return resultados

# ================= 8. CLICARS =================
def scrape_clicars(marca="audi", tipo="todas"):
    """Clicars expone toda la ficha -- marca, versión, año, km, potencia,
    cambio y combustible -- directamente en clases y atributos data-* del
    propio HTML servido por el servidor (ej. <span class="fuelName">,
    data-price-web-offer="..."). No hace falta Playwright, con requests
    es suficiente, y reutiliza los mismos extractores genéricos
    (potencia/km/cambio/combustible) que el resto de proveedores."""
    resultados = []
    marca_clean = marca.lower().strip().replace(' ', '-')
    base_url = f"https://www.clicars.com/coches-segunda-mano-ocasion/{marca_clean}"

    session = requests.Session()
    enlaces_procesados = set()
    MAX_PAGINAS = 3

    for pagina in range(1, MAX_PAGINAS + 1):
        url_pagina = base_url if pagina == 1 else f"{base_url}?page={pagina}"
        try:
            res = session.get(url_pagina, headers={"User-Agent": HEADERS["User-Agent"]}, timeout=8)
            if res.status_code != 200:
                break

            texto = res.text
            # Cada ficha empieza en un <article data-vehicle-web-id="...">;
            # se trocea el HTML por esos puntos para no mezclar los datos
            # de una ficha con los de la siguiente.
            starts = [m.start() for m in re.finditer(r'<article\s+data-vehicle-web-id="\d+"', texto)]
            if not starts:
                break
            starts.append(len(texto))

            nuevos_en_esta_pagina = 0
            for i in range(len(starts) - 1):
                bloque = texto[starts[i]:starts[i + 1]]

                href_m = re.search(r'href="(https://www\.clicars\.com/coches-segunda-mano-ocasion/[^"]+)"', bloque)
                if not href_m:
                    continue
                url_coche = href_m.group(1)
                if url_coche in enlaces_procesados:
                    continue
                enlaces_procesados.add(url_coche)
                nuevos_en_esta_pagina += 1

                maker_m = re.search(r'<h2 class="maker ellipsis"><strong>([^<]+)</strong>', bloque)
                version_m = re.search(r'<span class="version ellipsis">([^<]+)</span>', bloque)
                info_m = re.search(r'<span class="info ellipsis">\s*([^<]+)</span>', bloque)
                fuel_m = re.search(r'<span class="fuelName">([^<]+)</span>', bloque)

                marca_real = marca.capitalize()
                maker_texto = html.unescape(maker_m.group(1)).strip() if maker_m else marca_real
                version = html.unescape(version_m.group(1)).strip() if version_m else ""
                modelo = f"{maker_texto} {version}".strip()

                # El año, los km, la potencia y el cambio vienen juntos en
                # la propia línea "info" (ej. "2015 | 105.332km | 69CV |
                # Manual"), así que se reutilizan los mismos extractores
                # genéricos ya usados por el resto de proveedores.
                info_texto = info_m.group(1) if info_m else ""
                anio_match = re.search(r'\b(20\d{2})\b', info_texto)
                anio_val = anio_match.group(1) if anio_match else "2020"

                potencia = extraer_potencia(info_texto)
                kilometros = extraer_km(info_texto)
                cambio = extraer_cambio(info_texto)
                combustible = extraer_combustible(fuel_m.group(1)) if fuel_m else extraer_combustible(bloque)

                precio_oferta = re.search(r'data-price-web-offer="([\d.]+)€"', bloque)
                precio_web = re.search(r'data-price-web="([\d.]+)€"', bloque)
                precio_financ = re.search(r'data-price-financing="([\d.]+)€"', bloque)

                contado_val = limpiar_numero(precio_oferta.group(1)) if precio_oferta else (
                    limpiar_numero(precio_web.group(1)) if precio_web else None)
                if not contado_val:
                    continue
                financiado_val = limpiar_numero(precio_financ.group(1)) if precio_financ else contado_val

                resultados.append({
                    "proveedor": "Clicars",
                    "categoria": "Ocasión",
                    "marca": marca_real,
                    "modelo": modelo,
                    "anio": anio_val,
                    "contado": f"{contado_val:,}".replace(",", ".") + " €",
                    "financiado": f"{financiado_val:,}".replace(",", ".") + " €",
                    "potencia": potencia,
                    "kilometros": kilometros,
                    "combustible": combustible,
                    "cambio": cambio,
                    "url": url_coche
                })

            if nuevos_en_esta_pagina == 0:
                break

        except Exception as e:
            print(f"[Clicars] Error en {url_pagina}: {e}")
            break

    print(f"[Clicars] Vehículos extraídos: {len(resultados)}")
    return resultados

# ================= 9. SPOTICAR =================
async def scrape_spoticar_async(marca="audi", tipo="todas", max_paginas=3):
    """Spoticar (red de coches de ocasión de concesionarios oficiales del
    grupo Stellantis: Peugeot, Citroën, Opel...) carga el listado de
    vehículos vía JavaScript -- el HTML servido inicialmente llega vacío,
    hace falta Playwright para que se renderice, igual que en Flexicar y
    Coches.net.

    NOTA sobre el filtro de marca: no se ha podido verificar en vivo por
    no tener acceso de red a spoticar.es desde este entorno. Se ha
    construido por analogía con el patrón de filtro de categoría que sí
    aparece confirmado en el propio HTML de la web
    (?filters[0][category]=...), aplicándolo aquí a 'brand'. Si esta
    suposición fuera incorrecta, el scraper simplemente no encontrará
    marca en las tarjetas y devolverá 0 resultados (no fallos ni datos
    erróneos) -- conviene probarlo primero de forma aislada."""
    resultados = []
    marca_clean = marca.lower().strip()
    vistas = set()

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
            )
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
                viewport={"width": 1920, "height": 1080},
                locale="es-ES"
            )
            page = await context.new_page()
            await page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

            for pagina in range(1, max_paginas + 1):
                target_url = (
                    f"https://www.spoticar.es/comprar-coches-de-ocasion"
                    f"?page={pagina}&filters%5B0%5D%5Bbrand%5D={marca_clean}"
                )

                print(f"[Spoticar] Cargando: {target_url}")
                try:
                    await page.goto(target_url, wait_until="domcontentloaded", timeout=25000)
                except Exception as e:
                    print(f"[Spoticar] Timeout/navegación: {e}")
                    break

                for selector in ["#onetrust-accept-btn-handler", "button:has-text('Aceptar')", "button:has-text('ACEPTAR')"]:
                    try:
                        btn = page.locator(selector).first
                        if await btn.is_visible(timeout=2000):
                            await btn.click()
                            break
                    except Exception:
                        pass

                await page.wait_for_timeout(2500)
                for _ in range(3):
                    await page.evaluate("window.scrollBy(0, document.body.scrollHeight/4)")
                    await page.wait_for_timeout(800)

                content = await page.content()
                soup = BeautifulSoup(content, "html.parser")

                # Solo tarjetas reales del listado -- se excluye la carrusel
                # "home-teaser" que aparece en la página de inicio, para no
                # confundirla con resultados de búsqueda de verdad.
                cards = [
                    c for c in soup.find_all('div', class_='vehicle-card')
                    if 'home-teaser' not in (c.get('class') or [])
                ]

                if not cards:
                    break

                nuevos_en_esta_pagina = 0
                for card in cards:
                    link_tag = card.find('a', class_='vehicle-card-link') or card.find('a', class_='vehicle-images-link')
                    if not link_tag or not link_tag.get('href'):
                        continue
                    href = link_tag['href']
                    if href in vistas:
                        continue

                    titulo_tag = card.find('h3')
                    if not titulo_tag:
                        continue
                    modelo_base = titulo_tag.contents[0].strip() if titulo_tag.contents else titulo_tag.get_text(strip=True)
                    version_tag = titulo_tag.find('span', class_='car-version')
                    version = version_tag.get_text(strip=True) if version_tag else ""
                    titulo = f"{modelo_base} {version}".strip()

                    if marca_clean not in titulo.lower() and marca_clean not in href.lower():
                        continue

                    vistas.add(href)
                    nuevos_en_esta_pagina += 1

                    tags = [t.get_text(strip=True) for t in card.select('.vehicle-card-tags .tag')]
                    texto_tags = " | ".join(tags)

                    precio_tag = card.find('span', class_='price-value')
                    contado_val = limpiar_numero(precio_tag.get_text()) if precio_tag else None
                    if not contado_val:
                        continue
                    financiado_tag = card.find('span', class_='price-finance-value')
                    financiado_val = limpiar_numero(financiado_tag.get_text()) if financiado_tag else contado_val

                    anio_match = re.search(r'\b(20\d{2})\b', texto_tags)
                    anio_val = anio_match.group(1) if anio_match else "2022"

                    url_coche = href if href.startswith('http') else f"https://www.spoticar.es{href}"

                    resultados.append({
                        "proveedor": "Spoticar",
                        "categoria": "Ocasión",
                        "marca": marca.capitalize(),
                        "modelo": titulo,
                        "anio": anio_val,
                        "contado": f"{contado_val:,}".replace(",", ".") + " €",
                        "financiado": f"{financiado_val:,}".replace(",", ".") + " €",
                        "potencia": extraer_potencia(titulo + " " + href),
                        "kilometros": extraer_km(texto_tags),
                        "combustible": extraer_combustible(texto_tags),
                        "cambio": extraer_cambio(texto_tags),
                        "url": url_coche
                    })

                if nuevos_en_esta_pagina == 0:
                    break

            await browser.close()

    except Exception as e:
        print(f"[Spoticar] Error durante scraping: {e}")

    print(f"[Spoticar] Vehículos extraídos: {len(resultados)}")
    return resultados


def scrape_spoticar(marca="audi", tipo="todas"):
    return asyncio.run(scrape_spoticar_async(marca=marca, tipo=tipo))

# ================= 10. AUTOSCOUT24 =================
def obtener_precio_contado_real_autoscout(url_detalle, session):
    """Cuando AutoScout24 marca un anuncio como 'precio condicional'
    (ligado a financiación), el precio al contado real NO viene en el
    listado de búsqueda -- solo aparece como texto libre dentro del
    campo 'description' de la ficha individual del vehículo (ej.
    'Precio al contado: 23290 euros'), confirmado contra una ficha real.

    Se hace UNA petición adicional solo para estos casos concretos (no
    para todos los vehículos), reutilizando el mismo patrón __NEXT_DATA__
    que ya usa el listado de búsqueda."""
    try:
        res = session.get(url_detalle, headers={"User-Agent": HEADERS["User-Agent"]}, timeout=8)
        if res.status_code != 200:
            return None
        m = re.search(
            r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
            res.text, re.DOTALL
        )
        if not m:
            return None
        data = json.loads(m.group(1))
        descripcion = data.get("props", {}).get("pageProps", {}).get("listingDetails", {}).get("description", "")
        precio_match = re.search(r'Precio al contado:?\s*([\d.,]+)\s*euros?', descripcion, re.IGNORECASE)
        if precio_match:
            return limpiar_numero(precio_match.group(1))
    except Exception as e:
        print(f"[AutoScout24] No se pudo obtener el precio al contado real de {url_detalle}: {e}")
    return None

def scrape_autoscout24(marca="audi", tipo="todas", max_paginas=3):
    """AutoScout24 es una web Next.js: toda la información de cada anuncio
    de la página (precio, marca, modelo, km, potencia, combustible,
    cambio) viaja embebida como JSON estructurado dentro de un bloque
    <script id="__NEXT_DATA__">, ya en el HTML servido por el servidor --
    no hace falta parsear texto libre ni usar Playwright, con requests
    es suficiente.

    NOTA sobre el filtro de marca y paginación: no se ha podido verificar
    en vivo por no tener acceso de red a autoscout24.es desde este
    entorno. El patrón /lst/{marca}?atype=C&cy=E&page=N se basa en la
    estructura de URL pública y estable de AutoScout24 y en los propios
    parámetros (atype=C, cy=E) que aparecen en el pageQuery del HTML de
    ejemplo -- pero, igual que con Spoticar, conviene probarlo aislado
    antes de confiar en él del todo."""
    resultados = []
    marca_clean = marca.lower().strip()
    session = requests.Session()
    ids_procesados = set()

    next_data_pattern = re.compile(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        re.DOTALL
    )

    for pagina in range(1, max_paginas + 1):
        url_pagina = f"https://www.autoscout24.es/lst/{marca_clean}?atype=C&cy=E&page={pagina}"
        try:
            res = session.get(url_pagina, headers={"User-Agent": HEADERS["User-Agent"]}, timeout=10)
            if res.status_code != 200:
                break

            m = next_data_pattern.search(res.text)
            if not m:
                break
            try:
                data = json.loads(m.group(1))
            except Exception:
                break

            listings = data.get("props", {}).get("pageProps", {}).get("listings", [])
            if not listings:
                break

            nuevos_en_esta_pagina = 0
            for listing in listings:
                lid = listing.get("id")
                if not lid or lid in ids_procesados:
                    continue
                ids_procesados.add(lid)

                vehiculo = listing.get("vehicle", {})
                marca_real = vehiculo.get("make", marca.capitalize())
                modelo_txt = f"{vehiculo.get('model', '')} {vehiculo.get('modelVersionInput', '')}".strip()
                if not modelo_txt:
                    continue

                precio_info = listing.get("price", {})
                precio_val = precio_info.get("priceRaw")
                if not precio_val:
                    continue

                href = listing.get("url", "")
                url_coche = href if href.startswith("http") else f"https://www.autoscout24.es{href}"

                precio_destacado = f"{int(precio_val):,}".replace(",", ".") + " €"

                # Cuando el precio es "condicional" (normalmente ligado a
                # financiación), el número destacado del listado NO es el
                # precio al contado real -- se intenta recuperar el real
                # con UNA petición extra a la propia ficha (confirmado
                # contra una ficha real: aparece como texto libre en el
                # campo 'description'); si no se encuentra, se muestra
                # "Consultar" en vez de un dato incorrecto duplicado.
                if precio_info.get("isConditionalPrice"):
                    contado_real = obtener_precio_contado_real_autoscout(url_coche, session)
                    contado = f"{contado_real:,}".replace(",", ".") + " €" if contado_real else "Consultar"
                    financiado = precio_destacado
                else:
                    contado = precio_destacado
                    financiado = precio_destacado

                # vehicleDetails es una lista de {data, iconName, ariaLabel};
                # se indexa por iconName -- más estable que fiarse del orden
                # o del idioma del ariaLabel.
                detalles = {d.get("iconName"): d.get("data", "") for d in listing.get("vehicleDetails", [])}

                anio_match = re.search(r'(20\d{2})', detalles.get("calendar", ""))
                anio_val = anio_match.group(1) if anio_match else "2020"

                kilometros = extraer_km(detalles.get("mileage_odometer", ""))
                potencia = extraer_potencia(detalles.get("speedometer", ""))
                combustible = extraer_combustible(detalles.get("gas_pump", "") or vehiculo.get("fuel", ""))
                cambio = extraer_cambio(detalles.get("gearbox", "") or vehiculo.get("transmission", ""))

                resultados.append({
                    "proveedor": "AutoScout24",
                    "categoria": "Ocasión",
                    "marca": marca_real,
                    "modelo": modelo_txt,
                    "anio": anio_val,
                    "contado": contado,
                    "financiado": financiado,
                    "potencia": potencia,
                    "kilometros": kilometros,
                    "combustible": combustible,
                    "cambio": cambio,
                    "url": url_coche
                })
                nuevos_en_esta_pagina += 1

            if nuevos_en_esta_pagina == 0:
                break

        except Exception as e:
            print(f"[AutoScout24] Error en {url_pagina}: {e}")
            break

    print(f"[AutoScout24] Vehículos extraídos: {len(resultados)}")
    return resultados

# ========== RUTAS FLASK ==========
@app.route('/')
def index():
    resp = make_response(send_file(TEMPLATE_PATH))
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    return resp

@app.route('/api/scrape', methods=['POST'])
def scrape():
    data = request.json or {}
    marca = data.get('marca', 'audi')
    categoria = data.get('categoria', 'todas')
    proveedor_raw = str(data.get('proveedor', 'todos')).lower().strip()

    anio_min = data.get('anioMin')
    anio_max = data.get('anioMax')
    precio_min = data.get('precioMin')
    precio_max = data.get('precioMax')
    potencia_min = data.get('potenciaMin')
    potencia_max = data.get('potenciaMax')
    km_min = data.get('kmMin')
    km_max = data.get('kmMax')
    combustible_filtro = str(data.get('combustible', 'todas')).lower().strip()
    cambio_filtro = str(data.get('cambio', 'todos')).lower().strip()

    print(f"\n================ SOLICITUD DE BÚSQUEDA ================")
    print(f"Marca: {marca} | Proveedor: '{proveedor_raw}' | Años: {anio_min}-{anio_max} | Precios: {precio_min}-{precio_max} € | Potencia: {potencia_min}-{potencia_max} CV | Km: {km_min}-{km_max} | Combustible: {combustible_filtro} | Cambio: {cambio_filtro}")

    todos_los_resultados = []
    conteo_por_proveedor = {}
    urls_por_proveedor = {}
    tiempos_por_proveedor = {}

    def _ejecutar(nombre, funcion, **kwargs):
        """Llama a un scraper y anota cuántos resultados brutos ha traído,
        cuánto ha tardado y qué URLs concretas ha devuelto -- lo último es
        lo que permite comparar después, entre ejecuciones idénticas, si
        de verdad hay variabilidad y en qué proveedor concreto. No altera
        en nada el comportamiento del scraper en sí."""
        t0 = time.time()
        try:
            resultados_proveedor = funcion(**kwargs)
        except Exception as e:
            print(f"[{nombre}] Excepción no capturada por el propio scraper: {e}")
            resultados_proveedor = []
        tiempos_por_proveedor[nombre] = round(time.time() - t0, 2)
        conteo_por_proveedor[nombre] = len(resultados_proveedor)
        urls_por_proveedor[nombre] = [r.get("url") for r in resultados_proveedor]
        todos_los_resultados.extend(resultados_proveedor)

    if proveedor_raw == 'autokoleccio':
        _ejecutar("Autokolecció", scrape_autokoleccio, marca=marca, tipo=categoria)
    elif proveedor_raw == 'flexicar':
        _ejecutar("Flexicar", scrape_flexicar, marca=marca)
    elif proveedor_raw == 'cochesnet':
        _ejecutar("Coches.net", scrape_cochesnet, marca=marca)
    elif proveedor_raw in ['cochesinternet', 'cochesinternet.net']:
        _ejecutar("Cochesinternet.net", scrape_cochesinternet, marca=marca, tipo=categoria)
    elif proveedor_raw in ['cochescom', 'coches.com']:
        _ejecutar("Coches.com", scrape_cochescom, marca=marca, tipo=categoria)
    elif proveedor_raw == 'ocasionplus':
        _ejecutar("OcasionPlus", scrape_ocasionplus, marca=marca, tipo=categoria)
    elif proveedor_raw == 'mibec':
        _ejecutar("Mibec", scrape_mibec, marca=marca, tipo=categoria)
    elif proveedor_raw == 'clicars':
        _ejecutar("Clicars", scrape_clicars, marca=marca, tipo=categoria)
    elif proveedor_raw == 'spoticar':
        _ejecutar("Spoticar", scrape_spoticar, marca=marca, tipo=categoria)
    elif proveedor_raw == 'autoscout24':
        _ejecutar("AutoScout24", scrape_autoscout24, marca=marca, tipo=categoria)
    else:
        # Antes esto llamaba a los 10 scrapers en secuencia dentro de la misma
        # petición HTTP -- en local no había límite de tiempo y acababa
        # "funcionando", pero en Render la suma de los ~10 tiempos supera el
        # timeout del worker/proxy y la petición cae con 500/502 (confirmado
        # en el test de estabilidad: dos fallos consecutivos justo pasado el
        # minuto 2). Se ejecutan en paralelo con hilos para que el tiempo
        # total sea el del scraper más lento, no la suma de todos.
        #
        # max_workers=5 (no 10) a propósito: varios scrapers levantan un
        # navegador Playwright real, y lanzar 10 a la vez puede disparar el
        # consumo de memoria del plan de Render y provocar un OOM (502
        # distinto pero igual de bloqueante). Si tras desplegar esto se ve
        # estable, se puede subir gradualmente; si aparecen 502 nuevos,
        # bajarlo en vez de subirlo.
        tareas = [
            ("Autokolecció", scrape_autokoleccio, dict(marca=marca, tipo=categoria)),
            ("Flexicar", scrape_flexicar, dict(marca=marca)),
            ("Coches.net", scrape_cochesnet, dict(marca=marca)),
            ("Cochesinternet.net", scrape_cochesinternet, dict(marca=marca, tipo=categoria)),
            ("Coches.com", scrape_cochescom, dict(marca=marca, tipo=categoria)),
            ("OcasionPlus", scrape_ocasionplus, dict(marca=marca, tipo=categoria)),
            ("Mibec", scrape_mibec, dict(marca=marca, tipo=categoria)),
            ("Clicars", scrape_clicars, dict(marca=marca, tipo=categoria)),
            ("Spoticar", scrape_spoticar, dict(marca=marca, tipo=categoria)),
            ("AutoScout24", scrape_autoscout24, dict(marca=marca, tipo=categoria)),
        ]
        with ThreadPoolExecutor(max_workers=5) as executor:
            futuros = [
                executor.submit(_ejecutar, nombre, funcion, **kwargs)
                for nombre, funcion, kwargs in tareas
            ]
            for futuro in as_completed(futuros):
                futuro.result()  # _ejecutar ya captura sus propias excepciones; esto solo repropaga fallos inesperados del propio hilo

    resultados_filtrados = []
    for item in todos_los_resultados:
        item.setdefault("potencia", "N/D")
        item.setdefault("kilometros", "N/D")
        item.setdefault("combustible", "N/D")
        item.setdefault("cambio", "N/D")
        p_val = limpiar_numero(item.get("contado"))
        if p_val:
            if precio_min and p_val < int(precio_min):
                continue
            if precio_max and p_val > int(precio_max):
                continue

        a_val = limpiar_numero(item.get("anio"))
        if a_val:
            if anio_min and a_val < int(anio_min):
                continue
            if anio_max and a_val > int(anio_max):
                continue

        pot_val = valor_potencia_cv(item.get("potencia"))
        if pot_val is not None:
            if potencia_min and pot_val < int(potencia_min):
                continue
            if potencia_max and pot_val > int(potencia_max):
                continue

        km_val = limpiar_numero(item.get("kilometros"))
        if km_val is not None:
            if km_min and km_val < int(km_min):
                continue
            if km_max and km_val > int(km_max):
                continue

        if combustible_filtro not in ("todas", "todos", ""):
            etiqueta_esperada = COMBUSTIBLE_MAP.get(combustible_filtro)
            if etiqueta_esperada and item.get("combustible") != etiqueta_esperada:
                continue

        if cambio_filtro not in ("todas", "todos", ""):
            etiqueta_esperada_cambio = CAMBIO_MAP.get(cambio_filtro)
            if etiqueta_esperada_cambio and item.get("cambio") != etiqueta_esperada_cambio:
                continue

        resultados_filtrados.append(item)

    calcular_puntuaciones(resultados_filtrados)
    resultados_filtrados.sort(key=lambda item: item["puntuacion"] if item["puntuacion"] is not None else -1, reverse=True)

    print(f"TOTAL OBTENIDOS: {len(todos_los_resultados)} | ENVIADOS TRAS FILTROS: {len(resultados_filtrados)}")
    print(f"Desglose por proveedor: {conteo_por_proveedor}")
    print(f"Tiempos por proveedor (s): {tiempos_por_proveedor}")
    print(f"========================================================\n")

    registrar_diagnostico(
        filtros={
            "marca": marca, "categoria": categoria, "proveedor": proveedor_raw,
            "anioMin": anio_min, "anioMax": anio_max,
            "precioMin": precio_min, "precioMax": precio_max,
            "potenciaMin": potencia_min, "potenciaMax": potencia_max,
            "kmMin": km_min, "kmMax": km_max,
            "combustible": combustible_filtro, "cambio": cambio_filtro
        },
        conteo_por_proveedor=conteo_por_proveedor,
        urls_por_proveedor=urls_por_proveedor,
        tiempos_por_proveedor=tiempos_por_proveedor,
        total_filtrado=len(resultados_filtrados)
    )

    return jsonify({
        "status": "success",
        "total": len(resultados_filtrados),
        "data": resultados_filtrados,
        "proveedores": conteo_por_proveedor
    })

@app.route('/api/diagnostico', methods=['GET'])
def descargar_diagnostico():
    """Sirve el archivo diagnostico_estabilidad.jsonl para poder
    descargarlo desde fuera (útil en Render, donde no hay acceso directo
    al disco). Protegido con un token simple por query param para que no
    quede expuesto a cualquiera que adivine la URL.

    Uso: https://tu-app.onrender.com/api/diagnostico?token=TU_TOKEN
    El token se define con la variable de entorno DIAGNOSTICO_TOKEN en
    el panel de Render (Environment). Si no la defines, usa un valor por
    defecto -- cámbialo antes de desplegar."""
    token_esperado = os.environ.get('DIAGNOSTICO_TOKEN', 'cambia-este-token')
    if request.args.get('token') != token_esperado:
        return jsonify({"error": "no autorizado"}), 403
    if not os.path.exists(RUTA_LOG_DIAGNOSTICO):
        return jsonify({"error": "el log todavía no existe, lanza alguna búsqueda primero"}), 404
    return send_file(RUTA_LOG_DIAGNOSTICO, as_attachment=True, download_name='diagnostico_estabilidad.jsonl')

if __name__ == '__main__':
    print("Servidor listo en http://127.0.0.1:5000")
    app.run(debug=True, port=5000)
