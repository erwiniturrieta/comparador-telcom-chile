import os
import re
import sys
import asyncio
import subprocess
from typing import List, Dict, Tuple

import pandas as pd
import streamlit as st
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout


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
        completed = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        # Si necesitas ver el log de instalación, descomenta:
        # st.code(completed.stdout, language="bash")
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


def clp_to_int(texto: str) -> int:
    """Convierte '$21.990/mes' -> 21990; devuelve -1 si no se puede."""
    if not texto:
        return -1
    nums = re.findall(r"\d+", texto.replace(".", ""))
    return int(nums[0]) if nums else -1


def format_clp(valor: int) -> str:
    return f"${valor:,.0f}".replace(",", ".") if valor >= 0 else ""


def dump_preview(label: str, text: str, max_chars: int = 5000):
    """Muestra un recorte de HTML/Texto para diagnóstico."""
    if not text:
        st.caption(f"{label}: (vacío)")
        return
    preview = text[:max_chars]
    st.expander(f"🔎 {label} (primeros {len(preview)} chars)").code(preview, language="html")


# ============ Heurísticas para extraer planes desde HTML (robusto a cambios de CSS) ============
PRICE_RE = re.compile(r"\$\s?\d{1,3}(?:\.\d{3})+", re.IGNORECASE)

def extract_plans_via_regex(html: str, max_items: int = 8) -> List[Tuple[str, str, int]]:
    """
    Heurística: busca precios CLP y en una ventana cercana detecta el nombre del plan
    (líneas tipo 'PLAN 1 ...', 'Fibra 800', etc.).
    Devuelve lista de (plan, precio_str, precio_int) deduplicada y acotada.
    """
    results: List[Tuple[str, str, int]] = []

    for m in PRICE_RE.finditer(html):
        start = max(m.start() - 160, 0)
        end = min(m.end() + 160, len(html))
        ctx = html[start:end]

        # 'PLAN X ...' cerca del precio
        plan_match = re.search(r"(PLAN\s*[0-9A-Z]+\s*[^\$<>{}|]{0,80})", ctx, re.IGNORECASE)
        if not plan_match:
            # Alternativa: “Fibra 800”, “Fibra 1G”, “Fibra 10 GIGAS”, etc.
            plan_match = re.search(r"(Fibra\s*[0-9]+(?:\s*GIGAS?|G|GB|MB)?[^\$<>{}|]{0,40})",
                                   ctx, re.IGNORECASE)

        plan_name = None
        if plan_match:
            plan_name = re.sub(r"\s+", " ", plan_match.group(1)).strip()
            # Limpia ruido común
            plan_name = re.sub(r"(POR\s+\d+\s+MESES|SOLO\s+FIBRA|HASTA)\b.*",
                               "", plan_name, flags=re.IGNORECASE).strip(" -–:|")

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


# ============ Scraper Mundo (async) con fallback de URLs ============
async def buscar_en_mundo(quiere_internet: bool = True, modo_debug: bool = True) -> List[Dict]:
    """
    Intenta obtener planes de Mundo desde:
    1) https://mundointernet.cl/p/td/mundo-internet-planes.html  (landing con planes)
    2) https://www.tumundo.cl/  (home con ofertas)
    Nota: /internet/ hoy retorna 404 y no se usa. (Se confirmó de forma pública)  # Referencia en explicación
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
                viewport={"width": 390, "height": 844},  # viewport móvil
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

            # ---------- Intento 1: MundoInternet (planes)
            url1 = "https://mundointernet.cl/p/td/mundo-internet-planes.html"
            resp1 = await page.goto(url1, wait_until="domcontentloaded", timeout=30000)
            status1 = resp1.status if resp1 else None
            if modo_debug:
                st.caption(f"🌐 MundoInternet status: {status1}")

            html1 = await page.content()
            plans = extract_plans_via_regex(html1, max_items=8)

            # ---------- Fallback: Home TuMundo (ofertas) si el anterior no dio resultados
            if not plans:
                url2 = "https://www.tumundo.cl/"
                resp2 = await page.goto(url2, wait_until="domcontentloaded", timeout=30000)
                status2 = resp2.status if resp2 else None
                if modo_debug:
                    st.caption(f"🌐 TuMundo Home status: {status2}")
                try:
                    await page.wait_for_load_state("networkidle", timeout=15000)
                    await page.mouse.wheel(0, 1800)
                    await page.wait_for_timeout(800)
                except:
                    pass
                html2 = await page.content()
                plans = extract_plans_via_regex(html2, max_items=8)

            if modo_debug and not plans:
                try:
                    png = await page.screenshot(full_page=True)
                    st.image(png, caption="📸 Screenshot (Mundo)")
                except Exception as e:
                    st.caption(f"No se pudo tomar screenshot: {e}")

                try:
                    snippet = (await page.content())[:6000]
                    dump_preview("HTML capturado (primeros 6000 chars)", snippet, max_chars=6000)
                except:
                    pass

            # Estructura de salida
            out: List[Dict] = []
            for nombre_plan, precio_str, precio_int in plans:
                out.append({
                    "Compañía": "Mundo",
                    "Tipo": "Fibra",
                    "Plan": nombre_plan,
                    "Precio": format_clp(precio_int) or precio_str,
                    "Precio_CLP": precio_int
                })

            # Orden por precio (si hay numérico)
            out.sort(key=lambda r: (r["Precio_CLP"] if r["Precio_CLP"] > 0 else 99_999_999))
            return out

        finally:
            await browser.close()


# ============ Sidebar ============
with st.sidebar:
    st.header("Configuración")
    rut = st.text_input("RUT (para factibilidad)")
    dir_completa = st.text_input("Dirección y Comuna")


# ============ Preferencias ============
st.subheader("¿Qué servicios necesitas?")
col1, col2 = st.columns(2)
with col1:
    quiere_internet = st.checkbox("🌐 Internet Fibra", value=True)
    quiere_movil = st.checkbox("📱 Telefonía Móvil")
with col2:
    quiere_tv = st.checkbox("📺 TV Cable")
    quiere_fija = st.checkbox("☎️ Telefonía Fija")


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

        # 2) Consultar Mundo con modo debug activo
        with st.spinner("Consultando Mundo Pacífico..."):
            try:
                resultados_mundo = run_async(
                    buscar_en_mundo(quiere_internet=quiere_internet, modo_debug=True)
                )
                df = pd.DataFrame(resultados_mundo)

                if df.empty:
                    st.info("No se encontraron planes (cambió el DOM o hubo protección anti-bot). "
                            "Revisa los diagnósticos arriba 👆.")
                else:
                    mostrar = df[["Compañía", "Tipo", "Plan", "Precio"]]
                    st.success("¡Ofertas encontradas!")
                    st.dataframe(mostrar, use_container_width=True)

            except Exception as e:
                st.error(f"Falla al consultar Mundo: {e}")
                st.caption(
                    "Si aparece un mensaje pidiendo `playwright install`, presiona el botón otra vez. "
                    "La primera descarga de Chromium puede tardar un poco."
                )
