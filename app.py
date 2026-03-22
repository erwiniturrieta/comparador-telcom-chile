# app.py
# Comparador Telecom Chile - Hogar/Móvil
# - RUT: autoformato + validación (módulo-11), sin "punto fantasma" y respeta ceros iniciales
# - Dirección: forward search → reverse (Nominatim) con pausa ≤1 req/s
# - Scraping Hogar: listas blancas por proveedor + filtro negativo anti-Empresas/Convenios
# - Extracción robusta:
#     * Velocidad (Mbps/Gbps) desde nombre o CONTEXTO (±1200)
#     * Precio oferta (mensual), Periodo de oferta, Precio normal/luego
#     * Instalación (monto) y "sin costo" cuando aplique
# - DEDUP + Insignia: “🏷️ Oferta más barata” por (compañía, velocidad, pack)
# - Toggle "Modo desarrollador": oculta/mostrar toolbar, "Manage app", etc.

import os
import re
import sys
import time
import json
import asyncio
import subprocess
import unicodedata
from typing import List, Dict, Tuple, Optional

import pandas as pd
import streamlit as st
from playwright.async_api import async_playwright
import requests

# =================== Configuración de página ===================
import streamlit as st
 
# 1. Configuración inicial
st.set_page_config(page_title="Comparador Chile", page_icon="📡")
 
# 2. CÓDIGO PARA OCULTAR MENÚS (GitHub, Share, Deploy, etc.)
hide_st_style = """
            <style>
            #MainMenu {visibility: hidden;}
            header {visibility: hidden;}
            footer {visibility: hidden;}
            .stAppDeployButton {display:none;}
            #stDecoration {display:none;}
            </style>
            """
st.markdown(hide_st_style, unsafe_allow_html=True)
st.title("📡 Mi Comparador Teleco")

# =================== Utilidades base ===================
@st.cache_resource(show_spinner=False)
def ensure_chromium_installed():
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", os.path.expanduser("~/.cache/ms-playwright"))
    try:
        subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
    except subprocess.CalledProcessError as e:
        st.error("No fue posible descargar Chromium automáticamente.")
        st.code(e.stdout or "", language="bash")
        raise

def run_async(coro):
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

# ============== Toggle Dev: ocultar/mostrar toolbar & co. ============
def apply_chrome_visibility(dev_mode: bool):
    """
    Cuando dev_mode=False (prod), oculta:
      - Toolbar, MainMenu, header y footer
      - Controles flotantes de Streamlit Cloud (incl. 'Manage app' y su chevron)
    Cuando dev_mode=True (dev), no oculta nada.
    """
    if dev_mode:
        return

    st.markdown(
        """
        <style>
        /* ====== Ocultar barra superior / menús / footer ====== */
        div[data-testid="stToolbar"] { display: none !important; }
        #MainMenu { visibility: hidden !important; }
        header { visibility: hidden !important; }
        footer { visibility: hidden !important; }
        button[kind="header"] { display: none !important; }

        /* ====== Ocultar 'Manage app' (varias variantes) ====== */
        div[data-testid="stStatusWidget"] { display: none !important; }
        div[data-testid="StyledDeploymentStatus"] { display: none !important; }
        a[data-testid="manage-app-button"],
        button[data-testid="manage-app-button"] { display: none !important; }
        a[title="Manage app"],
        button[title="Manage app"],
        [aria-label="Manage app"] { display: none !important; }
        a[href*="manage"], a[href*="streamlit.io/"] { display: none !important; }

        /* Chevron/back del flotante (cuando aparece) */
        div[aria-label="Main menu"] { display: none !important; }
        [data-testid="stActionButtonIcon"] { display: none !important; }

        /* Fallback defensivo */
        div:has(> a[title="Manage app"]),
        div:has(> button[title="Manage app"]) {
          display: none !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

# =================== Parsers / Regex comunes ===================
PRICE_RE = re.compile(r"\$\s?\d{1,3}(?:\.\d{3})+", re.IGNORECASE)
NEGATIVE_RE = re.compile(r"empresa|empresas|corporativ|pyme|emprendedor|convenio", re.IGNORECASE)

# ---- Velocidad ----
# Solo permitir 0–10 para Gbps (evita "600 Gbps")
GIGA_RE = re.compile(r"\b(0?\d(?:[.,]\d+)?)\s*(?:g(?:ig?a)?|gb(?:ps)?)\b", re.IGNORECASE)
MBPS_RE = re.compile(r"\b(\d{2,5})\s*(?:m(?:b(?:ps)?|b\/s)?|mega?s?)\b", re.IGNORECASE)
HASTA_RE = re.compile(r"\bhasta\b\s+(\d+(?:[.,]\d+)?)\s*(?:mbps|mb\/s|mb|mega?s?|g(?:ig?a)?|gb(?:ps)?)", re.IGNORECASE)

# ---- Contexto de precios ----
MIN_MONTHLY_CLP = 7000  # umbral anti-ruido (descarta extensor $2.990, add-ons $1.990, etc.)
MONTHLY_NEAR_RE = re.compile(
    r"(al\s*mes|/mes|\bmes\b|mensual|por\s*\d+\s*mes(?:es)?|por\s*1\s*a[nñ]o|x\s*\d+\s*mes(?:es)?)",
    re.IGNORECASE
)
LUEGO_RE = re.compile(r"(luego|despu[eé]s|desde\s+el\s+mes\s*\d+|precio\s*normal)", re.IGNORECASE)
INSTALL_OR_EXTRA_RE = re.compile(
    r"(instalaci[oó]n|despacho|costo|arriendo|arr[ií]endo|router|extensor|repetidor|smart\s*wifi|"
    r"mcafee|m[uú]sica|cloud|prime\s*video|disney|streaming)",
    re.IGNORECASE
)
FREE_RE = re.compile(r"(sin\s*costo|gratis)", re.IGNORECASE)
PERIODO_RE = re.compile(
    r"(?:por|x)\s*(\d+)\s*mes(?:es)?|por\s*1\s*a[nñ]o|por\s*un\s*a[nñ]o",
    re.IGNORECASE
)

def normalize_gbps(num_str: str) -> str:
    val = num_str.replace(",", ".")
    try:
        f = float(val)
    except:
        return ""
    if f <= 0:
        return ""
    return f"{int(f) if f.is_integer() else f} Gbps"

def normalize_mbps(num_str: str) -> str:
    try:
        n = int(num_str)
    except:
        return ""
    return f"{n} Mbps" if n > 0 else ""

def extract_speed_from_text(text: str) -> str:
    if not text:
        return ""
    t = text.lower()
    m_h = HASTA_RE.search(t)
    if m_h:
        num = m_h.group(1)
        if re.search(r"g(?:ig?a)?|gb(?:ps)?", m_h.group(0), re.IGNORECASE):
            sp = normalize_gbps(num)
            if sp:
                return sp
        sp = normalize_mbps(num)
        if sp:
            return sp
    m_g = GIGA_RE.search(t)
    if m_g:
        sp = normalize_gbps(m_g.group(1))
        if sp:
            return sp
    m_m = MBPS_RE.search(t)
    if m_m:
        val = int(m_m.group(1))
        if val in (1000, 1500, 2000, 3000, 5000, 10000):
            g = val / 1000.0
            return f"{int(g) if g.is_integer() else g} Gbps"
        return f"{val} Mbps"
    if "gamer" in t:
        return "940 Mbps"
    return ""

def clp_to_int(texto: str) -> int:
    if not texto:
        return -1
    nums = re.findall(r"\d+", texto.replace(".", ""))
    return int(nums[0]) if nums else -1

def format_clp(valor: int) -> str:
    return f"${valor:,.0f}".replace(",", ".") if valor >= 0 else ""

def infer_speed(plan: str) -> str:
    sp = extract_speed_from_text(plan)
    if sp:
        return sp
    if not plan:
        return ""
    txt = plan.lower()
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
    if not plan:
        return "Solo Fibra"
    t = plan.lower()
    if "portabilidad" in t or "móvil" in t or "movil" in t or "gigas" in t:
        return "Fibra + Móvil"
    if "tv" in t or "zapping" in t or "mundo go" in t or "go!" in t or "televisión" in t or "television" in t:
        return "Fibra + TV"
    if "telef" in t or "fija" in t:
        return "Fibra + Telefonía"
    if "dúo" in t or "duo" in t:
        return "Fibra + TV/Telefonía"
    return "Solo Fibra"

def infer_service_type(plan: str, force_mobile: bool = False) -> str:
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
    if ("tv" in t or "zapping" in t or "mundo go" in t or "televisión" in t or "television" in t) and ("fibra" not in t and "internet" not in t):
        return "solo tv"
    mapping = {
        "Solo Fibra": "solo internet",
        "Fibra + TV": "fibra + tv",
        "Fibra + Telefonía": "fibra + telefonía",
        "Fibra + Móvil": "fibra + móvil",
        "Fibra + TV/Telefonía": "fibra + tv/telefonía",
    }
    return mapping.get(infer_pack(plan), "solo internet")

def infer_mobile_detail(plan: str) -> str:
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

# ---------- Clasificación de precios por contexto ----------
def _label_price_in_context(ctx: str, start: int, end: int, window: int = 100) -> str:
    """
    Etiqueta un precio según las palabras cercanas (±window):
      - 'monthly'   si hay "al mes", "/mes", "mensual", "por N meses/por 1 año/x N meses"
      - 'later'     si hay "luego", "precio normal", etc.
      - 'install'   si hay "instalación", "extensor", "repetidor", etc.
      - 'unknown'   en otro caso
    """
    s = max(0, start - window)
    e = min(len(ctx), end + window)
    around = ctx[s:e].lower()

    if INSTALL_OR_EXTRA_RE.search(around):
        return "install"
    if MONTHLY_NEAR_RE.search(around):
        return "monthly"
    if LUEGO_RE.search(around):
        return "later"
    return "unknown"

def _extract_offer_period(around_text: str) -> str:
    """
    Devuelve string normalizado del periodo en el contexto cercano, ej. '6 meses', '1 año'.
    """
    m = PERIODO_RE.search(around_text)
    if not m:
        return ""
    g = m.group(0).lower()
    g = g.replace("un", "1").replace("año", "año").strip()
    # normaliza espacios
    return re.sub(r"\s+", " ", g)

def _choose_prices_from_context(ctx: str) -> Optional[Dict[str, Optional[str]]]:
    """
    Recorre TODOS los precios del contexto y devuelve un diccionario con:
      - price_offer_str / price_offer_int
      - offer_period_str
      - price_normal_str / price_normal_int
      - install_cost_int / install_free (bool)
    Lógica:
      * Preferencia por 'monthly' con pi >= MIN_MONTHLY_CLP (elige el menor).
      * 'later' intenta ser el precio normal (elige el menor >= umbral fuera de oferta).
      * 'install' suma info de instalación (si 'sin costo', marca install_free=True; si tiene $X, captura).
    """
    if not ctx:
        return None

    labels = []
    for m in PRICE_RE.finditer(ctx):
        ps = m.group(0)
        pi = clp_to_int(ps)
        lb = _label_price_in_context(ctx, m.start(), m.end())
        labels.append((lb, ps, pi, m.start(), m.end()))

    # Oferta mensual (válida)
    monthly = [(ps, pi, s, e) for (lb, ps, pi, s, e) in labels if lb == "monthly" and pi >= MIN_MONTHLY_CLP]
    price_offer_str = None
    price_offer_int = None
    offer_period_str = ""

    if monthly:
        price_offer_str, price_offer_int, s_off, e_off = sorted(monthly, key=lambda x: x[1])[0]
        # Busca periodo cerca del precio oferta
        s = max(0, s_off - 120)
        e = min(len(ctx), e_off + 120)
        offer_period_str = _extract_offer_period(ctx[s:e]) or ""

    # Precio normal/luego (válido)
    later = [(ps, pi) for (lb, ps, pi, s, e) in labels if lb == "later" and pi >= MIN_MONTHLY_CLP]
    price_normal_str = None
    price_normal_int = None
    if later:
        price_normal_str, price_normal_int = sorted(later, key=lambda x: x[1])[0]

    # Instalación
    install_cost_int = None
    install_free = False
    # Si hay "instalación sin costo" sin precio explícito, marcamos free
    if re.search(r"instalaci[oó]n[^$]{0,40}(sin\s*costo|gratis)", ctx, flags=re.IGNORECASE):
        install_free = True
        install_cost_int = 0
    else:
        inst = [(ps, pi) for (lb, ps, pi, s, e) in labels if lb == "install"]
        if inst:
            ps, pi = sorted(inst, key=lambda x: x[1])[0]
            install_cost_int = pi
            install_free = (pi == 0)

    # Si no quedó nada válido, abortar
    if not price_offer_int and not price_normal_int:
        return None

    return {
        "price_offer_str": price_offer_str,
        "price_offer_int": price_offer_int,
        "offer_period_str": offer_period_str,
        "price_normal_str": price_normal_str,
        "price_normal_int": price_normal_int,
        "install_cost_int": install_cost_int,
        "install_free": install_free,
    }

# =================== EXTRACTOR con speed_hint (ventana ±1200) ===================
def extract_plans_via_regex(html: str, max_items: int = 24, ctx_window: int = 1200) -> List[Dict]:
    """
    Busca precios y datos en contexto (±ctx_window) y devuelve dicts con:
      - plan_name, speed_hint
      - price_offer_str / _int, offer_period_str
      - price_normal_str / _int
      - install_cost_int, install_free
    Descarta contextos de Empresas/Convenios.
    """
    results: List[Dict] = []
    for m in PRICE_RE.finditer(html):
        start = max(m.start() - ctx_window, 0)
        end = min(m.end() + ctx_window, len(html))
        ctx = html[start:end]

        if NEGATIVE_RE.search(ctx):
            continue

        # Nombre de plan
        plan_match = (
            re.search(r"(PLAN\s*[0-9A-Z]+\s*[^\$<>{}|]{0,180})", ctx, re.IGNORECASE)
            or re.search(r"((?:Internet\s*)?Fibra\s*(?:Gamer|Giga|[0-9]{2,4})\s*(?:Megas?|Mb|Mbps|Gigas?)?)", ctx, re.IGNORECASE)
            or re.search(r"((?:Fibra|Internet)\s*[0-9]{2,4}\s*(?:Mb|Mbps|Megas?))", ctx, re.IGNORECASE)
            or re.search(r"(TV\s*(?:Lite\+|Full\+|Online)?)", ctx, re.IGNORECASE)
            or re.search(r"(Telefon(?:ía|ia)\s*fija)", ctx, re.IGNORECASE)
        )
        plan_name = ""
        if plan_match:
            plan_name = re.sub(r"\s+", " ", plan_match.group(1)).strip()
            plan_name = re.sub(r"(POR\s+\d+\s+MESES|SOLO\s+FIBRA|HASTA|OFERTA\s+WEB).*$", "", plan_name, flags=re.IGNORECASE).strip(" -–:|.")

        # Velocidad
        speed_hint = extract_speed_from_text(ctx)

        # Precios
        price_payload = _choose_prices_from_context(ctx)
        if not price_payload:
            continue

        results.append({
            "plan_name": plan_name,
            "speed_hint": speed_hint,
            **price_payload
        })
        if len(results) >= max_items:
            break

    # Dedup por (plan_name, price_offer_int/normal_int, speed_hint)
    seen = set()
    dedup: List[Dict] = []
    for p in results:
        key = (
            (p.get("plan_name") or "").lower(),
            p.get("price_offer_int") or -1,
            p.get("price_normal_int") or -1,
            (p.get("speed_hint") or "").lower(),
        )
        if key not in seen:
            seen.add(key)
            dedup.append(p)
    return dedup

# =================== RUT (autoformato + validación) ===================
RUT_BODY_RE = re.compile(r"[0-9]+")
RUT_DV_RE = re.compile(r"[0-9Kk]$")

def calcular_dv(rut_cuerpo: str) -> str:
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

def rut_sin_formato(rut: str) -> str:
    if not rut:
        return ""
    r = unicodedata.normalize("NFKC", rut).strip()
    r = re.sub(r"[^0-9Kk]", "", r)
    if not r:
        return ""
    if len(r) >= 2 and RUT_DV_RE.search(r[-1:]):
        cuerpo = re.sub(r"[^0-9]", "", r[:-1])
        dv = r[-1].upper()
        return cuerpo + dv
    return re.sub(r"[^0-9]", "", r)

def formatear_rut_limpio(cuerpo: str, dv: str) -> str:
    if not cuerpo or not dv:
        return ""
    chunks = []
    while cuerpo:
        chunks.append(cuerpo[-3:])
        cuerpo = cuerpo[:-3]
    cuerpo_fmt = ".".join(reversed(chunks))
    return f"{cuerpo_fmt}-{dv}"

def formatear_rut(rut: str) -> str:
    limpio = rut_sin_formato(rut)
    if len(limpio) < 2:
        cuerpo = limpio
        if not cuerpo:
            return ""
        chunks, aux = [], cuerpo
        while aux:
            chunks.append(aux[-3:])
            aux = aux[:-3]
        return ".".join(reversed(chunks))
    cuerpo, dv = limpio[:-1], limpio[-1]
    return formatear_rut_limpio(cuerpo, dv)

def validar_rut(rut: str) -> bool:
    limpio = rut_sin_formato(rut)
    if len(limpio) < 2:
        return False
    cuerpo, dv = limpio[:-1], limpio[-1]
    dv_calc = calcular_dv(cuerpo)
    return dv_calc == dv

def on_rut_change_autofmt():
    raw = st.session_state.get("rut_raw", "") or ""
    limpio = rut_sin_formato(raw)

    if not limpio:
        st.session_state["rut_formateado"] = ""
        st.session_state["rut_valido"] = False
        st.session_state["rut_status"] = "Ingrese su RUT"
        return

    if limpio.isdigit() and 7 <= len(limpio) <= 8:
        dv = calcular_dv(limpio)
        fmt = formatear_rut_limpio(limpio, dv)
    elif len(limpio) >= 2 and RUT_DV_RE.search(limpio[-1:]):
        cuerpo, dv = limpio[:-1], limpio[-1].upper()
        fmt = formatear_rut_limpio(cuerpo, dv)
    else:
        st.session_state["rut_status"] = "❌ RUT inválido (DV debe ser 0-9 o K)"
        st.session_state["rut_valido"] = False
        return

    st.session_state["rut_raw"] = fmt
    es_ok = validar_rut(fmt)
    st.session_state["rut_formateado"] = fmt
    st.session_state["rut_valido"] = es_ok
    st.session_state["rut_status"] = "✅ RUT válido" if es_ok else "❌ RUT inválido"

# =================== Nominatim (Dirección) ===================
NOMINATIM_SEARCH = "https://nominatim.openstreetmap.org/search"
NOMINATIM_REVERSE = "https://nominatim.openstreetmap.org/reverse"

def _nominatim_headers():
    return {"User-Agent": "MiComparadorTelecom/1.0 (Streamlit; contacto: soporte@ejemplo.cl)"}

def buscar_direccion_gratis(q: str, countrycodes: str = "cl", limit: int = 3) -> List[Dict]:
    if not q:
        return []
    params = {"q": q, "format": "jsonv2", "addressdetails": 1, "limit": limit, "countrycodes": countrycodes}
    time.sleep(1.05)
    r = requests.get(NOMINATIM_SEARCH, params=params, headers=_nominatim_headers(), timeout=20)
    if r.status_code != 200:
        return []
    return r.json()

def normalizar_direccion_por_latlon(lat: str, lon: str) -> Optional[Dict]:
    if not lat or not lon:
        return None
    params = {"lat": lat, "lon": lon, "format": "jsonv2", "addressdetails": 1, "zoom": 18, "accept-language": "es-CL"}
    time.sleep(1.05)
    r = requests.get(NOMINATIM_REVERSE, params=params, headers=_nominatim_headers(), timeout=20)
    if r.status_code != 200:
        return None
    return r.json()

def on_dir_change_autovalidate():
    """
    Valida y normaliza dirección automáticamente al cambiar el input:
    - Busca en Nominatim (gratis) con countrycodes=cl.
    - Si hay resultado, normaliza (display_name) vía reverse y lo escribe en el input.
    """
    q = (st.session_state.get("dir_input") or "").strip()
    if len(q) < 5:
        st.session_state["dir_sugerencias"] = []
        st.session_state["dir_status"] = "Escriba una dirección más específica"
        return

    try:
        sug = buscar_direccion_gratis(q, countrycodes="cl", limit=3)
        st.session_state["dir_sugerencias"] = sug or []
        if not sug:
            st.session_state["dir_status"] = "❌ No se encontró la dirección"
            return

        best = sug[0]
        lat, lon = best.get("lat"), best.get("lon")
        if not (lat and lon):
            st.session_state["dir_status"] = "❌ No se pudo normalizar (coordenadas faltantes)"
            return

        rev = normalizar_direccion_por_latlon(lat, lon)
        if rev and "display_name" in rev:
            st.session_state["dir_input"] = rev["display_name"]
            st.session_state["dir_status"] = "✅ Dirección validada y normalizada"
        else:
            st.session_state["dir_status"] = "❌ No se pudo normalizar (reverse)"
    except Exception:
        st.session_state["dir_status"] = "⚠️ Error validando con Nominatim"

# =================== Scraping helper (Playwright) ===================
async def _scrape_urls(urls: List[str], filters_regex: str) -> List[Dict]:
    """
    - Deja pasar tarjetas cuyo NOMBRE case con `filters_regex` (fibra/mbps/mega/giga) o que tengan speed_hint.
    - Descarta contextos de Empresa/Corporativo/Convenios.
    - Devuelve dicts con plan/prices/instalación/periodo/velocidad.
    """
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
            results: List[Dict] = []
            for u in urls:
                try:
                    await page.goto(u, wait_until="domcontentloaded", timeout=30000)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=12000)
                        await page.mouse.wheel(0, 2600)
                        await page.wait_for_timeout(800)
                    except:
                        pass
                    html = await page.content()
                    found = extract_plans_via_regex(html, max_items=40, ctx_window=1200)
                    for d in found:
                        plan_name = d.get("plan_name", "")
                        speed_hint = d.get("speed_hint", "")
                        # Filtro positivo por nombre (o speed_hint)
                        if not (speed_hint or (plan_name and re.search(filters_regex, plan_name, re.IGNORECASE))):
                            continue
                        # Filtro negativo por nombre
                        if plan_name and NEGATIVE_RE.search(plan_name):
                            continue
                        results.append(d)
                except Exception:
                    continue
            # Dedup simple
            seen, dedup = set(), []
            for d in results:
                key = (
                    (d.get("plan_name") or "").lower(),
                    d.get("price_offer_int") or -1,
                    d.get("price_normal_int") or -1,
                    (d.get("speed_hint") or "").lower()
                )
                if key not in seen:
                    seen.add(key)
                    dedup.append(d)
            return dedup
        finally:
            await browser.close()

# =================== Scrapers HOGAR (URLs residenciales) ===================
def _row_from_dict(d: Dict, prov_flag: Dict[str, str], pack_tipo: str, velocidad: str) -> Dict:
    """
    Construye una fila estándar para DataFrame con nuevas columnas de oferta/normal/instalación.
    """
    price_offer_str = d.get("price_offer_str") or ""
    price_offer_int = d.get("price_offer_int") if d.get("price_offer_int") is not None else -1
    price_normal_str = d.get("price_normal_str") or ""
    price_normal_int = d.get("price_normal_int") if d.get("price_normal_int") is not None else -1
    periodo = d.get("offer_period_str") or ""
    install_cost_int = d.get("install_cost_int")
    install_free = d.get("install_free", False)

    # "costo total" = precio oferta (cuando exista) o precio normal
    precio_mostrar = price_offer_int if price_offer_int and price_offer_int > 0 else price_normal_int
    precio_mostrar_str = format_clp(precio_mostrar) if precio_mostrar >= 0 else (price_offer_str or price_normal_str or "")

    return {
        **prov_flag,
        "pack seleccionado": pack_tipo,
        "velocidad": velocidad,
        "precio oferta": format_clp(price_offer_int) if price_offer_int and price_offer_int > 0 else (price_offer_str or ""),
        "periodo oferta": periodo,
        "precio normal": format_clp(price_normal_int) if price_normal_int and price_normal_int > 0 else (price_normal_str or ""),
        "instalación": (format_clp(install_cost_int) if (install_cost_int is not None and install_cost_int > 0) else ("$0" if install_free else "")),
        "instalación sin costo": "Sí" if (install_free or install_cost_int == 0) else "No" if (install_cost_int and install_cost_int > 0) else "",
        "costo total": precio_mostrar_str,
        # Auxiliares:
        "Precio_CLP": precio_mostrar,
        "__prov": [k for k,v in prov_flag.items() if v=="✔"][0] if any(v=="✔" for v in prov_flag.values()) else "",
        "__plan": d.get("plan_name") or ""
    }

async def hogar_mundo() -> List[Dict]:
    urls = [
        "https://www.tumundo.cl/",
        "https://www.tumundo.cl/planes-hogar/fibra-3g/",
        "https://www.tumundo.cl/planes-hogar/fibra-3000-1500-tv-mundo-go/",
        "https://www.tumundo.cl/planes-hogar/fibra-1g-mundo-go/",
        "https://www.tumundo.cl/planes-hogar/fibra-10g/",
    ]
    found = await _scrape_urls(urls, r"fibra|giga|megas?|mbps|mb\/s")
    out: List[Dict] = []
    for d in found:
        plan = d.get("plan_name") or ""
        tipo = infer_service_type(plan)
        if tipo not in {"solo tv", "solo internet", "solo telefonía fija",
                        "fibra + tv", "fibra + telefonía", "fibra + móvil",
                        "fibra + tv + telefonía", "fibra + tv/telefonía"}:
            continue
        velocidad = infer_speed(plan) or d.get("speed_hint") or ""
        out.append(_row_from_dict(d, {"mundo":"✔","movistar":"","entel":"","wom":"","vtr":""},
                                  infer_pack(plan) if tipo.startswith("fibra") else tipo, velocidad))
    return out

async def hogar_movistar() -> List[Dict]:
    urls = [
        "https://ww2.movistar.cl/hogar/internet-hogar/",
        "https://ww2.movistar.cl/hogar/internet-fibra-optica/",
        "https://ww2.movistar.cl/hogar/arma-tu-plan/",
        "https://ww2.movistar.cl/hogar/pack-duos-internet-television/",
    ]
    found = await _scrape_urls(urls, r"fibra|giga|megas?|mbps|mb\/s")
    out: List[Dict] = []
    for d in found:
        plan = d.get("plan_name") or ""
        tipo = infer_service_type(plan)
        if tipo not in {"solo tv", "solo internet", "solo telefonía fija",
                        "fibra + tv", "fibra + telefonía", "fibra + móvil",
                        "fibra + tv + telefonía", "fibra + tv/telefonía"}:
            continue
        velocidad = infer_speed(plan) or d.get("speed_hint") or ""
        out.append(_row_from_dict(d, {"mundo":"","movistar":"✔","entel":"","wom":"","vtr":""},
                                  infer_pack(plan) if tipo.startswith("fibra") else tipo, velocidad))
    return out

async def hogar_entel() -> List[Dict]:
    urls = [
        "https://www.entel.cl/hogar/internet",
        "https://www.entel.cl/hogar/fibra-optica",
    ]
    found = await _scrape_urls(urls, r"fibra|giga|megas?|mbps|mb\/s")
    out: List[Dict] = []
    for d in found:
        plan = d.get("plan_name") or ""
        tipo = infer_service_type(plan)
        if tipo not in {"solo tv", "solo internet", "solo telefonía fija",
                        "fibra + tv", "fibra + telefonía", "fibra + móvil",
                        "fibra + tv + telefonía", "fibra + tv/telefonía"}:
            continue
        velocidad = infer_speed(plan) or d.get("speed_hint") or ""
        out.append(_row_from_dict(d, {"mundo":"","movistar":"","entel":"✔","wom":"","vtr":""},
                                  infer_pack(plan) if tipo.startswith("fibra") else tipo, velocidad))
    return out

async def hogar_wom() -> List[Dict]:
    urls = [
        "https://store.wom.cl/hogar/internet-hogar",
        "https://store.wom.cl/hogar/internet-tv-hogar",
        "https://store.wom.cl/fibra/",
    ]
    found = await _scrape_urls(urls, r"fibra|giga|megas?|mbps|mb\/s")
    out: List[Dict] = []
    for d in found:
        plan = d.get("plan_name") or ""
        tipo = infer_service_type(plan)
        if tipo not in {"solo tv", "solo internet", "solo telefonía fija",
                        "fibra + tv", "fibra + telefonía", "fibra + móvil",
                        "fibra + tv + telefonía", "fibra + tv/telefonía"}:
            continue
        velocidad = infer_speed(plan) or d.get("speed_hint") or ""
        out.append(_row_from_dict(d, {"mundo":"","movistar":"","entel":"","wom":"✔","vtr":""},
                                  infer_pack(plan) if tipo.startswith("fibra") else tipo, velocidad))
    return out

async def hogar_vtr() -> List[Dict]:
    urls = [
        "https://vtr.com/",
        "https://vtr.com/comparador-planes/",
        "https://www.nuevo.vtr.com/comparador-planes",
        "https://vtr.com/productos/hogar-packs/internet-hogar/",
    ]
    found = await _scrape_urls(urls, r"fibra|giga|megas?|mbps|mb\/s")
    out: List[Dict] = []
    for d in found:
        plan = d.get("plan_name") or ""
        tipo = infer_service_type(plan)
        if tipo not in {"solo tv", "solo internet", "solo telefonía fija",
                        "fibra + tv", "fibra + telefonía", "fibra + móvil",
                        "fibra + tv + telefonía", "fibra + tv/telefonía"}:
            continue
        velocidad = infer_speed(plan) or d.get("speed_hint") or ""
        out.append(_row_from_dict(d, {"mundo":"","movistar":"","entel":"","wom":"","vtr":"✔"},
                                  infer_pack(plan) if tipo.startswith("fibra") else tipo, velocidad))
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
    for d in found:
        plan = d.get("plan_name") or ""
        out.append({
            "mundo": "", "movistar": "✔", "entel": "", "wom": "", "vtr": "",
            "pack seleccionado": "solo internet móvil",
            "velocidad": "",
            "precio oferta": "",
            "periodo oferta": "",
            "precio normal": "",
            "instalación": "",
            "instalación sin costo": "",
            "detalle movil": infer_mobile_detail(plan) or "Gigas Libres/GB",
            "costo total": format_clp(d.get("price_offer_int") or d.get("price_normal_int") or -1) if (d.get("price_offer_int") or d.get("price_normal_int")) else "",
            "Precio_CLP": d.get("price_offer_int") or d.get("price_normal_int") or -1,
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
    for d in found:
        plan = d.get("plan_name") or ""
        out.append({
            "mundo": "", "movistar": "", "entel": "✔", "wom": "", "vtr": "",
            "pack seleccionado": "solo internet móvil",
            "velocidad": "",
            "precio oferta": "",
            "periodo oferta": "",
            "precio normal": "",
            "instalación": "",
            "instalación sin costo": "",
            "detalle movil": infer_mobile_detail(plan) or "Gigas Libres/GB",
            "costo total": format_clp(d.get("price_offer_int") or d.get("price_normal_int") or -1) if (d.get("price_offer_int") or d.get("price_normal_int")) else "",
            "Precio_CLP": d.get("price_offer_int") or d.get("price_normal_int") or -1,
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
    for d in found:
        plan = d.get("plan_name") or ""
        out.append({
            "mundo": "", "movistar": "", "entel": "", "wom": "✔", "vtr": "",
            "pack seleccionado": "solo internet móvil",
            "velocidad": "",
            "precio oferta": "",
            "periodo oferta": "",
            "precio normal": "",
            "instalación": "",
            "instalación sin costo": "",
            "detalle movil": infer_mobile_detail(plan) or "Gigas Libres/GB",
            "costo total": format_clp(d.get("price_offer_int") or d.get("price_normal_int") or -1) if (d.get("price_offer_int") or d.get("price_normal_int")) else "",
            "Precio_CLP": d.get("price_offer_int") or d.get("price_normal_int") or -1,
            "__prov": "wom", "__plan": plan
        })
    return out

# =================== Limpieza de filtros ===================
def _limpiar_filtros():
    st.session_state["incluir_mundo"] = True
    st.session_state["incluir_movistar"] = True
    st.session_state["incluir_entel"] = True
    st.session_state["incluir_wom"] = True
    st.session_state["incluir_vtr"] = False
    st.session_state["servicios_sel_hogar"] = ["solo internet", "fibra + tv", "fibra + telefonía", "fibra + móvil"]
    st.session_state["servicios_sel_movil"] = ["solo internet móvil"]
    st.session_state["fibra_por_proveedor"] = {}
    st.toast("Filtros restablecidos")

# =================== Sidebar: RUT + Dirección + Proveedores + Modo ===================
with st.sidebar:
    st.header("Datos del cliente")

    # Toggle Dev
    st.checkbox("Modo desarrollador (mostrar toolbar)", value=False, key="dev_mode")
    apply_chrome_visibility(st.session_state.get("dev_mode", False))

    st.text_input(
        "RUT (autoformato y validación)",
        key="rut_raw",
        placeholder="12.345.678-K",
        on_change=on_rut_change_autofmt
    )
    st.caption(st.session_state.get("rut_status", "Ingrese su RUT"))

    st.text_input(
        "Dirección (auto-validación y normalización)",
        key="dir_input",
        placeholder="calle y número, comuna (ej: San Óscar 2807 Maipú)",
        on_change=on_dir_change_autovalidate
    )
    st.caption(st.session_state.get("dir_status", "Escribe una dirección y presiona Enter"))

    sug = st.session_state.get("dir_sugerencias", []) or []
    if sug:
        st.write("Coincidencias (top 3):")
        for i, s in enumerate(sug[:3], start=1):
            st.write(f"{i}. {s.get('display_name','')}")

    st.divider()

    st.subheader("Compañías a comparar (Hogar/Móvil)")
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
cols_final = [
    "mundo","movistar","entel","wom","vtr",
    "pack seleccionado","velocidad",
    "precio oferta","periodo oferta","precio normal",
    "instalación","instalación sin costo",
    "detalle movil","costo total","insignia"
]

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
                    for prov in ["mundo","movistar","entel","wom","vtr"]:
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
                    df = df.drop(columns=["__tipo"], errors="ignore")

                # >>> INSIGNIA + DEDUP HOGAR (usa Precio_CLP basado en oferta)
                if not df.empty:
                    df["insignia"] = ""
                    if "Precio_CLP" in df.columns:
                        grp = ["__prov","velocidad","pack seleccionado"]
                        idx_min = df.groupby(grp, dropna=False)["Precio_CLP"].idxmin()
                        df.loc[idx_min, "insignia"] = "🏷️ Oferta más barata"
                        df = df.sort_values(by="Precio_CLP", na_position="last")
                        df = df.loc[~df.duplicated(subset=grp, keep="first")].copy()
                    else:
                        df = df.loc[~df.duplicated(subset=["__prov","velocidad","pack seleccionado"], keep="first")].copy()

                if df.empty:
                    st.info("No se encontraron planes que coincidan con los filtros (Hogar).")
                else:
                    # Asegurar columnas finales y vacíos como ""
                    for c in cols_final:
                        if c not in df.columns:
                            df[c] = ""
                    # Orden por precio oferta/normal
                    if "Precio_CLP" in df.columns:
                        df = df.sort_values(by="Precio_CLP", na_position="last")
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
                        for prov in ["movistar","entel","wom"]:
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
                                    "precio oferta": "",
                                    "periodo oferta": "",
                                    "precio normal": "",
                                    "instalación": "",
                                    "instalación sin costo": "",
                                    "costo total": format_clp(total),
                                    "Precio_CLP": total,
                                    "__prov": prov,
                                    "insignia": ""
                                })
                    if combos:
                        dfm = pd.concat([dfm, pd.DataFrame(combos)], ignore_index=True)

                # >>> INSIGNIA + DEDUP MÓVIL
                if not dfm.empty:
                    if "insignia" not in dfm.columns:
                        dfm["insignia"] = ""
                    if "Precio_CLP" in dfm.columns:
                        grp_m = ["__prov","pack seleccionado","detalle movil"]
                        idx_min_m = dfm.groupby(grp_m, dropna=False)["Precio_CLP"].idxmin()
                        dfm.loc[idx_min_m, "insignia"] = "🏷️ Oferta más barata"
                        dfm = dfm.sort_values(by="Precio_CLP", na_position="last")
                        dfm = dfm.loc[~dfm.duplicated(subset=grp_m, keep="first")].copy()
                    else:
                        dfm = dfm.loc[~dfm.duplicated(subset=["__prov","pack seleccionado","detalle movil"], keep="first")].copy()

                if dfm.empty:
                    st.info("No se encontraron planes móviles (o selecciona 'Fibra + Móvil' tras ejecutar primero Hogar).")
                else:
                    for c in cols_final:
                        if c not in dfm.columns:
                            dfm[c] = ""
                    if "Precio_CLP" in dfm.columns:
                        dfm = dfm.sort_values(by="Precio_CLP", na_position="last")
                    st.success("¡Planes móviles listos!")
                    st.dataframe(dfm[cols_final], use_container_width=True)

            except Exception as e:
                st.error(f"Falla en la consulta Móvil: {e}")
                st.caption("Si aparece un mensaje pidiendo `playwright install`, presiona el botón otra vez.")
