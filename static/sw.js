// Token Monitor service worker:
// API 及 HTML 頁面一律 network-first(確保連線時秒更最新介面與資料,斷線時回退快取)。
const CACHE = 'token-monitor-v4';
const SHELL = [
  '/',
  '/manifest.json',
  '/icons/icon-192.png',
  '/icons/icon-512.png',
];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  if (url.origin !== location.origin) return;  // Google Fonts 等交給瀏覽器

  // API 及 HTML 導向頁面一律使用 Network-First
  if (url.pathname.startsWith('/api/') || e.request.mode === 'navigate' || url.pathname === '/' || url.pathname.endsWith('.html')) {
    e.respondWith(
      fetch(e.request)
        .then(resp => {
          if (resp && resp.status === 200) {
            const copy = resp.clone();
            caches.open(CACHE).then(c => c.put(e.request, copy));
          }
          return resp;
        })
        .catch(() => caches.match(e.request))
    );
    return;
  }

  // 靜態圖示/資源 Cache-First
  e.respondWith(
    caches.match(e.request).then(hit =>
      hit || fetch(e.request).then(resp => {
        const copy = resp.clone();
        caches.open(CACHE).then(c => c.put(e.request, copy));
        return resp;
      })
    )
  );
});
