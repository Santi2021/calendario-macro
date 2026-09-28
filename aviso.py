#!/usr/bin/env python3
"""
Avisos al celular (notificaciones push de la propia web del calendario).

Cuándo manda:
  - Resumen de la mañana: en la primera corrida desde las 09:00 (hora argentina), una vez por día.
    Qué se publica hoy (prioridad alta y media, sin semanales) y cómo salió lo de ayer.
    Si hoy no hay nada y ayer no quedó nada pendiente, no manda (día sin novedades = sin aviso).
  - Cierre del día: en la primera corrida desde las 19:00 de cada día hábil, una vez por día.
    Qué salió hoy y qué falta, más los datos demorados o fuentes caídas que todavía no se avisaron.
    Si hoy no había nada y no hay problemas, no manda.
  - Alerta de la noche: después del cierre (y los fines de semana), sólo si aparece un dato demorado
    o una fuente caída que todavía no se avisó. Cada problema se avisa una sola vez.

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
            "url": WEB + "?v=hoy&origen=aviso",
            "acciones": [{"id": "hoy", "titulo": "Ver hoy", "url": WEB + "?v=hoy&origen=aviso"},
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


def cierre_dia(hoy, eventos, cat, estado, avisos):
    """Resumen de cierre: lo de hoy (salió / falta confirmar) + problemas todavía no avisados."""
    est = estado.get("eventos", {})
    ya = set(avisos.get("alertados", []))
    st = lambda e: est.get(clave(e), {}).get("estado")
    hoy_ev = sorted([e for e in eventos if e.get("fecha") == hoy.isoformat() and relevante(e, cat)],
                    key=lambda e: e.get("hora") or "99")
    conf = [e for e in hoy_ev if st(e) == "confirmado"]
    desde = (hoy - dt.timedelta(days=12)).isoformat()
    dem = [e for e in eventos if e.get("fecha") and desde <= e["fecha"] < hoy.isoformat() and relevante(e, cat)
           and st(e) == "demorado" and clave(e) not in ya]
    caidos = [m for m, n in (estado.get("salud") or {}).items() if n >= 3 and f"metodo:{m}" not in ya]
    if not hoy_ev and not dem and not caidos:
        return None, []
    dia = f"{DIAS[hoy.weekday()]} {hoy.day}/{hoy.month}"
    lineas, claves = [], []
    for e in hoy_ev[:MAX_LINEAS]:
        lineas.append(f"✓ {corto(e, cat)}" if st(e) == "confirmado" else f"· {corto(e, cat)}: todavía sin confirmar")
    if len(hoy_ev) > MAX_LINEAS:
        lineas.append(f"y {len(hoy_ev) - MAX_LINEAS} más")
    for e in dem[:3]:
        f = dt.date.fromisoformat(e["fecha"])
        lineas.append(f"Demorado: {corto(e, cat)} (programado el {f.day}/{f.month})")
        claves.append(clave(e))
    if len(dem) > 3:
        lineas.append(f"y {len(dem) - 3} demorados más")
        claves += [clave(e) for e in dem[3:]]
    for m in caidos:
        lineas.append(f"Fuente caída: método «{m}» falla hace {estado['salud'][m]} corridas")
        claves.append(f"metodo:{m}")
    if hoy_ev:
        titulo = f"Cierre del {dia} · {len(conf)} de {len(hoy_ev)} " + ("salió" if len(hoy_ev) == 1 else "salieron")
        if len(conf) == len(hoy_ev):
            titulo += " ✓"
    elif dem:
        titulo = f"Cierre del {dia} · demorado: {corto(dem[0], cat)}"
    else:
        titulo = f"Cierre del {dia} · fuente caída"
    problema = bool(dem or caidos)
    return ({"titulo": titulo, "cuerpo": "\n".join(lineas), "tag": "alerta" if problema else "cierre",
             "url": WEB + ("?v=agenda&origen=alerta" if problema else "?v=hoy&origen=cierre"),
             "acciones": [{"id": "hoy", "titulo": "Ver hoy", "url": WEB + "?v=hoy&origen=cierre"},
                          {"id": "agenda", "titulo": "Agenda", "url": WEB + "?v=agenda&origen=cierre"}]}, claves)


def decidir(ahora, eventos, cat, estado, avisos):
    """Qué avisos corresponden en esta corrida: lista de (tipo, aviso o None, claves de problemas)."""
    hoy = ahora.date()
    salidas = []
    if ahora.hour >= HORA_MANANA and avisos.get("manana") != hoy.isoformat() and hoy.weekday() < 5:
        salidas.append(("manana", resumen_manana(hoy, eventos, cat, estado), []))
    if ahora.hour >= HORA_NOCHE:
        if hoy.weekday() < 5 and avisos.get("cierre") != hoy.isoformat():
            p, claves = cierre_dia(hoy, eventos, cat, estado, avisos)
            salidas.append(("cierre", p, claves))
        else:
            p, claves = alerta_noche(hoy, eventos, cat, estado, avisos)
            salidas.append(("noche", p, claves))
    return salidas


def suscripciones(texto):
    """El secreto PUSH_SUSCRIPCION acepta un código o varios: un objeto {...}, una lista [{...},{...}]
    o varios objetos pegados uno debajo del otro (así es más fácil sumar el de un amigo)."""
    texto = (texto or "").strip()
    if not texto:
        return []
    try:
        v = json.loads(texto)
        return v if isinstance(v, list) else [v]
    except json.JSONDecodeError:
        dec, out, k = json.JSONDecoder(), [], 0
        while k < len(texto):
            while k < len(texto) and texto[k] in " \t\r\n,;":
                k += 1
            if k >= len(texto):
                break
            obj, k = dec.raw_decode(texto, k)
            out.append(obj)
        return out


def enviar(payload):
    priv = os.environ.get("VAPID_PRIVADA", "").strip()
    try:
        subs = suscripciones(os.environ.get("PUSH_SUSCRIPCION", ""))
    except (ValueError, json.JSONDecodeError) as ex:
        print(f"[avisos] el secreto PUSH_SUSCRIPCION no tiene un formato válido: {ex}")
        return "error"
    if not subs or not priv:
        print("[avisos] sin configurar (faltan los secretos PUSH_SUSCRIPCION / VAPID_PRIVADA): no se manda nada")
        return "sin_configurar"
    from pywebpush import webpush, WebPushException
    ok = 0
    for n, sub in enumerate(subs, 1):
        try:
            webpush(subscription_info=sub, data=json.dumps(payload, ensure_ascii=False),
                    vapid_private_key=priv, vapid_claims={"sub": "https://santi2021.github.io"}, ttl=6 * 3600,
                    headers={"Urgency": "high" if payload.get("tag") == "alerta" else "normal"})
            ok += 1
        except WebPushException as ex:
            st = getattr(ex.response, "status_code", None)
            if st in (404, 410):
                print(f"[avisos] celular {n}: la suscripción venció (hay que activar de nuevo en ese celular y reemplazar su código)")
            else:
                print(f"[avisos] celular {n}: error al enviar: {ex}")
        except Exception as ex:
            print(f"[avisos] celular {n}: código inválido: {ex}")
    print(f"[avisos] enviado a {ok} de {len(subs)} celulares: {payload['titulo']}")
    return "ok" if ok else "error"


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
        r = enviar({"titulo": "MacroCalendAR · prueba", "cuerpo": f"Los avisos funcionan. Enviado {ahora:%d/%m %H:%M}.",
                    "tag": "prueba", "url": WEB + "?origen=prueba"})
        return 0 if r in ("ok", "sin_configurar") else 1
    eventos = cargar("eventos.json", [])
    cat = {c["id"]: c for c in cargar("catalogo.json", [])}
    estado = cargar("estado.json", {})
    avisos = cargar("avisos.json", {})
    salidas = decidir(ahora, eventos, cat, estado, avisos)
    for tipo, p, claves in salidas:
        if p is None:
            print(f"[avisos] {tipo}: sin novedades, no se manda")
            if tipo in ("manana", "cierre") and not a.dry_run:
                avisos[tipo] = hoy.isoformat()
            continue
        if a.dry_run:
            print(f"[avisos] {tipo} (dry-run):\n  {p['titulo']}\n  " + p["cuerpo"].replace("\n", "\n  "))
            continue
        r = enviar(p)
        if r == "ok":
            if tipo in ("manana", "cierre"):
                avisos[tipo] = hoy.isoformat()
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
    a1 = '{"endpoint":"https://a","keys":{"p256dh":"x","auth":"y"}}'
    b1 = '{"endpoint":"https://b","keys":{"p256dh":"x","auth":"y"}}'
    check("un_codigo", len(suscripciones(a1)) == 1)
    check("varios_pegados", [x["endpoint"] for x in suscripciones(a1 + "\n" + b1)] == ["https://a", "https://b"])
    check("lista", len(suscripciones("[" + a1 + "," + b1 + "]")) == 2)
    # Cierre del día
    est3 = {"eventos": {"indec.ipc|2026-09-28|2026-08": {"estado": "confirmado"},
                        "indec.emae|2026-09-25|2026-07": {"estado": "demorado"}}, "salud": {}}
    c, cl3 = cierre_dia(dt.date(2026, 9, 28), ev, cat, est3, {})
    check("cierre_titulo", c and c["titulo"] == "Cierre del lunes 28/9 · 1 de 2 salieron")
    check("cierre_lineas", c and "✓ IPC · INDEC" in c["cuerpo"] and "· Licitación del Tesoro · Finanzas: todavía sin confirmar" in c["cuerpo"])
    check("cierre_demora", c and "Demorado: EMAE · INDEC (programado el 25/9)" in c["cuerpo"] and cl3 == ["indec.emae|2026-09-25|2026-07"])
    check("cierre_urgente_si_problema", c and c["tag"] == "alerta")
    c2, cl4 = cierre_dia(dt.date(2026, 9, 28), ev, cat, est3, {"alertados": cl3})
    check("cierre_no_repite_demora", c2 and "Demorado" not in c2["cuerpo"] and cl4 == [] and c2["tag"] == "cierre")
    est4 = {"eventos": {"indec.ipc|2026-09-28|2026-08": {"estado": "confirmado"},
                        "priv.licitaciones_tesoro|2026-09-28|": {"estado": "confirmado"}}}
    c3, _ = cierre_dia(dt.date(2026, 9, 28), ev, cat, est4, {})
    check("cierre_todo_ok", c3 and c3["titulo"].endswith("2 de 2 salieron ✓"))
    check("cierre_sin_nada", cierre_dia(dt.date(2026, 9, 30), ev, cat, {"eventos": {}}, {}) == (None, []))
    t = lambda h, dia=28: dt.datetime(2026, 9, dia, h, 7, tzinfo=TZ)
    tipos = lambda sal: [x[0] for x in sal]
    check("decidir_manana", tipos(decidir(t(9), ev, cat, est4, {})) == ["manana"])
    check("decidir_cierre_una_vez", tipos(decidir(t(19), ev, cat, est4, {"manana": "2026-09-28"})) == ["cierre"]
          and tipos(decidir(t(20), ev, cat, est4, {"manana": "2026-09-28", "cierre": "2026-09-28"})) == ["noche"])
    check("decidir_finde_sin_cierre", tipos(decidir(t(19, 27), ev, cat, est4, {})) == ["noche"])
    check("decidir_madrugada_nada", decidir(t(7), ev, cat, est4, {}) == [])
    print(f"Autotest avisos: {ok}/{ok + len(fallos)} OK" + (f" | fallaron: {', '.join(fallos)}" if fallos else ""))
    return 0 if not fallos else 1


if __name__ == "__main__":
    sys.exit(main())
