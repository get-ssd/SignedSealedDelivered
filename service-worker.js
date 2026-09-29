// Must match APP_VERSION in index.html
const CACHE_NAME = 'ssd-v88';
const SHELL = [
  './',
  './index.html',
  './advanced.html',
  './sign.html',
  './db.js',
  './cryptoOps.js',
  './passkey.js',
  './keyring.js',
  './signer-engines.js',
  './basic-exchange.js',
  './opfs-identicon.js',
  './pwa-updates.js',
  './vendor/fflate-0.8.2.js',
  './vendor/qrcode-generator-1.4.4.js',
  './vendor/jsqr-1.4.0.js',
  './manifest.json',
  './favicon.ico',
  './icon-192.png',
  './icon-512.png',
];

self.addEventListener('install', event => {
  event.waitUntil((async () => {
    const cache = await caches.open(CACHE_NAME);
    // Never fill a new versioned cache with HTTP-cached files from an old build.
    await cache.addAll(SHELL.map(url => new Request(url, {cache: 'reload'})));
    await self.skipWaiting();
  })());
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => /^ssd-v\d+$/.test(k) && k !== CACHE_NAME).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('message', event => {
  if (event.data?.type === 'SSD_GET_VERSION') event.ports[0]?.postMessage({version: CACHE_NAME});
});

self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);
  if (event.request.method !== 'GET') return;

  // Cache-first for shell files
  if (url.origin === self.location.origin) {
    // The document travels in the navigation URL, not in the HTML response.
    // Cache/load the shell without retaining artifact bytes in a cache key;
    // this also lets an installed app open dispatcher URLs while offline.
    const clean = new URL(url);
    clean.search = '';
    clean.hash = '';
    // Cache only named shell files, never the worker itself or private endpoints.
    if (!SHELL.some(path => new URL(path, self.location.href).href === clean.href)) return;
    const shellRequest = clean.href;
    event.respondWith(
      caches.open(CACHE_NAME).then(async cache => {
        const cached = await cache.match(shellRequest);
        if (cached) return cached;
        const response = await fetch(shellRequest, {cache: 'reload'});
        if (response.ok) await cache.put(shellRequest, response.clone());
        return response;
      })
    );
    return;
  }

  // Pass through everything else
});
