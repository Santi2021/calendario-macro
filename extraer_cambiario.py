#!/usr/bin/env python3
"""
Extrae el Anexo estadístico del mercado de cambios y balance cambiario (BCRA) a CSV.

Genera en data/cambiario/:
  <hoja>.csv          una fila por mes (desde 2003), una columna por serie (código del BCRA, ej. bal002)
  <hoja>_columnas.csv código -> encabezado original (filas de título de la planilla)
  meta.json           fecha de carga del XLSX y hojas extraídas

Sólo reescribe si el BCRA cargó una planilla nueva (compara la fecha interna del XLSX).
No usa librerías fuera de la estándar + requests.
"""
import csv, datetime as dt, io, json, os, re, sys, zipfile

URL = ("https://www.bcra.gob.ar/archivos/Pdfs/PublicacionesEstadisticas/informes/"
       "anexo-estadistico-mercado-cambios-balance-cambiario.xlsx")
SALIDA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "cambiario")
HOJA_MAX_MB = 20            # la hoja de microdatos (100 MB) no se extrae
CODIGO = re.compile(r"^[a-zA-Z]{2,5}\d{3}$")


def col_num(c):
    n = 0
    for ch in c:
        n = n * 26 + ord(ch) - 64
    return n


def slug(nombre):
    s = nombre.lower()
    for a, b in zip("áéíóúñ", "aeioun"):
        s = s.replace(a, b)
    return re.sub(r"[^a-z0-9]+", "_", s).strip("_")


def leer_hoja(z, ruta, ss):
    xml = z.read(ruta).decode("utf-8", "ignore")
    G = {}
    for r in re.finditer(r'<row [^>]*r="(\d+)"[^>]*>(.*?)</row>', xml, re.S):
        fila = {}
        for c in re.finditer(r'<c r="([A-Z]+)\d+"([^>]*?)(?:/>|>(.*?)</c>)', r.group(2), re.S):
            v = re.search(r"<v>([^<]*)</v>", c.group(3) or "")
            if not v:
                t = re.search(r"<t[^>]*>([^<]*)</t>", c.group(3) or "")   # texto en línea
                if t:
                    fila[c.group(1)] = t.group(1)
                continue
            fila[c.group(1)] = ss[int(v.group(1))] if 't="s"' in c.group(2) else v.group(1)
        G[int(r.group(1))] = fila
    return G


def extraer(datos):
    z = zipfile.ZipFile(io.BytesIO(datos))
    core = z.read("docProps/core.xml").decode("utf-8", "ignore")
    modificado = re.search(r"<dcterms:modified[^>]*>([^<]+)<", core).group(1)
    ss = []
    if "xl/sharedStrings.xml" in z.namelist():
        x = z.read("xl/sharedStrings.xml").decode("utf-8", "ignore")
        ss = ["".join(re.findall(r"<t[^>]*>([^<]*)</t>", si)) for si in re.findall(r"<si>(.*?)</si>", x, re.S)]
    wb = z.read("xl/workbook.xml").decode("utf-8", "ignore")
    rels = z.read("xl/_rels/workbook.xml.rels").decode("utf-8", "ignore")
    hojas = []
    for nombre, rid in re.findall(r'<sheet [^>]*name="([^"]+)"[^>]*r:id="([^"]+)"', wb):
        m = re.search(r'<Relationship [^>]*Id="%s"[^>]*Target="([^"]+)"' % rid, rels) or \
            re.search(r'<Relationship [^>]*Target="([^"]+)"[^>]*Id="%s"' % rid, rels)
        ruta = "xl/" + m.group(1).lstrip("/").replace("xl/", "")
        if z.getinfo(ruta).file_size > HOJA_MAX_MB * 1e6:
            continue
        G = leer_hoja(z, ruta, ss)
        # fila de códigos: la que tiene más celdas con formato de código (bal001, mc012...)
        fila_cod = max(G, key=lambda r: sum(1 for v in G[r].values() if CODIGO.match(str(v))), default=None)
        if fila_cod is None or sum(1 for v in G[fila_cod].values() if CODIGO.match(str(v))) < 3:
            continue
        codigos = {c: v for c, v in G[fila_cod].items() if CODIGO.match(str(v))}
        cols = sorted(codigos, key=col_num)
        col_fecha = cols[0]                               # la primera columna codificada es el mes
        filas = []
        for r in sorted(G):
            if r <= fila_cod:
                continue
            f = G[r].get(col_fecha)
            try:
                serial = float(f)
            except (TypeError, ValueError):
                break                                     # terminó la serie mensual (siguen totales anuales)
            if not 20000 < serial < 80000:
                break
            fecha = (dt.date(1899, 12, 30) + dt.timedelta(days=int(serial))).strftime("%Y-%m")
            filas.append([fecha] + [G[r].get(c, "") for c in cols[1:]])
        encabezados = []
        for c in cols[1:]:
            partes = [str(G[r][c]).strip() for r in range(1, fila_cod) if c in G.get(r, {}) and str(G[r][c]).strip()]
            encabezados.append([codigos[c], c, " / ".join(partes)])
        hojas.append({"nombre": nombre, "slug": slug(nombre), "codigos": [codigos[c] for c in cols[1:]],
                      "filas": filas, "encabezados": encabezados})
    return modificado, hojas


def main():
    import requests
    os.makedirs(SALIDA, exist_ok=True)
    meta_p = os.path.join(SALIDA, "meta.json")
    previo = json.load(open(meta_p)) if os.path.exists(meta_p) else {}
    r = requests.get(URL, headers={"User-Agent": "Mozilla/5.0 (calendario-macro)"}, timeout=120)
    r.raise_for_status()
    modificado, hojas = extraer(r.content)
    if previo.get("xlsx_modificado") == modificado and "--forzar" not in sys.argv:
        print(f"Sin cambios: la planilla sigue siendo la del {modificado}")
        return 0
    for h in hojas:
        with open(os.path.join(SALIDA, h["slug"] + ".csv"), "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["mes"] + h["codigos"])
            w.writerows(h["filas"])
        with open(os.path.join(SALIDA, h["slug"] + "_columnas.csv"), "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["codigo", "columna_excel", "encabezado_original"])
            w.writerows(h["encabezados"])
    meta = {"xlsx_modificado": modificado, "fuente": URL,
            "hojas": {h["slug"]: {"nombre": h["nombre"], "series": len(h["codigos"]),
                                  "desde": h["filas"][0][0] if h["filas"] else None,
                                  "hasta": h["filas"][-1][0] if h["filas"] else None} for h in hojas}}
    json.dump(meta, open(meta_p, "w"), ensure_ascii=False, indent=1)
    print(json.dumps(meta, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as ex:
        # Nunca frena el workflow: el confirmador es lo prioritario.
        print(f"[fallo] extraer_cambiario: {ex}")
        sys.exit(0)
