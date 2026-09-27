#!/usr/bin/env python3
"""
Avisos al celular (notificaciones push de la propia web del calendario).

Cuándo manda:
  - Resumen de la mañana: en la primera corrida desde las 09:00 (hora argentina), una vez por día.
    Qué se publica hoy (prioridad alta y media, sin semanales) y cómo salió lo de ayer.
    Si hoy no hay nada y ayer no quedó nada pendiente, no manda (día sin novedades = sin aviso).
  - Alerta de la noche: desde las 19:00, sólo si hay datos demorados o fuentes caídas que todavía
    no se avisaron. Cada problema se avisa una sola vez.

Qué necesita (secretos del repositorio en GitHub):
  PUSH_SUSCRIPCION   el código que muestra la web al tocar "Activar avisos"
  VAPID_PRIVADA      la clave privada con la que se firman los avisos
Sin esos secretos no hace nada (y no falla).

Uso:
  python aviso.py              # corrida normal (decide solo si corresponde mandar)
  python aviso.py --dry-run    # muestra lo que mandaría, sin mandar
  python aviso.py --prueba     # manda ya un aviso de prueba
  python aviso.py --selftest   # pruebas offline
"""
from __future__ import annotations

import argparse, datetime as dt, json, os, sys, tempfile

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
TZ = dt.timezone(dt.timedelta(hours=-3))
WEB = "https://santi2021.github.io/calendario-macro/"
DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
HORA_MANANA, HORA_NOCHE = 9, 19
MAX_LINEAS = 6


def cargar(nombre, defecto):
    p = os.path.join(DATA, nombre)
    if not os.path.exists(p):
        return defecto
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def guardar(nombre, obj):
    p = os.path.join(DATA, nombre)
    fd, tmp = tempfile.mkstemp(dir=DATA, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


def clave(ev):
    return f"{ev['indicador']}|{ev['fecha']}|{ev.get('periodo') or ''}"


CORTOS = {
    "indec.ipc": "IPC", "indec.cba_cbt": "Canasta básica", "indec.emae": "EMAE", "indec.ica": "Comercio exterior (ICA)",
    "indec.ipi_manuf": "Industria (IPI)", "indec.isac": "Construcción (ISAC)", "indec.sipm": "Precios mayoristas",
    "indec.icc": "Costo de la construcción", "indec.salarios": "Salarios", "indec.pib_avance": "PIB trimestral",
    "indec.eph_mercado_trabajo": "Desempleo (EPH)", "indec.pobreza": "Pobreza", "indec.bdp": "Balanza de pagos",
    "indec.eph_condiciones_vida": "Condiciones de vida (EPH)", "indec.eph_distribucion": "Distribución del ingreso",
    "indec.turismo_int": "Turismo internacional", "indec.ucii": "Capacidad instalada", "indec.supermercados": "Supermercados",
    "bcra.rem": "REM", "bcra.ipom": "IPOM", "bcra.imm": "Informe monetario", "bcra.cambiario": "Balance cambiario",
    "bcra.ief": "Estabilidad financiera", "bcra.ecc": "Condiciones crediticias", "bcra.bancos": "Informe de bancos",
    "priv.licitaciones_tesoro": "Licitación del Tesoro", "priv.llamado_tesoro": "Llamado a licitación",
    "priv.resultado_fiscal": "Resultado fiscal", "priv.recaudacion_arca": "Recaudación", "priv.sipa": "Empleo registrado (SIPA)",
    "priv.ripte_eil": "RIPTE", "priv.ciara_cec": "Liquidación del agro", "priv.acara": "Patentamientos",
    "priv.adefa": "Producción automotriz", "priv.cemento": "Despachos de cemento", "priv.fiel_ipi": "Industria FIEL",
    "priv.utdt_icc": "Confianza del consumidor", "priv.utdt_icg": "Confianza en el Gobierno", "priv.utdt_ei": "Expectativas de inflación",
    "priv.usda_wasde": "WASDE", "priv.ipc_caba": "IPC CABA", "priv.construya": "Índice Construya",
}
ORG_CORTO = [("Secretaría de Finanzas", "Finanzas"), ("Secretaría de Hacienda", "Hacienda"), ("Secretaría de Trabajo", "Trabajo"),
             ("Universidad Di Tella", "UTDT"), ("Ministerio de Economía", "Economía"), ("USDA", "USDA"), ("CIARA", "CIARA-CEC")]


def corto(ev, cat):
    """Nombre corto para el aviso: 'IPC · INDEC', 'Licitación del Tesoro · Finanzas'."""
    c = cat.get(ev["indicador"], {})
    nombre = CORTOS.get(ev["indicador"])
    if not nombre:
        nombre = (c.get("nombre") or ev.get("titulo") or ev["indicador"]).split(" — ")[0].split(",")[0]
        if len(nombre) > 34:
            nombre = nombre[:33].rsplit(" ", 1)[0] + "…"
    org = c.get("organismo", "")
    for largo, cor in ORG_CORTO:
        if org.startswith(largo):
            org = cor
            break
    org = org.split(" (")[0].split(" / ")[0]
    return f"{nombre} · {org}" if org and org.lower() not in nombre.lower() else nombre


def relevante(ev, cat):
    c = cat.get(ev["indicador"], {})
    return c.get("prioridad") in ("alta", "media") and c.get("frecuencia") != "semanal"


def resumen_manana(hoy, eventos, cat, estado):
    ayer = hoy - dt.timedelta(days=1)
    while ayer.weekday() >= 5:            # el lunes resume el viernes
        ayer -= dt.timedelta(days=1)
    hoy_ev = sorted([e for e in eventos if e.get("fecha") == hoy.isoformat() and relevante(e, cat)],
                    key=lambda e: ({"alta": 0, "media": 1}.get(cat.get(e["indicador"], {}).get("prioridad"), 2), e.get("hora") or "99"))
    hoy_ev.sort(key=lambda e: e.get("hora") or "99")
    ay = [e for e in eventos if e.get("fecha") == ayer.isoformat() and relevante(e, cat)]
    est = estado.get("eventos", {})
    conf = [e for e in ay if est.get(clave(e), {}).get("estado") == "confirmado"]
    pend = [e for e in ay if est.get(clave(e), {}).get("estado") in ("demorado", "sin_verificar")]
    if not hoy_ev and not pend:
        return None
    dia = f"{DIAS[hoy.weekday()].capitalize()} {hoy.day}/{hoy.month}"
    lineas = [(f"{e['hora']}  " if e.get("hora") else "") + corto(e, cat) for e in hoy_ev[:MAX_LINEAS]]
    if len(hoy_ev) > MAX_LINEAS:
        lineas.append(f"y {len(hoy_ev) - MAX_LINEAS} más")
    if ay:
        nom_ayer = "El viernes" if hoy.weekday() == 0 else "Ayer"
        tail = f"{nom_ayer}: {len(conf)} de {len(ay)} confirmadas" + (" ✓" if len(conf) == len(ay) else "")
        if pend:
            tail += " · pendiente: " + ", ".join(corto(e, cat) for e in pend[:2])
        lineas.append(("\n" if lineas else "") + tail)
    if hoy_ev:
        n = len(hoy_ev)
        titulo = f"{dia} · {n} " + ("publicaciones" if n > 1 else "publicación")
    else:
        titulo = f"{dia} · sin publicaciones"
    return {"titulo": titulo, "cuerpo": "\n".join(lineas), "tag": "manana",
            "url": WEB + "?v=agenda&origen=aviso",
            "acciones": [{"id": "hoy", "titulo": "Ver hoy", "url": WEB + "?v=agenda&origen=aviso"},
                         {"id": "semana", "titulo": "Semana", "url": WEB + "?v=semana&origen=aviso"}]}


def alerta_noche(hoy, eventos, cat, estado, avisos):
    est = estado.get("eventos", {})
    ya = set(avisos.get("alertados", []))
    desde = (hoy - dt.timedelta(days=12)).isoformat()
    dem = [e for e in eventos if e.get("fecha") and desde <= e["fecha"] <= hoy.isoformat() and relevante(e, cat)
           and est.get(clave(e), {}).get("estado") == "demorado" and clave(e) not in ya]
    caidos = [m for m, n in (estado.get("salud") or {}).items() if n >= 3 and f"metodo:{m}" not in ya]
    if not dem and not caidos:
        return None, []
    partes, claves = [], []
    for e in dem[:4]:
        f = dt.date.fromisoformat(e["fecha"])
        cuando = "hoy" if f == hoy else f"el {f.day}/{f.month}"
        partes.append(f"{corto(e, cat)}: programado {cuando}{' ' + e['hora'] if e.get('hora') else ''}, sin publicar.")
        claves.append(clave(e))
    for m in caidos:
        partes.append(f"Fuente caída: método «{m}» falla hace {estado['salud'][m]} corridas.")
        claves.append(f"metodo:{m}")
    if dem:
        titulo = f"Demorado: {corto(dem[0], cat)}" + (f" y {len(dem) - 1} más" if len(dem) > 1 else "")
    else:
        titulo = "Fuente caída en el confirmador"
    return ({"titulo": titulo, "cuerpo": "\n".join(partes), "tag": "alerta",
             "url": WEB + "?v=agenda&origen=alerta",
             "acciones": [{"id": "ver", "titulo": "Ver detalle", "url": WEB + "?v=agenda&origen=alerta"}]}, claves)


def enviar(payload):
    sub, priv = os.environ.get("PUSH_SUSCRIPCION", "").strip(), os.environ.get("VAPID_PRIVADA", "").strip()
    if not sub or not priv:
        print("[avisos] sin configurar (faltan los secretos PUSH_SUSCRIPCION / VAPID_PRIVADA): no se manda nada")
        return "sin_configurar"
    from pywebpush import webpush, WebPushException
    try:
        webpush(subscription_info=json.loads(sub), data=json.dumps(payload, ensure_ascii=False),
                vapid_private_key=priv, vapid_claims={"sub": "https://santi2021.github.io"}, ttl=6 * 3600,
                headers={"Urgency": "high" if payload.get("tag") == "alerta" else "normal"})
        print(f"[avisos] enviado: {payload['titulo']}")
        return "ok"
    except WebPushException as ex:
        st = getattr(ex.response, "status_code", None)
        if st in (404, 410):
            print("[avisos] la suscripción del celular venció: abrí la web, tocá Avisos → Activar y reemplazá el secreto PUSH_SUSCRIPCION")
            return "vencida"
        print(f"[avisos] error al enviar: {ex}")
        return "error"
    except (ValueError, json.JSONDecodeError) as ex:
        print(f"[avisos] el secreto PUSH_SUSCRIPCION no es un código válido: {ex}")
        return "error"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--prueba", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    ahora = dt.datetime.now(TZ)
    hoy = ahora.date()
    if a.prueba:
        r = enviar({"titulo": "Calendario macro · prueba", "cuerpo": f"Los avisos funcionan. Enviado {ahora:%d/%m %H:%M}.",
                    "tag": "prueba", "url": WEB + "?origen=prueba"})
        return 0 if r in ("ok", "sin_configurar") else 1
    eventos = cargar("eventos.json", [])
    cat = {c["id"]: c for c in cargar("catalogo.json", [])}
    estado = cargar("estado.json", {})
    avisos = cargar("avisos.json", {})
    salidas = []
    if ahora.hour >= HORA_MANANA and avisos.get("manana") != hoy.isoformat() and hoy.weekday() < 5:
        p = resumen_manana(hoy, eventos, cat, estado)
        salidas.append(("manana", p, []))
    if ahora.hour >= HORA_NOCHE:
        p, claves = alerta_noche(hoy, eventos, cat, estado, avisos)
        salidas.append(("noche", p, claves))
    for tipo, p, claves in salidas:
        if p is None:
            print(f"[avisos] {tipo}: sin novedades, no se manda")
            if tipo == "manana" and not a.dry_run:
                avisos["manana"] = hoy.isoformat()
            continue
        if a.dry_run:
            print(f"[avisos] {tipo} (dry-run):\n  {p['titulo']}\n  " + p["cuerpo"].replace("\n", "\n  "))
            continue
        r = enviar(p)
        if r == "ok":
            if tipo == "manana":
                avisos["manana"] = hoy.isoformat()
            avisos["alertados"] = (avisos.get("alertados", []) + claves)[-200:]
        avisos["ultimo_resultado"] = {"cuando": ahora.isoformat(timespec="minutes"), "tipo": tipo, "resultado": r}
    if not a.dry_run and salidas:
        guardar("avisos.json", avisos)
    return 0


def selftest():
    ok, fallos = 0, []

    def check(n, c):
        nonlocal ok
        ok += 1 if c else 0
        if not c:
            fallos.append(n)

    cat = {"indec.ipc": {"nombre": "Índice de precios al consumidor (IPC)", "organismo": "INDEC", "prioridad": "alta", "frecuencia": "mensual"},
           "priv.licitaciones_tesoro": {"nombre": "Licitaciones del Tesoro — resultado", "organismo": "Secretaría de Finanzas", "prioridad": "alta", "frecuencia": "~2 por mes"},
           "priv.lcg": {"nombre": "LCG", "organismo": "LCG", "prioridad": "alta", "frecuencia": "semanal"},
           "indec.emae": {"nombre": "Estimador mensual de actividad económica (EMAE)", "organismo": "INDEC", "prioridad": "alta", "frecuencia": "mensual"},
           "x.baja": {"nombre": "Algo menor", "organismo": "X", "prioridad": "baja", "frecuencia": "mensual"}}
    ev = [{"fecha": "2026-09-28", "hora": "16:00", "indicador": "indec.ipc", "periodo": "2026-08"},
          {"fecha": "2026-09-28", "hora": "17:00", "indicador": "priv.licitaciones_tesoro", "periodo": None},
          {"fecha": "2026-09-28", "hora": None, "indicador": "priv.lcg", "periodo": None},
          {"fecha": "2026-09-28", "hora": None, "indicador": "x.baja", "periodo": None},
          {"fecha": "2026-09-25", "hora": "16:00", "indicador": "indec.emae", "periodo": "2026-07"}]
    est = {"eventos": {"indec.emae|2026-09-25|2026-07": {"estado": "confirmado"}}, "salud": {"bcra_listado": 7}}
    p = resumen_manana(dt.date(2026, 9, 28), ev, cat, est)
    check("titulo", p and p["titulo"] == "Lunes 28/9 · 2 publicaciones")
    check("sin_semanales_ni_baja", p and "LCG" not in p["cuerpo"] and "menor" not in p["cuerpo"])
    check("nombre_corto", p and "IPC · INDEC" in p["cuerpo"] and "Licitación del Tesoro · Finanzas" in p["cuerpo"])
    check("lunes_resume_viernes", p and "El viernes: 1 de 1 confirmadas ✓" in p["cuerpo"])
    check("sin_nada_no_manda", resumen_manana(dt.date(2026, 9, 30), ev, cat, {"eventos": {}}) is None)
    est2 = {"eventos": {"indec.emae|2026-09-25|2026-07": {"estado": "demorado"}}, "salud": {"bcra_listado": 7}}
    a, cl = alerta_noche(dt.date(2026, 9, 28), ev, cat, est2, {})
    check("alerta_demorado", a and a["titulo"].startswith("Demorado: EMAE · INDEC") and "bcra_listado" in a["cuerpo"])
    a2, _ = alerta_noche(dt.date(2026, 9, 28), ev, cat, est2, {"alertados": cl})
    check("alerta_una_sola_vez", a2 is None)
    os.environ.pop("PUSH_SUSCRIPCION", None)
    check("sin_secretos_no_falla", enviar({"titulo": "x", "cuerpo": "y"}) == "sin_configurar")
    print(f"Autotest avisos: {ok}/{ok + len(fallos)} OK" + (f" | fallaron: {', '.join(fallos)}" if fallos else ""))
    return 0 if not fallos else 1


if __name__ == "__main__":
    sys.exit(main())
