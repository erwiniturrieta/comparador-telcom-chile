import os
import re
import sys
import asyncio
import subprocess
from typing import List, Dict

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
        # st.code(completed.stdout, language="bash")  # descomenta para ver log
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
    return f"${valor:,.0f}".replace(",", ".")


def dump_preview(label: str, text: str, max_chars: int = 5000):
    """Muestra un recorte de HTML/Texto para diagnóstico."""
    if not text:
        st.caption(f"{label}: (vacío)")
        return
    preview = text[:max_chars]
    st.expander(f"🔎 {label} (primeros {len(preview)} chars)").code(preview, language="html")


# ============ Scraper Mundo (async) con diagnóstico ============
async def buscar_en_mundo(quiere_internet: bool = True, modo_debug: bool = True) -> List[Dict]:
    """
    Extrae los primeros planes de Internet de Mundo.
    Si no se solicita Internet, retorna lista vacía.
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
                viewport={"width": 390, "height": 844},  # móvil
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

            resp = await page.goto(
                "https://www.tumundo.cl/internet/",
                wait_until="domcontentloaded",
                timeout=30000
            )
            status = resp.status if resp else None
            if modo_debug:
                st.caption(f"🌐 Estado HTTP: {status}")

            # Respirito + idle (evita capturar wall/carga parcial)
            try:
                await page.wait_for_timeout(1000)
                await page.wait_for_load_state("networkidle", timeout=15000)
            except:
                pass

            # Opcional: scroll para disparar lazy-load
            try:
                await page.mouse.wheel(0, 2000)
                await page.wait_for_timeout(800)
            except:
                pass

            # Selectores candidatos (múltiples variantes)
            candidate_selectors = [
                ".card-plan",
                ".plan-card",
                ".card--plan",
                "[class*='plan'] [class*='card']",
                "[class*='Plan']",
                "section:has-text('Internet') .card, .cards .card",
            ]

            cards = []
            for sel in candidate_selectors:
                try:
                    loc = page.locator(sel)
                    count = await loc.count()
                    if modo_debug:
                        st.caption(f"🔎 Selector '{sel}' → {count} nodos")
                    if count > 0:
                        for i in range(min(count, 6)):
                            cards.append(loc.nth(i))
                        break
                except Exception:
                    continue

            if not cards:
                if modo_debug:
                    try:
                        png = await page.screenshot(full_page=True)
                        st.image(png, caption="📸 Screenshot (Mundo)")
                    except Exception as e:
                        st.caption(f"No se pudo tomar screenshot: {e}")

                    try:
                        html = await page.content()
                        dump_preview("HTML de la página", html, max_chars=6000)
                    except Exception as e:
                        st.caption(f"No se pudo obtener HTML: {e}")

                return []

            planes = []
            for card in cards:
                try:
                    # Título
                    posibles_titulos = [
                        ".title", ".card-title", "h3", "h2", "[class*='title']",
                        ".plan-title", ".nombre", ".name"
                    ]
                    nombre_plan = None
                    for tsel in posibles_titulos:
                        if await card.locator(tsel).count() > 0:
                            nombre_plan = (await card.locator(tsel).first.inner_text()).strip()
                            break
                    if not nombre_plan:
                        nombre_plan = (await card.inner_text()).splitlines()[0].strip()

                    # Precio
                    posibles_precios = [
                        "[class*='precio']", "[class*='price']", ".price", ".precio",
                        "strong:has-text('$')", "span:has-text('$')"
                    ]
                    precio_line = ""
                    for psel in posibles_precios:
                        if await card.locator(psel).count() > 0:
                            txt = (await card.locator(psel).first.inner_text()).strip()
                            if "$" in txt or "CLP" in txt:
                                precio_line = txt
                                break
                    if not precio_line:
                        raw = await card.inner_text()
                        for l in [l.strip() for l in raw.splitlines() if l.strip()]:
                            if "$" in l:
                                precio_line = l
                                break

                    precio_num = clp_to_int(precio_line)
                    precio_fmt = format_clp(precio_num) if precio_num >= 0 else precio_line

                    planes.append({
                        "Compañía": "Mundo",
                        "Tipo": "Fibra",
                        "Plan": nombre_plan,
                        "Precio": precio_fmt,
                        "Precio_CLP": precio_num
                    })
                except Exception:
                    continue

            planes = [p for p in planes if p.get("Precio")]
            if any(p["Precio_CLP"] >= 0 for p in planes):
                planes.sort(key=lambda r: (r["Precio_CLP"] if r["Precio_CLP"] >= 0 else 9_999_999))

            if not planes and modo_debug:
                try:
                    png = await page.screenshot(full_page=True)
                    st.image(png, caption="📸 Screenshot (sin planes)")
                    html = await page.content()
                    dump_preview("HTML de la página (sin planes)", html)
                except Exception:
                    pass

            return planes

        finally:
            await browser.close()


# ============ Sidebar ============
with st.sidebar:
    st.header("Configuración")
    rut = st.text_input("RUT (para factibilidad)")
    dir_completa = st.text_input("Dirección y Comuna")


# ============ Preferencias (AQUÍ se define quiere_internet) ============
st.subheader("¿Qué servicios necesitas?")
col1, col2 = st.columns(2)
with col1:
    quiere_internet = st.checkbox("🌐 Internet Fibra", value=True)
    quiere_movil = st.checkbox("📱 Telefonía Móvil")
with col2:
    quiere_tv = st.checkbox("📺 TV Cable")
    quiere_fija = st.checkbox("☎️ Telefonía Fija")


# ============ Acción (botón) ============
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
                    st.info("No se encontraron planes (cambió el DOM o hubo protección anti-bot). Revisa los diagnósticos arriba 👆.")
                else:
                    mostrar = df[["Compañía", "Tipo", "Plan", "Precio"]]
                    st.success("¡Ofertas encontradas!")
                    st.dataframe(mostrar, use_container_width=True)

            except Exception as e:
                st.error(f"Falla al consultar Mundo: {e}")
