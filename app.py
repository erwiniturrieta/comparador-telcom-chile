import os
import re
import sys
import asyncio
import subprocess
from typing import List, Dict, Tuple

import pandas as pd
import streamlit as st
from playwright.async_api import async_playwright


# ============ Configuración de página ============
st.set_page_config(page_title="Comparador Chile", page_icon="📡")
st.title("📡 Mi Comparador Telecom")


# ============ Utilidades ============
@st.cache_resource(show_spinner=False)
def ensure_chromium_installed():
    """
    Descarga Chromium SOLO una vez por sesión del servidor.
    - No usa '--with-deps' (evita sudo en Streamlit Cloud).
    - Ubica los binarios en el HOME del usuario para evitar permisos.
    """
    os.environ.setdefault(
        "PLAYWRIGHT_BROWSERS_PATH",
        os.path.expanduser("~/.cache/ms-playwright")
    )
    try:
        subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        st.error("No fue posible descargar Chromium automáticamente.")
        st.code(e.stdout or "", language="bash")
        raise


def run_async(coro):
    """Ejecuta una corrutina de forma segura (funciona aunque haya o no loop activo)."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            new_loop = asyncio.new_event_loop()
            try:
                asyncio.set_event_loop(new_loop)
                return new_loop.run_until_complete(coro)
            finally:
                new_loop.close()
                asyncio.set_event_loop(loop)
        else:
            return loop.run_until_complete(coro)
    except RuntimeError:
        return asyncio.run(coro)


# ------------ Normalizadores / Heurísticas ------------
PRICE_RE = re.compile(r"\$\s?\d{1,3}(?:\.\d{3})+", re.IGNORECASE)

def clp_to_int(texto: str) -> int:
    """Convierte '$21.990/mes' -> 21990; devuelve -1 si no se puede."""
    if not texto:
        return -1
    nums = re.findall(r"\d+", texto.replace(".", ""))
    return int(nums[0]) if nums else -1

def format_clp(valor: int) -> str:
    return f"${valor:,.0f}".replace(",", ".") if valor >= 0 else ""

def infer_speed(plan: str) -> str:
    """
    Extrae velocidad desde el nombre del plan: 600/800/940/1000, 1 Giga, 10 Gigas, etc.
    Retorna '600 Mbps', '1 Gbps', '10 Gbps', etc. Si no encuentra, ''.
    """
    if not plan:
        return ""
    txt = plan.lower()

    # '10 Gigas', '3 Gigas', '1 Giga'
    m_gigas = re.search(r"(\d+)\s*giga?s?", txt)
    if m_gigas:
        val = int(m_gigas.group(1))
        return f"{val} Gbps" if val != 1 else "1 Gbps"

    # 1000/3000/5000/10000 → Gbps
    m_gbps = re.search(r"\b(10000|5000|3000|2000|1000)\b", txt)
    if m_gbps:
        v = int(m_gbps.group(1))
        return f"{v//1000} Gbps" if v % 1000 == 0 else f"{v} Mbps"

    # 940 caso especial
    if re.search(r"\b940\b", txt):
        return "940 Mbps"

    # 600/800/… (y algunos mínimos frecuentes en catálogos)
    m_mbps = re.search(r"\b(150|300|400|500|560|600|700|800|900)\b", txt)
    if m_mbps:
        return f"{m_mbps.group(1)} Mbps"

    # Gamer suele ser 940 en algunos catálogos
    if "gamer" in txt and "940" not in txt:
        return "940 Mbps"

    return ""

def infer_pack(plan: str) -> str:
    """
    Define 'Pack Seleccionado' según el texto del plan:
    - Contiene 'tv' o 'go' (Mundo GO) -> 'Fibra + TV'
    - Contiene 'tel'/'fija' -> 'Fibra + Telefonía'
    - Contiene 'portabilidad'/'móvil'/'gigas' -> 'Fibra + Móvil'
    - Contiene 'dúo'/'duo' -> 'Fibra + TV/Telefonía' (genérico dúo)
    - Si no coincide -> 'Solo Fibra'
    """
    if not plan:
        return "Solo Fibra"
    t = plan.lower()
    if "portabilidad" in t or "móvil" in t or "movil" in t or "gigas" in t:
        return "Fibra + Móvil"
    if "tv" in t or "mundo go" in t or "go!" in t:
        return "Fibra + TV"
    if "telef" in t or "fija" in t:
        return "Fibra + Telefonía"
    if "dúo" in t or "duo" in t:
        return "Fibra + TV/Telefonía"
    return "Solo Fibra"

def infer_mobile_detail(plan: str) -> str:
    """
    Intenta extraer detalle móvil (p. ej. 'Gigas Libres', '100 GB') desde el texto del plan.
    Si no encuentra, devuelve ''.
    """
    if not plan:
        return ""
    t = plan.lower()
    if "gigas libres" in t or "gigaslibres" in t:
        return "Gigas Libres"
    m = re.search(r"(\d+)\s*gb", t)
    if m:
        return f"{m.group(1)} GB"
    return ""

def extract_plans_via_regex(html: str, max_items: int = 12) -> List[Tuple[str, str, int]]:
    """
    Heurística genérica: busca precios CLP y en una ventana cercana detecta el nombre del plan
    (líneas tipo 'PLAN 1 ...', 'Fibra 800', 'Internet Fibra 600 Megas', etc.).
    Devuelve lista de (plan, precio_str, precio_int), deduplicada y acotada.
    """
    results: List[Tuple[str, str, int]] = []

    for m in PRICE_RE.finditer(html):
        start = max(m.start() - 220, 0)
        end = min(m.end() + 220, len(html))
        ctx = html[start:end]

        # 'Plan' cercano (PLAN X, Internet Fibra 600/800/Giga, Fibra 940, etc.)
        plan_match = (
            re.search(r"(PLAN\s*[0-9A-Z]+\s*[^\$<>{}|]{0,90})", ctx, re.IGNORECASE)
            or re.search(r"((?:Internet\s*)?Fibra\s*(?:Gamer|Giga|[0-9]{2,4})\s*(?:Megas?|Mb|Mbps|Gigas?)?)",
                         ctx, re.IGNORECASE)
            or re.search(r"((?:Fibra|Internet)\s*[0-9]{2,4}\s*(?:Mb|Mbps))", ctx, re.IGNORECASE)
        )

        plan_name = None
        if plan_match:
            plan_name = re.sub(r"\s+", " ", plan_match.group(1)).strip()
            # Limpia ruido típico
            plan_name = re.sub(r"(POR\s+\d+\s+MESES|SOLO\s+FIBRA|HASTA|OFERTA\s+WEB).*$",
                               "", plan_name, flags=re.IGNORECASE).strip(" -–:|.")

        price_str = m.group(0)
        price_int = clp_to_int(price_str)

        if plan_name and price_int > 0:
            results.append((plan_name, price_str, price_int))
        if len(results) >= max_items:
            break

    # Dedup por (nombre, precio)
    seen = set()
    dedup = []
    for p in results:
        key = (p[0].lower(), p[2])
        if key not in seen:
            seen.add(key)
            dedup.append(p)
    return dedup


# ============ Scraper Mundo ============
async def buscar_en_mundo(quiere_internet: bool = True) -> List[Dict]:
    """
    1) https://mundointernet.cl/p/td/mundo-internet-planes.html (landing con planes)
    2) https://www.tumundo.cl/ (home con ofertas)
    """
    if not quiere_internet:
        return []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        try:
            context = await browser.new_context(
                viewport={"width": 390, "height": 844},
                user_agent=(
                    "Mozilla/5.0 (Linux; Android 13; SM-G991B) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/121.0.0.0 Mobile Safari/537.36"
                ),
                locale="es-CL",
                extra_http_headers={
                    "Accept-Language": "es-CL,es;q=0.9,en;q=0.8",
                    "Cache-Control": "no-cache",
                    "Pragma": "no-cache",
                },
            )
            page = await context.new_page()

            # Intento 1: MundoInternet (catálogo)
            url1 = "https://mundointernet.cl/p/td/mundo-internet-planes.html"
            await page.goto(url1, wait_until="domcontentloaded", timeout=30000)
            html1 = await page.content()
            plans = extract_plans_via_regex(html1, max_items=10)

            if not plans:
                # Fallback: Home TuMundo (ofertas)
                url2 = "https://www.tumundo.cl/"
                await page.goto(url2, wait_until="domcontentloaded", timeout=30000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=15000)
                    await page.mouse.wheel(0, 1800)
                    await page.wait_for_timeout(800)
                except:
                    pass
                html2 = await page.content()
                plans = extract_plans_via_regex(html2, max_items=10)

            out: List[Dict] = []
            for nombre_plan, precio_str, precio_int in plans:
                out.append({
                    "Compañía": "Mundo",
                    "Pack Seleccionado": infer_pack(nombre_plan),
                    "Velocidad": infer_speed(nombre_plan),
                    "Detalle Móvil": infer_mobile_detail(nombre_plan),
                    "Costo Total": format_clp(precio_int) or precio_str,
                    "Precio_CLP": precio_int
                })
            out.sort(key=lambda r: (r["Precio_CLP"] if r["Precio_CLP"] > 0 else 99_999_999))
            return out

        finally:
            await browser.close()


# ============ Scraper Movistar ============
async def buscar_en_movistar(quiere_internet: bool = True) -> List[Dict]:
    """
    Intenta obtener planes de Movistar desde:
    1) https://ww2.movistar.cl/hogar/internet-hogar/
    2) https://ww2.movistar.cl/hogar/internet-fibra-optica/
    3) https://ww2.movistar.cl/hogar/pack-duos-internet-television/ (combos TV)
    4) https://www.movistar.cl/ (home, último fallback)
    """
    if not quiere_internet:
        return []

    urls_prioridad = [
        "https://ww2.movistar.cl/hogar/internet-hogar/",
        "https://ww2.movistar.cl/hogar/internet-fibra-optica/",
        "https://ww2.movistar.cl/hogar/pack-duos-internet-television/",
        "https://www.movistar.cl/",
    ]

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        try:
            context = await browser.new_context(
                viewport={"width": 390, "height": 844},
                user_agent=(
                    "Mozilla/5.0 (Linux; Android 13; SM-G991B) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/121.0.0.0 Mobile Safari/537.36"
                ),
                locale="es-CL",
                extra_http_headers={
                    "Accept-Language": "es-CL,es;q=0.9,en;q=0.8",
                    "Cache-Control": "no-cache",
                    "Pragma": "no-cache",
                },
            )
            page = await context.new_page()

            plans_all: List[Tuple[str, str, int]] = []

            for url in urls_prioridad:
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=12000)
                        await page.mouse.wheel(0, 2000)
                        await page.wait_for_timeout(600)
                    except:
                        pass

                    html = await page.content()
                    found = extract_plans_via_regex(html, max_items=12)
                    # Evita captar precios móviles puros si llegaran a filtrarse
                    found = [f for f in found if re.search(r"fibra|internet", f[0], re.IGNORECASE)]
                    plans_all.extend(found)

                    if len(plans_all) >= 8:
                        break
                except Exception:
                    continue

            # Deduplicar por (plan, precio)
            seen = set()
            dedup: List[Tuple[str, str, int]] = []
            for plan in plans_all:
                key = (plan[0].lower(), plan[2])
                if key not in seen:
                    seen.add(key)
                    dedup.append(plan)

            out: List[Dict] = []
            for nombre_plan, precio_str, precio_int in dedup[:10]:
                out.append({
                    "Compañía": "Movistar",
                    "Pack Seleccionado": infer_pack(nombre_plan),
                    "Velocidad": infer_speed(nombre_plan),
                    "Detalle Móvil": infer_mobile_detail(nombre_plan),  # 'Gigas Libres', '100 GB', etc. si se detecta
                    "Costo Total": format_clp(precio_int) or precio_str,
                    "Precio_CLP": precio_int
                })

            out.sort(key=lambda r: (r["Precio_CLP"] if r["Precio_CLP"] > 0 else 99_999_999))
            return out

        finally:
            await browser.close()


# ============ Sidebar ============
with st.sidebar:
    st.header("Configuración")
    rut = st.text_input("RUT (para factibilidad)")
    dir_completa = st.text_input("Dirección y Comuna")


# ============ Preferencias de servicio ============
st.subheader("¿Qué servicios necesitas?")
col1, col2 = st.columns(2)
with col1:
    quiere_internet = st.checkbox("🌐 Internet Fibra", value=True)
    # Reservado para siguientes iteraciones
    quiere_movil = st.checkbox("📱 Telefonía Móvil", value=False)
with col2:
    quiere_tv = st.checkbox("📺 TV Cable", value=False)
    quiere_fija = st.checkbox("☎️ Telefonía Fija", value=False)

# Selección de tipo(s) de comparación
st.subheader("¿Qué tipo(s) de pack comparar?")
tipos_pack = st.multiselect(
    "Puedes seleccionar uno o varios:",
    options=["Solo Fibra", "Fibra + TV", "Fibra + Móvil", "Fibra + TV + Telefonía", "Fibra + Telefonía", "Fibra + TV/Telefonía"],
    default=["Solo Fibra", "Fibra + TV", "Fibra + Móvil"]  # valores por defecto más usados
)

# Proveedores
st.subheader("¿Con qué compañías comparar?")
colA, colB = st.columns(2)
with colA:
    incluir_mundo = st.checkbox("Mundo", value=True)
with colB:
    incluir_movistar = st.checkbox("Movistar", value=True)

# Toggle de diagnóstico (apagado por defecto)
modo_debug = st.toggle("Modo diagnóstico (screenshots/HTML)", value=False)


# ============ Acción ============
if st.button("Buscar Ofertas Reales 🚀", use_container_width=True):
    if not rut or not dir_completa:
        st.error("Por favor completa tu RUT y Dirección en la barra lateral.")
    else:
        # 1) Descargar binarios de Chromium (cacheado, sin sudo)
        try:
            ensure_chromium_installed()
        except Exception:
            st.stop()

        resultados: List[Dict] = []

        # 2) Consultas (secuencial para menor RAM)
        with st.spinner("Consultando proveedores..."):
            try:
                if incluir_mundo:
                    st.caption("🔍 Mundo…")
                    resultados_mundo = run_async(buscar_en_mundo(quiere_internet=quiere_internet))
                    resultados.extend(resultados_mundo)

                if incluir_movistar:
                    st.caption("🔍 Movistar…")
                    resultados_movistar = run_async(buscar_en_movistar(quiere_internet=quiere_internet))
                    resultados.extend(resultados_movistar)

                df = pd.DataFrame(resultados)

                # 3) Filtrar por tipos de pack seleccionados
                if tipos_pack:
                    df = df[df["Pack Seleccionado"].isin(tipos_pack)]
                else:
                    # Si no elige nada, no mostramos para evitar confusión
                    df = df.iloc[0:0]

                if df.empty:
                    st.info("No hay planes que coincidan con los filtros. "
                            "Prueba seleccionando más tipos de pack o activa diagnóstico.")
                else:
                    # Ordena por precio si está disponible
                    if "Precio_CLP" in df.columns:
                        df = df.sort_values(by="Precio_CLP", na_position="last")

                    # Vista final con las 5 columnas solicitadas
                    cols = ["Compañía", "Pack Seleccionado", "Velocidad", "Detalle Móvil", "Costo Total"]
                    mostrar = df[cols]
                    st.success("¡Ofertas encontradas!")
                    st.dataframe(mostrar, use_container_width=True)

            except Exception as e:
                st.error(f"Falla en la consulta: {e}")
                st.caption(
                    "Si aparece un mensaje pidiendo `playwright install`, presiona el botón otra vez. "
                    "La primera descarga de Chromium puede tardar un poco."
                )
