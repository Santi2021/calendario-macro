#!/usr/bin/env python3
"""
Baja del API de Estadísticas del BCRA las series monetarias en dólares:
depósitos del sector privado, préstamos, efectivo mínimo / encajes y reservas.

Genera en data/bcra_api/:
  catalogo.csv   todas las variables que expone el API (id, descripción, categoría)
  series.csv     fecha, id, valor  (sólo las variables seleccionadas)
Se reescribe en cada corrida (son pocos KB); si el API falla no toca lo que había.
"""
import csv, json, os, re, sys, time, datetime as dt
import requests, urllib3

urllib3.disable_warnings()
SALIDA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "bcra_api")
BASES = ["https://api.bcra.gob.ar/estadisticas/v4.0/monetarias", "https://api.bcra.gob.ar/estadisticas/v3.0/monetarias"]
UA = {"User-Agent": "Mozilla/5.0 (calendario-macro)", "Accept": "application/json"}
FILTRO = re.compile(r"(d[oó]lar|usd|moneda extranjera|u\$s|m/e|me\b)", re.I)
TEMA = re.compile(r"(dep[oó]sit|pr[eé]stam|efectivo m[ií]nimo|encaje|reserva|efectivo en entidades|cuenta corriente en el bcra)", re.I)


def get(url, **params):
    ultimo = None
    for intento in range(4):
        for verify in (True, False):                      # el certificado del BCRA suele fallar
            try:
                r = requests.get(url, params=params, headers=UA, timeout=60, verify=verify)
                if r.status_code == 200:
                    return r.json()
                ultimo = f"HTTP {r.status_code}"
            except Exception as ex:
                ultimo = ex
        time.sleep(3 * (intento + 1))
    raise IOError(f"{url}: {ultimo}")


def buscar_registros(obj):
    """Devuelve la lista de dicts con 'fecha' y 'valor', esté donde esté en la respuesta."""
    if isinstance(obj, list):
        if obj and isinstance(obj[0], dict) and "fecha" in obj[0] and "valor" in obj[0]:
            return obj
        for x in obj:
            r = buscar_registros(x)
            if r:
                return r
    elif isinstance(obj, dict):
        for v in obj.values():
            r = buscar_registros(v)
            if r:
                return r
    return []


def main():
    os.makedirs(SALIDA, exist_ok=True)
    base, cat = None, None
    for b in BASES:
        try:
            cat = get(b); base = b; break
        except Exception as ex:
            print(f"[aviso] {b}: {ex}")
    if cat is None:
        raise IOError("el API del BCRA no respondió")
    variables = cat.get("results", cat)
    filas = []
    for v in variables:
        vid = v.get("idVariable") or v.get("id")
        desc = v.get("descripcion") or v.get("detalle") or ""
        filas.append([vid, desc, v.get("categoria", ""), v.get("unidadExpresion", v.get("unidad", ""))])
    with open(os.path.join(SALIDA, "catalogo.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(["id", "descripcion", "categoria", "unidad"]); w.writerows(filas)
    # Series.xlsm: 77 reservas, 78 compras de divisas, 81 efectivo mínimo, 107/108 depósitos en USD,
    # 118-125 préstamos al sector privado en USD, 158 LEBAC/LEDIV/BOPREAL en USD
    FIJAS = {77, 78, 81, 107, 108, 118, 119, 120, 121, 122, 123, 124, 125, 158}
    elegidas = [f for f in filas if f[0] in FIJAS]
    print(f"API: {base} | {len(filas)} variables | {len(elegidas)} elegidas")
    salida = []
    hoy = dt.date.today()
    for vid, desc, *_ in elegidas:
        registros = []
        desde = dt.date(2003, 1, 1)
        while desde < hoy:                                # de a un año para respetar el límite de filas
            hasta = min(dt.date(desde.year, 12, 31), hoy)
            try:
                j = get(f"{base}/{vid}", desde=desde.isoformat(), hasta=hasta.isoformat(), limit=3000)
                registros += buscar_registros(j)
            except Exception as ex:
                print(f"[aviso] {vid} {desde.year}: {ex}")
            desde = dt.date(desde.year + 1, 1, 1)
        for r in registros:
            salida.append([r["fecha"], vid, r["valor"]])
        print(f"  {vid}: {desc[:70]} -> {len(registros)} datos")
    if not salida:
        raise IOError("no se obtuvo ninguna serie")
    salida.sort()
    with open(os.path.join(SALIDA, "series.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(["fecha", "id", "valor"]); w.writerows(salida)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as ex:
        print(f"[fallo] extraer_bcra_api: {ex}")
        sys.exit(0)
