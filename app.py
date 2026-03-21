# app.py
# Comparador Telecom Chile - Hogar/Móvil
# - RUT: autoformato + validación (módulo-11) al escribir (corrige punto fantasma; respeta ceros iniciales)
# - Dirección: validación/normalización automática (forward search + reverse) con tu función
# - Solo Hogar: listas blancas de URLs residenciales por proveedor
# - Velocidad: extraída del nombre o del CONTEXTO HTML cercano al precio (Mbps/Gbps, "Mega(s)", "Mb/s", "Giga", "hasta ...")
# - Scrapers: Mundo / Movistar / Entel / WOM (+ VTR fallback) para Hogar; Movistar/Entel/WOM para Móvil
# - Botón "Limpiar filtros" y modo exclusivo Hogar/Móvil
# - DEDUP + INSIGNIA: evita duplicados por compañía/velocidad/pack (Hogar) y compañía/pack/detalle/Precio (Móvil),
#   marcando la fila ganadora como "🏷️ Oferta más barata"

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
st.set_page_config(page_title="Comparador Chile", page_icon="📡")
st.title("📡 Mi Comparador Telecom")

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

# =================== Parsers / Regex comunes ===================
PRICE_RE = re.compile(r"\$\s?\d{1,3}(?:\.\d{3})+", re.IGNORECASE)

# ---- Parser robusto de velocidad ----
# Soporta: "600 Mb/s", "600 Mbps", "600 Mb", "600 Mega(s)", "1 Giga", "1 Gb", "1.5 Gbps", "hasta 940 Mbps"
GIGA_RE = re.compile(r"\b(\d+(?:[.,]\d+)?)\s*(?:g(?:ig?a)?|gb(?:ps)?)\b", re.IGNORECASE)
MBPS_RE = re.compile(r"\b(\d{2,5})\s*(?:m(?:b(?:ps)?|b\/s)?|mega?s?)\b", re.IGNORECASE)
HASTA_RE = re.compile(r"\bhasta\b\s+(\d+(?:[.,]\d+)?)\s*(?:mbps|mb\/s|mb|mega?s?|g(?:ig?a)?|gb(?:ps)?)", re.IGNORECASE)

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

# =================== EXTRACTOR: incluye speed_hint desde CONTEXTO ===================
def extract_plans_via_regex(html: str, max_items: int = 24) -> List[Tuple[str, str, int, str]]:
    results: List[Tuple[str, str, int, str]] = []
    for m in PRICE_RE.finditer(html):
        start = max(m.start() - 320, 0)
        end = min(m.end() + 320, len(html))
        ctx = html[start:end]

        plan_match = (
            re.search(r"(PLAN\s*[0-9A-Z]+\s*[^\$<>{}|]{0,140})", ctx, re.IGNORECASE)
            or re.search(r"((?:Internet\s*)?Fibra\s*(?:Gamer|Giga|[0-9]{2,4})\s*(?:Megas?|Mb|Mbps|Gigas?)?)", ctx, re.IGNORECASE)
            or re.search(r"((?:Fibra|Internet)\s*[0-9]{2,4}\s*(?:Mb|Mbps))", ctx, re.IGNORECASE)
            or re.search(r"(TV\s*(?:Lite\+|Full\+|Online)?)", ctx, re.IGNORECASE)
            or re.search(r"(Telefon(?:ía|ia)\s*fija)", ctx, re.IGNORECASE)
        )
        plan_name = None
        if plan_match:
            plan_name = re.sub(r"\s+", " ", plan_match.group(1)).strip()
            plan_name = re.sub(r"(POR\s+\d+\s+MESES|SOLO\s+FIBRA|HASTA|OFERTA\s+WEB).*$", "", plan_name, flags=re.IGNORECASE).strip(" -–:|.")

        speed_hint = extract_speed_from_text(ctx)
        price_str = m.group(0)
        price_int = clp_to_int(price_str)

        if plan_name and price_int > 0:
            results.append((plan_name, price_str, price_int, speed_hint))
        elif price_int > 0:
            results.append(("", price_str, price_int, speed_hint))

        if len(results) >= max_items:
            break

    seen = set()
    dedup: List[Tuple[str, str, int, str]] = []
    for p in results:
        key = (p[0].lower(), p[2], p[3].lower() if p[3] else "")
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
    r = re.sub(r"[^0-9Kk]", "", r)  # deja solo dígitos y K/k
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

# === TU VALIDACIÓN (forward search → reverse) ===
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
        # 1) forward search (gratis) con límites/headers adecuados
        sug = buscar_direccion_gratis(q, countrycodes="cl", limit=3)  # 1.05s de pausa interna
        st.session_state["dir_sugerencias"] = sug or []
        if not sug:
            st.session_state["dir_status"] = "❌ No se encontró la dirección"
            return

        # 2) toma la mejor coincidencia (primera) y normaliza por reverse
        best = sug[0]
        lat, lon = best.get("lat"), best.get("lon")
        if not (lat and lon):
            st.session_state["dir_status"] = "❌ No se pudo normalizar (coordenadas faltantes)"
            return

        rev = normalizar_direccion_por_latlon(lat, lon)  # incluye 1.05s de pausa
        if rev and "display_name" in rev:
            st.session_state["dir_input"] = rev["display_name"]  # auto-normaliza en el input
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
                        await page.mouse.wheel(0, 2200)
                        await page.wait_for_timeout(700)
                    except:
                        pass
                    html = await page.content()
                    found = extract_plans_via_regex(html, max_items=24)
                    kept = []
                    for f in found:
                        plan_name, _, _, speed_hint = f
                        if (plan_name and re.search(filters_regex, plan_name, re.IGNORECASE)) or speed_hint:
                            kept.append(f)
                    results.extend(kept)
                except Exception:
                    continue
            seen, dedup = set(), []
            for plan, ps, pi, sh in results:
                k = (plan.lower(), pi, sh.lower() if sh else "")
                if k not in seen:
                    seen.add(k)
                    dedup.append((plan, ps, pi, sh))
            return dedup
        finally:
            await browser.close()

# =================== Scrapers HOGAR (solo URLs residenciales) ===================
async def hogar_mundo() -> List[Dict]:
    urls = [
        "https://www.tumundo.cl/",
        "https://www.tumundo.cl/planes-hogar/fibra-3g/",
        "https://www.tumundo.cl/planes-hogar/fibra-3000-1500-tv-mundo-go/",
        "https://www.tumundo.cl/planes-hogar/fibra-1g-mundo-go/",
        "https://www.tumundo.cl/planes-hogar/fibra-10g/",
    ]
    found = await _scrape_urls(urls, r"fibra|internet|tv|televisi[oó]n|telefon")
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
        "https://ww2.movistar.cl/hogar/arma-tu-plan/",
        "https://ww2.movistar.cl/hogar/pack-duos-internet-television/",
    ]
    found = await _scrape_urls(urls, r"fibra|internet|tv|televisi[oó]n|telefon")
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
    ]
    found = await _scrape_urls(urls, r"fibra|internet|tv|televisi[oó]n|telefon")
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
        "https://store.wom.cl/hogar/internet-tv-hogar",
        "https://store.wom.cl/fibra/",
    ]
    found = await _scrape_urls(urls, r"fibra|internet|tv|televisi[oó]n|telefon")
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
    found = await _scrape_urls(urls, r"fibra|internet|tv|televisi[oó]n|telefon")
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
    for plan, price_str, price_int, _ in found:
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
    for plan, price_str, price_int, _ in found:
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
    for plan, price_str, price_int, _ in found:
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
cols_final = ["mundo", "movistar", "entel", "wom", "vtr",
              "pack seleccionado", "velocidad", "detalle movil", "costo total", "insignia"]

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
                    df = df.drop(columns=["__tipo"], errors="ignore")

                # >>> INSIGNIA + DEDUP HOGAR
                if not df.empty:
                    # Inicializa columna insignia
                    df["insignia"] = ""
                    # Marca como "Oferta más barata" al mínimo Precio_CLP por (__prov, velocidad, pack)
                    if "Precio_CLP" in df.columns:
                        grp = ["__prov", "velocidad", "pack seleccionado"]
                        idx_min = df.groupby(grp, dropna=False)["Precio_CLP"].idxmin()
                        df.loc[idx_min, "insignia"] = "🏷️ Oferta más barata"
                        # Ordena por precio y dedup por grupo, quedando la ganadora
                        df = df.sort_values(by="Precio_CLP", na_position="last")
                        df = df.loc[~df.duplicated(subset=grp, keep="first")].copy()
                    else:
                        # Si no hay Precio_CLP por alguna razón, al menos dedup por grupo
                        df = df.loc[~df.duplicated(subset=["__prov","velocidad","pack seleccionado"], keep="first")].copy()

                if df.empty:
                    st.info("No se encontraron planes que coincidan con los filtros (Hogar).")
                else:
                    if "Precio_CLP" in df.columns:
                        df = df.sort_values(by="Precio_CLP", na_position="last")
                    # Asegura todas las columnas de salida
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
                                    "Precio_CLP": total,
                                    "__prov": prov,
                                })
                    if combos:
                        dfm = pd.concat([dfm, pd.DataFrame(combos)], ignore_index=True)

                # >>> INSIGNIA + DEDUP MÓVIL
                if not dfm.empty:
                    dfm["insignia"] = ""
                    if "Precio_CLP" in dfm.columns:
                        grp_m = ["__prov", "pack seleccionado", "detalle movil"]
                        idx_min_m = dfm.groupby(grp_m, dropna=False)["Precio_CLP"].idxmin()
                        dfm.loc[idx_min_m, "insignia"] = "🏷️ Oferta más barata"
                        dfm = dfm.sort_values(by="Precio_CLP", na_position="last")
                        dfm = dfm.loc[~dfm.duplicated(subset=grp_m, keep="first")].copy()
                    else:
                        dfm = dfm.loc[~dfm.duplicated(subset=["__prov","pack seleccionado","detalle movil"], keep="first")].copy()

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
