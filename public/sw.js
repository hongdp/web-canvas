/**
 * Service Worker for Web Canvas PWA
 *
 * Strategy:
 * - App shell (HTML, JS, CSS, fonts) → Cache-first (instant load even when backgrounded)
 * - API calls (/api/*) → Network-only (never cache, always fresh)
 * - Image assets → Cache-first with network fallback
 *
 * This prevents the page from doing a full network reload when the OS
 * kills the renderer in the background — the shell loads from cache instantly.
 */

const CACHE_NAME = 'web-canvas-shell-v1'

// Resources that form the app shell — cache these on install
const SHELL_ASSETS = [
  '/',
  '/src/main.tsx',
]

// ── Install: pre-cache nothing critical (Vite handles hashed assets) ──────────
self.addEventListener('install', (event) => {
  // Skip waiting so the new SW activates immediately
  self.skipWaiting()
})

// ── Activate: claim all clients immediately ───────────────────────────────────
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys
          .filter((key) => key !== CACHE_NAME)
          .map((key) => caches.delete(key))
      )
    ).then(() => self.clients.claim())
  )
})

// ── Fetch: route requests ─────────────────────────────────────────────────────
self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url)

  // 1. API calls — always go to network, never cache
  if (url.pathname.startsWith('/api/')) {
    return // let the browser handle it normally
  }

  // 2. Non-GET requests — pass through
  if (event.request.method !== 'GET') {
    return
  }

  // 3. Bypass cache for same-origin assets on localhost (development)
  if (url.origin === self.location.origin && (self.location.hostname === 'localhost' || self.location.hostname === '127.0.0.1')) {
    return // let the browser fetch directly from the dev server
  }

  // 3. Cross-origin requests (e.g. Google Fonts, CDNs) — cache-first
  if (url.origin !== self.location.origin) {
    event.respondWith(
      caches.match(event.request).then((cached) => {
        if (cached) return cached
        return fetch(event.request).then((response) => {
          if (response.ok) {
            const clone = response.clone()
            caches.open(CACHE_NAME).then((cache) => cache.put(event.request, clone))
          }
          return response
        }).catch(() => cached) // offline fallback
      })
    )
    return
  }

  // 4. Same-origin assets — stale-while-revalidate
  // Serve from cache immediately for speed, update cache in background
  event.respondWith(
    caches.open(CACHE_NAME).then((cache) => {
      return cache.match(event.request).then((cached) => {
        const networkFetch = fetch(event.request).then((response) => {
          if (response.ok) {
            cache.put(event.request, response.clone())
          }
          return response
        }).catch(() => cached) // if offline, return whatever we have

        // Return cached immediately if available, otherwise wait for network
        return cached || networkFetch
      })
    })
  )
})
