#!/usr/bin/env python3
"""
Extrae el Anexo estadístico sectorial del mercado de cambios (CLANAE) del BCRA.

La hoja de datos tiene una fila por mes x sector (letra y división CLAE a dos dígitos) x concepto,
con el monto en dólares. Son ~250 MB de XML, así que se lee en streaming.

Genera en data/cambiario/clanae/:
  sectores.csv              id, letra, division
  conceptos.csv             id, nivel_a, nivel_b, nivel_c, nivel_d (jerarquía del BCRA)
  letra_concepto.csv        mes, anexo, letra_id, concepto_id, musd        (detalle completo de conceptos por letra)
  division_rubro.csv        mes, anexo, sector_id, rubro_c, musd           (divisiones a dos dígitos, conceptos agregados a nivel C)
  meta.json                 fecha de carga, filas leídas, control de sumas
Sólo reescribe si el BCRA cargó una planilla nueva.
"""
import csv, datetime as dt, io, json, os, re, sys, zipfile
import xml.etree.ElementTree as ET
from collections import defaultdict

URL = ("https://www.bcra.gob.ar/archivos/Pdfs/PublicacionesEstadisticas/informes/"
       "anexo-estadistico-sectorial-mercado-cambios-clanae.xlsx")
SALIDA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "cambiario", "clanae")
NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def shared_strings(z):
    if "xl/sharedStrings.xml" not in z.namelist():
        return []
    ss = []
    for _, el in ET.iterparse(z.open("xl/sharedStrings.xml"), events=("end",)):
        if el.tag == NS + "si":
            ss.append("".join(t.text or "" for t in el.iter(NS + "t")))
            el.clear()
    return ss


def ruta_hoja(z, patron):
    wb = z.read("xl/workbook.xml").decode("utf-8", "ignore")
    rels = z.read("xl/_rels/workbook.xml.rels").decode("utf-8", "ignore")
    for nombre, rid in re.findall(r'<sheet [^>]*name="([^"]+)"[^>]*r:id="([^"]+)"', wb):
        if re.search(patron, nombre, re.I):
            m = re.search(r'<Relationship [^>]*Id="%s"[^>]*Target="([^"]+)"' % rid, rels) or \
                re.search(r'<Relationship [^>]*Target="([^"]+)"[^>]*Id="%s"' % rid, rels)
            return "xl/" + m.group(1).lstrip("/").replace("xl/", ""), nombre
    raise IOError(f"no encontré la hoja '{patron}'")


def filas(z, ruta, ss):
    """Itera las filas de la hoja como dict {columna: valor}."""
    for _, el in ET.iterparse(z.open(ruta), events=("end",)):
        if el.tag != NS + "row":
            continue
        fila = {}
        for c in el.iter(NS + "c"):
            col = re.match(r"[A-Z]+", c.get("r")).group(0)
            v = c.find(NS + "v")
            if v is None:
                t = c.find(f"{NS}is/{NS}t")
                if t is not None:
                    fila[col] = t.text
                continue
            fila[col] = ss[int(v.text)] if c.get("t") == "s" else v.text
        yield int(el.get("r")), fila
        el.clear()


def extraer(datos):
    z = zipfile.ZipFile(io.BytesIO(datos))
    core = z.read("docProps/core.xml").decode("utf-8", "ignore")
    modificado = re.search(r"<dcterms:modified[^>]*>([^<]+)<", core).group(1)
    ss = shared_strings(z)
    ruta, nombre = ruta_hoja(z, r"^Datos")
    it = filas(z, ruta, ss)
    _, cab = next(it)
    # columnas por nombre de encabezado (robusto a reordenamientos)
    inv = {str(v).strip().lower(): k for k, v in cab.items()}
    c_anexo, c_mes, c_letra, c_div, c_monto = inv["anexo"], inv["mes"], inv["letra"], inv["dos digitos"], inv["monto"]
    c_niv = [inv[x] for x in ("a", "b", "c", "d")]
    sectores, conceptos = {}, {}
    agg_letra = defaultdict(float)
    agg_div = defaultdict(float)
    total_por_mes = defaultdict(float)
    n = 0
    for _, f in it:
        try:
            serial = float(f[c_mes]); monto = float(f.get(c_monto) or 0)
        except (KeyError, ValueError, TypeError):
            continue
        mes = (dt.date(1899, 12, 30) + dt.timedelta(days=int(serial))).strftime("%Y-%m")
        sec = (f.get(c_letra, ""), f.get(c_div, ""))
        con = tuple(f.get(c, "") for c in c_niv)
        sid = sectores.setdefault(sec, len(sectores) + 1)
        cid = conceptos.setdefault(con, len(conceptos) + 1)
        anexo = f.get(c_anexo, "")
        lid = sec[0]
        agg_letra[(mes, anexo, lid, cid)] += monto
        agg_div[(mes, anexo, sid, con[2])] += monto
        total_por_mes[mes] += monto
        n += 1
    return modificado, nombre, n, sectores, conceptos, agg_letra, agg_div, total_por_mes


def main():
    import requests
    os.makedirs(SALIDA, exist_ok=True)
    meta_p = os.path.join(SALIDA, "meta.json")
    previo = json.load(open(meta_p)) if os.path.exists(meta_p) else {}
    r = requests.get(URL, headers={"User-Agent": "Mozilla/5.0 (calendario-macro)"}, timeout=300)
    r.raise_for_status()
    modificado, nombre, n, sectores, conceptos, agg_letra, agg_div, total = extraer(r.content)
    if previo.get("xlsx_modificado") == modificado and "--forzar" not in sys.argv:
        print(f"Sin cambios: la planilla sigue siendo la del {modificado}")
        return 0
    letras = sorted({s[0] for s in sectores})
    letra_id = {l: i + 1 for i, l in enumerate(letras)}
    with open(os.path.join(SALIDA, "sectores.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(["sector_id", "letra_id", "letra", "division"])
        for (l, d), i in sorted(sectores.items(), key=lambda x: x[1]):
            w.writerow([i, letra_id[l], l, d])
    with open(os.path.join(SALIDA, "conceptos.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(["concepto_id", "nivel_a", "nivel_b", "nivel_c", "nivel_d"])
        for c, i in sorted(conceptos.items(), key=lambda x: x[1]):
            w.writerow([i, *c])
    with open(os.path.join(SALIDA, "letra_concepto.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(["mes", "anexo", "letra_id", "concepto_id", "musd"])
        for (mes, anexo, l, cid), v in sorted(agg_letra.items()):
            if abs(v) >= 500:                                   # descarta montos menores a US$ 500
                w.writerow([mes, anexo, letra_id[l], cid, round(v / 1e6, 4)])
    with open(os.path.join(SALIDA, "division_rubro.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(["mes", "anexo", "sector_id", "rubro_c", "musd"])
        for (mes, anexo, sid, rub), v in sorted(agg_div.items()):
            if abs(v) >= 500:
                w.writerow([mes, anexo, sid, rub, round(v / 1e6, 4)])
    meta = {"xlsx_modificado": modificado, "fuente": URL, "hoja": nombre, "filas": n,
            "sectores": len(sectores), "letras": len(letras), "conceptos": len(conceptos),
            "desde": min(total), "hasta": max(total),
            "total_ultimo_mes_musd": round(total[max(total)] / 1e6, 2)}
    json.dump(meta, open(meta_p, "w"), ensure_ascii=False, indent=1)
    print(json.dumps(meta, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as ex:
        print(f"[fallo] extraer_clanae: {ex}")
        sys.exit(0)
