# app.py
# Comparador Telecom Chile - Hogar/Móvil
# - RUT: autoformato + validación (módulo-11) al escribir
# - Dirección: validación/normalización automática con Nominatim (≤1 req/s)
# - Modo exclusivo: Hogar o Móvil
# - Scrapers: Mundo / Movistar / Entel / WOM (+ VTR fallback) para Hogar; Movistar/Entel/WOM para Móvil
# - Botón "Limpiar filtros" (restaura defaults y limpia cachés)
# - NUEVO: velocidad (Mbps/Gbps) también se extrae desde el CONTEXTO HTML (speed_hint)

import os
import re
import sys
import time
import json
import asyncio
import subprocess
from typing import List, Dict, Tuple, Optional

import pandas as pd
import streamlit as st
from playwright.async_api import async_playwright
import requests


# =================== Configuración de página ===================
st.set_page_config(page_title="Comparador Chile", page_icon="📡")
st.title("📡 Mi Comparador Telecom")


# =================== Utilidades base ===================
@st.cache_resource(show_spinner=False)
def ensure_chromium_installed():
    """
    Descarga Chromium SOLO una vez por sesión.
    Evita '--with-deps' (no hay sudo) para Streamlit Cloud.
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
    """Ejecuta corrutina de forma segura, incluso si ya existe event loop."""
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


# =================== Heurísticas / Regex ===================
PRICE_RE = re.compile(r"\$\s?\d{1,3}(?:\.\d{3})+", re.IGNORECASE)

def clp_to_int(texto: str) -> int:
    if not texto:
        return -1
    nums = re.findall(r"\d+", texto.replace(".", ""))
    return int(nums[0]) if nums else -1

def format_clp(valor: int) -> str:
    return f"${valor:,.0f}".replace(",", ".") if valor >= 0 else ""

def infer_speed(plan: str) -> str:
    """Devuelve '600 Mbps', '1 Gbps', etc., si detecta en el nombre del plan."""
    if not plan:
        return ""
    txt = plan.lower()

    m_gigas = re.search(r"(\d+)\s*giga?s?", txt)
    if m_gigas:
        val = int(m_gigas.group(1))
        return f"{val} Gbps" if val != 1 else "1 Gbps"

    m_gbps = re.search(r"\b(10000|5000|3000|2000|1000)\b", txt)
    if m_gbps:
        v = int(m_gbps.group(1))
        return f"{v//1000} Gbps" if v % 1000 == 0 else f"{v} Mbps"

    if re.search(r"\b940\b", txt):
        return "940 Mbps"

    m_mbps = re.search(r"\b(150|200|300|400|500|560|600|700|800|900)\b", txt)
    if m_mbps:
        return f"{m_mbps.group(1)} Mbps"

    if "gamer" in txt and "940" not in txt:
        return "940 Mbps"

    return ""

def infer_pack(plan: str) -> str:
    """Clasificación simple de pack a partir del texto del plan."""
    if not plan:
        return "Solo Fibra"
    t = plan.lower()
    if "portabilidad" in t or "móvil" in t or "movil" in t or "gigas" in t:
        return "Fibra + Móvil"
    if "tv" in t or "zapping" in t or "mundo go" in t or "go!" in t:
        return "Fibra + TV"
    if "telef" in t or "fija" in t:
        return "Fibra + Telefonía"
    if "dúo" in t or "duo" in t:
        return "Fibra + TV/Telefonía"
    return "Solo Fibra"

def infer_service_type(plan: str, force_mobile: bool = False) -> str:
    """
    Tipos:
      - Individuales: 'solo tv', 'solo internet', 'solo internet móvil', 'solo telefonía móvil', 'solo telefonía fija'
      - Packs: 'fibra + tv', 'fibra + telefonía', 'fibra + móvil', 'fibra + tv + telefonía', 'fibra + tv/telefonía'
    """
    if force_mobile:
        return "solo internet móvil"
    if not plan:
        return "solo internet"
    t = plan.lower()

    if ("móvil" in t or "movil" in t) and ("fibra" not in t and "internet" not in t and "tv" not in t):
        if "gigas" in t or "gb" in t:
            return "solo internet móvil"
        return "solo telefonía móvil"

    if ("telef" in t or "fija" in t) and ("fibra" not in t and "internet" not in t and "tv" not in t):
        return "solo telefonía fija"

    if ("tv" in t or "zapping" in t or "mundo go" in t) and ("fibra" not in t and "internet" not in t):
        return "solo tv"

    pack = infer_pack(plan)
    mapping = {
        "Solo Fibra": "solo internet",
        "Fibra + TV": "fibra + tv",
        "Fibra + Telefonía": "fibra + telefonía",
        "Fibra + Móvil": "fibra + móvil",
        "Fibra + TV/Telefonía": "fibra + tv/telefonía",
    }
    return mapping.get(pack, "solo internet")

def infer_mobile_detail(plan: str) -> str:
    """Devuelve 'Gigas Libres' o '<n> GB' si está presente en el nombre del plan."""
    if not plan:
        return ""
    t = plan.lower()
    if "gigas libres" in t or "gigaslibres" in t:
        return "Gigas Libres"
    m = re.search(r"(\d{2,5})\s*gb", t)
    if m:
        return f"{m.group(1)} GB"
    m2 = re.search(r"\b(1000)\b", t)
    if m2:
        return "1000 GB"
    return ""


# =================== EXTRACTOR con pista de velocidad (speed_hint) ===================
def extract_plans_via_regex(html: str, max_items: int = 24) -> List[Tuple[str, str, int, str]]:
    """
    Heurística: detecta precios CLP y, en una ventana cercana, intenta:
      - Nombre del plan (plan_name)
      - Pista de velocidad (speed_hint) incluso si no aparece en el nombre
    Retorna lista de (plan_name, price_str, price_int, speed_hint)
    """
    results: List[Tuple[str, str, int, str]] = []

    for m in PRICE_RE.finditer(html):
        start = max(m.start() - 260, 0)
        end = min(m.end() + 260, len(html))
        ctx = html[start:end]

        # Nombre cercano
        plan_match = (
            re.search(r"(PLAN\s*[0-9A-Z]+\s*[^\$<>{}|]{0,120})", ctx, re.IGNORECASE)
            or re.search(r"((?:Internet\s*)?Fibra\s*(?:Gamer|Giga|[0-9]{2,4})\s*(?:Megas?|Mb|Mbps|Gigas?)?)",
                         ctx, re.IGNORECASE)
            or re.search(r"((?:Fibra|Internet)\s*[0-9]{2,4}\s*(?:Mb|Mbps))", ctx, re.IGNORECASE)
            or re.search(r"(5G\s*Libre\s*(?:Full|Pro|Ultra)?\s*\d{0,4}\s*GB?)", ctx, re.IGNORECASE)
            or re.search(r"(Plan\s*(?:\d{2,4}|Gigas\s*Libres).{0,40})", ctx, re.IGNORECASE)
            or re.search(r"(TV\s*(?:Lite\+|Full\+|Online)?)", ctx, re.IGNORECASE)
            or re.search(r"(Telefon(?:ía|ia)\s*fija)", ctx, re.IGNORECASE)
        )

        plan_name = None
        if plan_match:
            plan_name = re.sub(r"\s+", " ", plan_match.group(1)).strip()
            plan_name = re.sub(r"(POR\s+\d+\s+MESES|SOLO\s+FIBRA|HASTA|OFERTA\s+WEB).*$",
                               "", plan_name, flags=re.IGNORECASE).strip(" -–:|.")

        # Pista de velocidad desde el contexto (aunque no esté en el nombre)
        speed_hint = ""
        # 1) 'X Giga / Gigas' -> Gbps
        m_giga = re.search(r"\b(\d{1,2})\s*giga?s?\b", ctx, re.IGNORECASE)
        if m_giga:
            val = int(m_giga.group(1))
            speed_hint = f"{val} Gbps" if val != 1 else "1 Gbps"
        else:
            # 2) 'X Mb/Mbps/Megas' (incluye 940, 1000, 2000, 3000, 5000, 10000)
            m_mbps = re.search(
                r"\b(150|200|300|400|500|560|600|700|800|900|940|1000|1500|2000|3000|5000|10000)\s*(?:mbps|mb|megas?)\b",
                ctx, re.IGNORECASE
            )
            if m_mbps:
                num = int(m_mbps.group(1))
                if num in (1000, 1500, 2000, 3000, 5000, 10000):
                    speed_hint = f"{num//1000} Gbps" if num % 1000 == 0 else f"{num} Mbps"
                else:
                    speed_hint = f"{num} Mbps"
            else:
                # 3) casos especiales “Gamer” → comúnmente 940
                if re.search(r"gamer", ctx, re.IGNORECASE):
                    speed_hint = "940 Mbps"

        price_str = m.group(0)
        price_int = clp_to_int(price_str)

        if plan_name and price_int > 0:
            results.append((plan_name, price_str, price_int, speed_hint))
        elif price_int > 0:
            # A falta de nombre, guarda con speed_hint si existe
            results.append(("", price_str, price_int, speed_hint))

        if len(results) >= max_items:
            break

    # Dedup por (nombre.lower(), precio_int, speed_hint) (para no perder velocidades)
    seen = set()
    dedup: List[Tuple[str, str, int, str]] = []
    for p in results:
        key = (p[0].lower(), p[2], p[3].lower() if p[3] else "")
        if key not in seen:
            seen.add(key)
            dedup.append(p)
    return dedup


# =================== RUT (autoformato + validación) ===================
def rut_sin_formato(rut: str) -> str:
    """Elimina puntos/guion y normaliza K mayúscula."""
    if not rut:
        return ""
    rut = rut.strip().replace(".", "").replace("-", "").replace(" ", "")
    return rut[:-1] + rut[-1].upper() if rut else ""

def calcular_dv(rut_cuerpo: str) -> str:
    """
    Algoritmo módulo 11:
      - Serie repetitiva 2..7 sobre dígitos (derecha->izquierda)
      - dv = 11 - (suma % 11); si dv=11 -> 0; si dv=10 -> K
    """
    if not rut_cuerpo.isdigit():
        return ""
    factores = [2, 3, 4, 5, 6, 7]
    suma = 0
    for i, d in enumerate(reversed(rut_cuerpo)):
        suma += int(d) * factores[i % 6]
    resto = suma % 11
    dv = 11 - resto
    if dv == 11:
        return "0"
    if dv == 10:
        return "K"
    return str(dv)

def formatear_rut(rut: str) -> str:
    """Devuelve RUT como 12.345.678-K."""
    rut = rut_sin_formato(rut)
    if len(rut) < 2:
        return rut
    cuerpo, dv = rut[:-1], rut[-1]
    cuerpo_fmt = ""
    while cuerpo:
        cuerpo_fmt = (("." + cuerpo[-3:]) if cuerpo_fmt else cuerpo[-3:]) + cuerpo_fmt
        cuerpo = cuerpo[:-3]
    return f"{cuerpo_fmt}-{dv}"

def validar_rut(rut: str) -> bool:
    """Valida formato y dígito verificador."""
    limpio = rut_sin_formato(rut)
    if len(limpio) < 2:
        return False
    cuerpo, dv = limpio[:-1], limpio[-1]
    dv_calc = calcular_dv(cuerpo)
    return dv_calc == dv


# =================== Geocodificación gratuita (Nominatim) ===================
NOMINATIM_SEARCH = "https://nominatim.openstreetmap.org/search"
NOMINATIM_REVERSE = "https://nominatim.openstreetmap.org/reverse"

def _nominatim_headers():
    return {"User-Agent": "MiComparadorTelecom/1.0 (Streamlit; contacto: soporte@ejemplo.cl)"}

def buscar_direccion_gratis(q: str, countrycodes: str = "cl", limit: int = 5) -> List[Dict]:
    """
    Forward geocoding con Nominatim (gratis).
    Respeta política: ≤1 req/seg y User-Agent propio.
    """
    if not q:
        return []
    params = {"q": q, "format": "jsonv2", "addressdetails": 1, "limit": limit, "countrycodes": countrycodes}
    time.sleep(1.05)  # 1 req/s
    r = requests.get(NOMINATIM_SEARCH, params=params, headers=_nominatim_headers(), timeout=20)
    if r.status_code != 200:
        return []
    return r.json()

def normalizar_direccion_por_latlon(lat: str, lon: str) -> Optional[Dict]:
    """Reverse geocoding para normalizar a 'display_name' canónico."""
    if not lat or not lon:
        return None
    params = {"lat": lat, "lon": lon, "format": "jsonv2", "addressdetails": 1, "zoom": 18, "accept-language": "es-CL"}
    time.sleep(1.05)  # 1 req/s
    r = requests.get(NOMINATIM_REVERSE, params=params, headers=_nominatim_headers(), timeout=20)
    if r.status_code != 200:
        return None
    return r.json()


# =================== Callbacks automáticos (RUT y Dirección) ===================
def on_rut_change_autofmt():
    """Formatea el RUT con puntos/guion y valida al cambiar el input."""
    raw = st.session_state.get("rut_raw", "")
    limpio = rut_sin_formato(raw)
    if not limpio:
        st.session_state["rut_formateado"] = ""
        st.session_state["rut_valido"] = False
        st.session_state["rut_status"] = "Ingrese su RUT"
        return

    # Si usuario ingresa solo dígitos (7-8), autocompleta DV
    if limpio.isdigit() and 7 <= len(limpio) <= 8:
        dv = calcular_dv(limpio)
        limpio = limpio + dv

    fmt = formatear_rut(limpio)
    st.session_state["rut_raw"] = fmt  # auto-escribe en el mismo campo
    es_ok = validar_rut(fmt)
    st.session_state["rut_formateado"] = fmt
    st.session_state["rut_valido"] = es_ok
    st.session_state["rut_status"] = "✅ RUT válido" if es_ok else "❌ RUT inválido"

def on_dir_change_autovalidate():
    """
    Valida/normaliza dirección automáticamente:
      - forward search (q) -> mejor match
      - reverse -> display_name canónico en el input
    """
    q = (st.session_state.get("dir_input") or "").strip()
    if len(q) < 5:
        st.session_state["dir_sugerencias"] = []
        st.session_state["dir_status"] = "Escriba una dirección más específica"
        return

    try:
        sug = buscar_direccion_gratis(q, countrycodes="cl", limit=3)  # 1.05s dentro
        st.session_state["dir_sugerencias"] = sug or []
        if not sug:
            st.session_state["dir_status"] = "❌ No se encontró la dirección"
            return

        best = sug[0]
        lat, lon = best.get("lat"), best.get("lon")
        if not (lat and lon):
            st.session_state["dir_status"] = "❌ No se pudo normalizar (coordenadas faltantes)"
            return

        rev = normalizar_direccion_por_latlon(lat, lon)  # 1.05s dentro
        if rev and "display_name" in rev:
            st.session_state["dir_input"] = rev["display_name"]  # auto-normaliza
            st.session_state["dir_status"] = "✅ Dirección validada y normalizada"
        else:
            st.session_state["dir_status"] = "❌ No se pudo normalizar (reverse)"
    except Exception:
        st.session_state["dir_status"] = "⚠️ Error validando con Nominatim"


# =================== Scraping helper (Playwright) ===================
async def _scrape_urls(urls: List[str], filters_regex: str) -> List[Tuple[str, str, int, str]]:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        try:
            ctx = await browser.new_context(
                viewport={"width": 390, "height": 844},
                user_agent=("Mozilla/5.0 (Linux; Android 13; SM-G991B) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/121.0.0.0 Mobile Safari/537.36"),
                locale="es-CL",
                extra_http_headers={"Accept-Language": "es-CL,es;q=0.9,en;q=0.8"},
            )
            page = await ctx.new_page()
            results: List[Tuple[str, str, int, str]] = []
            for u in urls:
                try:
                    await page.goto(u, wait_until="domcontentloaded", timeout=30000)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=12000)
                        await page.mouse.wheel(0, 1800)
                        await page.wait_for_timeout(600)
                    except:
                        pass
                    html = await page.content()
                    found = extract_plans_via_regex(html, max_items=24)
                    found = [f for f in found if re.search(filters_regex, f[0], re.IGNORECASE)]
                    results.extend(found)
                except Exception:
                    continue
            # Dedup (ya viene deduplicado desde extractor, pero por seguridad)
            seen, dedup = set(), []
            for plan in results:
                k = (plan[0].lower(), plan[2], plan[3].lower() if plan[3] else "")
                if k not in seen:
                    seen.add(k)
                    dedup.append(plan)
            return dedup
        finally:
            await browser.close()


# =================== Scrapers HOGAR ===================
async def hogar_mundo() -> List[Dict]:
    urls = ["https://mundointernet.cl/p/td/mundo-internet-planes.html", "https://www.tumundo.cl/"]
    found = await _scrape_urls(urls, r"fibra|internet|tv|telefon")
    out: List[Dict] = []
    for plan, price_str, price_int, speed_hint in found:
        tipo = infer_service_type(plan)
        if tipo not in {"solo tv", "solo internet", "solo telefonía fija",
                        "fibra + tv", "fibra + telefonía", "fibra + móvil",
                        "fibra + tv + telefonía", "fibra + tv/telefonía"}:
            continue
        velocidad = infer_speed(plan) or speed_hint
        out.append({
            "mundo": "✔", "movistar": "", "entel": "", "wom": "", "vtr": "",
            "pack seleccionado": infer_pack(plan) if tipo.startswith("fibra") else tipo,
            "velocidad": velocidad,
            "detalle movil": infer_mobile_detail(plan),
            "costo total": format_clp(price_int) or price_str,
            "Precio_CLP": price_int,
            "__prov": "mundo", "__plan": plan
        })
    return out

async def hogar_movistar() -> List[Dict]:
    urls = [
        "https://ww2.movistar.cl/hogar/internet-hogar/",
        "https://ww2.movistar.cl/hogar/internet-fibra-optica/",
        "https://ww2.movistar.cl/hogar/pack-duos-internet-television/",
        "https://www.movistar.cl/",
    ]
    found = await _scrape_urls(urls, r"fibra|internet|tv|telefon")
    out: List[Dict] = []
    for plan, price_str, price_int, speed_hint in found:
        tipo = infer_service_type(plan)
        if tipo not in {"solo tv", "solo internet", "solo telefonía fija",
                        "fibra + tv", "fibra + telefonía", "fibra + móvil",
                        "fibra + tv + telefonía", "fibra + tv/telefonía"}:
            continue
        velocidad = infer_speed(plan) or speed_hint
        out.append({
            "mundo": "", "movistar": "✔", "entel": "", "wom": "", "vtr": "",
            "pack seleccionado": infer_pack(plan) if tipo.startswith("fibra") else tipo,
            "velocidad": velocidad,
            "detalle movil": infer_mobile_detail(plan),
            "costo total": format_clp(price_int) or price_str,
            "Precio_CLP": price_int,
            "__prov": "movistar", "__plan": plan
        })
    return out

async def hogar_entel() -> List[Dict]:
    urls = [
        "https://www.entel.cl/hogar/internet",
        "https://www.entel.cl/hogar/fibra-optica",
        "https://www.entel.cl/hogar/doble-pack",
        "https://www.entel.cl/hogar",
    ]
    found = await _scrape_urls(urls, r"fibra|internet|tv|pack|telefon")
    out: List[Dict] = []
    for plan, price_str, price_int, speed_hint in found:
        tipo = infer_service_type(plan)
        if tipo not in {"solo tv", "solo internet", "solo telefonía fija",
                        "fibra + tv", "fibra + telefonía", "fibra + móvil",
                        "fibra + tv + telefonía", "fibra + tv/telefonía"}:
            continue
        velocidad = infer_speed(plan) or speed_hint
        out.append({
            "mundo": "", "movistar": "", "entel": "✔", "wom": "", "vtr": "",
            "pack seleccionado": infer_pack(plan) if tipo.startswith("fibra") else tipo,
            "velocidad": velocidad,
            "detalle movil": infer_mobile_detail(plan),
            "costo total": format_clp(price_int) or price_str,
            "Precio_CLP": price_int,
            "__prov": "entel", "__plan": plan
        })
    return out

async def hogar_wom() -> List[Dict]:
    urls = [
        "https://store.wom.cl/hogar/internet-hogar",
        "https://store.wom.cl/hogar/internet-fibra-optica",
        "https://store.wom.cl/hogar/internet-tv-hogar",
        "https://www.wom.cl/",
    ]
    found = await _scrape_urls(urls, r"fibra|internet|tv|zapping|telefon")
    out: List[Dict] = []
    for plan, price_str, price_int, speed_hint in found:
        tipo = infer_service_type(plan)
        if tipo not in {"solo tv", "solo internet", "solo telefonía fija",
                        "fibra + tv", "fibra + telefonía", "fibra + móvil",
                        "fibra + tv + telefonía", "fibra + tv/telefonía"}:
            continue
        velocidad = infer_speed(plan) or speed_hint
        out.append({
            "mundo": "", "movistar": "", "entel": "", "wom": "✔", "vtr": "",
            "pack seleccionado": infer_pack(plan) if tipo.startswith("fibra") else tipo,
            "velocidad": velocidad,
            "detalle movil": infer_mobile_detail(plan),
            "costo total": format_clp(price_int) or price_str,
            "Precio_CLP": price_int,
            "__prov": "wom", "__plan": plan
        })
    return out

async def hogar_vtr() -> List[Dict]:
    urls = [
        "https://vtr.com/",
        "https://vtr.com/comparador-planes/",
        "https://www.nuevo.vtr.com/comparador-planes",
        "https://vtr.com/productos/hogar-packs/internet-hogar/",
    ]
    found = await _scrape_urls(urls, r"fibra|internet|tv|telefon")
    out: List[Dict] = []
    for plan, price_str, price_int, speed_hint in found:
        tipo = infer_service_type(plan)
        if tipo not in {"solo tv", "solo internet", "solo telefonía fija",
                        "fibra + tv", "fibra + telefonía", "fibra + móvil",
                        "fibra + tv + telefonía", "fibra + tv/telefonía"}:
            continue
        velocidad = infer_speed(plan) or speed_hint
        out.append({
            "mundo": "", "movistar": "", "entel": "", "wom": "", "vtr": "✔",
            "pack seleccionado": infer_pack(plan) if tipo.startswith("fibra") else tipo,
            "velocidad": velocidad,
            "detalle movil": infer_mobile_detail(plan),
            "costo total": format_clp(price_int) or price_str,
            "Precio_CLP": price_int,
            "__prov": "vtr", "__plan": plan
        })
    return out


# =================== Scrapers MÓVIL ===================
async def movistar_movil() -> List[Dict]:
    urls = [
        "https://ww2.movistar.cl/movil/",
        "https://ww2.movistar.cl/ofertas/ofertador-movil/",
        "https://ww2.movistar.cl/movil/deglose-planes-moviles/",
    ]
    found = await _scrape_urls(urls, r"5g|plan|gb|gigas|móvil|movil")
    out: List[Dict] = []
    for plan, price_str, price_int, _speed_hint in found:
        out.append({
            "mundo": "", "movistar": "✔", "entel": "", "wom": "", "vtr": "",
            "pack seleccionado": "solo internet móvil",
            "velocidad": "",
            "detalle movil": infer_mobile_detail(plan) or "Gigas Libres/GB",
            "costo total": format_clp(price_int) or price_str,
            "Precio_CLP": price_int,
            "__prov": "movistar", "__plan": plan
        })
    return out

async def entel_movil() -> List[Dict]:
    urls = [
        "https://www.entel.cl/soycliente/movil",
        "https://www.entel.cl/planes/telefonia-movil",
        "https://www.entel.cl/planes/internet-movil",
        "https://www.entel.cl/planes/detalle/plan-libre",
    ]
    found = await _scrape_urls(urls, r"plan|5g|gigas|gb|internet móvil|movil")
    out: List[Dict] = []
    for plan, price_str, price_int, _speed_hint in found:
        out.append({
            "mundo": "", "movistar": "", "entel": "✔", "wom": "", "vtr": "",
            "pack seleccionado": "solo internet móvil",
            "velocidad": "",
            "detalle movil": infer_mobile_detail(plan) or "Gigas Libres/GB",
            "costo total": format_clp(price_int) or price_str,
            "Precio_CLP": price_int,
            "__prov": "entel", "__plan": plan
        })
    return out

async def wom_movil() -> List[Dict]:
    urls = [
        "https://store.wom.cl/planes/",
        "https://store.wom.cl/planes/planes-portabilidad",
        "https://store.wom.cl/planes/planes-linea-nueva/grupales",
    ]
    found = await _scrape_urls(urls, r"plan|gigas|gb|5g")
    out: List[Dict] = []
    for plan, price_str, price_int, _speed_hint in found:
        out.append({
            "mundo": "", "movistar": "", "entel": "", "wom": "✔", "vtr": "",
            "pack seleccionado": "solo internet móvil",
            "velocidad": "",
            "detalle movil": infer_mobile_detail(plan) or "Gigas Libres/GB",
            "costo total": format_clp(price_int) or price_str,
            "Precio_CLP": price_int,
            "__prov": "wom", "__plan": plan
        })
    return out


# =================== Limpieza de filtros ===================
def _limpiar_filtros():
    # Proveedores (defaults)
    st.session_state["incluir_mundo"] = True
    st.session_state["incluir_movistar"] = True
    st.session_state["incluir_entel"] = True
    st.session_state["incluir_wom"] = True
    st.session_state["incluir_vtr"] = False
    # Servicios/packs (defaults)
    st.session_state["servicios_sel_hogar"] = ["solo internet", "fibra + tv", "fibra + telefonía", "fibra + móvil"]
    st.session_state["servicios_sel_movil"] = ["solo internet móvil"]
    # Cachés
    st.session_state["fibra_por_proveedor"] = {}
    st.toast("Filtros restablecidos")


# =================== Sidebar: RUT + Dirección + Proveedores + Modo ===================
with st.sidebar:
    st.header("Datos del cliente")

    # --- RUT autoformato/validación ---
    st.text_input(
        "RUT (autoformato y validación)",
        key="rut_raw",
        placeholder="12.345.678-K",
        on_change=on_rut_change_autofmt
    )
    st.caption(st.session_state.get("rut_status", "Ingrese su RUT"))

    # --- Dirección autovalidación/normalización ---
    st.text_input(
        "Dirección (auto-validación y normalización)",
        key="dir_input",
        placeholder="calle y número, comuna",
        on_change=on_dir_change_autovalidate
    )
    st.caption(st.session_state.get("dir_status", "Escribe una dirección y presiona Enter"))

    # Diagnóstico breve (top-3 coincidencias)
    sug = st.session_state.get("dir_sugerencias", []) or []
    if sug:
        st.write("Coincidencias (top 3):")
        for i, s in enumerate(sug[:3], start=1):
            st.write(f"{i}. {s.get('display_name','')}")

    st.divider()

    # Proveedores
    st.subheader("Compañías a comparar")
    st.checkbox("Mundo", value=True, key="incluir_mundo")
    st.checkbox("Movistar", value=True, key="incluir_movistar")
    st.checkbox("Entel", value=True, key="incluir_entel")
    st.checkbox("WOM", value=True, key="incluir_wom")
    st.checkbox("VTR", value=False, key="incluir_vtr")

    st.button("🧽 Limpiar filtros", on_click=_limpiar_filtros)

    st.divider()
    st.radio("¿Qué quieres comparar?", ["Hogar", "Móvil"], index=0, horizontal=True, key="modo_busqueda")


# =================== Filtros según MODO ===================
if st.session_state.get("modo_busqueda") == "Hogar":
    st.subheader("Tipos de servicio/pack (Hogar)")
    st.multiselect(
        "Elige uno o varios",
        options=[
            "solo tv", "solo internet", "solo telefonía fija",
            "fibra + tv", "fibra + telefonía", "fibra + móvil",
            "fibra + tv + telefonía", "fibra + tv/telefonía",
        ],
        default=st.session_state.get(
            "servicios_sel_hogar",
            ["solo internet", "fibra + tv", "fibra + telefonía", "fibra + móvil"]
        ),
        key="servicios_sel_hogar"
    )
else:
    st.subheader("Tipos de servicio (Móvil)")
    st.multiselect(
        "Elige uno o varios",
        options=["solo internet móvil", "solo telefonía móvil", "fibra + móvil"],
        default=st.session_state.get("servicios_sel_movil", ["solo internet móvil"]),
        key="servicios_sel_movil"
    )


# =================== Acciones por MODO ===================
cols_final = ["mundo", "movistar", "entel", "wom", "vtr",
              "pack seleccionado", "velocidad", "detalle movil", "costo total"]

# -------- HOGAR --------
if st.session_state.get("modo_busqueda") == "Hogar":
    if st.button("🔍 Buscar Hogar"):
        try:
            ensure_chromium_installed()
        except Exception:
            st.stop()

        resultados: List[Dict] = []

        with st.spinner("Consultando proveedores (Hogar)…"):
            try:
                if st.session_state.get("incluir_mundo"):    resultados.extend(run_async(hogar_mundo()))
                if st.session_state.get("incluir_movistar"): resultados.extend(run_async(hogar_movistar()))
                if st.session_state.get("incluir_entel"):    resultados.extend(run_async(hogar_entel()))
                if st.session_state.get("incluir_wom"):      resultados.extend(run_async(hogar_wom()))
                if st.session_state.get("incluir_vtr"):      resultados.extend(run_async(hogar_vtr()))

                df = pd.DataFrame(resultados)

                # Guardar mejor fibra por proveedor (para combos Fibra+Móvil)
                fibra_map = {}
                if not df.empty:
                    tmp = df.copy()
                    tmp["__fibra"] = tmp["pack seleccionado"].str.contains("Fibra", case=False) | \
                                     (tmp["pack seleccionado"].str.lower() == "solo internet")
                    tmp = tmp[tmp["__fibra"] == True]
                    for prov in ["mundo", "movistar", "entel", "wom", "vtr"]:
                        sub = tmp[tmp[prov] == "✔"].sort_values(by="Precio_CLP", na_position="last")
                        if not sub.empty:
                            first = sub.iloc[0]
                            fibra_map[prov] = {
                                "precio": int(first.get("Precio_CLP", -1)),
                                "velocidad": first.get("velocidad", ""),
                                "plan": first.get("__plan", "")
                            }
                st.session_state["fibra_por_proveedor"] = fibra_map

                # Filtrar por selección de Hogar
                seleccion = [s.lower() for s in st.session_state.get("servicios_sel_hogar", [])]
                if not df.empty:
                    df["__tipo"] = df["pack seleccionado"].str.lower().fillna("")
                    df.loc[df["__tipo"] == "solo fibra", "__tipo"] = "solo internet"
                    df = df[df["__tipo"].isin(seleccion)]
                    df = df.drop(columns=["__tipo", "__prov", "__plan"], errors="ignore")

                if df.empty:
                    st.info("No se encontraron planes que coincidan con los filtros (Hogar).")
                else:
                    if "Precio_CLP" in df.columns:
                        df = df.sort_values(by="Precio_CLP", na_position="last")
                    for c in cols_final:
                        if c not in df.columns:
                            df[c] = ""
                    st.success("¡Ofertas Hogar encontradas!")
                    st.dataframe(df[cols_final], use_container_width=True)

            except Exception as e:
                st.error(f"Falla en la consulta Hogar: {e}")
                st.caption("Si aparece un mensaje pidiendo `playwright install`, presiona el botón otra vez.")

# -------- MÓVIL --------
else:
    if st.button("📶 Consultar Móvil"):
        try:
            ensure_chromium_installed()
        except Exception:
            st.stop()

        resultados_movil: List[Dict] = []

        with st.spinner("Consultando planes móviles…"):
            try:
                if st.session_state.get("incluir_movistar"): resultados_movil.extend(run_async(movistar_movil()))
                if st.session_state.get("incluir_entel"):    resultados_movil.extend(run_async(entel_movil()))
                if st.session_state.get("incluir_wom"):      resultados_movil.extend(run_async(wom_movil()))
                # (Móvil de Mundo/VTR no se expone como landing clara en esta versión)

                dfm = pd.DataFrame(resultados_movil)

                # Filtrar por selección móvil
                seleccion = [s.lower() for s in st.session_state.get("servicios_sel_movil", [])]
                if not dfm.empty:
                    dfm["__tipo"] = dfm["pack seleccionado"].str.lower().fillna("")
                    dfm = dfm[dfm["__tipo"].isin(seleccion)]
                    dfm = dfm.drop(columns=["__tipo"], errors="ignore")

                # Combos Fibra + Móvil si está seleccionado y tenemos fibra previa
                if "fibra + móvil" in seleccion:
                    fibra_map = st.session_state.get("fibra_por_proveedor", {})
                    combos: List[Dict] = []
                    if fibra_map and not dfm.empty:
                        for prov in ["movistar", "entel", "wom"]:
                            mov_sub = dfm[dfm[prov] == "✔"].sort_values(by="Precio_CLP", na_position="last")
                            if not mov_sub.empty and prov in fibra_map and fibra_map[prov]["precio"] >= 0:
                                movil_row = mov_sub.iloc[0]
                                total = int(movil_row.get("Precio_CLP", 0)) + int(fibra_map[prov]["precio"])
                                combos.append({
                                    "mundo": "✔" if prov == "mundo" else "",
                                    "movistar": "✔" if prov == "movistar" else "",
                                    "entel": "✔" if prov == "entel" else "",
                                    "wom": "✔" if prov == "wom" else "",
                                    "vtr": "✔" if prov == "vtr" else "",
                                    "pack seleccionado": "Fibra + Móvil",
                                    "velocidad": fibra_map[prov].get("velocidad", ""),
                                    "detalle movil": movil_row.get("detalle movil", "") or "Gigas Libres/GB",
                                    "costo total": format_clp(total),
                                    "Precio_CLP": total
                                })
                    if combos:
                        dfm = pd.concat([dfm, pd.DataFrame(combos)], ignore_index=True)

                if dfm.empty:
                    st.info("No se encontraron planes móviles (o selecciona 'Fibra + Móvil' tras ejecutar primero Hogar).")
                else:
                    if "Precio_CLP" in dfm.columns:
                        dfm = dfm.sort_values(by="Precio_CLP", na_position="last")
                    for c in cols_final:
                        if c not in dfm.columns:
                            dfm[c] = ""
                    st.success("¡Planes móviles listos!")
                    st.dataframe(dfm[cols_final], use_container_width=True)

            except Exception as e:
                st.error(f"Falla en la consulta Móvil: {e}")
                st.caption("Si aparece un mensaje pidiendo `playwright install`, presiona el botón otra vez.")
