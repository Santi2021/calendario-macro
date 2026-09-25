#!/usr/bin/env python3
"""
Confirmador diario del calendario macro.

Qué hace, en orden:
  1. Carga eventos, catálogo, feriados y el estado previo.
  2. Para cada evento cuya fecha ya pasó (ventana de 12 días hacia atrás) y que no
     está confirmado, recorre su cadena de métodos hasta obtener evidencia.
  3. Vigila si cambiaron los calendarios oficiales (INDEC y BCRA).
  4. Guarda el estado, escribe un reporte y, si hay credenciales, avisa por Telegram.

Reglas de robustez (el porqué de cada decisión está en el código):
  - Ninguna fuente puede tirar abajo la corrida: cada chequeo está aislado.
  - Un estado sólo avanza (programado -> confirmado); nunca se degrada por un fallo de red.
  - La escritura es atómica: si algo falla a mitad de camino, el estado anterior queda intacto.
  - Si un método falla 3 corridas seguidas, se reporta como "método caído" (fallo visible, no silencioso).
  - Modo --dry-run: calcula todo sin escribir nada.
  - Modo --selftest: corre pruebas offline con respuestas simuladas.

Uso:
  python confirmador.py                 # corrida normal
  python confirmador.py --dry-run       # prueba sin escribir
  python confirmador.py --fecha 2026-09-24   # simular otro "hoy"
  python confirmador.py --selftest      # pruebas offline
"""
from __future__ import annotations

import argparse, datetime as dt, hashlib, json, os, re, sys, tempfile, time, traceback
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus

try:
    import requests
except ImportError:  # el selftest no necesita red
    requests = None

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
TZ = dt.timezone(dt.timedelta(hours=-3))  # Argentina, sin horario de verano
MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
         "agosto", "septiembre", "octubre", "noviembre", "diciembre"]
UA = {"User-Agent": "Mozilla/5.0 (calendario-macro; confirmador diario)"}
VENTANA_DIAS = 12          # hasta cuántos días atrás se sigue buscando evidencia
TOLERANCIA_DIAS = 3        # días de gracia antes de marcar "demorado"
FALLOS_PARA_ALERTA = 3     # corridas seguidas fallando para declarar un método caído

# Notas previas al dato que la prensa publica antes de que salga (validado en el backtest:
# sin este filtro el IPC daba "confirmado" días antes en 16 de 21 meses).
EXCLUIR_PREVIAS = re.compile(
    r"esper|se conoce|conocer[áa]|proyect|anticip|cu[áa]ndo|previa|antes del dato|"
    r"estim|consultoras|privad|podr[íi]a|ser[íi]a|qu[ée] dir[áa]", re.I)

# --------------------------------------------------------------------------------------
# Configuración de confirmación por indicador.
# Cada entrada es una cadena de métodos que se prueba en orden hasta obtener evidencia.
#   lag: meses entre el período del dato y el mes de publicación (para exigir el mes en el título)
# --------------------------------------------------------------------------------------
CONF = {
    # BCRA: fuente directa (listado de últimos informes con fecha exacta)
    "bcra.rem":              [("bcra_listado", {"nombre": "Relevamiento de Expectativas de Mercado"}),
                              ("prensa", {"q": "REM Banco Central", "lag": 1})],
    "bcra.imm":              [("bcra_listado", {"nombre": "Informe Monetario Mensual"})],
    "bcra.boletin":          [("bcra_publicacion", {"slug": "boletin-estadistico-{mes}-de-{yyyy}", "lag": 0, "verificado": True}),
                              ("bcra_listado", {"nombre": "Boletín Estadístico"})],
    "bcra.bancos":           [("bcra_publicacion", {"slug": "informe-sobre-bancos-{mes}-de-{yyyy}", "lag": 2, "verificado": True}),
                              ("bcra_listado", {"nombre": "Informe sobre Bancos"})],
    "bcra.cambiario":        [("bcra_listado", {"nombre": "Mercado de Cambios y Balance Cambiario"}),
                              ("prensa", {"q": '"balance cambiario" BCRA', "lag": 1})],
    "bcra.pagos_minoristas": [("bcra_listado", {"nombre": "Informe de Pagos Minoristas"})],
    "bcra.ipom":             [("bcra_listado", {"nombre": "Informe de Política Monetaria"})],
    "bcra.ied":              [("bcra_listado", {"nombre": "Inversión Extranjera Directa"})],
    "bcra.deuda_privada":    [("bcra_listado", {"nombre": "Deuda Externa Privada"})],
    "bcra.ecc":              [("bcra_listado", {"nombre": "Condiciones Crediticias"})],
    "bcra.ief":              [("bcra_listado", {"nombre": "Estabilidad Financiera"})],
    "bcra.inclusion":        [("bcra_listado", {"nombre": "Inclusión Financiera"})],
    "bcra.pnfc":             [("bcra_listado", {"nombre": "Proveedores No Financieros"})],
    # INDEC: archivo previsible cuando existe, si no prensa con filtro ajustado
    "indec.ipc":  [("archivo", {"url": "https://www.indec.gob.ar/ftp/cuadros/economia/sh_ipc_{mm_pub}_{yy_pub}.xls"}),
                   ("prensa", {"q": "INDEC inflación", "lag": 1})],
    "indec.emae": [("prensa", {"q": "EMAE INDEC", "lag": 2})],
    "indec.ica":  [("prensa", {"q": "INDEC comercial exportaciones importaciones", "lag": 1})],
    "indec.sipm": [("prensa", {"q": "INDEC mayoristas", "lag": 1})],
    "indec.ipi_manuf": [("prensa", {"q": "INDEC industria manufacturera", "lag": 2})],
    "indec.isac": [("prensa", {"q": "INDEC construcción", "lag": 2})],
    "indec.ucii": [("prensa", {"q": "INDEC capacidad instalada industria", "lag": 2})],
    "indec.salarios": [("prensa", {"q": "INDEC salarios", "lag": 2})],
    "indec.supermercados": [("prensa", {"q": "INDEC supermercados", "lag": 2})],
    "indec.pobreza": [("prensa", {"q": "INDEC pobreza indigencia", "clave_titulo": "pobreza", "lag": 0})],
    # Privadas y Estado: sitio propio primero, prensa de respaldo
    "priv.adefa": [("archivo", {"url": "https://adefa.org.ar/upload/estadisticas/resumen-{yyyy_ref}-{mm_ref}-es.pdf"}),
                   ("prensa", {"q": "ADEFA producción", "lag": 1})],
    "priv.cca_usados": [("rss", {"url": "https://cca.org.ar/feed/", "clave": "usados", "lag": 1}),
                        ("prensa", {"q": '"Cámara del Comercio Automotor" usados', "lag": 1})],
    "priv.escrituras_caba": [("rss", {"url": "https://www.colegio-escribanos.org.ar/feed/", "clave": "escrituras", "lag": 1}),
                             ("prensa", {"q": '"Colegio de Escribanos" escrituras', "lag": 1})],
    "priv.construya": [("pagina_fecha", {"url": "https://www.grupoconstruya.com.ar/servicios/indice_construya"}),
                       ("prensa", {"q": '"Índice Construya"', "lag": 1})],
    "priv.ipc_caba": [("rss", {"url": "https://www.estadisticaciudad.gob.ar/eyc/feed/", "clave": "precios", "lag": 1}),
                      ("prensa", {"q": "inflación CABA IDECBA", "lag": 1})],
    "priv.acara": [("prensa", {"q": "ACARA patentamientos", "lag": 1})],
    "priv.ciara_cec": [("prensa", {"q": "CIARA CEC liquidación", "lag": 1})],
    "priv.came_minoristas": [("prensa", {"q": 'CAME "ventas minoristas"', "lag": 1})],
    "priv.adimra": [("prensa", {"q": "ADIMRA metalúrgica", "lag": 1})],
    "priv.scentia": [("prensa", {"q": 'Scentia "consumo masivo"', "lag": 1})],
    "priv.uia_ceu": [("prensa", {"q": 'UIA "actividad industrial"', "lag": 1})],
    "priv.fractura_vm": [("prensa", {"q": '"etapas de fractura"', "lag": 1})],
    "priv.utdt_icc": [("prensa", {"q": '"confianza del consumidor" "Di Tella"', "lag": 0})],
    "priv.utdt_icg": [("prensa", {"q": '"confianza en el gobierno" "Di Tella"', "lag": 0})],
    "priv.resultado_fiscal": [("prensa", {"q": '"Sector Público Nacional" superávit OR déficit', "lag": 1})],
    "priv.recaudacion_arca": [("prensa", {"q": "recaudación ARCA interanual", "lag": 1})],
}

# Calendarios oficiales vigilados: si cambian, se avisa para recargarlos.
CALENDARIOS = {
    "indec_2sem": "https://www.indec.gob.ar/ftp/cuadros/publicaciones/calendario_2sem{yyyy}.pdf",
    "indec_1sem_prox": "https://www.indec.gob.ar/ftp/cuadros/publicaciones/calendario_1sem{yyyy1}.pdf",
    "bcra_calendario": "https://www.bcra.gob.ar/calendario-de-informes/",
}


# ======================================================================================
# Utilidades
# ======================================================================================
class Http:
    """Cliente HTTP con timeout, reintentos con espera creciente y caché por corrida."""

    def __init__(self, fake: dict | None = None):
        self.fake = fake          # para el selftest: {url_prefijo: (status, texto)}
        self.cache: dict = {}

    def get(self, url: str, head: bool = False) -> tuple[int, str]:
        key = ("HEAD " if head else "") + url
        if key in self.cache:
            return self.cache[key]
        if self.fake is not None:
            for pref, resp in self.fake.items():
                if url.startswith(pref):
                    self.cache[key] = resp
                    return resp
            return (404, "")
        ultimo = None
        for intento in range(3):
            try:
                if head:
                    r = requests.head(url, headers=UA, timeout=20, allow_redirects=True)
                    if r.status_code in (405, 403):  # algunos servidores no aceptan HEAD
                        r = requests.get(url, headers=UA, timeout=30, stream=True)
                        r.close()
                    out = (r.status_code, "")
                else:
                    r = requests.get(url, headers=UA, timeout=30)
                    r.encoding = r.encoding or "utf-8"
                    out = (r.status_code, r.text)
                if out[0] >= 500:
                    raise IOError(f"HTTP {out[0]}")
                self.cache[key] = out
                return out
            except Exception as e:  # red caída, timeout, 5xx
                ultimo = e
                time.sleep(2 * (intento + 1))
        raise IOError(f"{url}: {ultimo}")


def d(s: str) -> dt.date:
    return dt.date.fromisoformat(s)


def mes_ref(fecha_pub: dt.date, lag: int) -> tuple[int, int]:
    m = fecha_pub.month - 1 - lag
    y = fecha_pub.year + m // 12
    return y, m % 12 + 1


def cargar(nombre: str, defecto):
    p = os.path.join(DATA, nombre)
    if not os.path.exists(p):
        return defecto
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def guardar_atomico(nombre: str, obj) -> None:
    """Escribe en un temporal y lo renombra: nunca deja un JSON a medio escribir."""
    p = os.path.join(DATA, nombre)
    fd, tmp = tempfile.mkstemp(dir=DATA, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


# ======================================================================================
# Métodos de confirmación. Cada uno devuelve (fecha_real|None, url_evidencia|None).
# Si no hay evidencia devuelve (None, None). Si la fuente no responde, lanza excepción.
# ======================================================================================
def m_bcra_listado(http: Http, ev: dict, p: dict):
    st, html = http.get("https://www.bcra.gob.ar/ultimos-informes/")
    if st != 200:
        raise IOError(f"BCRA listado HTTP {st}")
    ab = {"ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6, "jul": 7,
          "ago": 8, "sep": 9, "oct": 10, "nov": 11, "dic": 12}
    texto = re.sub(r"<[^>]+>", " ", html)
    texto = re.sub(r"\s+", " ", texto)
    if not re.search(r"\b\d{2} (ene|feb|mar|abr|may|jun|jul|ago|sep|oct|nov|dic) \d{4}\b", texto):
        # Página sin ninguna fecha: la tabla se arma con JavaScript o cambió la estructura.
        # No es evidencia de que el informe no salió.
        raise IOError("listado BCRA sin fechas legibles (tabla cargada por JavaScript)")
    objetivo = d(ev["fecha"])
    # Cada fila del listado: "<Nombre> <Periodicidad> <Período> DD mmm AAAA"
    for m in re.finditer(re.escape(p["nombre"]) + r".{0,140}?\b(\d{2}) ([a-z]{3}) (\d{4})", texto):
        try:
            f = dt.date(int(m.group(3)), ab[m.group(2).lower()], int(m.group(1)))
        except (KeyError, ValueError):
            continue
        if abs((f - objetivo).days) <= VENTANA_DIAS:
            return f, "https://www.bcra.gob.ar/ultimos-informes/"
    return None, None


def m_bcra_publicacion(http: Http, ev: dict, p: dict):
    """Página propia de cada informe: bcra.gob.ar/publicaciones/<slug>/ con 'Publicado el DD Mmm AAAA'.
    El slug usa el mes del dato (lag) o el de publicación. Sólo si el patrón está verificado,
    un 404 se toma como 'todavía no salió'; si no, se toma como error para no acusar en falso."""
    f = d(ev["fecha"])
    y, m = mes_ref(f, p.get("lag", 0))
    slug = p["slug"].format(mes=MESES[m - 1], yyyy=y)
    url = f"https://www.bcra.gob.ar/publicaciones/{slug}/"
    st, html = http.get(url)
    if st == 404:
        if p.get("verificado"):
            return None, None
        raise IOError(f"{url}: 404 con patrón no verificado")
    if st != 200:
        raise IOError(f"{url}: HTTP {st}")
    ab = {"ene": 1, "jan": 1, "feb": 2, "mar": 3, "abr": 4, "apr": 4, "may": 5, "jun": 6, "jul": 7,
          "ago": 8, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dic": 12, "dec": 12}
    # 1) Texto visible "Publicado el 18 Sep 2026" (con etiquetas o &nbsp; en el medio)
    plano = re.sub(r"<[^>]+>", " ", html).replace("&nbsp;", " ")
    plano = re.sub(r"\s+", " ", plano)
    mm = re.search(r"Publicado el (\d{1,2}) (\w+)\.? (\d{4})", plano)
    if mm and mm.group(2).lower()[:3] in ab:
        return dt.date(int(mm.group(3)), ab[mm.group(2).lower()[:3]], int(mm.group(1))), url
    # 2) Metadato estándar de WordPress: article:published_time
    mm = re.search(r'article:published_time"\s+content="(\d{4}-\d{2}-\d{2})', html)
    if mm:
        return dt.date.fromisoformat(mm.group(1)), url
    raise IOError(f"{url}: sin fecha de publicación legible")


def m_archivo(http: Http, ev: dict, p: dict):
    f = d(ev["fecha"])
    y, m = mes_ref(f, 1)
    url = p["url"].format(mm_pub=f"{f.month:02d}", yy_pub=f"{f.year % 100:02d}",
                          yyyy_ref=y, mm_ref=f"{m:02d}")
    st, _ = http.get(url, head=True)
    if st == 200:
        # El archivo existe: el dato salió. La fecha exacta no la da el archivo,
        # así que se toma la fecha programada (o hoy, si se detecta después).
        return min(f, HOY), url
    if st in (404, 410):
        return None, None                  # el archivo todavía no existe: no salió
    raise IOError(f"{url}: HTTP {st}")     # 403 u otro: bloqueo o error, no es evidencia de nada


def m_rss(http: Http, ev: dict, p: dict):
    st, xml = http.get(p["url"])
    if st != 200:
        raise IOError(f"RSS HTTP {st}")
    f = d(ev["fecha"])
    y, m = mes_ref(f, p.get("lag", 1))
    mes = MESES[m - 1]
    for it in re.findall(r"<item>(.*?)</item>", xml, re.S):
        t = re.search(r"<title>(.*?)</title>", it, re.S)
        pd = re.search(r"<pubDate>(.*?)</pubDate>", it, re.S)
        ln = re.search(r"<link>(.*?)</link>", it, re.S)
        if not (t and pd):
            continue
        titulo = re.sub(r"<!\[CDATA\[|\]\]>", "", t.group(1)).lower()
        if p["clave"] in titulo and mes in titulo:
            fecha = parsedate_to_datetime(pd.group(1).strip()).astimezone(TZ).date()
            if abs((fecha - f).days) <= VENTANA_DIAS:
                return fecha, ln.group(1).strip() if ln else p["url"]
    return None, None


def m_pagina_fecha(http: Http, ev: dict, p: dict):
    st, html = http.get(p["url"])
    if st != 200:
        raise IOError(f"página HTTP {st}")
    m = re.search(r"Buenos Aires,\s*(\d{1,2}) de (\w+) de (\d{4})", html)
    if not m or m.group(2).lower() not in MESES:
        return None, None
    fecha = dt.date(int(m.group(3)), MESES.index(m.group(2).lower()) + 1, int(m.group(1)))
    if abs((fecha - d(ev["fecha"])).days) <= VENTANA_DIAS:
        return fecha, p["url"]
    return None, None


def m_prensa(http: Http, ev: dict, p: dict):
    """Google News con el filtro validado en el backtest:
    título que nombra el mes del dato, con una cifra, sin notas previas,
    y el primer día con al menos 2 notas (o 1 si es la única evidencia y ya pasaron 2 días)."""
    f = d(ev["fecha"])
    y, m = mes_ref(f, p.get("lag", 1))
    mes = p.get("clave_titulo") or MESES[m - 1]
    a, b = f - dt.timedelta(days=4), min(HOY, f + dt.timedelta(days=VENTANA_DIAS)) + dt.timedelta(days=1)
    url = ("https://news.google.com/rss/search?q=" + quote_plus(p["q"]) +
           f"+after:{a.isoformat()}+before:{b.isoformat()}&hl=es-419&gl=AR&ceid=AR:es-419")
    st, xml = http.get(url)
    if st != 200:
        raise IOError(f"Google News HTTP {st}")
    dias: dict = {}
    links: dict = {}
    for it in re.findall(r"<item>(.*?)</item>", xml, re.S):
        t = re.search(r"<title>(.*?)</title>", it, re.S)
        pd = re.search(r"<pubDate>(.*?)</pubDate>", it, re.S)
        ln = re.search(r"<link>(.*?)</link>", it, re.S)
        if not (t and pd):
            continue
        tit = re.sub(r"<!\[CDATA\[|\]\]>", "", t.group(1)).lower()
        if mes not in tit or not re.search(r"\d", tit) or EXCLUIR_PREVIAS.search(tit):
            continue
        fecha = parsedate_to_datetime(pd.group(1).strip()).astimezone(TZ).date()
        if fecha < f - dt.timedelta(days=1):   # nunca confirmar antes de la fecha prevista - 1
            continue
        dias[fecha] = dias.get(fecha, 0) + 1
        links.setdefault(fecha, ln.group(1).strip() if ln else url)
    for fecha in sorted(dias):
        if dias[fecha] >= 2:
            return fecha, links[fecha]
    if dias and (HOY - min(dias)).days >= 2:
        fecha = min(dias)
        return fecha, links[fecha]
    return None, None


METODOS = {"bcra_listado": m_bcra_listado, "bcra_publicacion": m_bcra_publicacion, "archivo": m_archivo, "rss": m_rss,
           "pagina_fecha": m_pagina_fecha, "prensa": m_prensa}


# ======================================================================================
# Núcleo
# ======================================================================================
HOY = dt.datetime.now(TZ).date()


def clave(ev: dict) -> str:
    return f"{ev['indicador']}|{ev['fecha']}|{ev.get('periodo') or ''}"


def confirmar(http: Http, eventos: list, estado: dict, salud: dict, log: list) -> dict:
    cambios = {"confirmados": [], "demorados": [], "sin_verificar": [], "sin_metodo": 0}
    fallidos, exitosos = set(), set()
    desde = HOY - dt.timedelta(days=VENTANA_DIAS)
    for ev in eventos:
        if not ev.get("fecha"):
            continue
        f = d(ev["fecha"])
        if f > HOY or f < desde:
            continue
        k = clave(ev)
        e = estado.setdefault(k, {"estado": "programado"})
        if e["estado"] == "confirmado":
            continue                       # un estado confirmado nunca se toca
        cadena = CONF.get(ev["indicador"])
        if not cadena:
            cambios["sin_metodo"] += 1
            e["estado"] = e.get("estado") if e.get("estado") != "programado" else "sin_metodo"
            continue
        consultado_ok = False
        for nombre, params in cadena:
            try:
                fecha, url = METODOS[nombre](http, ev, params)
                exitosos.add(nombre)
                consultado_ok = True
            except Exception as ex:
                fallidos.add(nombre)
                log.append(f"[fallo] {ev['indicador']} {nombre}: {str(ex)[:160]}")
                continue                   # pasa al siguiente método de la cadena
            if fecha:
                e.update(estado="confirmado", fecha_real=fecha.isoformat(), evidencia=url,
                         metodo=nombre, confirmado_el=HOY.isoformat(),
                         desvio_dias=(fecha - f).days)
                cambios["confirmados"].append((ev, e))
                break
        if e["estado"] == "confirmado":
            continue
        if not consultado_ok:
            # Ninguna fuente respondió: no se sabe si salió o no. No se lo acusa de demorado.
            if (HOY - f).days > TOLERANCIA_DIAS:
                e["estado"] = "sin_verificar"
                cambios["sin_verificar"].append(ev)
        elif (HOY - f).days > TOLERANCIA_DIAS and e["estado"] != "demorado":
            e["estado"] = "demorado"
            cambios["demorados"].append(ev)
    # Salud por corrida: un método suma a lo sumo 1 fallo por corrida y vuelve a 0 si funcionó alguna vez.
    for m in fallidos - exitosos:
        salud[m] = salud.get(m, 0) + 1
    for m in exitosos:
        salud[m] = 0
    return cambios


def vigilar_calendarios(http: Http, estado_cal: dict, log: list) -> list:
    avisos = []
    for nombre, url in CALENDARIOS.items():
        url = url.format(yyyy=HOY.year, yyyy1=HOY.year + 1)
        try:
            st, texto = http.get(url)
        except Exception as ex:
            log.append(f"[fallo] calendario {nombre}: {str(ex)[:160]}")
            continue
        if st != 200:
            if nombre == "indec_1sem_prox":
                continue                   # todavía no publicado: es lo esperable
            log.append(f"[aviso] calendario {nombre}: HTTP {st}")
            continue
        if url.endswith(".pdf") and not texto.lstrip().startswith("%PDF"):
            # El servidor respondió 200 pero con una página HTML: el PDF no existe todavía.
            continue
        h = hashlib.sha256(texto.encode("utf-8", "ignore")).hexdigest()
        previo = estado_cal.get(nombre)
        if previo is None:
            estado_cal[nombre] = h
            if nombre == "indec_1sem_prox":
                avisos.append(f"INDEC publicó el calendario del primer semestre {HOY.year + 1}: hay que cargarlo.")
        elif previo != h:
            estado_cal[nombre] = h
            avisos.append(f"Cambió el calendario oficial «{nombre}». Revisar reprogramaciones: {url}")
    return avisos


def reporte(cambios: dict, avisos: list, salud: dict, eventos: list, estado: dict) -> str:
    lin = [f"# Reporte del {HOY.isoformat()}", ""]
    if cambios["confirmados"]:
        lin.append("## Confirmados")
        for ev, e in cambios["confirmados"]:
            extra = "" if e["desvio_dias"] == 0 else f" (desvío {e['desvio_dias']:+d} días)"
            lin.append(f"- {ev['fecha']} {ev['titulo']}: {e['metodo']}{extra}")
        lin.append("")
    if cambios["demorados"]:
        lin.append("## Sin evidencia pasada la tolerancia")
        for ev in cambios["demorados"]:
            lin.append(f"- {ev['fecha']} {ev['titulo']}")
        lin.append("")
    if cambios["sin_verificar"]:
        lin.append("## Sin verificar (ninguna fuente respondió)")
        for ev in cambios["sin_verificar"]:
            lin.append(f"- {ev['fecha']} {ev['titulo']}")
        lin.append("")
    caidos = [m for m, n in salud.items() if n >= FALLOS_PARA_ALERTA]
    if caidos:
        lin.append("## Métodos caídos (3 corridas o más fallando)")
        lin += [f"- {m}" for m in caidos] + [""]
    if avisos:
        lin.append("## Calendarios oficiales")
        lin += [f"- {a}" for a in avisos] + [""]
    hoy_ev = [e for e in eventos if e.get("fecha") == HOY.isoformat()]
    if hoy_ev:
        lin.append("## Hoy se publica")
        lin += [f"- {e.get('hora') or ''} {e['titulo']}".replace("  ", " ") for e in hoy_ev]
    return "\n".join(lin).strip() + "\n"


def telegram(texto: str, log: list) -> None:
    tok, chat = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (tok and chat and requests):
        return
    try:
        requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                      data={"chat_id": chat, "text": texto[:3900]}, timeout=20)
    except Exception as ex:
        log.append(f"[fallo] telegram: {ex}")


def main(argv=None) -> int:
    global HOY
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--fecha")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if a.fecha:
        HOY = d(a.fecha)
    if requests is None:
        print("Falta la librería requests (pip install requests)")
        return 1

    eventos = cargar("eventos.json", [])
    estado = cargar("estado.json", {"eventos": {}, "salud": {}, "calendarios": {}})
    log: list = []
    http = Http()

    cambios = confirmar(http, eventos, estado["eventos"], estado["salud"], log)
    avisos = vigilar_calendarios(http, estado["calendarios"], log)
    rep = reporte(cambios, avisos, estado["salud"], eventos, estado["eventos"])
    estado["ultima_corrida"] = dt.datetime.now(TZ).isoformat(timespec="seconds")

    print(rep)
    if log:
        print("\n".join(log))
    if a.dry_run:
        print("\n[dry-run] no se escribió nada")
        return 0
    guardar_atomico("estado.json", estado)
    with open(os.path.join(DATA, "reporte.md"), "w", encoding="utf-8") as f:
        f.write(rep + ("\n## Log técnico\n" + "\n".join(log) + "\n" if log else ""))
    telegram(rep, log)
    return 0


# ======================================================================================
# Autotest offline: simula cada fuente y verifica el comportamiento esperado.
# ======================================================================================
def selftest() -> int:
    global HOY
    HOY = dt.date(2026, 9, 24)
    ok = 0
    fallos = []

    def check(nombre, cond):
        nonlocal ok
        if cond:
            ok += 1
        else:
            fallos.append(nombre)

    bcra_html = ("<table><tr><td>Relevamiento de Expectativas de Mercado (REM)</td><td>Mensual</td>"
                 "<td>Agosto 2026</td><td>04 sep 2026</td></tr>"
                 "<tr><td>Informe Monetario Mensual</td><td>Mensual</td><td>Agosto 2026</td><td>07 sep 2026</td></tr></table>")
    rss_cca = ("<rss><channel><item><title>En agosto se vendieron 155.246 autos usados</title>"
               "<link>https://cca.org.ar/x</link><pubDate>Wed, 02 Sep 2026 13:00:00 +0000</pubDate></item></channel></rss>")
    news = ("<rss><channel>"
            "<item><title>Qué esperan las consultoras para la inflación de agosto 1,8%</title><pubDate>Mon, 07 Sep 2026 12:00:00 +0000</pubDate></item>"
            "<item><title>La inflación de agosto fue 1,7%</title><link>https://a</link><pubDate>Thu, 10 Sep 2026 19:10:00 +0000</pubDate></item>"
            "<item><title>Agosto: la inflación marcó 1,7% según INDEC</title><link>https://b</link><pubDate>Thu, 10 Sep 2026 19:30:00 +0000</pubDate></item>"
            "</channel></rss>")
    http = Http(fake={
        "https://www.bcra.gob.ar/ultimos-informes/": (200, bcra_html),
        "https://cca.org.ar/feed/": (200, rss_cca),
        "https://news.google.com/": (200, news),
        "https://adefa.org.ar/upload/estadisticas/resumen-2026-08-es.pdf": (200, ""),
        "https://www.grupoconstruya.com.ar/": (200, "<p>Buenos Aires, 9 de septiembre de 2026.- En agosto el Índice</p>"),
    })
    # 1. BCRA: fecha exacta desde el listado
    f, u = m_bcra_listado(http, {"fecha": "2026-09-04"}, {"nombre": "Relevamiento de Expectativas de Mercado"})
    check("bcra_rem", f == dt.date(2026, 9, 4))
    # 2. RSS con mes y palabra clave
    f, u = m_rss(http, {"fecha": "2026-09-02"}, {"url": "https://cca.org.ar/feed/", "clave": "usados", "lag": 1})
    check("rss_cca", f == dt.date(2026, 9, 2))
    # 3. Prensa: ignora la nota previa del 7-sep y confirma el 10-sep (2 notas)
    f, u = m_prensa(http, {"fecha": "2026-09-10"}, {"q": "INDEC inflación", "lag": 1})
    check("prensa_ipc_sin_previa", f == dt.date(2026, 9, 10))
    # 4. Archivo previsible
    f, u = m_archivo(http, {"fecha": "2026-09-03"}, {"url": "https://adefa.org.ar/upload/estadisticas/resumen-{yyyy_ref}-{mm_ref}-es.pdf"})
    check("archivo_adefa", f == dt.date(2026, 9, 3))
    # 5. Página con fecha
    f, u = m_pagina_fecha(http, {"fecha": "2026-09-09"}, {"url": "https://www.grupoconstruya.com.ar/servicios/indice_construya"})
    check("pagina_construya", f == dt.date(2026, 9, 9))
    # 6. Cadena: primer método cae (excepción), el segundo confirma; el estado avanza y la salud registra el fallo
    class HttpRoto(Http):
        def get(self, url, head=False):
            if "adefa.org.ar" in url:
                raise IOError("timeout simulado")
            return super().get(url, head)
    http2 = HttpRoto(fake=http.fake)
    HOY = dt.date(2026, 9, 14)
    ev = [{"fecha": "2026-09-10", "indicador": "indec.ipc", "titulo": "IPC", "periodo": "2026-08"},
          {"fecha": "2026-09-03", "indicador": "priv.adefa", "titulo": "ADEFA", "periodo": None}]
    CONF["priv.adefa"] = [("archivo", {"url": "https://adefa.org.ar/upload/estadisticas/resumen-{yyyy_ref}-{mm_ref}-es.pdf"}),
                          ("prensa", {"q": "ADEFA", "lag": 1})]
    est, salud, log = {}, {}, []
    confirmar(http2, ev, est, salud, log)
    check("cadena_respaldo", est[clave(ev[1])]["estado"] in ("confirmado", "demorado"))
    check("salud_registra_fallo", salud.get("archivo", 0) >= 1 or any("adefa" in l for l in log))
    # 7. Un confirmado nunca se degrada aunque todo falle después
    class HttpMuerto(Http):
        def get(self, url, head=False):
            raise IOError("sin red")
    confirmar(HttpMuerto(), ev, est, salud, log)
    check("no_degrada", est[clave(ev[0])]["estado"] == "confirmado")
    # 8. Sin red y sin confirmar: sigue "programado" dentro de la tolerancia y pasa a "demorado" después
    HOY = dt.date(2026, 9, 24)
    est2 = {}
    confirmar(HttpMuerto(), [{"fecha": "2026-09-23", "indicador": "indec.emae", "titulo": "EMAE", "periodo": "x"}], est2, {}, [])
    check("tolerancia", list(est2.values())[0]["estado"] == "programado")
    HOY = dt.date(2026, 9, 28)
    confirmar(HttpMuerto(), [{"fecha": "2026-09-23", "indicador": "indec.emae", "titulo": "EMAE", "periodo": "x"}], est2, {}, [])
    check("sin_red_no_acusa_demora", list(est2.values())[0]["estado"] == "sin_verificar")
    # 10. Con fuente respondiendo pero sin evidencia: demorado
    http3 = Http(fake={"https://news.google.com/": (200, "<rss><channel></channel></rss>")})
    est3 = {}
    confirmar(http3, [{"fecha": "2026-09-23", "indicador": "indec.emae", "titulo": "EMAE", "periodo": "x"}], est3, {}, [])
    check("demorado_con_fuente_viva", list(est3.values())[0]["estado"] == "demorado")
    # 11. Salud: 5 fallos en una misma corrida cuentan como 1
    sal = {}
    confirmar(HttpMuerto(), [{"fecha": "2026-09-2%d" % i, "indicador": "indec.emae", "titulo": "E", "periodo": str(i)} for i in range(1, 6)], {}, sal, [])
    check("salud_por_corrida", sal.get("prensa") == 1)
    # 9. Escritura atómica
    global DATA
    viejo = DATA
    DATA = tempfile.mkdtemp()
    guardar_atomico("x.json", {"a": 1})
    check("atomica", cargar("x.json", None) == {"a": 1} and not [p for p in os.listdir(DATA) if p.endswith(".tmp")])
    DATA = viejo

    # 12. Listado BCRA sin fechas (tabla por JavaScript): debe ser error, no "no salió"
    try:
        m_bcra_listado(Http(fake={"https://www.bcra.gob.ar/ultimos-informes/": (200, "<div id='tabla'></div>")}),
                       {"fecha": "2026-09-18"}, {"nombre": "Informe sobre Bancos"})
        check("listado_js_es_error", False)
    except IOError:
        check("listado_js_es_error", True)
    # 13. Página propia del BCRA con 'Publicado el'
    hb = Http(fake={"https://www.bcra.gob.ar/publicaciones/informe-sobre-bancos-julio-de-2026/":
                    (200, "<h1>Informe sobre Bancos</h1><p>Publicado el 18 Sep 2026</p>")})
    f, u = m_bcra_publicacion(hb, {"fecha": "2026-09-18"}, {"slug": "informe-sobre-bancos-{mes}-de-{yyyy}", "lag": 2, "verificado": True})
    check("bcra_publicacion", f == dt.date(2026, 9, 18))
    hb2 = Http(fake={"https://www.bcra.gob.ar/publicaciones/boletin-estadistico-septiembre-de-2026/":
                     (200, '<meta property="article:published_time" content="2026-09-14T17:05:00-03:00" /><p>Publicado el <span>14</span> Sep 2026</p>')})
    f, u = m_bcra_publicacion(hb2, {"fecha": "2026-09-14"}, {"slug": "boletin-estadistico-{mes}-de-{yyyy}", "lag": 0, "verificado": True})
    check("bcra_publicacion_con_etiquetas", f == dt.date(2026, 9, 14))
    # 14. Calendario: una página HTML con status 200 en lugar del PDF no dispara aviso
    cal = {}
    HOY = dt.date(2026, 9, 24)
    av = vigilar_calendarios(Http(fake={"https://www.indec.gob.ar/ftp/cuadros/publicaciones/calendario_1sem2027.pdf": (200, "<html>no encontrado</html>")}), cal, [])
    check("soft404_calendario", not any("2027" in a for a in av))

    total = ok + len(fallos)
    print(f"Autotest: {ok}/{total} OK" + (f" | fallaron: {', '.join(fallos)}" if fallos else ""))
    return 0 if not fallos else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # Un error acá es un bug del script, no de una fuente: que la Action falle y avise por mail.
        traceback.print_exc()
        sys.exit(2)
