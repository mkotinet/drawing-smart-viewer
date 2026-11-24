/**
 * 도면 스마트 뷰어 — PWA Service Worker (v1.2.0)
 * 앱 쉘: Network-First / 불변 도면 타일·썸네일: Cache-First (현장 음영지역 오프라인 열람 지원)
 */
const SHELL_CACHE = 'dsv-shell-v1.2.0';
const TILE_CACHE = 'dsv-tiles-v1.0.0';
const MAX_TILE_ENTRIES = 260;

const PRECACHE_URLS = [
  '/',
  '/static/style.css?v=1.2.0',
  '/static/app.js?v=1.2.0',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png',
  '/static/icons/apple-touch-icon.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(SHELL_CACHE)
      .then((cache) => cache.addAll(PRECACHE_URLS).catch(() => {}))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((names) =>
        Promise.all(
          names
            .filter((n) => n !== SHELL_CACHE && n !== TILE_CACHE)
            .map((n) => caches.delete(n))
        )
      )
      .then(() => self.clients.claim())
  );
});

async function trimTileCache() {
  const cache = await caches.open(TILE_CACHE);
  const keys = await cache.keys();
  if (keys.length > MAX_TILE_ENTRIES) {
    const excess = keys.slice(0, keys.length - MAX_TILE_ENTRIES);
    await Promise.all(excess.map((k) => cache.delete(k)));
  }
}

self.addEventListener('fetch', (event) => {
  if (event.request.method !== 'GET') return;
  const url = new URL(event.request.url);
  if (url.origin !== self.location.origin) return;

  // 불변 도면 타일/프리뷰/썸네일: Cache-First (인증 401 응답은 캐시하지 않음)
  if (
    url.pathname.startsWith('/api/sheets/') &&
    (url.pathname.endsWith('.webp'))
  ) {
    event.respondWith(
      caches.open(TILE_CACHE).then(async (cache) => {
        const hit = await cache.match(event.request);
        if (hit) return hit;
        const resp = await fetch(event.request);
        if (resp && resp.ok && resp.status === 200) {
          cache.put(event.request, resp.clone()).then(trimTileCache).catch(() => {});
        }
        return resp;
      })
    );
    return;
  }

  // 일반 API / manifest / version은 항상 네트워크 직결
  if (
    url.pathname.startsWith('/api/') ||
    url.pathname === '/manifest.json' ||
    url.pathname === '/version.json'
  ) {
    event.respondWith(fetch(event.request));
    return;
  }

  // 앱 쉘: Network-First with Offline Fallback
  event.respondWith(
    fetch(event.request)
      .then((resp) => {
        if (resp && resp.ok && resp.type === 'basic') {
          const clone = resp.clone();
          caches.open(SHELL_CACHE).then((c) => c.put(event.request, clone).catch(() => {}));
        }
        return resp;
      })
      .catch(async () => {
        const cached = await caches.match(event.request);
        if (cached) return cached;
        if (event.request.mode === 'navigate') return caches.match('/');
        return new Response('Offline', { status: 503 });
      })
  );
});
