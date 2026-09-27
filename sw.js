/* Service worker de MacroCalendAR: avisos + funcionamiento sin conexión.
   Estrategia "red primero": siempre intenta traer lo último; si no hay internet (o tarda más de 4 s),
   muestra la última copia guardada. Así nunca queda una versión vieja pegada. */
const CACHE = "macrocalendar-v1";
const BASE = ["./", "index.html", "manifest.webmanifest", "icono-192.png", "favicon-64.png", "badge-96.png",
              "data/eventos.json", "data/catalogo.json", "data/estado.json", "data/feriados.json"];
self.addEventListener("install", e => { self.skipWaiting(); e.waitUntil(caches.open(CACHE).then(c => c.addAll(BASE)).catch(() => {})); });
self.addEventListener("activate", e => e.waitUntil((async () => {
  for (const k of await caches.keys()) if (k !== CACHE) await caches.delete(k);
  await self.clients.claim();
})()));
self.addEventListener("fetch", e => {
  const u = new URL(e.request.url);
  if (e.request.method !== "GET" || u.origin !== location.origin) return;   // fuentes externas: sin tocar
  e.respondWith((async () => {
    const cache = await caches.open(CACHE);
    const clave = u.pathname.endsWith("/") ? new Request(u.origin + u.pathname) : new Request(u.origin + u.pathname);
    const red = fetch(e.request, { cache: "no-store" }).then(r => { if (r && r.ok) cache.put(clave, r.clone()); return r; });
    try {
      return await Promise.race([red, new Promise((_, no) => setTimeout(() => no(new Error("lento")), 4000))]);
    } catch (x) {
      const c = await cache.match(clave);
      if (c) return c;
      if (e.request.mode === "navigate") { const i = await cache.match(new Request(new URL("index.html", self.registration.scope))); if (i) return i; }
      return red;                                   // sin copia guardada: esperar a la red
    }
  })());
});

self.addEventListener("push", e => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (x) { d = { titulo: "MacroCalendAR", cuerpo: e.data ? e.data.text() : "" }; }
  const acciones = (d.acciones || []).slice(0, 2).map(a => ({ action: a.id, title: a.titulo }));
  e.waitUntil(self.registration.showNotification(d.titulo || "MacroCalendAR", {
    body: d.cuerpo || "",
    icon: "icono-192.png",
    badge: "badge-96.png",
    tag: d.tag || "calendario",          // un aviso nuevo reemplaza al anterior del mismo tipo
    renotify: true,
    timestamp: Date.now(),
    actions: acciones,
    data: { url: d.url || "./?v=agenda", acciones: d.acciones || [] }
  }));
});

self.addEventListener("notificationclick", e => {
  e.notification.close();
  const data = e.notification.data || {};
  const acc = (data.acciones || []).find(a => a.id === e.action);
  const destino = new URL((acc && acc.url) || data.url || "./", self.registration.scope).href;
  e.waitUntil((async () => {
    const abiertas = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const c of abiertas) {
      if (c.url.startsWith(self.registration.scope)) { await c.navigate(destino); return c.focus(); }
    }
    return self.clients.openWindow(destino);
  })());
});
