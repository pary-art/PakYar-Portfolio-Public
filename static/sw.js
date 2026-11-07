const CACHE_NAME = "pakyar-static-v3";
const STATIC_ASSETS = [
  "/static/manifest.webmanifest",
  "/static/pakyar-mark.svg",
  "/static/pakyar-icon-192.png",
  "/static/pakyar-icon-512.png",
  "/static/pakyar-icon-180.png"
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(STATIC_ASSETS))
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys
          .filter((key) => key !== CACHE_NAME)
          .map((key) => caches.delete(key))
      )
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  if (event.request.method !== "GET") {
    return;
  }

  const requestUrl = new URL(event.request.url);
  if (requestUrl.origin !== self.location.origin) {
    return;
  }

  event.respondWith(
    fetch(event.request)
      .then((response) => {
        if (event.request.url.includes("/static/")) {
          const copy = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(event.request, copy));
        }
        return response;
      })
      .catch(() => caches.match(event.request))
  );
});

self.addEventListener("push", (event) => {
  let data = {title: "پاکیار", body: "یادآوری جدیدی دارید.", url: "/notifications"};
  try { if (event.data) data = {...data, ...event.data.json()}; } catch (_) {}
  event.waitUntil(self.registration.showNotification(data.title, {
    body: data.body,
    icon: "/static/pakyar-mark.png",
    badge: "/static/pakyar-mark.png",
    dir: "rtl",
    lang: "fa",
    data: {url: data.url || "/notifications"}
  }));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = event.notification.data?.url || "/notifications";
  event.waitUntil(clients.matchAll({type: "window", includeUncontrolled: true}).then((windows) => {
    for (const client of windows) {
      if ("focus" in client) { client.navigate(target); return client.focus(); }
    }
    return clients.openWindow(target);
  }));
});
