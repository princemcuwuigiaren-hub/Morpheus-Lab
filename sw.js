// Morpheus Lab service worker.
//
// Strategy:
//  - The app shell (index.html) is served network-first. Every deploy is
//    picked up on the next load that has connectivity, with no version bump
//    and no manual cache clear. The saved copy exists so the app still opens
//    with zero network.
//  - If the network is slow, the saved copy opens right away while the new
//    build finishes downloading in the background. When it lands, every open
//    window is told, so the app can offer a Reload.
//  - Anything else (API calls to the AI backend, future services) is never
//    touched. It goes straight to the network.
//
// Bump SHELL_CACHE_NAME only when PRECACHE_URLS changes. Ordinary changes to
// index.html need no bump.

const SHELL_CACHE_NAME = 'morpheus-lab-shell-v2';

const PRECACHE_URLS = [
  './morpheus-lab.html'
];

// How long the shell waits on the network before falling back to the saved copy.
const SHELL_NETWORK_TIMEOUT_MS = 2500;

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(SHELL_CACHE_NAME).then((cache) =>
      Promise.all(
        PRECACHE_URLS.map((url) =>
          // cache:'reload' skips the browser's own HTTP cache, so install never
          // stores a stale copy of the previous build.
          cache.add(new Request(url, { cache: 'reload' })).catch((e) => {
            console.warn('Morpheus SW: precache skipped for', url, e && e.message);
          })
        )
      )
    )
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((names) =>
      Promise.all(
        names.filter((name) => name !== SHELL_CACHE_NAME).map((name) => caches.delete(name))
      )
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);

  // Only GETs are ever handled. Writes and API calls reach the network untouched.
  if (event.request.method !== 'GET') return;

  const isNavigation = event.request.mode === 'navigate';
  const isShellFile = url.origin === self.location.origin && url.pathname.endsWith('/morpheus-lab.html');

  if (isNavigation || isShellFile) {
    event.respondWith(networkFirst(event.request, event));
  }
  // Everything else: no respondWith, so the request behaves as if this worker did not exist.
});

// A cheap fingerprint of a saved or fresh shell: ETag or Last-Modified when the
// server sends one, otherwise the byte length of the body.
async function shellSignature(resp) {
  const h = resp.headers.get('etag') || resp.headers.get('last-modified');
  if (h) return 'h:' + h;
  try { return 'n:' + (await resp.clone().arrayBuffer()).byteLength; } catch (e) { return ''; }
}

async function notifyUpdateReady() {
  const windows = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
  windows.forEach((c) => c.postMessage({ type: 'morpheus-update-ready' }));
}

async function networkFirst(request, event) {
  const cache = await caches.open(SHELL_CACHE_NAME);
  const shellUrl = new URL('./morpheus-lab.html', self.registration.scope).href;
  const isNav = request.mode === 'navigate';

  // The app is one page. Whatever address a launch used (site root, index.html,
  // or either with a query added), it is saved to and served from one entry.
  const fromCache = async () => {
    if (isNav) {
      const shell = await cache.match(shellUrl);
      if (shell) return shell;
    }
    return cache.match(request, { ignoreSearch: true });
  };

  // True once the page was handed the saved copy. Only then does a newer build
  // arriving later mean this window is out of date.
  let servedSavedCopy = false;

  const networkPromise = fetch(request, { cache: 'no-cache' }).then(async (fresh) => {
    // Only cache real, successful responses. A 404 or 500 never overwrites a good shell.
    if (fresh && fresh.ok) {
      let changed = false;
      if (isNav) {
        try {
          const old = await cache.match(shellUrl);
          if (old) {
            const [a, b] = await Promise.all([shellSignature(old), shellSignature(fresh)]);
            changed = !!a && !!b && a !== b;
          }
        } catch (e) { /* cannot compare: just do not announce */ }
      }
      const saved = cache.put(isNav ? shellUrl : request, fresh.clone()).then(() => {
        if (changed && servedSavedCopy) return notifyUpdateReady();
      }).catch((e) => {
        console.warn('Morpheus SW: could not save', request.url, e && e.message);
      });
      if (event && event.waitUntil) event.waitUntil(saved);
    }
    return fresh;
  });
  networkPromise.catch(() => {});
  if (event && event.waitUntil) event.waitUntil(networkPromise.catch(() => {}));

  const cached = await fromCache();
  if (!cached) return networkPromise; // first ever load: nothing saved, so wait

  const timeout = new Promise((resolve) => setTimeout(() => resolve(null), SHELL_NETWORK_TIMEOUT_MS));
  try {
    const winner = await Promise.race([networkPromise, timeout]);
    if (winner && winner.ok) return winner;
  } catch (e) { /* offline or failed: use the saved copy */ }
  servedSavedCopy = true;
  return cached;
}
