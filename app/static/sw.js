// Minimal service worker: makes the app installable. It caches nothing, so
// Dave always sees live calendar and mail data, never a stale copy.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));
self.addEventListener("fetch", () => {});
