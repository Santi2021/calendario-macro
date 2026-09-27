/* Service worker del calendario: sólo recibe avisos y abre la app al tocarlos. No guarda datos. */
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", e => e.waitUntil(self.clients.claim()));

self.addEventListener("push", e => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (x) { d = { titulo: "Calendario macro", cuerpo: e.data ? e.data.text() : "" }; }
  const acciones = (d.acciones || []).slice(0, 2).map(a => ({ action: a.id, title: a.titulo }));
  e.waitUntil(self.registration.showNotification(d.titulo || "Calendario macro", {
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
