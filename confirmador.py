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
    "bcra.cambiario":        [("bcra_publicacion", {"slug": "informe-de-evolucion-del-mercado-de-cambios-y-balance-cambiario-{mes}-de-{yyyy}", "lag": 1, "verificado": True}),
                              ("xlsx_modificado", {"url": "https://www.bcra.gob.ar/archivos/Pdfs/PublicacionesEstadisticas/informes/anexo-estadistico-mercado-cambios-balance-cambiario.xlsx"}),
                              ("bcra_listado", {"nombre": "Mercado de Cambios y Balance Cambiario"}),
                              ("prensa", {"q": '"balance cambiario" BCRA', "lag": 1})],
    "bcra.pagos_minoristas": [("bcra_publicacion", {"slug": "informe-de-pagos-minoristas-{mes}-de-{yyyy}", "lag": 1, "verificado": False}),
                              ("bcra_listado", {"nombre": "Informe de Pagos Minoristas"})],
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
    # Privados con sitio propio verificado el 27-sep-2026 (prensa de respaldo)
    "priv.acara": [("pagina_periodo", {"url": "https://api.acara.org.ar/api/v1/views/index", "patron": "patentados durante {mes} de {yyyy}", "lag": 1}),
                   ("prensa", {"q": "ACARA patentamientos", "lag": 1})],
    "priv.ciara_cec": [("pagina_lista", {"url": "https://www.ciaracec.com.ar/ciara/Informaci%C3%B3n/Liquidaci%C3%B3n%20Mensual",
                                         "patron": r"Divisas\s*<strong>(?P<d>\d{2})-(?P<m>[a-z]{3})-(?P<y>\d{4})", "tol": 3}),
                       ("prensa", {"q": "CIARA CEC liquidación", "lag": 1})],
    "priv.fiel_ipi": [("pagina_lista", {"url": "https://www.fiel.org/", "patron": r"N\S{1,8}mero \d+, (?P<d>\d{1,2}) de (?P<m>[a-z]+) de (?P<y>\d{4})", "tol": 3}),
                      ("pagina_periodo", {"url": "https://www.fiel.org/", "patron": r"Var\. Anual {mes} {yyyy}", "lag": 1})],
    "priv.cemento": [("pagina_periodo", {"url": "https://www.afcp.org.ar/despacho-mensual", "patron": "Despachos de cemento en el mes de {mes} de {yyyy}", "lag": 1})],
    "priv.usda_wasde": [("archivo_lm", {"url": "https://www.usda.gov/oce/commodity/wasde/wasde{mm_ref}{yy_ref}.pdf", "lag": 0, "exigir_lm": True})],
    "priv.came_minoristas": [("prensa", {"q": 'CAME "ventas minoristas"', "lag": 1})],
    "priv.adimra": [("prensa", {"q": "ADIMRA metalúrgica", "lag": 1})],
    "priv.scentia": [("prensa", {"q": 'Scentia "consumo masivo"', "lag": 1})],
    "priv.uia_ceu": [("prensa", {"q": 'UIA "actividad industrial"', "lag": 1})],
    "priv.fractura_vm": [("prensa", {"q": '"etapas de fractura"', "lag": 1})],
    "priv.utdt_icc": [("pagina_periodo", {"url": "https://www.utdt.edu/listado_contenidos.php?id_item_menu=4982", "patron": r'\(ICC\)</h4>\s*<div class="fecha">{mes} {yyyy}', "lag": 0}),
                      ("prensa", {"q": '"confianza del consumidor" "Di Tella"', "lag": 0})],
    "priv.utdt_ei": [("pagina_periodo", {"url": "https://www.utdt.edu/listado_contenidos.php?id_item_menu=4982", "patron": r'\(EI\)</h4>\s*<div class="fecha">{mes} {yyyy}', "lag": 0})],
    "priv.utdt_icg": [("pagina_periodo", {"url": "https://www.utdt.edu/ver_contenido.php?id_contenido=1351&id_item_menu=2970", "patron": r'\(ICG\)</h4>\s*<div class="fecha">{mes} {yyyy}', "lag": 0}),
                      ("prensa", {"q": '"confianza en el gobierno" "Di Tella"', "lag": 0})],
    # Estado nacional: fuente oficial primero (verificado 27-sep-2026), prensa de respaldo
    "priv.resultado_fiscal": [("gob_noticias", {"url": "https://www.argentina.gob.ar/economia/sechacienda/noticias", "clave": "Sector Público Nacional"}),
                              ("prensa", {"q": '"Sector Público Nacional" superávit OR déficit', "lag": 1})],
    "priv.recaudacion_arca": [("archivo_lm", {"url": "https://www.arca.gob.ar/institucional/documentos/ARCA-Recaudacion-{mm_ref}{yyyy_ref}.pdf", "lag": 1}),
                              ("prensa", {"q": "recaudación ARCA interanual", "lag": 1})],
    "priv.licitaciones_tesoro": [("gob_noticias", {"url": "https://www.argentina.gob.ar/economia/finanzas/noticias", "clave": r"^Resultado de la licitaci[oó]n(?!.*conversi)", "tol": 2})],
    "priv.llamado_tesoro": [("gob_noticias", {"url": "https://www.argentina.gob.ar/economia/finanzas/noticias", "clave": r"^Llamado a licitaci[oó]n", "tol": 2})],
    "priv.sipa": [("archivo_lm", {"url": "https://www.argentina.gob.ar/sites/default/files/trabajoregistrado_{yy_ref}{mm_ref}_estadisticas.xlsx", "lag": 3})],
    "priv.ripte_eil": [("archivo_lm", {"url": "https://www.argentina.gob.ar/sites/default/files/ripte_{mes_ref}_{yyyy_ref}-mdch.pdf", "lag": 2}),
                       ("prensa", {"q": "RIPTE remuneración imponible", "lag": 2})],
}

# INDEC: prefijo del PDF del informe técnico (verificado contra el listado el 27-sep-2026).
# Va primero en la cadena de cada indicador; los métodos previos quedan de respaldo.
INDEC_PDF = {
    "indec.ipc": "ipc", "indec.cba_cbt": "canasta", "indec.canasta_crianza": "canasta_crianza",
    "indec.sipm": "ipm", "indec.icc": "icc", "indec.ica": "ica", "indec.emae": "emae",
    "indec.ipi_manuf": "ipi_manufacturero", "indec.isac": "isac", "indec.ipi_minero": "ipi_minero",
    "indec.ipi_pesquero": "ipi_pesquero", "indec.issp": "issp", "indec.ucii": "capacidad",
    "indec.supermercados": "super", "indec.mayoristas": "autoservicios_mayoristas", "indec.shoppings": "com",
    "indec.etn_super": "etn_super_mayoristas", "indec.etn_industria": "etn_industria_manufacturera",
    "indec.salarios": "salarios", "indec.turismo_int": "eti", "indec.dotacion_apn": "dotacion_personal_apn",
    "indec.eoh": "eoh", "indec.pib_avance": "pib", "indec.eph_mercado_trabajo": "mercado_trabajo_eph",
    "indec.bdp": "bal", "indec.cgi": "cgi", "indec.eph_distribucion": "ingresos",
    "indec.precios_cant_comex": "ipcext", "indec.aft_stats": "i_argent", "indec.patentamientos": "patentamientos",
    "indec.accesos_internet": "internet", "indec.farmaceutica": "farm", "indec.energia_ind": "indicadores_energeticos",
    "indec.maquinaria_agricola": "maq_agricola", "indec.electrodomesticos": "electro", "indec.pobreza": "eph_pobreza",
    "indec.eph_condiciones_vida": "eph_indicadores_hogares", "indec.complejos_exportadores": "complejos",
    "indec.opex": "opex", "indec.enge": "enge", "indec.eph_tu_tasas": "eph_total_urbano",
    "indec.eph_tu_distribucion": "eph_total_urbano_ingresos", "indec.eph_informalidad": "informalidad_laboral_eph",
    "indec.csc_cultura": "csc", "indec.cuenta_energia": "cuenta_energia", "indec.remuneracion_sexo_edad": "cgi_sexo_edad",
    "indec.ingreso_ahorro_nacional": "ingreso_ahorro_nac", "indec.csi_gobierno": "cuentas_sectores_institucionales",
    "indec.csi_financieras": "cuentas_sociedades_financieras", "indec.csi_rdm": "cuentas_resto_del_mundo",
    "indec.fbkf_gobierno": "formacion_capital_fijo", "indec.tic_eph": "mautic",
    "indec.cuenta_emisiones": "cuenta_emisiones_aire", "indec.cuenta_recursos_energ": "cuenta_re",
}
for _ind, _pref in INDEC_PDF.items():
    CONF[_ind] = [("indec_informes", {"pref": _pref})] + CONF.get(_ind, [])

# Calendarios oficiales vigilados: si cambian, se avisa para recargarlos.
CALENDARIOS = {
    "indec_2sem": "https://www.indec.gob.ar/ftp/cuadros/publicaciones/calendario_2sem{yyyy}.pdf",
    "indec_1sem_prox": "https://www.indec.gob.ar/ftp/cuadros/publicaciones/calendario_1sem{yyyy1}.pdf",
    "bcra_fechas": "https://www.bcra.gob.ar/calendario-de-informes/",
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
                    out = (r.status_code, r.headers.get("Last-Modified", ""))  # HEAD: el texto es la fecha de carga
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
BCRA_API = "https://www.bcra.gob.ar/wp-json/bcra/v1/publicaciones?category=informes,estadisticas&lang=es&action=total"


def m_bcra_listado(http: Http, ev: dict, p: dict):
    """Listado completo de informes del BCRA. La página 'Últimos informes' arma su tabla con JavaScript
    a partir de esta API (JSON con título, período y fecha de cada publicación, ~2.200 filas)."""
    st, txt = http.get(BCRA_API)
    if st != 200:
        raise IOError(f"API BCRA HTTP {st}")
    try:
        pubs = json.loads(txt)["data"]["publicaciones"]
    except Exception as ex:
        raise IOError(f"API BCRA: respuesta ilegible ({ex})")
    if not pubs:
        raise IOError("API BCRA: listado vacío")
    ab = {"ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6, "jul": 7,
          "ago": 8, "sep": 9, "oct": 10, "nov": 11, "dic": 12}
    objetivo = d(ev["fecha"])
    nombre = p["nombre"].lower()
    for pub in pubs:
        if nombre not in (pub.get("titulo") or "").lower():
            continue
        m = re.match(r"(\d{1,2}) ([a-z]{3})\w* (\d{4})", (pub.get("fecha") or "").lower())
        if not m or m.group(2) not in ab:
            continue
        f = dt.date(int(m.group(3)), ab[m.group(2)], int(m.group(1)))
        if abs((f - objetivo).days) <= VENTANA_DIAS:
            return f, pub.get("url") or "https://www.bcra.gob.ar/ultimos-informes/"
    return None, None


INDEC_INFORMES = "https://www.indec.gob.ar/Institucional/Indec/InformesTecnicos"


def m_indec_informes(http: Http, ev: dict, p: dict):
    """Listado completo de informes técnicos de INDEC (~4.000 filas: fecha DD/MM/AAAA + PDF).
    Cada PDF se llama <prefijo>_<período><hash>.pdf (ej. ipc_09_26A1BE2DC4CD.pdf); el prefijo
    identifica al indicador. Se exige prefijo exacto seguido de '_<dígito>' para no confundir
    'canasta' con 'canasta_crianza' ni 'cgi' con 'cgi_sexo_edad'."""
    st, html = http.get(INDEC_INFORMES)
    if st != 200:
        raise IOError(f"INDEC informes HTTP {st}")
    filas = []
    ultima = None
    for m in re.finditer(r"\b(\d{2})/(\d{2})/(20\d{2})\b|informesdeprensa/([A-Za-z0-9_\-]+)\.pdf", html):
        if m.group(1):
            try:
                ultima = dt.date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
            except ValueError:
                ultima = None
        elif ultima:
            filas.append((ultima, m.group(4)))
    if len(filas) < 100:
        raise IOError(f"INDEC informes: sólo {len(filas)} filas legibles (¿cambió la página?)")
    objetivo = d(ev["fecha"])
    patron = re.compile(re.escape(p["pref"]) + r"_\d", re.I)
    for fecha, archivo in filas:
        if patron.match(archivo) and abs((fecha - objetivo).days) <= VENTANA_DIAS:
            return fecha, f"https://www.indec.gob.ar/uploads/informesdeprensa/{archivo}.pdf"
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


def m_xlsx_modificado(http: Http, ev: dict, p: dict):
    """Planilla de datos que el organismo reemplaza en cada publicación (misma dirección siempre).
    La fecha de modificación interna del XLSX (docProps/core.xml) es la hora exacta de la carga."""
    import io, zipfile
    if requests is None and http.fake is None:
        raise IOError("sin requests")
    if http.fake is not None:
        st, contenido = http.get(p["url"])
        datos = contenido if isinstance(contenido, bytes) else contenido.encode("latin-1")
    else:
        r = None
        for intento in range(3):
            try:
                r = requests.get(p["url"], headers=UA, timeout=90)
                break
            except Exception as ex:
                if intento == 2:
                    raise IOError(f"{p['url']}: {ex}")
                time.sleep(3)
        st, datos = r.status_code, r.content
    if st != 200:
        raise IOError(f"{p['url']}: HTTP {st}")
    try:
        core = zipfile.ZipFile(io.BytesIO(datos)).read("docProps/core.xml").decode("utf-8", "ignore")
    except Exception as ex:
        raise IOError(f"XLSX ilegible: {ex}")
    m = re.search(r"<dcterms:modified[^>]*>([^<]+)</dcterms:modified>", core)
    if not m:
        raise IOError("XLSX sin fecha de modificación")
    fecha = dt.datetime.fromisoformat(m.group(1).replace("Z", "+00:00")).astimezone(TZ).date()
    f = d(ev["fecha"])
    if f - dt.timedelta(days=1) <= fecha <= f + dt.timedelta(days=VENTANA_DIAS):
        return fecha, p["url"]
    return None, None                      # la planilla todavía es la del mes anterior


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


def m_archivo_lm(http: Http, ev: dict, p: dict):
    """Archivo con nombre previsible por período del dato (ej. trabajoregistrado_2606_estadisticas.xlsx).
    Existe = salió. La fecha sale del Last-Modified del servidor si cae cerca de la programada;
    si el archivo se volvió a subir después (pasa en ARCA), se toma la programada: la existencia ya prueba que salió."""
    f = d(ev["fecha"])
    y, m = mes_ref(f, p.get("lag", 1))
    url = p["url"].format(yyyy_ref=y, yy_ref=f"{y % 100:02d}", mm_ref=f"{m:02d}", mes_ref=MESES[m - 1])
    st, lm = http.get(url, head=True)
    exigir = p.get("exigir_lm", False)   # cuando el nombre se repite entre años (USDA reusa wasdeMMYY)
    if st in (404, 410):
        return None, None
    if st != 200:
        raise IOError(f"{url}: HTTP {st}")
    try:
        fecha = parsedate_to_datetime(lm).astimezone(TZ).date() if lm else None
    except Exception:
        fecha = None
    if fecha and f - dt.timedelta(days=3) <= fecha <= f + dt.timedelta(days=VENTANA_DIAS):
        return fecha, url
    if exigir:
        return None, None                  # el archivo existe pero es viejo: no es este
    return min(f, HOY), url


def m_gob_noticias(http: Http, ev: dict, p: dict):
    """Listado de noticias de un área de argentina.gob.ar (ej. /economia/finanzas/noticias).
    Cada tarjeta trae <time datetime='AAAA-MM-DD ...'> y el título en <h3>. Se busca un título
    que contenga la clave con fecha a no más de 'tol' días de la programada."""
    st, html = http.get(p["url"])
    if st != 200:
        raise IOError(f"{p['url']}: HTTP {st}")
    items = re.findall(r'<a href="(/noticias/[^"]+)"[^>]*>(?:(?!</a>).)*?<time datetime=\'(\d{4}-\d{2}-\d{2})[^\']*\'>'
                       r'[^<]*</time>\s*<h3>([^<]+)</h3>', html, re.S)
    if not items:
        raise IOError(f"{p['url']}: sin noticias legibles (¿cambió la página?)")
    f = d(ev["fecha"])
    tol = p.get("tol", VENTANA_DIAS)
    clave_re = re.compile(p["clave"], re.I)
    for link, fecha, titulo in items:
        fe = dt.date.fromisoformat(fecha)
        if clave_re.search(titulo.strip()) and abs((fe - f).days) <= tol:
            return fe, "https://www.argentina.gob.ar" + link
    return None, None


MES_ABR = {"ene": 1, "jan": 1, "feb": 2, "mar": 3, "abr": 4, "apr": 4, "may": 5, "jun": 6, "jul": 7,
           "ago": 8, "aug": 8, "sep": 9, "set": 9, "oct": 10, "nov": 11, "dic": 12, "dec": 12}


def m_pagina_lista(http: Http, ev: dict, p: dict):
    """Página propia con un listado de publicaciones fechadas (ej. CIARA: 'Liquidación de Divisas 01-SEP-2026').
    'patron' es una regex con grupos d, m, y (mes en número, abreviatura o nombre). Confirma si alguna
    fecha del listado cae a no más de 'tol' días de la programada."""
    st, html = http.get(p["url"])
    if st != 200:
        raise IOError(f"{p['url']}: HTTP {st}")
    fechas = []
    for m in re.finditer(p["patron"], html, re.I):
        mm = m.group("m")
        mes = int(mm) if mm.isdigit() else MES_ABR.get(mm.lower()[:3])
        try:
            fechas.append(dt.date(int(m.group("y")), mes, int(m.group("d"))))
        except (TypeError, ValueError):
            continue
    if not fechas:
        raise IOError(f"{p['url']}: listado sin fechas legibles (¿cambió la página?)")
    f = d(ev["fecha"])
    tol = p.get("tol", 3)
    for fe in sorted(fechas, key=lambda x: abs((x - f).days)):
        if abs((fe - f).days) <= tol and fe <= HOY:
            return fe, p["url"]
    return None, None


def m_pagina_periodo(http: Http, ev: dict, p: dict):
    """Página propia que muestra el último período publicado (ej. AFCP: 'Despachos de cemento en el mes de
    Agosto de 2026'). Si aparece el período esperado, salió. La página no da el día: se toma la fecha
    programada (o hoy, si se detecta después). En 'patron', {mes} y {yyyy} se reemplazan por el período."""
    st, html = http.get(p["url"])
    if st != 200:
        raise IOError(f"{p['url']}: HTTP {st}")
    f = d(ev["fecha"])
    y, m = mes_ref(f, p.get("lag", 1))
    patron = p["patron"].replace("{mes}", MESES[m - 1]).replace("{yyyy}", str(y))
    if re.search(patron, html, re.I):
        return min(f, HOY), p["url"]
    generico = p["patron"].replace("{mes}", "(?:" + "|".join(MESES) + ")").replace("{yyyy}", r"20\d{2}")
    if not re.search(generico, html, re.I):
        raise IOError(f"{p['url']}: no aparece ningún período (¿cambió la página?)")
    return None, None


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


METODOS = {"bcra_listado": m_bcra_listado, "indec_informes": m_indec_informes, "archivo_lm": m_archivo_lm, "gob_noticias": m_gob_noticias, "pagina_lista": m_pagina_lista, "pagina_periodo": m_pagina_periodo, "bcra_publicacion": m_bcra_publicacion, "xlsx_modificado": m_xlsx_modificado, "archivo": m_archivo, "rss": m_rss,
           "pagina_fecha": m_pagina_fecha, "prensa": m_prensa}


# ======================================================================================
# Núcleo
# ======================================================================================
HOY = dt.datetime.now(TZ).date()
AHORA = None   # momento real de la corrida; sólo el autotest lo fija (None = reloj)


def s_bcra_listado(http: Http) -> None:
    """Sondeo liviano: la API del BCRA responde y trae el listado."""
    st, txt = http.get(BCRA_API)
    if st != 200:
        raise IOError(f"API BCRA HTTP {st}")
    if len(json.loads(txt)["data"]["publicaciones"]) < 100:
        raise IOError("API BCRA: listado incompleto")


def s_indec_informes(http: Http) -> None:
    """Sondeo liviano: el listado de informes técnicos de INDEC responde con fechas."""
    st, html = http.get(INDEC_INFORMES)
    if st != 200:
        raise IOError(f"INDEC informes HTTP {st}")
    if len(re.findall(r"\b\d{2}/\d{2}/20\d{2}\b", html)) < 100:
        raise IOError("INDEC informes: listado incompleto")


# Los listados centrales se prueban en cada corrida aunque ese día no haya nada que confirmar,
# así la salud refleja el presente y no queda colgado un fallo viejo.
SONDEOS = {"bcra_listado": s_bcra_listado, "indec_informes": s_indec_informes}


def clave(ev: dict) -> str:
    return f"{ev['indicador']}|{ev['fecha']}|{ev.get('periodo') or ''}"


def confirmar(http: Http, eventos: list, estado: dict, salud: dict, log: list, sondear: bool = False) -> dict:
    cambios = {"confirmados": [], "demorados": [], "sin_verificar": [], "sin_metodo": 0}
    vis = AHORA or dt.datetime.now(TZ)
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
                         desvio_dias=(fecha - f).days, visto=vis.isoformat(timespec="minutes"))
                # Hora real: sólo tiene sentido si se detectó el mismo día en que salió. Es una cota:
                # el dato apareció entre la última consulta que respondió sin encontrarlo (ese mismo
                # día) y esta. Si la fuente no respondía antes, no se inventa el piso.
                if fecha == vis.date():
                    e["hora_detectada"] = vis.strftime("%H:%M")
                    try:
                        ne = dt.datetime.fromisoformat(e.get("no_estaba") or "").astimezone(TZ)
                    except ValueError:
                        ne = None
                    if ne and ne.date() == fecha and ne < vis:
                        e["hora_desde"] = ne.strftime("%H:%M")
                e.pop("no_estaba", None)
                cambios["confirmados"].append((ev, e))
                break
        if e["estado"] == "confirmado":
            continue
        if consultado_ok:
            e["no_estaba"] = vis.isoformat(timespec="minutes")   # una fuente respondió y todavía no estaba
        if not consultado_ok:
            # Ninguna fuente respondió: no se sabe si salió o no. No se lo acusa de demorado.
            if (HOY - f).days > TOLERANCIA_DIAS:
                e["estado"] = "sin_verificar"
                cambios["sin_verificar"].append(ev)
        elif (HOY - f).days > TOLERANCIA_DIAS and e["estado"] != "demorado":
            e["estado"] = "demorado"
            cambios["demorados"].append(ev)
    if sondear:
        for m, fn in SONDEOS.items():
            if m in exitosos or m in fallidos:
                continue
            try:
                fn(http)
                exitosos.add(m)
            except Exception as ex:
                fallidos.add(m)
                log.append(f"[sondeo] {m}: {str(ex)[:160]}")
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
        if not url.endswith(".pdf"):
            # Las páginas HTML cambian en cada carga (scripts, tokens de sesión): se compara sólo
            # el conjunto de fechas que publican. Si no hay fechas legibles, no se puede vigilar así.
            limpio = re.sub(r"<script.*?</script>|<style.*?</style>", " ", texto, flags=re.S | re.I)
            fechas = re.findall(r"\b\d{1,2}(?:/\d{1,2}/\d{2,4}| de [a-záéíóú]+(?: de \d{4})?)", limpio, re.I)
            if not fechas:
                continue
            texto = " ".join(sorted(set(f.lower() for f in fechas)))
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

    cambios = confirmar(http, eventos, estado["eventos"], estado["salud"], log, sondear=True)
    avisos = vigilar_calendarios(http, estado["calendarios"], log)
    rep = reporte(cambios, avisos, estado["salud"], eventos, estado["eventos"])
    ahora = dt.datetime.now(TZ)
    estado["ultima_corrida"] = ahora.isoformat(timespec="seconds")
    # Historial de corridas (unos 5 días): mide cuánto atrasa GitHub los horarios programados.
    estado["corridas"] = (estado.get("corridas", []) + [ahora.isoformat(timespec="minutes")])[-120:]

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

    bcra_json = json.dumps({"success": True, "data": {"publicaciones": [
        {"titulo": "Relevamiento de Expectativas de Mercado (REM)", "url": "https://www.bcra.gob.ar/publicaciones/rem/", "periodo": "Agosto 2026", "fecha": "04 sep 2026"},
        {"titulo": "Informe Monetario Mensual", "url": "https://www.bcra.gob.ar/publicaciones/imm/", "periodo": "Agosto 2026", "fecha": "07 sep 2026"},
        {"titulo": "Relevamiento de Expectativas de Mercado (REM)", "url": "x", "periodo": "Julio 2026", "fecha": "05 ago 2026"}]}})
    rss_cca = ("<rss><channel><item><title>En agosto se vendieron 155.246 autos usados</title>"
               "<link>https://cca.org.ar/x</link><pubDate>Wed, 02 Sep 2026 13:00:00 +0000</pubDate></item></channel></rss>")
    news = ("<rss><channel>"
            "<item><title>Qué esperan las consultoras para la inflación de agosto 1,8%</title><pubDate>Mon, 07 Sep 2026 12:00:00 +0000</pubDate></item>"
            "<item><title>La inflación de agosto fue 1,7%</title><link>https://a</link><pubDate>Thu, 10 Sep 2026 19:10:00 +0000</pubDate></item>"
            "<item><title>Agosto: la inflación marcó 1,7% según INDEC</title><link>https://b</link><pubDate>Thu, 10 Sep 2026 19:30:00 +0000</pubDate></item>"
            "</channel></rss>")
    http = Http(fake={
        BCRA_API: (200, bcra_json),
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
    # 12. Sondeo: un fallo viejo se limpia aunque no haya nada que confirmar; si la fuente cae, suma
    pubs = json.dumps({"data": {"publicaciones": [{"titulo": "x", "fecha": "1 ene 2026"}] * 150}})
    filas = "".join(f"<td>0{i % 9 + 1}/01/2026</td>" for i in range(150))
    http4 = Http(fake={BCRA_API: (200, pubs), INDEC_INFORMES: (200, filas)})
    sal4 = {"bcra_listado": 7, "indec_informes": 2}
    confirmar(http4, [], {}, sal4, [], sondear=True)
    check("sondeo_limpia", sal4 == {"bcra_listado": 0, "indec_informes": 0})
    sal5 = {"bcra_listado": 7}
    confirmar(HttpMuerto(), [], {}, sal5, [], sondear=True)
    check("sondeo_suma", sal5.get("bcra_listado") == 8)
    # 12b. Hora real: detectado el mismo día → hora y ventana; detectado otro día → sin hora
    global AHORA
    HOY = dt.date(2026, 9, 4)
    AHORA = dt.datetime(2026, 9, 4, 16, 7, tzinfo=TZ)
    evr = [{"fecha": "2026-09-04", "indicador": "bcra.rem", "titulo": "REM", "periodo": "2026-08"}]
    todavia = json.dumps({"success": True, "data": {"publicaciones": [
        {"titulo": "Relevamiento de Expectativas de Mercado (REM)", "url": "x", "periodo": "Julio 2026", "fecha": "05 ago 2026"}]}})
    esth = {}
    AHORA = dt.datetime(2026, 9, 4, 15, 37, tzinfo=TZ)
    confirmar(Http(fake={BCRA_API: (200, todavia), "https://news.google.com/": (200, "<rss></rss>")}), evr, esth, {}, [])
    check("no_estaba_registrado", list(esth.values())[0].get("no_estaba") == "2026-09-04T15:37-03:00")
    AHORA = dt.datetime(2026, 9, 4, 16, 7, tzinfo=TZ)
    confirmar(http, evr, esth, {}, [])
    eh = list(esth.values())[0]
    check("hora_detectada", eh.get("estado") == "confirmado" and eh.get("hora_detectada") == "16:07"
          and eh.get("hora_desde") == "15:37" and eh.get("visto") == "2026-09-04T16:07-03:00" and "no_estaba" not in eh)
    esth3 = {}
    confirmar(HttpMuerto(), evr, esth3, {}, [])
    check("sin_piso_si_fuente_caida", "no_estaba" not in list(esth3.values())[0])
    HOY = dt.date(2026, 9, 7)
    AHORA = dt.datetime(2026, 9, 7, 9, 7, tzinfo=TZ)
    esth2 = {}
    confirmar(http, evr, esth2, {}, [])
    eh2 = list(esth2.values())[0]
    check("sin_hora_otro_dia", eh2.get("estado") == "confirmado" and "hora_detectada" not in eh2 and eh2.get("desvio_dias") == 0)
    AHORA = None
    # 9. Escritura atómica
    global DATA
    viejo = DATA
    DATA = tempfile.mkdtemp()
    guardar_atomico("x.json", {"a": 1})
    check("atomica", cargar("x.json", None) == {"a": 1} and not [p for p in os.listdir(DATA) if p.endswith(".tmp")])
    DATA = viejo

    # 12. API BCRA con respuesta rota (HTML en vez de JSON): debe ser error, no "no salió"
    try:
        m_bcra_listado(Http(fake={BCRA_API: (200, "<html>mantenimiento</html>")}),
                       {"fecha": "2026-09-18"}, {"nombre": "Informe sobre Bancos"})
        check("api_bcra_rota_es_error", False)
    except IOError:
        check("api_bcra_rota_es_error", True)
    # 12b. BCRA: el REM de julio (05-ago) no confirma el de agosto buscado el 04-sep ± ventana, pero sí el 04-sep
    f, u = m_bcra_listado(http, {"fecha": "2026-09-03"}, {"nombre": "Relevamiento de Expectativas de Mercado"})
    check("bcra_rem_fecha_real", f == dt.date(2026, 9, 4))
    # 12c. INDEC: prefijo exacto (canasta no confunde con canasta_crianza) y fecha del listado
    filas = "".join(f"<div class='row'><div>{fe}</div><a href='/uploads/informesdeprensa/{ar}.pdf'>Ver informe</a></div>"
                    for fe, ar in [("14/09/2026", "canasta_crianza_09_2661D221F8CF"), ("11/09/2026", "canasta_09_265C7188EBE6"),
                                   ("10/09/2026", "ipc_09_26A1BE2DC4CD"), ("17/09/2026", "mercado_trabajo_eph_2trim26433FCBC5A8")] * 30)
    hi = Http(fake={INDEC_INFORMES: (200, filas)})
    f, u = m_indec_informes(hi, {"fecha": "2026-09-14"}, {"pref": "canasta"})
    check("indec_prefijo_exacto", f == dt.date(2026, 9, 11) and "canasta_09" in u)
    f, u = m_indec_informes(hi, {"fecha": "2026-09-17"}, {"pref": "mercado_trabajo_eph"})
    check("indec_trimestral", f == dt.date(2026, 9, 17))
    f, u = m_indec_informes(hi, {"fecha": "2026-09-17"}, {"pref": "cgi"})
    check("indec_sin_evidencia", f is None)
    try:
        m_indec_informes(Http(fake={INDEC_INFORMES: (200, "<html>nuevo diseño</html>")}), {"fecha": "2026-09-14"}, {"pref": "ipc"})
        check("indec_pagina_cambiada_es_error", False)
    except IOError:
        check("indec_pagina_cambiada_es_error", True)
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

    # 15. Página HTML con tokens que cambian en cada carga: sin aviso si las fechas no cambian
    cal2 = {}
    p1 = "<script>var t='abc123'</script><td>14 de octubre</td><td>21 de octubre</td>"
    p2 = "<script>var t='zzz999'</script><td>14 de octubre</td><td>21 de octubre</td>"
    vigilar_calendarios(Http(fake={"https://www.bcra.gob.ar/calendario-de-informes/": (200, p1)}), cal2, [])
    av2 = vigilar_calendarios(Http(fake={"https://www.bcra.gob.ar/calendario-de-informes/": (200, p2)}), cal2, [])
    check("calendario_html_sin_ruido", not av2)
    p3 = p2.replace("21 de octubre", "23 de octubre")
    av3 = vigilar_calendarios(Http(fake={"https://www.bcra.gob.ar/calendario-de-informes/": (200, p3)}), cal2, [])
    check("calendario_html_detecta_cambio", len(av3) == 1)

    # 16. XLSX: la fecha de modificación interna confirma la publicación; la del mes anterior no
    import io, zipfile
    def xlsx(fecha_iso):
        b = io.BytesIO()
        with zipfile.ZipFile(b, "w") as z:
            z.writestr("docProps/core.xml", f'<cp:coreProperties><dcterms:modified xsi:type="dcterms:W3CDTF">{fecha_iso}</dcterms:modified></cp:coreProperties>')
        return b.getvalue().decode("latin-1")
    ux = "https://x/anexo.xlsx"
    HOY = dt.date(2026, 9, 26)
    f, u = m_xlsx_modificado(Http(fake={ux: (200, xlsx("2026-09-25T20:10:00Z"))}), {"fecha": "2026-09-25"}, {"url": ux})
    check("xlsx_confirma", f == dt.date(2026, 9, 25))
    f, u = m_xlsx_modificado(Http(fake={ux: (200, xlsx("2026-08-28T16:40:00Z"))}), {"fecha": "2026-09-25"}, {"url": ux})
    check("xlsx_mes_anterior_no_confirma", f is None)

    # 14. Noticias de argentina.gob.ar: resultado de licitación el mismo día, sin confundir con el llamado
    card = lambda link, fe, t: (f'<a href="/noticias/{link}" class="panel panel-default"><div class="panel-heading"></div>'
                                f"<div class=\"panel-body\"><time datetime='{fe} 17:18:06'>x</time>  <h3>{t}</h3></div></a>")
    noti = (card("llamado-9", "2026-09-24", "Llamado a licitación de instrumentos del Tesoro Nacional")
            + card("resu-12", "2026-09-11", "Resultado de la licitación por efectivo de instrumentos del Tesoro Nacional")
            + card("llamado-8", "2026-09-09", "Llamado a licitación de instrumentos del Tesoro Nacional"))
    hn = Http(fake={"https://www.argentina.gob.ar/economia/finanzas/noticias": (200, noti)})
    f, u = m_gob_noticias(hn, {"fecha": "2026-09-11"}, {"url": "https://www.argentina.gob.ar/economia/finanzas/noticias", "clave": r"^Resultado de la licitaci[oó]n", "tol": 2})
    check("noticias_resultado", f == dt.date(2026, 9, 11) and u.endswith("resu-12"))
    f, u = m_gob_noticias(hn, {"fecha": "2026-09-28"}, {"url": "https://www.argentina.gob.ar/economia/finanzas/noticias", "clave": r"^Resultado de la licitaci[oó]n", "tol": 2})
    check("noticias_sin_resultado_aun", f is None)
    f, u = m_gob_noticias(hn, {"fecha": "2026-09-24"}, {"url": "https://www.argentina.gob.ar/economia/finanzas/noticias", "clave": r"^Llamado a licitaci[oó]n", "tol": 2})
    check("noticias_llamado", f == dt.date(2026, 9, 24))
    # 15. Archivo por período con Last-Modified: SIPA de junio subido el 10-sep; julio todavía no existe
    HOY = dt.date(2026, 9, 27)
    ua = "https://www.argentina.gob.ar/sites/default/files/trabajoregistrado_{yy_ref}{mm_ref}_estadisticas.xlsx"
    hs = Http(fake={"https://www.argentina.gob.ar/sites/default/files/trabajoregistrado_2606_estadisticas.xlsx": (200, "Thu, 10 Sep 2026 18:33:19 GMT")})
    f, u = m_archivo_lm(hs, {"fecha": "2026-09-11"}, {"url": ua, "lag": 3})
    check("archivo_lm_fecha", f == dt.date(2026, 9, 10))
    f, u = m_archivo_lm(hs, {"fecha": "2026-10-13"}, {"url": ua, "lag": 3})
    check("archivo_lm_no_salio", f is None)
    # 16. ARCA re-subió el PDF meses después: la existencia confirma, con la fecha programada
    ha = Http(fake={"https://www.arca.gob.ar/institucional/documentos/ARCA-Recaudacion-032026.pdf": (200, "Thu, 14 May 2026 15:17:34 GMT")})
    f, u = m_archivo_lm(ha, {"fecha": "2026-04-01"}, {"url": "https://www.arca.gob.ar/institucional/documentos/ARCA-Recaudacion-{mm_ref}{yyyy_ref}.pdf", "lag": 1})
    check("archivo_lm_resubido", f == dt.date(2026, 4, 1))

    # 17. Listado fechado (CIARA) y período en página (AFCP, UTDT)
    HOY = dt.date(2026, 9, 27)
    hc = Http(fake={"https://www.ciaracec.com.ar/": (200, '<li><a href="/x.pdf">Liquidaci&oacute;n de Divisas  <strong>01-SEP-2026</strong></a></li>'
                                                            '<li><a href="/y.pdf">Liquidaci&oacute;n de Divisas  <strong>03-AGO-2026</strong></a></li>')})
    pc = {"url": "https://www.ciaracec.com.ar/ciara/liq", "patron": r"Divisas\s*<strong>(?P<d>\d{2})-(?P<m>[a-z]{3})-(?P<y>\d{4})", "tol": 3}
    f, u = m_pagina_lista(hc, {"fecha": "2026-09-01"}, pc)
    check("lista_ciara", f == dt.date(2026, 9, 1))
    f, u = m_pagina_lista(hc, {"fecha": "2026-10-01"}, pc)
    check("lista_ciara_no_salio", f is None)
    ha2 = Http(fake={"https://www.afcp.org.ar/": (200, "<p>Despachos de cemento en el mes de Agosto de 2026</p>")})
    pa = {"url": "https://www.afcp.org.ar/despacho-mensual", "patron": "Despachos de cemento en el mes de {mes} de {yyyy}", "lag": 1}
    f, u = m_pagina_periodo(ha2, {"fecha": "2026-09-04"}, pa)
    check("periodo_afcp", f == dt.date(2026, 9, 4))
    f, u = m_pagina_periodo(ha2, {"fecha": "2026-10-06"}, pa)
    check("periodo_afcp_no_salio", f is None)
    try:
        m_pagina_periodo(Http(fake={"https://www.afcp.org.ar/": (200, "<p>sitio nuevo</p>")}), {"fecha": "2026-10-06"}, pa)
        check("periodo_pagina_cambiada_es_error", False)
    except IOError:
        check("periodo_pagina_cambiada_es_error", True)
    hu = Http(fake={"https://www.utdt.edu/": (200, '<h4>Encuesta de Expectativas (EI)</h4> <div class="fecha">Septiembre 2026</div>'
                                                   '<h4>Confianza del Consumidor (ICC)</h4> <div class="fecha">Agosto 2026</div>')})
    pu = {"url": "https://www.utdt.edu/x", "patron": r'\(ICC\)</h4>\s*<div class="fecha">{mes} {yyyy}', "lag": 0}
    f, u = m_pagina_periodo(hu, {"fecha": "2026-09-17"}, pu)
    check("periodo_utdt_no_confunde_widgets", f is None)
    # 18. USDA reusa nombres: archivo viejo (2020) no confirma el WASDE de octubre
    hw = Http(fake={"https://www.usda.gov/oce/commodity/wasde/wasde1026.pdf": (200, "Wed, 15 Jan 2020 20:23:57 GMT")})
    HOY = dt.date(2026, 10, 12)
    f, u = m_archivo_lm(hw, {"fecha": "2026-10-09"}, {"url": "https://www.usda.gov/oce/commodity/wasde/wasde{mm_ref}{yy_ref}.pdf", "lag": 0, "exigir_lm": True})
    check("wasde_archivo_viejo", f is None)

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
