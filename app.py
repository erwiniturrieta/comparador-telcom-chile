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
    - Fuerza una ruta de cache dentro del home del usuario.
    """
    os.environ.setdefault(
        "PLAYWRIGHT_BROWSERS_PATH",
        os.path.expanduser("~/.cache/ms-playwright")
    )
    try:
        res = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium", "--retry", "3"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        # st.text(res.stdout)  # descomenta si quieres ver el log de instalación
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


# ============ Scraper Mundo (async) ============
async def buscar_en_mundo(quiere_internet: bool) -> List[Dict]:
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
            page = await browser.new_page(
                user_agent=(
                    "Mozilla/5.0 (Linux; Android 13; SM-G991B) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/121.0.0.0 Mobile Safari/537.36"
                )
            )

            await page.goto(
                "https://www.tumundo.cl/internet/",
                wait_until="domcontentloaded",
                timeout=30000
            )

            # Selectores con fallbacks (ajusta según DOM real si cambia)
            selectors = [
                ".card-plan",
                ".plan-card",
                "[class*='plan'] [class*='card']"
            ]

            cards = []
            for sel in selectors:
                try:
                    await page.wait_for_selector(sel, timeout=8000)
                    cards = await page.query_selector_all(sel)
                    if cards:
                        break
                except PlaywrightTimeout:
                    continue

            if not cards:
                return []

            planes = []
            for card in cards[:5]:  # límite para rapidez y consumo
                texto = (await card.inner_text()).strip()
                lineas = [l.strip() for l in texto.splitlines() if l.strip()]
                nombre_plan = lineas[0] if lineas else "Plan"
                precio_line = next((l for l in lineas[1:5] if "$" in l or "CLP" in l), "")
                precio_num = clp_to_int(precio_line)
                precio_fmt = format_clp(precio_num) if precio_num >= 0 else precio_line

                planes.append({
                    "Compañía": "Mundo",
                    "Tipo": "Fibra",
                    "Plan": nombre_plan,
                    "Precio": precio_fmt,
                    "Precio_CLP": precio_num
                })

            # Ordenar por precio válido
            planes.sort(key=lambda r: (r["Precio_CLP"] if r["Precio_CLP"] >= 0 else 9_999_999))
            return planes

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
        ensure_chromium_installed()

        # 2) Consultar Mundo
        with st.spinner("Consultando Mundo Pacífico..."):
            try:
                resultados_mundo = run_async(buscar_en_mundo(quiere_internet))
                df = pd.DataFrame(resultados_mundo)

                if df.empty:
                    st.info("No se encontraron planes (cambió el DOM o hubo protección anti-bot).")
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
