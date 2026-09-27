#!/usr/bin/env python3
"""Genera data/macrocalendar.ics: el calendario suscribible (Google Calendar, Apple, Outlook).
Se regenera en cada corrida; quien se suscribe ve los cambios solo. Sin semanales ni prioridad baja."""
import datetime as dt, json, os
DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
WEB = "https://santi2021.github.io/calendario-macro/"

def cargar(n, d):
    p = os.path.join(DATA, n)
    return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else d

def esc(s):
    return str(s or "").replace("\\", "\\\\").replace(";", "\;").replace(",", "\\,").replace("\n", "\\n")

def plegar(linea):
    b, out = linea.encode("utf-8"), []
    while len(b) > 74:
        corte = 74
        while (b[corte] & 0xC0) == 0x80:
            corte -= 1
        out.append(b[:corte].decode()); b = b" " + b[corte:]
    out.append(b.decode())
    return "\r\n".join(out)

def main():
    import aviso
    ev = cargar("eventos.json", []); cat = {c["id"]: c for c in cargar("catalogo.json", [])}
    est = cargar("estado.json", {}).get("eventos", {})
    hoy = dt.date.today(); desde, hasta = (hoy - dt.timedelta(days=60)).isoformat(), (hoy + dt.timedelta(days=400)).isoformat()
    ahora = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    L = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//MacroCalendAR//AR", "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
         "X-WR-CALNAME:MacroCalendAR", "X-WR-TIMEZONE:America/Argentina/Buenos_Aires",
         "X-WR-CALDESC:Publicaciones de datos macro y sectoriales de Argentina", "REFRESH-INTERVAL;VALUE=DURATION:PT6H", "X-PUBLISHED-TTL:PT6H"]
    n = 0
    for e in ev:
        f, c = e.get("fecha"), cat.get(e["indicador"], {})
        if not f or not (desde <= f <= hasta) or c.get("frecuencia") == "semanal" or c.get("prioridad") == "baja":
            continue
        s = est.get(aviso.clave(e), {})
        uid = "".join(ch for ch in f"{e['indicador']}{f}{e.get('periodo') or ''}" if ch.isalnum()) + "@macrocalendar"
        titulo = aviso.corto(e, cat) + (" ✓" if s.get("estado") == "confirmado" else "")
        desc = [e.get("titulo") or c.get("nombre", ""), "Fecha oficial" if e.get("origen") == "oficial" else "Fecha estimada",
                f"Prioridad {c.get('prioridad', '')}"]
        if s.get("evidencia"):
            desc.append("Informe: " + s["evidencia"])
        L += ["BEGIN:VEVENT", "UID:" + uid, "DTSTAMP:" + ahora]
        if e.get("hora"):
            h, m = map(int, e["hora"].split(":"))
            ini = dt.datetime.fromisoformat(f).replace(hour=h, minute=m) + dt.timedelta(hours=3)   # AR = UTC-3
            L += ["DTSTART:" + ini.strftime("%Y%m%dT%H%M00Z"), "DTEND:" + (ini + dt.timedelta(minutes=15)).strftime("%Y%m%dT%H%M00Z")]
        else:
            d0 = dt.date.fromisoformat(f)
            L += ["DTSTART;VALUE=DATE:" + d0.strftime("%Y%m%d"), "DTEND;VALUE=DATE:" + (d0 + dt.timedelta(days=1)).strftime("%Y%m%d"), "TRANSP:TRANSPARENT"]
        L += ["SUMMARY:" + esc(titulo), "DESCRIPTION:" + esc("\n".join(desc)), "URL:" + (s.get("evidencia") or c.get("url") or WEB), "END:VEVENT"]
        n += 1
    L.append("END:VCALENDAR")
    with open(os.path.join(DATA, "macrocalendar.ics"), "w", encoding="utf-8", newline="") as fh:
        fh.write("\r\n".join(plegar(x) for x in L) + "\r\n")
    print(f"[ics] {n} eventos en data/macrocalendar.ics")

if __name__ == "__main__":
    main()
