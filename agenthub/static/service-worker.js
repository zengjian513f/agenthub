const CACHE_PREFIX = 'agenthub-shell-';
const CACHE_NAME = `${CACHE_PREFIX}v1`;
const APP_SHELL = [
  new URL('./', self.location.href).href,
  new URL('./manifest.webmanifest', self.location.href).href,
  new URL('./icons/icon-192.png', self.location.href).href,
  new URL('./icons/icon-512.png', self.location.href).href,
];

self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME).then(cache =>
      Promise.allSettled(APP_SHELL.map(url => cache.add(new Request(url, { cache: 'reload' }))))
    )
  );
  self.skipWaiting();
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys => Promise.all(
      keys.filter(key => key.startsWith(CACHE_PREFIX) && key !== CACHE_NAME)
        .map(key => caches.delete(key))
    )).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', event => {
  const { request } = event;
  const url = new URL(request.url);
  const scopePath = new URL(self.registration.scope).pathname;
  const relativePath = url.pathname.startsWith(scopePath)
    ? url.pathname.slice(scopePath.length)
    : url.pathname;
  if (request.method !== 'GET' || url.origin !== self.location.origin || relativePath.startsWith('api/')) return;

  const cacheable = request.mode === 'navigate'
    || ['style', 'script', 'font', 'image', 'manifest'].includes(request.destination);
  if (!cacheable) return;

  event.respondWith(
    fetch(request).then(response => {
      if (response.ok && response.type === 'basic') {
        caches.open(CACHE_NAME).then(cache => cache.put(request, response.clone()));
      }
      return response;
    }).catch(async () => {
      const cached = await caches.match(request);
      if (cached) return cached;
      if (request.mode === 'navigate') {
        const shell = await caches.match(new URL('./', self.registration.scope).href);
        if (shell) return shell;
      }
      return new Response('AgentHub 当前离线，请恢复网络后重试。', {
        status: 503,
        headers: { 'Content-Type': 'text/plain; charset=utf-8' },
      });
    })
  );
});
