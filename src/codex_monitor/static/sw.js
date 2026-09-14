const CACHE = "codex-monitor-shell-v1";
const SHELL = ["/static/styles.css?v=1", "/static/app.js?v=1", "/static/icon.svg", "/manifest.webmanifest"];
const SHELL_PATHS = new Set(SHELL.map(path => new URL(path, location.origin).pathname));

self.addEventListener("install", event => {
  event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", event => {
  event.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(key => key !== CACHE).map(key => caches.delete(key)))));
  self.clients.claim();
});

self.addEventListener("fetch", event => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.origin !== location.origin || url.pathname.startsWith("/api/")) return;
  if (!SHELL_PATHS.has(url.pathname)) return;
  event.respondWith(caches.match(event.request).then(hit => hit || fetch(event.request)));
});
