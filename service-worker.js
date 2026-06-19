// Must match APP_VERSION in index.html
const CACHE_NAME = 'ssd-v58';
const SHELL = [
  './',
  './index.html',
  './sign.html',
  './db.js',
  './cryptoOps.js',
  './passkey.js',
  './keyring.js',
  './manifest.json',
  './favicon.ico',
  './icon-192.png',
  './icon-512.png',
];

const CDN_ORIGINS = ['https://cdn.jsdelivr.net'];
const CDN_PRECACHE = [
  'https://cdn.jsdelivr.net/npm/fflate@0.8.2/umd/index.js',
  'https://cdn.jsdelivr.net/npm/qrcode-generator@1.4.4/qrcode.js',
  'https://cdn.jsdelivr.net/npm/jsqr@1.4.0/dist/jsQR.js',
];

self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then(cache => cache.addAll(SHELL))
      .then(() => caches.open(CACHE_NAME).then(cache => cache.addAll(CDN_PRECACHE).catch(() => {})))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);

  // Cache-first for shell files
  if (url.origin === self.location.origin) {
    event.respondWith(
      caches.match(event.request).then(cached => cached || fetch(event.request).then(response => {
        const clone = response.clone();
        caches.open(CACHE_NAME).then(cache => cache.put(event.request, clone));
        return response;
      }))
    );
    return;
  }

  // Network-first for CDN libraries, cache on success
  if (CDN_ORIGINS.some(o => url.origin === o)) {
    event.respondWith(
      fetch(event.request).then(response => {
        const clone = response.clone();
        caches.open(CACHE_NAME).then(cache => cache.put(event.request, clone));
        return response;
      }).catch(() => caches.match(event.request))
    );
    return;
  }

  // Pass through everything else
});
