import re
import asyncio
import subprocess
import sys
from typing import List, Dict

import pandas as pd
import streamlit as st
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout


# ---------- Configuración de página ----------
st.set_page_config(page_title="Comparador Chile", page_icon="📡")
st.title("📡 Mi Comparador Telecom")


# ---------- Utilidades ----------
@st.cache_resource(show_spinner=False)
def ensure_chromium_installed():
    """
    Descarga Chromium y dependencias SOLO una vez por sesión de servidor.
    En algunas plataformas esto puede tardar ~100-200 MB la primera vez.
    """
    try:
        # "--with-deps" instala librerías del sistema cuando la plataforma lo permite.
        # Si tu plataforma no lo soporta, elimina "--with-deps".
        subprocess.run(
            [sys.executable, "-m", "playwright", "install", "--with-deps", "chromium"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        st.warning("No fue posible instalar Chromium automáticamente. "
                   "Si estás en Streamlit Cloud y falla, podemos mover el scraping a un microservicio.")
        st.text(e.stdout)


def clp_to_int(texto: str) -> int:
    """Convierte '$21.990/mes' -> 21990; devuelve -1 si no se puede."""
    if not texto:
        return -1
    nums = re.findall(r"\d+", texto.replace(".", ""))
    return int(nums[0]) if nums else -1


def format_clp(valor: int) -> str:
    return f"${valor:,.0f}".replace(",", ".")


# ---------- Scraper Mundo (async) ----------
async def buscar_en_mundo(quiere_internet: bool) -> List[Dict]:
    """
    Extrae los primeros planes de Internet de Mundo.
    Si no se solicita Internet, retorna lista vacía (puedes extender a TV/Combos).
    """
    if not quiere_internet:
        return []

    # Lanzamiento en headless, con flags útiles en contenedores
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

            await page.goto("https://www.tumundo.cl/internet/", wait_until="domcontentloaded", timeout=30000)

            # Espera por tarjetas de planes (selector principal + fallback)
            selectors = [
                ".card-plan",                 # hipotético
                ".plan-card",                 # fallback común
                "[class*='plan'] [class*='card']"  # fallback genérico
            ]

            cards = []
            last_error = None
            for sel in selectors:
                try:
                    await page.wait_for_selector(sel, timeout=8000)
                    cards = await page.query_selector_all(sel)
                    if cards:
                        break
                except PlaywrightTimeout as te:
                    last_error = te

            if not cards:
                raise RuntimeError(f"No se encontraron tarjetas de planes. Último error: {last_error}")

            planes = []
            for card in cards[:5]:  # limita para rapidez
                texto = (await card.inner_text()).strip()
                lineas = [l.strip() for l in texto.splitlines() if l.strip()]
                # heurística: primera línea nombre, siguiente(s) precio
                nombre_plan = lineas[0] if lineas else "Plan"
                precio_line = next((l for l in lineas[1:4] if "$" in l or "CLP" in l), "")
                precio_num = clp_to_int(precio_line)

                planes.append({
                    "Compañía": "Mundo",
                    "Plan": nombre_plan,
                    "Precio_CLP": precio_num,
                    "Precio": format_clp(precio_num) if precio_num >= 0 else precio_line,
                    "Tipo": "Fibra"
                })

            # ordena por precio válido
            planes.sort(key=lambda r: (r["Precio_CLP"] if r["Precio_CLP"] >= 0 else 9_999_999))
            return planes

        finally:
            await browser.close()


# ---------- Sidebar ----------
with st.sidebar:
    st.header("Configuración")
    rut = st.text_input("RUT (para factibilidad)")
    dir_completa = st.text_input("Dirección y Comuna")

# ---------- Preferencias ----------
st.subheader("¿Qué servicios necesitas?")
col1, col2 = st.columns(2)
with col1:
    quiere_internet = st.checkbox("🌐 Internet Fibra", value=True)
    quiere_movil = st.checkbox("📱 Telefonía Móvil")
with col2:
    quiere_tv = st.checkbox("📺 TV Cable")
    quiere_fija = st.checkbox("☎️ Telefonía Fija")

# ---------- Acción ----------
if st.button("Buscar Ofertas Reales 🚀", use_container_width=True):
    if not rut or not dir_completa:
        st.error("Por favor completa tu RUT y Dirección en la barra lateral.")
    else:
        ensure_chromium_installed()

        with st.spinner("Consultando Mundo Pacífico..."):
            try:
                # Ejecuta la corrutina (en Streamlit suele ser seguro usar asyncio.run)
                resultados_mundo = asyncio.run(buscar_en_mundo(quiere_internet))
                df = pd.DataFrame(resultados_mundo)

                if df.empty:
                    st.info("No se encontraron planes para los filtros seleccionados.")
                else:
                    # Muestra columnas ordenadas y formateadas
                    mostrar = df[["Compañía", "Tipo", "Plan", "Precio"]]
                    st.success("¡Ofertas encontradas!")
                    st.dataframe(mostrar, use_container_width=True)
            except Exception as e:
                st.error(f"Falla al consultar Mundo: {e}")
