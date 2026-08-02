import os
from flask import Flask, request, jsonify, send_file, make_response
import requests
from bs4 import BeautifulSoup
import json
import re
import asyncio
from playwright.async_api import async_playwright

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_PATH = os.path.join(BASE_DIR, 'templates', 'index.html')
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

def limpiar_numero(cadena):
    if not cadena or cadena == "N/D":
        return None
    num = re.sub(r'[^\d]', '', str(cadena))
    return int(num) if num else None

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

                    resultados.append({
                        "proveedor": "Autokolecció",
                        "categoria": cat_label,
                        "marca": marca_real,
                        "modelo": titulo,
                        "anio": str(anio_val),
                        "contado": contado,
                        "financiado": financiado,
                        "url": url_coche
                    })

            except Exception as e:
                print(f"[Autokolecció] Error en {url}: {e}")
                break

    return resultados

# ================= 2. FLEXICAR (PLAYWRIGHT - VERSIÓN FIABLE) =================
async def scrape_flexicar_async(marca="audi", tipo="todas", max_scrolls=4):
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

            # Scroll progresivo para forzar la carga de tarjetas con lazy-load
            for _ in range(max_scrolls):
                await page.evaluate("window.scrollBy(0, document.body.scrollHeight/4)")
                await page.wait_for_timeout(900)

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

            resultados.append({
                "proveedor": "Flexicar",
                "categoria": "Ocasión",
                "marca": marca.capitalize(),
                "modelo": titulo_raw,
                "anio": str(anio_val),
                "contado": f"{precio_contado:,}".replace(",", ".") + " €",
                "financiado": f"{precio_oferta:,}".replace(",", ".") + " €",
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
            try:
                await page.goto(url, wait_until="commit", timeout=12000)
            except Exception:
                pass

            await page.wait_for_timeout(3500)
            content = await page.content()
            soup = BeautifulSoup(content, "html.parser")
            
            car_links = soup.find_all('a', href=lambda h: h and ('-covo.aspx' in h or ('.aspx' in h and marca_clean in h.lower())))
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

                resultados.append({
                    "proveedor": "Coches.net",
                    "categoria": "Ocasión",
                    "marca": marca.capitalize(),
                    "modelo": titulo,
                    "anio": str(anio_str),
                    "contado": precio_str,
                    "financiado": precio_str,
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

                resultados.append({
                    "proveedor": "Cochesinternet.net",
                    "categoria": cat_label,
                    "marca": marca.capitalize(),
                    "modelo": titulo,
                    "anio": str(anio_val),
                    "contado": contado,
                    "financiado": financiado,
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

                resultados.append({
                    "proveedor": "Coches.com",
                    "categoria": cat_label,
                    "marca": marca.capitalize(),
                    "modelo": titulo,
                    "anio": str(anio_val),
                    "contado": f"{contado_val:,}".replace(",", ".") + " €",
                    "financiado": f"{financiado_val:,}".replace(",", ".") + " €",
                    "url": url_coche
                })

        except Exception as e:
            print(f"[Coches.com] Error en {base_url}: {e}")

    print(f"[Coches.com] Vehículos extraídos: {len(resultados)}")
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

    print(f"\n================ SOLICITUD DE BÚSQUEDA ================")
    print(f"Marca: {marca} | Proveedor: '{proveedor_raw}' | Años: {anio_min}-{anio_max} | Precios: {precio_min}-{precio_max} €")

    todos_los_resultados = []

    if proveedor_raw == 'autokoleccio':
        todos_los_resultados.extend(scrape_autokoleccio(marca=marca, tipo=categoria))
    elif proveedor_raw == 'flexicar':
        todos_los_resultados.extend(scrape_flexicar(marca=marca))
    elif proveedor_raw == 'cochesnet':
        todos_los_resultados.extend(scrape_cochesnet(marca=marca))
    elif proveedor_raw in ['cochesinternet', 'cochesinternet.net']:
        todos_los_resultados.extend(scrape_cochesinternet(marca=marca, tipo=categoria))
    elif proveedor_raw in ['cochescom', 'coches.com']:
        todos_los_resultados.extend(scrape_cochescom(marca=marca, tipo=categoria))
    else:
        todos_los_resultados.extend(scrape_autokoleccio(marca=marca, tipo=categoria))
        todos_los_resultados.extend(scrape_flexicar(marca=marca))
        todos_los_resultados.extend(scrape_cochesnet(marca=marca))
        todos_los_resultados.extend(scrape_cochesinternet(marca=marca, tipo=categoria))
        todos_los_resultados.extend(scrape_cochescom(marca=marca, tipo=categoria))

    resultados_filtrados = []
    for item in todos_los_resultados:
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

        resultados_filtrados.append(item)

    print(f"TOTAL OBTENIDOS: {len(todos_los_resultados)} | ENVIADOS TRAS FILTROS: {len(resultados_filtrados)}")
    print(f"========================================================\n")

    return jsonify({
        "status": "success",
        "total": len(resultados_filtrados),
        "data": resultados_filtrados
    })

if __name__ == '__main__':
    print("Servidor listo en http://127.0.0.1:5000")
    app.run(debug=True, port=5000)
