/* Service worker minimale per rendere il telecomando installabile come
 * PWA su Android.
 *
 * Non fa cache: il telecomando ha senso solo con il server raggiungibile
 * e non vogliamo lasciare sul telefono copie di pagine autenticate,
 * sessioni o risposte di /api/. Tutto passa dalla rete.
 */
self.addEventListener("install", event => {
  event.waitUntil(self.skipWaiting());
});

self.addEventListener("activate", event => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener("fetch", event => {
  event.respondWith(fetch(event.request));
});
