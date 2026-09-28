#!/usr/bin/env python3
"""
Pruebas automáticas de MacroCalendAR. Las corre el flujo .github/workflows/pruebas.yml cada vez que
se sube un cambio; también se pueden correr a mano.

  python pruebas.py --datos   # los archivos de data/ son válidos y coherentes entre sí
  python pruebas.py --web     # la app abre sin errores en celular y compu, y lo principal funciona
                              # (necesita: pip install playwright && python -m playwright install chromium)

Sale con código 0 si todo pasa y 1 si algo falla, con el detalle de qué falló.
"""
from __future__ import annotations

import argparse, functools, http.server, json, os, re, sys, threading

RAIZ = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(RAIZ, "data")


class Resultado:
    def __init__(self, nombre):
        self.nombre, self.ok, self.fallos = nombre, 0, []

    def check(self, nombre, cond, detalle=""):
        if cond:
            self.ok += 1
        else:
            self.fallos.append(nombre + (f" ({detalle})" if detalle else ""))

    def fin(self):
        total = self.ok + len(self.fallos)
        print(f"{self.nombre}: {self.ok}/{total} OK")
        for f in self.fallos:
            print(f"  FALLA: {f}")
        return 0 if not self.fallos else 1


def cargar(nombre):
    with open(os.path.join(DATA, nombre), encoding="utf-8") as f:
        return json.load(f)


# --------------------------------------------------------------------------------------
# Datos
# --------------------------------------------------------------------------------------
def pruebas_datos():
    r = Resultado("Pruebas de datos")
    try:
        ev, cat, fer = cargar("eventos.json"), cargar("catalogo.json"), cargar("feriados.json")
    except Exception as ex:
        r.check("json_legible", False, str(ex))
        return r.fin()
    r.check("json_legible", True)
    ids = {c.get("id") for c in cat}
    r.check("catalogo_sin_ids_repetidos", len(ids) == len(cat))
    r.check("catalogo_campos", all(c.get("id") and c.get("nombre") and c.get("organismo") for c in cat))
    import datetime as dt

    class _Iso:
        """Fecha AAAA-MM-DD que además existe en el calendario (rechaza 2026-13-40)."""
        @staticmethod
        def match(t):
            try:
                return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", t)) and bool(dt.date.fromisoformat(t))
            except ValueError:
                return False
    iso = _Iso()
    hora = re.compile(r"([01]\d|2[0-3]):[0-5]\d$")
    malas_fechas = [e for e in ev if not (iso.match(e.get("fecha") or "") or (e.get("fecha") is None and e.get("origen") == "pendiente"))]
    r.check("eventos_fecha_valida", not malas_fechas, f"{len(malas_fechas)} eventos, ej. {malas_fechas[:1]}")
    malas_horas = [e for e in ev if e.get("hora") and not hora.match(e["hora"])]
    r.check("eventos_hora_valida", not malas_horas, f"ej. {malas_horas[:1]}")
    sin_cat = sorted({e.get("indicador") for e in ev if e.get("indicador") not in ids})
    r.check("eventos_en_catalogo", not sin_cat, ", ".join(sin_cat[:5]))
    claves = [f"{e.get('indicador')}|{e.get('fecha')}|{e.get('periodo') or ''}" for e in ev]
    rep = sorted({k for k in claves if claves.count(k) > 1})
    r.check("eventos_sin_duplicados", not rep, ", ".join(rep[:3]))
    r.check("eventos_origen", all(e.get("origen") in ("oficial", "estimada", "derivada", "pendiente") for e in ev))
    r.check("feriados_validos", all(iso.match(f.get("fecha") or "") and f.get("nombre") for f in fer))
    fechas = [e["fecha"] for e in ev if e.get("fecha")]
    r.check("calendario_con_futuro", fechas and max(fechas) >= dt.date.today().isoformat(),
            "no hay publicaciones futuras cargadas")
    for nombre in ("estado.json", "avisos.json"):
        if os.path.exists(os.path.join(DATA, nombre)):
            try:
                cargar(nombre)
                r.check(f"{nombre}_legible", True)
            except Exception as ex:
                r.check(f"{nombre}_legible", False, str(ex))
    return r.fin()


# --------------------------------------------------------------------------------------
# Web (navegador real)
# --------------------------------------------------------------------------------------
def servir():
    class Silencioso(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass

    class Servidor(http.server.ThreadingHTTPServer):
        def handle_error(self, request, client_address):
            pass          # el navegador corta descargas al cerrar la página: no es un error de la app
    srv = Servidor(("127.0.0.1", 0), functools.partial(Silencioso, directory=RAIZ))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_address[1]}/index.html"


def pruebas_web():
    from playwright.sync_api import sync_playwright
    r = Resultado("Pruebas de la web")
    url = servir()
    with sync_playwright() as p:
        b = p.chromium.launch()
        for tema in ("light", "dark"):
            # ---- celular ----
            c = b.new_context(viewport={"width": 392, "height": 820}, is_mobile=True, has_touch=True,
                              color_scheme=tema, service_workers="block")
            pg = c.new_page()
            errores = []
            pg.on("pageerror", lambda e: errores.append(str(e)))
            pg.goto(url + "?v=hoy")
            pg.wait_for_timeout(1500)
            r.check(f"celu_{tema}_sin_errores", not errores, "; ".join(errores[:2]))
            r.check(f"celu_{tema}_hoy", pg.locator("#pg-hoy .mhero").count() == 1)
            r.check(f"celu_{tema}_cuatro_secciones", pg.locator("#mv .pg").count() == 4)
            r.check(f"celu_{tema}_agenda_con_datos", pg.locator("#pg-agenda .row").count() > 0)
            r.check(f"celu_{tema}_compu_oculta", pg.evaluate("getComputedStyle(document.querySelector('#d')).display") == "none")
            pg.click(".tabs button[data-t=mes]")
            pg.wait_for_timeout(600)
            r.check(f"celu_{tema}_pestana_mes", pg.evaluate("ms.tab") == "mes")
            pg.locator("#pg-agenda .row").first.dispatch_event("click")
            pg.wait_for_timeout(500)
            r.check(f"celu_{tema}_detalle_abre", pg.evaluate("document.querySelector('#panel').classList.contains('on')")
                    and pg.locator("#panel .mdt").count() == 1)
            pg.evaluate("cerrar()")
            r.check(f"celu_{tema}_sin_errores_al_usar", not errores, "; ".join(errores[:2]))
            c.close()
            # ---- compu ----
            c = b.new_context(viewport={"width": 1440, "height": 900}, color_scheme=tema, service_workers="block")
            pg = c.new_page()
            errores = []
            pg.on("pageerror", lambda e: errores.append(str(e)))
            pg.goto(url)
            pg.evaluate("store.set('dvista','resumen')")
            pg.reload()
            pg.wait_for_timeout(1500)
            r.check(f"compu_{tema}_sin_errores", not errores, "; ".join(errores[:2]))
            r.check(f"compu_{tema}_resumen", pg.locator("#dv .dhero").count() == 1)
            r.check(f"compu_{tema}_celu_oculto", pg.evaluate("getComputedStyle(document.querySelector('#m')).display") == "none")
            for tecla, vista in (("2", "semana"), ("3", "mes"), ("4", "lista"), ("1", "resumen")):
                pg.keyboard.press(tecla)
                pg.wait_for_timeout(250)
                r.check(f"compu_{tema}_vista_{vista}", pg.evaluate("ds.v") == vista)
            pg.locator("#dv .drow").first.click()
            pg.wait_for_timeout(500)
            r.check(f"compu_{tema}_detalle_abre", pg.evaluate("document.querySelector('#panel').classList.contains('on')"))
            pg.keyboard.press("Escape")
            pg.wait_for_timeout(400)
            r.check(f"compu_{tema}_esc_cierra", not pg.evaluate("document.querySelector('#panel').classList.contains('on')"))
            r.check(f"compu_{tema}_sin_errores_al_usar", not errores, "; ".join(errores[:2]))
            c.close()
        b.close()
    return r.fin()


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--datos", action="store_true")
    ap.add_argument("--web", action="store_true")
    a = ap.parse_args(argv)
    if not (a.datos or a.web):
        a.datos = a.web = True
    rc = 0
    if a.datos:
        rc |= pruebas_datos()
    if a.web:
        rc |= pruebas_web()
    return rc


if __name__ == "__main__":
    sys.exit(main())
