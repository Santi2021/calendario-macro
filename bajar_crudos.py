#!/usr/bin/env python3
"""
Baja archivos crudos de fuentes oficiales al repo (data/raw/) para análisis posteriores.
Cada archivo se guarda tal cual lo publica la fuente; el procesamiento se hace aparte.
Si una descarga falla, las demás siguen y el archivo anterior queda intacto.
"""
import csv, json, os, sys, time
import requests, urllib3

urllib3.disable_warnings()
RAIZ = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "raw")
UA = {"User-Agent": "Mozilla/5.0 (calendario-macro)"}
INDEC = "https://www.indec.gob.ar/ftp/cuadros/economia/"
ARCHIVOS = {
    # comercio exterior: índices de precios, cantidades y términos del intercambio (base 2004)
    "indec_indices_comext.xls": INDEC + "sh_indices_comext_04.xls",
    "indec_indices_expo_grandes_rubros.xls": INDEC + "sh_indicesexpgr_04.xls",
    "indec_indices_impo_uso_economico.xls": INDEC + "sh_indicesimpue_04.xls",
    # cuentas nacionales trimestrales
    "indec_oferta_demanda.xls": INDEC + "sh_oferta_demanda_09_26.xls",
    "indec_oferta_demanda_desest.xls": INDEC + "sh_oferta_demanda_desest_09_26.xls",
    # balanza de pagos y posición de inversión internacional
    "indec_bdp_pii.xls": INDEC + "cin_I_2026.xls",
    "indec_bdp_1994_2016.xls": INDEC + "series_bdp_de_1994_2016.xls",
    # comercio exterior por rubro: energía (exportaciones de combustibles y energía, importaciones de combustibles y lubricantes)
    "indec_expo_grandes_rubros_1980_2025.xls": INDEC + "exp_gr_80_25.xls",
    "indec_impo_uso_economico_1980_2025.xls": INDEC + "imp_uso_econ_80_25.xls",
    "indec_expo_grandes_rubros_mensual.xls": INDEC + "expo_grandes_rubros_2025_2026.xls",
    "indec_impo_uso_economico_mensual.xls": INDEC + "impo_uso_economico_2025_2026.xls",
}
# Banco Mundial: PIB per cápita a precios constantes (proxy de productividad relativa) y consumo del gobierno
BM_PAISES = "ARG;BRA;CHN;USA;EMU;CHL;MEX;CAN;JPN;GBR;CHE;IND;URY;VNM"
BM_INDICADORES = ["NY.GDP.PCAP.KD", "NE.CON.GOVT.ZS", "NE.TRD.GNFS.ZS", "NY.GDP.MKTP.CD", "BN.CAB.XOKA.GD.ZS", "BN.CAB.XOKA.CD", "NY.GDP.MKTP.KD"]


def bajar(url, destino):
    ultimo = None
    for intento in range(3):
        for verify in (True, False):
            try:
                r = requests.get(url, headers=UA, timeout=120, verify=verify)
                if r.status_code == 200 and len(r.content) > 500:
                    open(destino, "wb").write(r.content)
                    return len(r.content)
                ultimo = f"HTTP {r.status_code}"
            except Exception as ex:
                ultimo = ex
        time.sleep(3)
    raise IOError(f"{url}: {ultimo}")


def main():
    os.makedirs(RAIZ, exist_ok=True)
    reporte = {}
    for nombre, url in ARCHIVOS.items():
        try:
            reporte[nombre] = bajar(url, os.path.join(RAIZ, nombre))
        except Exception as ex:
            reporte[nombre] = f"[fallo] {ex}"
    filas = []
    for ind in BM_INDICADORES:
        url = f"https://api.worldbank.org/v2/country/{BM_PAISES}/indicator/{ind}?format=json&per_page=20000&date=1990:2026"
        try:
            r = requests.get(url, headers=UA, timeout=120)
            datos = r.json()[1] or []
            for d in datos:
                if d.get("value") is not None:
                    filas.append([ind, d["countryiso3code"], d["date"], d["value"]])
            reporte[ind] = len(datos)
        except Exception as ex:
            reporte[ind] = f"[fallo] {ex}"
    if filas:
        with open(os.path.join(RAIZ, "banco_mundial.csv"), "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f); w.writerow(["indicador", "pais", "anio", "valor"]); w.writerows(filas)
    json.dump(reporte, open(os.path.join(RAIZ, "reporte.json"), "w"), indent=1)
    print(json.dumps(reporte, indent=1))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as ex:
        print(f"[fallo] bajar_crudos: {ex}")
        sys.exit(0)
